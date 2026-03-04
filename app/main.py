import os
import io
import uuid
import json
import base64
import smtplib
from datetime import datetime, timezone
from typing import Dict, Any, Optional, List, Tuple

import stripe
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
app = FastAPI()
app.mount("/static", StaticFiles(directory="app/static"), name="static")
templates = Jinja2Templates(directory="app/templates")

SESSION_SECRET = os.getenv("SESSION_SECRET", "dev-change-me")
app.add_middleware(SessionMiddleware, secret_key=SESSION_SECRET)

# ------------------------------------------------------------------------------
# ENV / Settings
# ------------------------------------------------------------------------------
BASE_URL = os.getenv("BASE_URL", "http://localhost:10000").rstrip("/")

MAIL_FROM = os.getenv("MAIL_FROM", "HP@hpjuridik.se")
CONTACT_TO = os.getenv("CONTACT_TO", "hp@hpjuridik.se")
CONTACT_FROM = os.getenv("CONTACT_FROM", MAIL_FROM)
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

CANONICAL_HOST = os.getenv("CANONICAL_HOST", "hpjuridik.se").strip().lower()
SITE_URL = os.getenv("SITE_URL", f"https://{CANONICAL_HOST}").rstrip("/")

# Filpersistens (MVP): funkar även om processen restartar / annan worker får webhooken
AGREEMENTS_DIR = os.getenv("AGREEMENTS_DIR", "/tmp/hpj_agreements")

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
# Small utils
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
    # Bara uuid i våra id:n -> säkert filnamn
    return os.path.join(AGREEMENTS_DIR, f"{agreement_id}.json")


def save_agreement(agreement: Dict[str, Any]) -> None:
    _ensure_dir(AGREEMENTS_DIR)
    agreement_id = agreement["agreement_id"]
    with open(_agreement_path(agreement_id), "w", encoding="utf-8") as f:
        json.dump(agreement, f, ensure_ascii=False)


def load_agreement(agreement_id: str) -> Optional[Dict[str, Any]]:
    path = _agreement_path(agreement_id)
    if not os.path.exists(path):
        return None
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def mark_delivered(agreement_id: str, delivered: bool, is_paid: bool = False, stripe_session_id: Optional[str] = None) -> None:
    ag = load_agreement(agreement_id)
    if not ag:
        return
    ag["delivered"] = bool(delivered)
    if is_paid:
        ag["is_paid"] = True
    if stripe_session_id:
        ag["stripe_session_id"] = stripe_session_id
    save_agreement(ag)


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


def send_email(
    to_emails: List[str],
    subject: str,
    text: str,
    pdf_bytes: Optional[bytes] = None,
    reply_to: Optional[str] = None,
    from_email: Optional[str] = None,
) -> None:
    _smtp_send(to_emails, subject, text, pdf_bytes=pdf_bytes, reply_to=reply_to, from_email=from_email)


def safe_send_email(
    to_emails: List[str],
    subject: str,
    text: str,
    pdf_bytes: Optional[bytes] = None,
    reply_to: Optional[str] = None,
    from_email: Optional[str] = None,
) -> Tuple[bool, Optional[str]]:
    try:
        send_email(to_emails, subject, text, pdf_bytes=pdf_bytes, reply_to=reply_to, from_email=from_email)
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
# Delivery helpers
# ------------------------------------------------------------------------------
def deliver_free(agreement_id: str, agreement: Dict[str, Any]) -> None:
    flat = agreement["flat"]
    pdf_bytes = base64.b64decode(agreement["pdf_b64"])
    lender_email = flat.get("utlanare_epost")
    borrower_email = flat.get("lantagare_epost")

    send_email(
        [lender_email, borrower_email],
        "Tillfälligt låneavtal – bil (PDF)",
        "Här kommer ert avtal som PDF.\n\n/HP Juridik",
        pdf_bytes=pdf_bytes,
    )

    send_email(
        [LEAD_INBOX],
        "Lead: Låna bil till skuldsatt (FREE)",
        f"agreement_id: {agreement_id}\n\n{flat}",
    )

    agreement["delivered"] = True
    save_agreement(agreement)


def deliver_premium_pdf(agreement_id: str, agreement: Dict[str, Any], stripe_session_id: Optional[str]) -> None:
    """
    Premium: skicka 'signeringsdokument' (PDF) till båda parter.
    (Oneflow/BankID-signering lägger vi senare.)
    """
    flat = agreement["flat"]
    pdf_bytes = base64.b64decode(agreement["pdf_b64"])
    lender_email = flat.get("utlanare_epost")
    borrower_email = flat.get("lantagare_epost")

    send_email(
        [lender_email, borrower_email],
        "Premium – signeringsdokument (PDF)",
        "Tack för er betalning. Här kommer signeringsdokumentet som PDF.\n\n/HP Juridik",
        pdf_bytes=pdf_bytes,
    )

    send_email(
        [LEAD_INBOX],
        "Lead: Låna bil till skuldsatt (PREMIUM)",
        f"agreement_id: {agreement_id}\nstripe_session_id: {stripe_session_id}\n\n{flat}",
    )

    agreement["is_paid"] = True
    agreement["delivered"] = True
    agreement["stripe_session_id"] = stripe_session_id
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

    try:
        send_email(
            [CONTACT_TO],
            subject,
            body,
            reply_to=epost or None,
            from_email=CONTACT_FROM,
        )
    except Exception as e:
        ctx = page_ctx(request, "/", "HP Juridik", "HP Juridik – juridisk rådgivning.")
        ctx.update({"sent": False, "error": str(e), "free_ok": False, "premium_ok": False})
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
    ctx.update({"error": None, "sent_ok": False, "sent_error": None})
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
        ctx.update({"error": "Du måste godkänna friskrivningen för att fortsätta.", "sent_ok": False, "sent_error": None})
        return templates.TemplateResponse("pages/lana_bil.html", ctx, status_code=400)

    try:
        from_obj = datetime.fromisoformat(from_dt)
        to_obj = datetime.fromisoformat(to_dt)
    except ValueError:
        ctx = page_ctx(request, "/lana-bil-till-skuldsatt", "Låna bil till skuldsatt | HP Juridik", "Skapa avtal.")
        ctx.update({"error": "Ogiltigt datum/tid-format.", "sent_ok": False, "sent_error": None})
        return templates.TemplateResponse("pages/lana_bil.html", ctx, status_code=400)

    if to_obj <= from_obj:
        ctx = page_ctx(request, "/lana-bil-till-skuldsatt", "Låna bil till skuldsatt | HP Juridik", "Skapa avtal.")
        ctx.update({"error": "Till (datum & tid) måste vara efter Från.", "sent_ok": False, "sent_error": None})
        return templates.TemplateResponse("pages/lana_bil.html", ctx)

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
    }

    save_agreement(agreement)

    request.session["agreement_id"] = agreement_id
    return RedirectResponse(url="/lana-bil-till-skuldsatt/review", status_code=303)


@app.get("/lana-bil-till-skuldsatt/review", response_class=HTMLResponse)
def lana_bil_review_get(request: Request):
    agreement_id = request.session.get("agreement_id")
    agreement = load_agreement(agreement_id) if agreement_id else None
    if not agreement_id or not agreement:
        return RedirectResponse(url="/lana-bil-till-skuldsatt", status_code=303)

    ctx = page_ctx(request, "/lana-bil-till-skuldsatt/review", "Granska uppgifter | HP Juridik", "Granska uppgifter och välj Gratis eller Premium.")
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
    agreement = load_agreement(agreement_id) if agreement_id else None
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
                        "product_data": {"name": "Premium – Låna bil till skuldsatt"},
                        "unit_amount": PREMIUM_PRICE_ORE,
                    },
                    "quantity": 1,
                }
            ],
            metadata={"agreement_id": agreement_id},
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
        event = stripe.Webhook.construct_event(
            payload=payload,
            sig_header=sig_header,
            secret=STRIPE_WEBHOOK_SECRET,
        )
    except Exception as e:
        print("Stripe verify failed:", repr(e))
        return PlainTextResponse(f"invalid signature: {type(e).__name__}", status_code=400)

    event_type = event.get("type")
    print("Stripe event:", event_type)

    # Acceptera både "completed" och async success (vissa betalningar blir inte 'paid' direkt)
    if event_type not in (
        "checkout.session.completed",
        "checkout.session.async_payment_succeeded",
    ):
        return PlainTextResponse("ok", status_code=200)

    session_obj = event["data"]["object"]
    session_id = session_obj.get("id")
    metadata = session_obj.get("metadata") or {}
    agreement_id = metadata.get("agreement_id")
    payment_status = session_obj.get("payment_status")
    status = session_obj.get("status")

    print(
        "checkout session:",
        "session_id=", session_id,
        "agreement_id=", agreement_id,
        "status=", status,
        "payment_status=", payment_status,
    )

    # Leverera inte förrän Stripe säger PAID (skydd mot att success-sida nås innan betalning är klar)
    if payment_status != "paid":
        print("Not paid yet -> no delivery. Waiting for async_payment_succeeded. payment_status=", payment_status)
        return PlainTextResponse("ok", status_code=200)

    # Fallback 1: metadata saknar agreement_id
    if not agreement_id:
        ok, err = safe_send_email(
            [LEAD_INBOX],
            "Stripe ALERT: saknar agreement_id",
            f"session_id={session_id}\nstatus={status}\npayment_status={payment_status}\nmetadata={metadata}",
        )
        if not ok:
            print("ALERT email failed:", err)
        return PlainTextResponse("ok", status_code=200)

    agreement = load_agreement(agreement_id)

    # Fallback 2: agreement saknas på disk
    if not agreement:
        msg = (
            "Stripe PAID men agreement saknas i persistens.\n\n"
            f"agreement_id={agreement_id}\n"
            f"session_id={session_id}\n"
            f"status={status}\n"
            f"payment_status={payment_status}\n"
            f"metadata={metadata}\n"
        )
        print("WARNING:", msg)
        ok, err = safe_send_email([LEAD_INBOX], "Stripe ALERT: agreement saknas (persistens)", msg)
        if not ok:
            print("ALERT email failed:", err)
        return PlainTextResponse("ok", status_code=200)

    # Idempotens
    if agreement.get("delivered"):
        print("Already delivered:", agreement_id)
        return PlainTextResponse("ok", status_code=200)

    # Leverera PDF till båda parter
    try:
        deliver_premium_pdf(agreement_id, agreement, stripe_session_id=session_id)
        print("Premium delivered OK:", agreement_id)
    except Exception as e:
        err_txt = f"Premium delivery failed agreement_id={agreement_id} session_id={session_id}: {e}"
        print(err_txt)
        ok, err2 = safe_send_email([LEAD_INBOX], "SMTP/Delivery ERROR i Stripe webhook", err_txt)
        if not ok:
            print("Could not send error email:", err2)
        # 500 => Stripe retry (bra om SMTP var tillfälligt nere)
        return PlainTextResponse("delivery error", status_code=500)

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
