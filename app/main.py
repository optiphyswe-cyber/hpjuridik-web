import os
import io
import uuid
import json
import base64
import smtplib
from datetime import datetime, timezone
from typing import Dict, Any, Optional, List, Tuple

import stripe
import requests
from fastapi import FastAPI, Request, Form, HTTPException
from fastapi.responses import HTMLResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware
from starlette.responses import RedirectResponse

# PDF (ReportLab)
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle
from reportlab.lib import colors
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle


# ------------------------------------------------------------------------------
# App setup
# ------------------------------------------------------------------------------
app = FastAPI(redirect_slashes=False)
app.mount("/static", StaticFiles(directory="app/static"), name="static")
templates = Jinja2Templates(directory="app/templates")

SESSION_SECRET = os.getenv("SESSION_SECRET", "dev-change-me")
app.add_middleware(SessionMiddleware, secret_key=SESSION_SECRET)

# ------------------------------------------------------------------------------
# ENV / Settings
# ------------------------------------------------------------------------------
BASE_URL = os.getenv("BASE_URL", "http://localhost:10000").rstrip("/")

MAIL_FROM = os.getenv("MAIL_FROM", "lanabil@hpjuridik.se")
CONTACT_TO = os.getenv("CONTACT_TO", "hp@hpjuridik.se")
CONTACT_FROM = os.getenv("CONTACT_FROM", "hp@hpjuridik.se")  # försök ha hp@ som From
LEAD_INBOX = os.getenv("LEAD_INBOX", "lanabil@hpjuridik.se")

SMTP_HOST = os.getenv("SMTP_HOST", "")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER = os.getenv("SMTP_USER", "")
SMTP_PASS = os.getenv("SMTP_PASS", "")

STRIPE_SECRET_KEY = os.getenv("STRIPE_SECRET_KEY", "")
STRIPE_WEBHOOK_SECRET = os.getenv("STRIPE_WEBHOOK_SECRET", "")
PREMIUM_PRICE_ORE = int(os.getenv("PREMIUM_PRICE_ORE", "15000"))

if STRIPE_SECRET_KEY:
    stripe.api_key = STRIPE_SECRET_KEY

# Canonical (ni kör www som primary)
CANONICAL_HOST = os.getenv("CANONICAL_HOST", "www.hpjuridik.se").strip().lower()
SITE_URL = os.getenv("SITE_URL", f"https://{CANONICAL_HOST}").rstrip("/")

# Filpersistens (MVP): funkar även om processen restartar / annan worker får webhooken
AGREEMENTS_DIR = os.getenv("AGREEMENTS_DIR", "/tmp/hpj_agreements")

# ------------------------------------------------------------------------------
# Oneflow
# ------------------------------------------------------------------------------
ONEFLOW_API_TOKEN = os.getenv("ONEFLOW_API_TOKEN", "")
ONEFLOW_BASE_URL = os.getenv("ONEFLOW_BASE_URL", "https://api.oneflow.com/v1").rstrip("/")
ONEFLOW_WORKSPACE_ID = os.getenv("ONEFLOW_WORKSPACE_ID", "")  # rekommenderas
ONEFLOW_TEMPLATE_ID = os.getenv("ONEFLOW_TEMPLATE_ID", "")    # krävs för template-flöde
ONEFLOW_USER_EMAIL = os.getenv("ONEFLOW_USER_EMAIL", "")      # valfritt men ofta bra
ONEFLOW_WEBHOOK_SECRET = os.getenv("ONEFLOW_WEBHOOK_SECRET", "")  # om du vill verifiera senare

ONEFLOW_ENABLED = bool(ONEFLOW_API_TOKEN and ONEFLOW_TEMPLATE_ID)

COMPANY = {
    "brand": "HP Juridik",
    "signature_name": "HP",
    "phone": "0763171284",
    "email": "hp@hpjuridik.se",
    "website": "hpjuridik.se",
    "address": "Karl XI gata 21, 222 20 Lund",
    "company": "Subsidiaritet i Lund AB",
    "orgnr": "559365-2018",
}


# ------------------------------------------------------------------------------
# Utils
# ------------------------------------------------------------------------------
def utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def page_ctx(request: Request, path: str, title: str, description: str) -> Dict[str, Any]:
    canonical = f"{SITE_URL}{path if path.startswith('/') else '/' + path}"
    return {
        "request": request,
        "company": COMPANY,
        "seo": {
            "title": title,
            "description": description,
            "canonical": canonical,
            "robots": "index, follow",
        },
        "path": path,
    }


def _ensure_dir(path: str) -> None:
    os.makedirs(path, exist_ok=True)


def _agreement_path(agreement_id: str) -> str:
    return os.path.join(AGREEMENTS_DIR, f"{agreement_id}.json")


def save_agreement(agreement: Dict[str, Any]) -> None:
    _ensure_dir(AGREEMENTS_DIR)
    with open(_agreement_path(agreement["agreement_id"]), "w", encoding="utf-8") as f:
        json.dump(agreement, f, ensure_ascii=False)


def load_agreement(agreement_id: str) -> Optional[Dict[str, Any]]:
    if not agreement_id:
        return None
    path = _agreement_path(agreement_id)
    if not os.path.exists(path):
        return None
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


# ------------------------------------------------------------------------------
# Email (SMTP)
# ------------------------------------------------------------------------------
def _smtp_send(
    to_emails: List[str],
    subject: str,
    text: str,
    pdf_bytes: Optional[bytes] = None,
    reply_to: Optional[str] = None,
    from_email: Optional[str] = None,
) -> None:
    from email.message import EmailMessage

    clean_to = [e for e in to_emails if e]
    if not clean_to:
        raise RuntimeError("No recipients provided")

    if not (SMTP_HOST and SMTP_USER and SMTP_PASS):
        raise RuntimeError("SMTP settings missing (SMTP_HOST/SMTP_USER/SMTP_PASS)")

    msg = EmailMessage()
    msg["From"] = from_email or MAIL_FROM
    msg["To"] = ", ".join(clean_to)
    msg["Subject"] = subject
    if reply_to:
        msg["Reply-To"] = reply_to
    msg.set_content(text)

    if pdf_bytes:
        msg.add_attachment(pdf_bytes, maintype="application", subtype="pdf", filename="avtal.pdf")

    with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
        server.starttls()
        server.login(SMTP_USER, SMTP_PASS)
        server.send_message(msg)


def safe_send_email(
    to_emails: List[str],
    subject: str,
    text: str,
    pdf_bytes: Optional[bytes] = None,
    reply_to: Optional[str] = None,
    from_email: Optional[str] = None,
) -> Tuple[bool, Optional[str]]:
    try:
        _smtp_send(to_emails, subject, text, pdf_bytes=pdf_bytes, reply_to=reply_to, from_email=from_email)
        return True, None
    except Exception as e:
        return False, str(e)


# ------------------------------------------------------------------------------
# PDF generator
# ------------------------------------------------------------------------------
def build_loan_pdf(flat: Dict[str, Any]) -> bytes:
    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf,
        pagesize=A4,
        leftMargin=18 * mm,
        rightMargin=18 * mm,
        topMargin=18 * mm,
        bottomMargin=18 * mm,
    )

    styles = getSampleStyleSheet()
    h1 = ParagraphStyle("H1", parent=styles["Heading1"], fontSize=16, leading=20, spaceAfter=10)
    p = ParagraphStyle("P", parent=styles["BodyText"], fontSize=10.5, leading=14)

    story = []
    story.append(Paragraph("Tillfälligt låneavtal – bil", h1))
    story.append(Paragraph(f"Avtals-ID: {flat.get('agreement_id','')}", p))
    story.append(Paragraph(f"Skapat (UTC): {flat.get('created_utc','')}", p))
    story.append(Spacer(1, 10))

    def row(label: str, value: Any):
        return [Paragraph(f"<b>{label}</b>", p), Paragraph(str(value or ""), p)]

    data = [
        row("Utlånare – namn", flat.get("utlanare_namn")),
        row("Utlånare – personnummer", flat.get("utlanare_pnr")),
        row("Utlånare – adress", flat.get("utlanare_adress")),
        row("Utlånare – telefon", flat.get("utlanare_tel")),
        row("Utlånare – e-post", flat.get("utlanare_epost")),
        row("Låntagare – namn", flat.get("lantagare_namn")),
        row("Låntagare – personnummer", flat.get("lantagare_pnr")),
        row("Låntagare – adress", flat.get("lantagare_adress")),
        row("Låntagare – telefon", flat.get("lantagare_tel")),
        row("Låntagare – e-post", flat.get("lantagare_epost")),
        row("Fordon – märke/modell", flat.get("fordon_modell")),
        row("Fordon – reg.nr", flat.get("fordon_regnr")),
        row("Avtalsperiod – från", flat.get("from_str")),
        row("Avtalsperiod – till", flat.get("to_str")),
        row("Ändamål / syfte", flat.get("andamal")),
    ]

    t = Table(data, colWidths=[55 * mm, 110 * mm])
    t.setStyle(
        TableStyle(
            [
                ("BOX", (0, 0), (-1, -1), 0.8, colors.black),
                ("INNERGRID", (0, 0), (-1, -1), 0.4, colors.grey),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LEFTPADDING", (0, 0), (-1, -1), 6),
                ("RIGHTPADDING", (0, 0), (-1, -1), 6),
                ("TOPPADDING", (0, 0), (-1, -1), 4),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
            ]
        )
    )
    story.append(t)
    story.append(Spacer(1, 12))

    story.append(Paragraph("<b>Friskrivning</b>", p))
    story.append(
        Paragraph(
            "Detta dokument är ett standardiserat bevisunderlag baserat på angivna uppgifter. "
            "HP Juridik lämnar ingen garanti för att avtalet godtas av Kronofogden, domstol eller annan part. "
            "Myndighetsbedömningar sker alltid utifrån en helhetsprövning.",
            p,
        )
    )

    story.append(Spacer(1, 18))
    story.append(Paragraph("Signaturer:", p))
    story.append(Spacer(1, 24))
    story.append(Paragraph("______________________________", p))
    story.append(Paragraph("Utlånare", p))
    story.append(Spacer(1, 18))
    story.append(Paragraph("______________________________", p))
    story.append(Paragraph("Låntagare", p))

    doc.build(story)
    return buf.getvalue()


# ------------------------------------------------------------------------------
# Oneflow helpers
# ------------------------------------------------------------------------------
class OneflowError(RuntimeError):
    pass


def oneflow_headers() -> Dict[str, str]:
    if not ONEFLOW_API_TOKEN:
        raise OneflowError("ONEFLOW_API_TOKEN saknas")
    h = {
        "x-oneflow-api-token": ONEFLOW_API_TOKEN,
        "Content-Type": "application/json",
    }
    if ONEFLOW_USER_EMAIL:
        h["x-oneflow-user-email"] = ONEFLOW_USER_EMAIL
    return h


def oneflow_create_contract_from_template(agreement: Dict[str, Any]) -> Dict[str, Any]:
    """
    Skapar kontrakt från template och sätter parter + BankID.
    Oneflow skickar signeringsinbjudan efter publish.
    """
    if not ONEFLOW_TEMPLATE_ID:
        raise OneflowError("ONEFLOW_TEMPLATE_ID saknas")

    flat = agreement["flat"]
    lender_name = flat.get("utlanare_namn") or "Utlånare"
    lender_email = flat.get("utlanare_epost")
    borrower_name = flat.get("lantagare_namn") or "Låntagare"
    borrower_email = flat.get("lantagare_epost")

    if not lender_email or not borrower_email:
        raise OneflowError("Saknar e-post för en eller båda parter")

    payload: Dict[str, Any] = {
        "template_id": int(ONEFLOW_TEMPLATE_ID),
        "name": f"Låna bil – {agreement['agreement_id']}",
        "parties": [
            {
                "type": "individual",
                "name": lender_name,
                "participants": [{
                    "name": lender_name,
                    "email": lender_email,
                    "delivery_channel": "email",
                    "sign_method": "swedish_bankid",
                }],
            },
            {
                "type": "individual",
                "name": borrower_name,
                "participants": [{
                    "name": borrower_name,
                    "email": borrower_email,
                    "delivery_channel": "email",
                    "sign_method": "swedish_bankid",
                }],
            },
        ],
    }

    # Om du har workspace-id så sätter vi det (bra)
    if ONEFLOW_WORKSPACE_ID:
        payload["workspace_id"] = int(ONEFLOW_WORKSPACE_ID)

    r = requests.post(f"{ONEFLOW_BASE_URL}/contracts/create", headers=oneflow_headers(), json=payload, timeout=25)
    if r.status_code >= 300:
        raise OneflowError(f"Oneflow create failed {r.status_code}: {r.text}")

    return r.json()


def oneflow_publish_contract(contract: Dict[str, Any]) -> None:
    """
    Publicera kontraktet.
    Vi använder _links.publish om den finns, annars fallback till /contracts/{id}/publish
    """
    contract_id = contract.get("id")
    if not contract_id:
        raise OneflowError("Oneflow contract saknar id")

    publish_url = None
    links = contract.get("_links") or {}
    if isinstance(links, dict):
        pub = links.get("publish") or {}
        if isinstance(pub, dict):
            publish_url = pub.get("href")

    payload = {
        "subject": "Signera ert avtal (BankID) – HP Juridik",
        "message": "Hej! Oneflow skickar nu en signeringsinbjudan via e-post. Signera med BankID.\n\n/HP Juridik",
    }

    if publish_url:
        r = requests.post(publish_url, headers=oneflow_headers(), json=payload, timeout=25)
    else:
        r = requests.post(f"{ONEFLOW_BASE_URL}/contracts/{int(contract_id)}/publish", headers=oneflow_headers(), json=payload, timeout=25)

    if r.status_code >= 300:
        raise OneflowError(f"Oneflow publish failed {r.status_code}: {r.text}")


# ------------------------------------------------------------------------------
# Delivery helpers
# ------------------------------------------------------------------------------
def deliver_free(agreement_id: str, agreement: Dict[str, Any]) -> None:
    flat = agreement["flat"]
    pdf_bytes = base64.b64decode(agreement["pdf_b64"])
    lender_email = flat.get("utlanare_epost")
    borrower_email = flat.get("lantagare_epost")

    ok, err = safe_send_email(
        [lender_email, borrower_email],
        "Tillfälligt låneavtal – bil (PDF)",
        "Här kommer ert avtal som PDF.\n\n/HP Juridik",
        pdf_bytes=pdf_bytes,
    )
    if not ok:
        raise RuntimeError(err)

    safe_send_email(
        [LEAD_INBOX],
        "Lead: Låna bil till skuldsatt (FREE)",
        f"agreement_id: {agreement_id}\n\n{flat}",
    )

    agreement["delivered"] = True
    save_agreement(agreement)


def deliver_premium_oneflow(agreement_id: str, agreement: Dict[str, Any], stripe_session_id: Optional[str]) -> None:
    """
    Premium = Oneflow/BankID:
    - skapa kontrakt från template (om ej redan finns)
    - publish => Oneflow skickar signeringsinbjudan
    - maila båda parter med info (och ev. PDF som fallback om ni vill)
    """
    flat = agreement["flat"]
    lender_email = flat.get("utlanare_epost")
    borrower_email = flat.get("lantagare_epost")

    # Idempotens: skapa bara en gång
    if not agreement.get("oneflow_contract_id"):
        contract = oneflow_create_contract_from_template(agreement)
        agreement["oneflow_contract_id"] = contract.get("id")
        agreement["oneflow_contract"] = {
            "id": contract.get("id"),
            "name": contract.get("name"),
        }
        save_agreement(agreement)

    # Publish bara en gång
    if not agreement.get("oneflow_published"):
        # hämta "contract" object att publish:a (vi kan skapa en minimal med id om vi vill)
        contract_stub = {"id": agreement["oneflow_contract_id"]}
        try:
            # Försök publish med endpoint /contracts/{id}/publish
            oneflow_publish_contract(contract_stub)
        except OneflowError:
            # Om publish behöver _links från full contract: skapa om (sällan)
            # (vi försöker igen genom att skapa ett mer komplett create+publish-flöde)
            contract = oneflow_create_contract_from_template(agreement)
            agreement["oneflow_contract_id"] = contract.get("id")
            save_agreement(agreement)
            oneflow_publish_contract(contract)

        agreement["oneflow_published"] = True
        agreement["oneflow_status"] = "published"
        save_agreement(agreement)

    # Bekräftelsemail (Oneflow skickar signeringslänken separat)
    text = (
        "Tack för er betalning.\n\n"
        "Vi har nu skickat avtalet till Oneflow för signering med BankID.\n"
        "Ni kommer att få en signeringsinbjudan via e-post från Oneflow.\n\n"
        "Om ni inte ser mailet: kontrollera skräppost/övrigt.\n\n"
        "/HP Juridik"
    )

    ok, err = safe_send_email(
        [lender_email, borrower_email],
        "Premium – signering med BankID (Oneflow)",
        text,
        pdf_bytes=None,  # vill ni bifoga PDF ändå? sätt pdf_bytes=base64.b64decode(...)
    )
    if not ok:
        raise RuntimeError(err)

    safe_send_email(
        [LEAD_INBOX],
        "Lead: Låna bil till skuldsatt (PREMIUM/ONEFLOW)",
        f"agreement_id: {agreement_id}\nstripe_session_id: {stripe_session_id}\noneflow_contract_id: {agreement.get('oneflow_contract_id')}\n\n{flat}",
    )

    agreement["is_paid"] = True
    agreement["delivered"] = True  # delivered = signering initierad
    agreement["stripe_session_id"] = stripe_session_id
    save_agreement(agreement)


def deliver_premium_pdf_fallback(agreement_id: str, agreement: Dict[str, Any], stripe_session_id: Optional[str]) -> None:
    """
    Om Oneflow failar: skicka PDF så att kunden ändå får något.
    """
    flat = agreement["flat"]
    pdf_bytes = base64.b64decode(agreement["pdf_b64"])
    lender_email = flat.get("utlanare_epost")
    borrower_email = flat.get("lantagare_epost")

    ok, err = safe_send_email(
        [lender_email, borrower_email],
        "Premium – signeringsdokument (PDF)",
        "Tack för er betalning. Här kommer signeringsdokumentet som PDF.\n\n/HP Juridik",
        pdf_bytes=pdf_bytes,
    )
    if not ok:
        raise RuntimeError(err)

    safe_send_email(
        [LEAD_INBOX],
        "Lead: Låna bil till skuldsatt (PREMIUM/PDF FALLBACK)",
        f"agreement_id: {agreement_id}\nstripe_session_id: {stripe_session_id}\n\n{flat}",
    )

    agreement["is_paid"] = True
    agreement["delivered"] = True
    agreement["stripe_session_id"] = stripe_session_id
    agreement["oneflow_status"] = "failed_fallback_pdf"
    save_agreement(agreement)


# ------------------------------------------------------------------------------
# Routes: Home + Contact
# ------------------------------------------------------------------------------
@app.get("/", response_class=HTMLResponse)
def home(request: Request):
    ctx = page_ctx(request, "/", "HP Juridik", "HP Juridik – juridisk rådgivning.")
    ctx.update(
        {
            "sent": request.query_params.get("sent") == "1",
            "free_ok": request.query_params.get("free") == "1",
            "premium_ok": request.query_params.get("premium") == "1",
            "error": None,
        }
    )
    return templates.TemplateResponse("pages/home.html", ctx)


@app.get("/kontakta-oss", response_class=HTMLResponse)
def contact_page(request: Request):
    ctx = page_ctx(request, "/kontakta-oss", "Kontakt | HP Juridik", "Kontakta HP Juridik.")
    ctx.update({"sent": False, "error": None})
    return templates.TemplateResponse("pages/contact.html", ctx)


@app.post("/contact", response_class=HTMLResponse)
def contact_submit_alias(
    request: Request,
    website: str = Form(""),
    namn: str = Form(""),
    epost: str = Form(""),
    telefon: str = Form(""),
    meddelande: str = Form(""),
):
    return contact_submit(request, website, namn, epost, telefon, meddelande)


@app.post("/kontakta-oss", response_class=HTMLResponse)
def contact_submit(
    request: Request,
    website: str = Form(""),  # honeypot
    namn: str = Form(""),
    epost: str = Form(""),
    telefon: str = Form(""),
    meddelande: str = Form(""),
):
    if website.strip():
        ctx = page_ctx(request, "/", "HP Juridik", "HP Juridik – juridisk rådgivning.")
        ctx.update({"sent": True, "error": None, "free_ok": False, "premium_ok": False})
        return templates.TemplateResponse("pages/home.html", ctx)

    subject = "HP Juridik | Ny kontaktförfrågan från webb"
    body = (
        "NY KONTAKTFÖRFRÅGAN (HPJURIDIK.SE)\n"
        "===============================\n\n"
        f"Namn: {namn}\n"
        f"E-post: {epost}\n"
        f"Telefon: {telefon}\n\n"
        "MEDDELANDE\n"
        "------------------------------\n"
        f"{meddelande}\n\n"
        "TEKNISK INFO\n"
        "------------------------------\n"
        f"Tid (UTC): {utc_iso()}\n"
        f"IP: {request.client.host if request.client else ''}\n"
        f"User-Agent: {request.headers.get('user-agent','')}\n"
    )

    ok, err = safe_send_email(
        [CONTACT_TO],
        subject,
        body,
        reply_to=epost or None,        # Reply-To = besökarens e-post
        from_email=CONTACT_FROM,       # From = hp@ om möjligt
    )
    if not ok:
        ctx = page_ctx(request, "/", "HP Juridik", "HP Juridik – juridisk rådgivning.")
        ctx.update({"sent": False, "error": err, "free_ok": False, "premium_ok": False})
        return templates.TemplateResponse("pages/home.html", ctx, status_code=500)

    ctx = page_ctx(request, "/", "HP Juridik", "HP Juridik – juridisk rådgivning.")
    ctx.update({"sent": True, "error": None, "free_ok": False, "premium_ok": False})
    return templates.TemplateResponse("pages/home.html", ctx)


# ------------------------------------------------------------------------------
# Routes: Låna bil (form -> POST -> 303 review)
# ------------------------------------------------------------------------------
@app.get("/lana-bil-till-skuldsatt", response_class=HTMLResponse)
def lana_bil_form(request: Request):
    ctx = page_ctx(
        request,
        "/lana-bil-till-skuldsatt",
        "Låna bil till skuldsatt | HP Juridik",
        "Skapa avtal och välj Gratis eller Premium.",
    )
    ctx.update({"error": None})
    return templates.TemplateResponse("pages/lana_bil.html", ctx)


@app.post("/lana-bil-till-skuldsatt")
def lana_bil_submit(
    request: Request,
    utlanare_namn: str = Form(...),
    utlanare_pnr: str = Form(""),
    utlanare_adress: str = Form(...),
    utlanare_tel: str = Form(...),
    utlanare_epost: str = Form(...),
    lantagare_namn: str = Form(...),
    lantagare_pnr: str = Form(""),
    lantagare_adress: str = Form(...),
    lantagare_tel: str = Form(...),
    lantagare_epost: str = Form(...),
    fordon_modell: str = Form(...),
    fordon_regnr: str = Form(...),
    from_dt: str = Form(...),
    to_dt: str = Form(...),
    andamal: str = Form(...),
    disclaimer_accept: Optional[str] = Form(None),
    newsletter_optin: Optional[str] = Form(None),
):
    if not disclaimer_accept:
        ctx = page_ctx(request, "/lana-bil-till-skuldsatt", "Låna bil till skuldsatt | HP Juridik", "Skapa avtal.")
        ctx.update({"error": "Du måste godkänna friskrivningen för att fortsätta."})
        return templates.TemplateResponse("pages/lana_bil.html", ctx, status_code=400)

    try:
        from_obj = datetime.fromisoformat(from_dt)
        to_obj = datetime.fromisoformat(to_dt)
    except ValueError:
        ctx = page_ctx(request, "/lana-bil-till-skuldsatt", "Låna bil till skuldsatt | HP Juridik", "Skapa avtal.")
        ctx.update({"error": "Ogiltigt datum/tid-format."})
        return templates.TemplateResponse("pages/lana_bil.html", ctx, status_code=400)

    if to_obj <= from_obj:
        ctx = page_ctx(request, "/lana-bil-till-skuldsatt", "Låna bil till skuldsatt | HP Juridik", "Skapa avtal.")
        ctx.update({"error": "Till (datum & tid) måste vara efter Från."})
        return templates.TemplateResponse("pages/lana_bil.html", ctx, status_code=400)

    agreement_id = str(uuid.uuid4())

    flat = {
        "agreement_id": agreement_id,
        "created_utc": utc_iso(),
        "utlanare_namn": utlanare_namn,
        "utlanare_pnr": utlanare_pnr,
        "utlanare_adress": utlanare_adress,
        "utlanare_tel": utlanare_tel,
        "utlanare_epost": utlanare_epost,
        "lantagare_namn": lantagare_namn,
        "lantagare_pnr": lantagare_pnr,
        "lantagare_adress": lantagare_adress,
        "lantagare_tel": lantagare_tel,
        "lantagare_epost": lantagare_epost,
        "fordon_modell": fordon_modell,
        "fordon_regnr": "".join(fordon_regnr.split()).upper(),
        "from_str": from_obj.strftime("%Y-%m-%d %H:%M"),
        "to_str": to_obj.strftime("%Y-%m-%d %H:%M"),
        "andamal": andamal,
        "newsletter_optin": bool(newsletter_optin),
    }

    pdf_bytes = build_loan_pdf(flat)

    structured = {
        "utlanare": {"namn": utlanare_namn, "pnr": utlanare_pnr, "adress": utlanare_adress, "tel": utlanare_tel, "epost": utlanare_epost},
        "lantagare": {"namn": lantagare_namn, "pnr": lantagare_pnr, "adress": lantagare_adress, "tel": lantagare_tel, "epost": lantagare_epost},
        "fordon": {"modell": fordon_modell, "regnr": flat["fordon_regnr"]},
        "period": {"from_str": flat["from_str"], "to_str": flat["to_str"]},
        "andamal": andamal,
        "newsletter_optin": flat["newsletter_optin"],
    }

    agreement = {
        "agreement_id": agreement_id,
        "created_utc": flat["created_utc"],
        "data": structured,
        "flat": flat,
        "pdf_b64": base64.b64encode(pdf_bytes).decode("utf-8"),
        "is_paid": False,
        "stripe_session_id": None,
        "delivered": False,
        "oneflow_contract_id": None,
        "oneflow_published": False,
        "oneflow_status": None,
    }

    save_agreement(agreement)

    request.session["agreement_id"] = agreement_id
    return RedirectResponse(url="/lana-bil-till-skuldsatt/review", status_code=303)


@app.get("/lana-bil-till-skuldsatt/review", response_class=HTMLResponse)
def lana_bil_review_get(request: Request):
    agreement_id = request.session.get("agreement_id")
    agreement = load_agreement(agreement_id)
    if not agreement_id or not agreement:
        return RedirectResponse(url="/lana-bil-till-skuldsatt", status_code=303)

    ctx = page_ctx(request, "/lana-bil-till-skuldsatt/review", "Granska uppgifter | HP Juridik", "Granska och välj Gratis eller Premium.")
    ctx.update({"agreement_id": agreement_id, "data": agreement["data"], "error": None})
    return templates.TemplateResponse("pages/lana_bil_review.html", ctx)


@app.post("/lana-bil-till-skuldsatt/review")
def lana_bil_review_post(
    request: Request,
    plan: str = Form(...),  # free | premium
    confirm_correct: Optional[str] = Form(None),
    disclaimer_accept: Optional[str] = Form(None),
):
    agreement_id = request.session.get("agreement_id")
    agreement = load_agreement(agreement_id)
    if not agreement_id or not agreement:
        return RedirectResponse(url="/lana-bil-till-skuldsatt", status_code=303)

    if not (confirm_correct and disclaimer_accept):
        ctx = page_ctx(request, "/lana-bil-till-skuldsatt/review", "Granska uppgifter | HP Juridik", "Granska.")
        ctx.update({"agreement_id": agreement_id, "data": agreement["data"], "error": "Du måste kryssa i båda rutorna för att fortsätta."})
        return templates.TemplateResponse("pages/lana_bil_review.html", ctx, status_code=400)

    if plan == "free":
        deliver_free(agreement_id, agreement)
        return RedirectResponse(url="/?free=1", status_code=303)

    if plan == "premium":
        if not STRIPE_SECRET_KEY:
            raise HTTPException(status_code=500, detail="STRIPE_SECRET_KEY saknas")

        session = stripe.checkout.Session.create(
            mode="payment",
            payment_method_types=["card"],
            line_items=[
                {
                    "price_data": {
                        "currency": "sek",
                        "product_data": {"name": "Premium – BankID-signering (Oneflow)"},
                        "unit_amount": PREMIUM_PRICE_ORE,
                    },
                    "quantity": 1,
                }
            ],
            metadata={"agreement_id": agreement_id},
            payment_intent_data={"metadata": {"agreement_id": agreement_id}},
            success_url=f"{BASE_URL}/checkout-success?agreement_id={agreement_id}",
            cancel_url=f"{BASE_URL}/checkout-cancel?agreement_id={agreement_id}",
        )

        agreement["stripe_session_id"] = session.id
        save_agreement(agreement)

        return RedirectResponse(url=session.url, status_code=303)

    raise HTTPException(status_code=400, detail="Invalid plan")


# ------------------------------------------------------------------------------
# Stripe Webhook (premium leverans sker här)
# ------------------------------------------------------------------------------
@app.post("/stripe/webhook")
@app.post("/stripe/webhook/")
async def stripe_webhook(request: Request):
    print("=== STRIPE WEBHOOK HIT ===", utc_iso())

    if not STRIPE_WEBHOOK_SECRET:
        raise HTTPException(status_code=500, detail="STRIPE_WEBHOOK_SECRET saknas")

    payload = await request.body()
    sig_header = request.headers.get("stripe-signature")
    print("Has stripe-signature header:", bool(sig_header), "payload bytes:", len(payload))

    if not sig_header:
        return PlainTextResponse("missing stripe-signature header", status_code=400)

    try:
        event = stripe.Webhook.construct_event(payload=payload, sig_header=sig_header, secret=STRIPE_WEBHOOK_SECRET)
    except Exception as e:
        print("Stripe verify failed:", repr(e))
        return PlainTextResponse(f"invalid signature: {type(e).__name__}", status_code=400)

    event_type = event.get("type")
    print("Stripe event:", event_type)

    if event_type not in ("checkout.session.completed", "checkout.session.async_payment_succeeded"):
        return PlainTextResponse("ok", status_code=200)

    session_obj = event["data"]["object"]
    session_id = session_obj.get("id")
    metadata = session_obj.get("metadata") or {}
    agreement_id = metadata.get("agreement_id")
    payment_status = session_obj.get("payment_status")
    status = session_obj.get("status")

    print("checkout session:", "session_id=", session_id, "agreement_id=", agreement_id, "status=", status, "payment_status=", payment_status)

    if payment_status != "paid":
        print("Not paid yet -> no delivery.")
        return PlainTextResponse("ok", status_code=200)

    if not agreement_id:
        ok, err = safe_send_email([LEAD_INBOX], "Stripe ALERT: saknar agreement_id", f"session_id={session_id}\nmetadata={metadata}")
        if not ok:
            print("ALERT email failed:", err)
        return PlainTextResponse("ok", status_code=200)

    agreement = load_agreement(agreement_id)
    if not agreement:
        msg = f"Stripe PAID men agreement saknas.\nagreement_id={agreement_id}\nsession_id={session_id}\nmetadata={metadata}"
        print("WARNING:", msg)
        safe_send_email([LEAD_INBOX], "Stripe ALERT: agreement saknas (persistens)", msg)
        return PlainTextResponse("ok", status_code=200)

    # Idempotens
    if agreement.get("delivered"):
        print("Already delivered:", agreement_id)
        return PlainTextResponse("ok", status_code=200)

    agreement["stripe_session_id"] = agreement.get("stripe_session_id") or session_id
    agreement["is_paid"] = True
    save_agreement(agreement)

    # Premium: Oneflow först, annars PDF fallback
    try:
        if ONEFLOW_ENABLED:
            deliver_premium_oneflow(agreement_id, agreement, stripe_session_id=session_id)
            print("Premium Oneflow delivered OK:", agreement_id)
        else:
            # Om Oneflow inte är konfiggat än -> premium pdf
            deliver_premium_pdf_fallback(agreement_id, agreement, stripe_session_id=session_id)
            print("Premium PDF delivered (no Oneflow):", agreement_id)

    except Exception as e:
        err_txt = f"Premium delivery failed agreement_id={agreement_id} session_id={session_id}: {e}"
        print(err_txt)
        safe_send_email([LEAD_INBOX], "Delivery ERROR i Stripe webhook", err_txt)
        return PlainTextResponse("delivery error", status_code=500)

    return PlainTextResponse("ok", status_code=200)


# ------------------------------------------------------------------------------
# Oneflow webhook (du skapar den senare i Oneflow UI)
# ------------------------------------------------------------------------------
@app.post("/oneflow/webhook")
async def oneflow_webhook(request: Request):
    """
    När du skapat webhook i Oneflow kommer events hit.
    Vi loggar och uppdaterar status om vi kan hitta agreement via oneflow_contract_id.
    """
    raw = await request.body()
    try:
        payload = json.loads(raw.decode("utf-8"))
    except Exception:
        return PlainTextResponse("invalid json", status_code=400)

    # TODO: verifiera signatur med ONEFLOW_WEBHOOK_SECRET om du vill
    # (Oneflow skickar signatur-header beroende på webhook-typ/inställning)

    event_type = payload.get("event") or payload.get("type") or "unknown"
    contract_id = payload.get("contract_id") or payload.get("contract", {}).get("id")

    print("=== ONEFLOW WEBHOOK ===", utc_iso(), "event=", event_type, "contract_id=", contract_id)

    # Hitta agreement genom att skanna filer (MVP)
    if contract_id:
        _ensure_dir(AGREEMENTS_DIR)
        for fn in os.listdir(AGREEMENTS_DIR):
            if not fn.endswith(".json"):
                continue
            p = os.path.join(AGREEMENTS_DIR, fn)
            try:
                with open(p, "r", encoding="utf-8") as f:
                    ag = json.load(f)
                if str(ag.get("oneflow_contract_id")) == str(contract_id):
                    ag["oneflow_status"] = event_type
                    save_agreement(ag)
                    break
            except Exception:
                continue

    return PlainTextResponse("ok", status_code=200)


# ------------------------------------------------------------------------------
# Checkout pages (leverans sker via webhook)
# ------------------------------------------------------------------------------
@app.get("/checkout-success", response_class=HTMLResponse)
def checkout_success(request: Request):
    ctx = page_ctx(request, "/checkout-success", "Tack | HP Juridik", "Tack för din betalning.")
    return templates.TemplateResponse("pages/checkout_success.html", ctx)


@app.get("/checkout-cancel", response_class=HTMLResponse)
def checkout_cancel(request: Request):
    ctx = page_ctx(request, "/checkout-cancel", "Avbrutet | HP Juridik", "Betalningen avbröts.")
    return templates.TemplateResponse("pages/checkout_cancel.html", ctx)


@app.get("/healthz", response_class=PlainTextResponse)
def healthz():
    return "ok"
