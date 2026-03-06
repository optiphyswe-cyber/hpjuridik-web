from __future__ import annotations

import os
import io
import re
import uuid
import json
import base64
import hmac
import smtplib
import hashlib
from datetime import datetime, timezone
from typing import Dict, Any, Optional, List, Tuple

import requests
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
CONTACT_FROM = os.getenv("CONTACT_FROM", "hp@hpjuridik.se")
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

CANONICAL_HOST = os.getenv("CANONICAL_HOST", "www.hpjuridik.se").strip().lower()
SITE_URL = os.getenv("SITE_URL", f"https://{CANONICAL_HOST}").rstrip("/")

# Render-safe filpersistens
AGREEMENTS_DIR = os.getenv("AGREEMENTS_DIR", "/tmp/hpj_agreements")

# Oneflow
ONEFLOW_API_TOKEN = os.getenv("ONEFLOW_API_TOKEN", "")
ONEFLOW_BASE_URL = os.getenv("ONEFLOW_BASE_URL", "https://api.oneflow.com/v1").rstrip("/")
ONEFLOW_WORKSPACE_ID = os.getenv("ONEFLOW_WORKSPACE_ID", "")
ONEFLOW_TEMPLATE_ID = os.getenv("ONEFLOW_TEMPLATE_ID", "")
ONEFLOW_USER_EMAIL = os.getenv("ONEFLOW_USER_EMAIL", "")
ONEFLOW_WEBHOOK_SIGN_KEY = os.getenv("ONEFLOW_WEBHOOK_SIGN_KEY", "")

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
    agreement["updated_utc"] = utc_iso()
    with open(_agreement_path(agreement["agreement_id"]), "w", encoding="utf-8") as f:
        json.dump(agreement, f, ensure_ascii=False, indent=2)


def load_agreement(agreement_id: Optional[str]) -> Optional[Dict[str, Any]]:
    if not agreement_id:
        return None
    path = _agreement_path(agreement_id)
    if not os.path.exists(path):
        return None
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def find_agreement_by_contract_id(contract_id: str) -> Optional[Dict[str, Any]]:
    if not contract_id:
        return None
    _ensure_dir(AGREEMENTS_DIR)
    for name in os.listdir(AGREEMENTS_DIR):
        if not name.endswith(".json"):
            continue
        try:
            with open(os.path.join(AGREEMENTS_DIR, name), "r", encoding="utf-8") as f:
                agreement = json.load(f)
            if str(agreement.get("oneflow_contract_id") or "") == str(contract_id):
                return agreement
        except Exception:
            continue
    return None


def normalize_regnr(value: str) -> str:
    return "".join((value or "").split()).upper()


def safe_send_email(
    to_list: List[str],
    subject: str,
    body: str,
    pdf_bytes: Optional[bytes] = None,
    pdf_filename: str = "avtal.pdf",
    reply_to: Optional[str] = None,
    from_email: Optional[str] = None,
) -> Tuple[bool, Optional[str]]:
    try:
        recipients = [x.strip() for x in to_list if x and x.strip()]
        if not recipients:
            return False, "Inga mottagare angivna"
        if not SMTP_HOST or not SMTP_USER or not SMTP_PASS:
            return False, "SMTP env saknas"

        msg = EmailMessage()
        msg["Subject"] = subject
        msg["From"] = from_email or MAIL_FROM
        msg["To"] = ", ".join(recipients)
        if reply_to:
            msg["Reply-To"] = reply_to
        msg.set_content(body)

        if pdf_bytes:
            msg.add_attachment(
                pdf_bytes,
                maintype="application",
                subtype="pdf",
                filename=pdf_filename,
            )

        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=30) as s:
            s.starttls()
            s.login(SMTP_USER, SMTP_PASS)
            s.send_message(msg)

        return True, None
    except Exception as e:
        return False, repr(e)


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
    story.append(Paragraph(f"Avtals-ID: {flat.get('agreement_id', '')}", p))
    story.append(Paragraph(f"Skapat (UTC): {flat.get('created_utc', '')}", p))
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
    headers = {
        "x-oneflow-api-token": ONEFLOW_API_TOKEN,
        "Content-Type": "application/json",
    }
    if ONEFLOW_USER_EMAIL:
        headers["x-oneflow-user-email"] = ONEFLOW_USER_EMAIL
    return headers


def oneflow_create_contract_from_template(agreement: Dict[str, Any]) -> Dict[str, Any]:
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
                "participants": [
                    {
                        "name": lender_name,
                        "email": lender_email,
                        "delivery_channel": "email",
                        "sign_method": "swedish_bankid",
                    }
                ],
            },
            {
                "type": "individual",
                "name": borrower_name,
                "participants": [
                    {
                        "name": borrower_name,
                        "email": borrower_email,
                        "delivery_channel": "email",
                        "sign_method": "swedish_bankid",
                    }
                ],
            },
        ],
    }

    if ONEFLOW_WORKSPACE_ID:
        payload["workspace_id"] = int(ONEFLOW_WORKSPACE_ID)

    r = requests.post(
        f"{ONEFLOW_BASE_URL}/contracts/create",
        headers=oneflow_headers(),
        json=payload,
        timeout=30,
    )
    if r.status_code >= 300:
        raise OneflowError(f"Oneflow create failed {r.status_code}: {r.text}")

    return r.json()


def agreement_to_oneflow_fields(flat: Dict[str, Any]) -> Dict[str, str]:
    def s(x: Any) -> str:
        return "" if x is None else str(x)

    return {
        "utlanare_namn": s(flat.get("utlanare_namn")),
        "utlanare_adress": s(flat.get("utlanare_adress")),
        "lantagare_namn": s(flat.get("lantagare_namn")),
        "lantagare_adress": s(flat.get("lantagare_adress")),
        "fordon_regnr": s(flat.get("fordon_regnr")),
        "from_str": s(flat.get("from_str")),
        "to_str": s(flat.get("to_str")),
        "andamal": s(flat.get("andamal")),
    }


def oneflow_set_data_fields(contract_id: str, fields: Dict[str, str]) -> None:
    if not fields:
        return
    payload = [{"external_key": k, "value": v} for k, v in fields.items()]
    r = requests.put(
        f"{ONEFLOW_BASE_URL}/contracts/{contract_id}/data_fields",
        headers=oneflow_headers(),
        data=json.dumps(payload),
        timeout=30,
    )
    if r.status_code >= 300:
        raise OneflowError(f"Oneflow set_data_fields failed {r.status_code}: {r.text}")


def oneflow_publish_contract(contract: Dict[str, Any]) -> None:
    contract_id = contract.get("id")
    if not contract_id:
        raise OneflowError("Oneflow contract saknar id")

    publish_url = None
    links = contract.get("_links") or {}
    if isinstance(links, dict):
        publish = links.get("publish") or {}
        if isinstance(publish, dict):
            publish_url = publish.get("href")

    payload = {
        "subject": "Signera ert avtal (BankID) – HP Juridik",
        "message": "Hej! Oneflow skickar nu en signeringsinbjudan via e-post. Signera med BankID.\n\n/HP Juridik",
    }

    if publish_url:
        r = requests.post(publish_url, headers=oneflow_headers(), json=payload, timeout=30)
    else:
        r = requests.post(
            f"{ONEFLOW_BASE_URL}/contracts/{int(contract_id)}/publish",
            headers=oneflow_headers(),
            json=payload,
            timeout=30,
        )

    if r.status_code >= 300:
        raise OneflowError(f"Oneflow publish failed {r.status_code}: {r.text}")


def oneflow_get_contract(contract_id: str) -> Dict[str, Any]:
    r = requests.get(
        f"{ONEFLOW_BASE_URL}/contracts/{contract_id}",
        headers=oneflow_headers(),
        timeout=20,
    )
    if r.status_code >= 300:
        raise OneflowError(f"Oneflow get contract failed {r.status_code}: {r.text}")
    return r.json()


def oneflow_download_contract_pdf(contract_id: str) -> bytes:
    r = requests.get(
        f"{ONEFLOW_BASE_URL}/contracts/{contract_id}/files",
        headers=oneflow_headers(),
        timeout=30,
    )
    if r.status_code >= 300:
        raise OneflowError(f"Oneflow list files failed {r.status_code}: {r.text}")

    files = r.json()
    if isinstance(files, dict) and "files" in files:
        files = files["files"]

    if not isinstance(files, list) or not files:
        raise OneflowError("Inga filer hittades på kontraktet")

    pdf = None
    for f in files:
        content_type = (f.get("content_type") or "").lower()
        name = (f.get("name") or "").lower()
        if content_type == "application/pdf" or name.endswith(".pdf"):
            pdf = f
            break
    if not pdf:
        pdf = files[0]

    file_id = str(pdf.get("id") or pdf.get("file_id") or "")
    if not file_id:
        raise OneflowError(f"Kunde inte läsa file id: {pdf}")

    r2 = requests.get(
        f"{ONEFLOW_BASE_URL}/contracts/{contract_id}/files/{file_id}/download",
        headers=oneflow_headers(),
        timeout=60,
    )
    if r2.status_code >= 300:
        raise OneflowError(f"Oneflow download failed {r2.status_code}: {r2.text}")

    return r2.content


def verify_oneflow_webhook(headers: Dict[str, str]) -> bool:
    if not ONEFLOW_WEBHOOK_SIGN_KEY:
        return True

    callback_id = (
        headers.get("x-oneflow-callback-id")
        or headers.get("X-Oneflow-Callback-Id")
        or ""
    )
    signature = (
        headers.get("x-oneflow-signature")
        or headers.get("X-Oneflow-Signature")
        or ""
    )

    if not callback_id or not signature:
        return False

    expected = hashlib.sha1((callback_id + ONEFLOW_WEBHOOK_SIGN_KEY).encode("utf-8")).hexdigest()
    return hmac.compare_digest(expected, signature)


def oneflow_extract_contract_id(payload: Dict[str, Any]) -> str:
    candidates = [
        payload.get("contract_id"),
        payload.get("id"),
        (payload.get("contract") or {}).get("id"),
        (payload.get("data") or {}).get("contract_id"),
        ((payload.get("data") or {}).get("contract") or {}).get("id"),
        ((payload.get("payload") or {}).get("contract") or {}).get("id"),
    ]
    for value in candidates:
        if value is not None and str(value).strip():
            return str(value)
    return ""


def oneflow_extract_event_type(payload: Dict[str, Any]) -> str:
    for key in ("type", "event_type", "event", "name"):
        value = payload.get(key)
        if value:
            return str(value).lower()
    return ""


def oneflow_contract_is_signed(contract: Dict[str, Any]) -> bool:
    raw = json.dumps(contract, ensure_ascii=False).lower()
    signed_markers = [
        '"state":"signed"',
        '"status":"signed"',
        '"lifecycle_state":"signed"',
        '"is_signed":true',
        '"fully_signed":true',
    ]
    if any(marker in raw for marker in signed_markers):
        return True

    parties = contract.get("parties") or []
    participants = []
    for party in parties:
        participants.extend(party.get("participants") or [])

    if participants:
        signedish = 0
        for participant in participants:
            p_raw = json.dumps(participant, ensure_ascii=False).lower()
            if any(x in p_raw for x in ['"signed"', '"sign_state":"signed"', '"state":"signed"']):
                signedish += 1
        if signedish == len(participants):
            return True

    return False


# ------------------------------------------------------------------------------
# Delivery helpers
# ------------------------------------------------------------------------------
def deliver_free(agreement_id: str, agreement: Dict[str, Any]) -> None:
    if agreement.get("delivered"):
        return

    flat = agreement["flat"]
    pdf_bytes = base64.b64decode(agreement["pdf_b64"])

    lender_email = flat.get("utlanare_epost")
    borrower_email = flat.get("lantagare_epost")

    ok, err = safe_send_email(
        [lender_email, borrower_email],
        "Bilutlåningsavtal – PDF",
        "Här kommer ert bilutlåningsavtal som PDF.\n\n/HP Juridik",
        pdf_bytes=pdf_bytes,
        pdf_filename=f"bilutlaningsavtal-{agreement_id}.pdf",
    )
    if not ok:
        raise RuntimeError(err)

    agreement["delivered"] = True
    agreement["delivery_mode"] = "free_pdf"
    save_agreement(agreement)


def deliver_premium_pdf_fallback(agreement_id: str, agreement: Dict[str, Any], stripe_session_id: str) -> None:
    flat = agreement["flat"]
    pdf_bytes = base64.b64decode(agreement["pdf_b64"])

    lender_email = flat.get("utlanare_epost")
    borrower_email = flat.get("lantagare_epost")

    ok, err = safe_send_email(
        [lender_email, borrower_email],
        "Premium – signeringsdokument (PDF)",
        "Tack för er betalning. Här kommer signeringsdokumentet som PDF.\n\n/HP Juridik",
        pdf_bytes=pdf_bytes,
        pdf_filename=f"bilutlaningsavtal-{agreement_id}.pdf",
    )
    if not ok:
        raise RuntimeError(err)

    agreement["is_paid"] = True
    agreement["delivered"] = True
    agreement["stripe_session_id"] = stripe_session_id
    agreement["oneflow_status"] = "failed_fallback_pdf"
    agreement["delivery_mode"] = "premium_pdf_fallback"
    save_agreement(agreement)


def deliver_premium_oneflow(agreement_id: str, agreement: Dict[str, Any], stripe_session_id: str) -> None:
    if agreement.get("oneflow_contract_id") and agreement.get("oneflow_published"):
        return

    contract = oneflow_create_contract_from_template(agreement)
    contract_id = str(contract.get("id") or "")
    if not contract_id:
        raise OneflowError(f"Oneflow contract saknar id: {contract}")

    oneflow_set_data_fields(contract_id, agreement_to_oneflow_fields(agreement["flat"]))
    oneflow_publish_contract(contract)

    agreement["is_paid"] = True
    agreement["stripe_session_id"] = stripe_session_id
    agreement["oneflow_contract_id"] = contract_id
    agreement["oneflow_published"] = True
    agreement["oneflow_status"] = "published"
    agreement["delivery_mode"] = "oneflow"
    save_agreement(agreement)

    lender_email = agreement["flat"].get("utlanare_epost")
    borrower_email = agreement["flat"].get("lantagare_epost")

    ok, err = safe_send_email(
        [lender_email, borrower_email],
        "Tack för din betalning – signering skickas via Oneflow",
        (
            "Tack för er betalning.\n\n"
            "En signeringsinbjudan skickas nu via Oneflow till de e-postadresser som angivits i avtalet. "
            "Signering sker med BankID.\n\n"
            f"Orderreferens: {agreement_id}\n\n"
            "/HP Juridik"
        ),
    )
    if not ok:
        print("INFO: kundmail efter premium/oneflow misslyckades:", err)

    safe_send_email(
        [LEAD_INBOX],
        "Lead: Låna bil till skuldsatt (PREMIUM/ONEFLOW)",
        f"agreement_id: {agreement_id}\nstripe_session_id: {stripe_session_id}\noneflow_contract_id: {contract_id}",
    )


def finalize_signed_oneflow_contract(agreement: Dict[str, Any]) -> None:
    if agreement.get("delivered") and agreement.get("signed_pdf_b64"):
        return

    contract_id = str(agreement.get("oneflow_contract_id") or "")
    if not contract_id:
        raise OneflowError("Agreement saknar oneflow_contract_id")

    pdf_bytes = oneflow_download_contract_pdf(contract_id)
    agreement["signed_pdf_b64"] = base64.b64encode(pdf_bytes).decode("utf-8")
    agreement["oneflow_status"] = "signed"
    agreement["delivered"] = True
    save_agreement(agreement)

    flat = agreement["flat"]
    lender_email = flat.get("utlanare_epost")
    borrower_email = flat.get("lantagare_epost")

    ok, err = safe_send_email(
        [lender_email, borrower_email],
        "Signerat bilutlåningsavtal – PDF",
        (
            "Avtalet är nu signerat och bifogas som PDF.\n\n"
            f"Avtals-ID: {agreement['agreement_id']}\n\n"
            "/HP Juridik"
        ),
        pdf_bytes=pdf_bytes,
        pdf_filename=f"bilutlaningsavtal-signerat-{agreement['agreement_id']}.pdf",
    )
    if not ok:
        raise RuntimeError(err)


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
    website: str = Form(""),
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
        f"User-Agent: {request.headers.get('user-agent', '')}\n"
    )

    ok, err = safe_send_email(
        [CONTACT_TO],
        subject,
        body,
        reply_to=epost or None,
        from_email=CONTACT_FROM,
    )
    if not ok:
        ctx = page_ctx(request, "/", "HP Juridik", "HP Juridik – juridisk rådgivning.")
        ctx.update({"sent": False, "error": err, "free_ok": False, "premium_ok": False})
        return templates.TemplateResponse("pages/home.html", ctx, status_code=500)

    ctx = page_ctx(request, "/", "HP Juridik", "HP Juridik – juridisk rådgivning.")
    ctx.update({"sent": True, "error": None, "free_ok": False, "premium_ok": False})
    return templates.TemplateResponse("pages/home.html", ctx)


# ------------------------------------------------------------------------------
# Routes: Låna bil
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
        "fordon_regnr": normalize_regnr(fordon_regnr),
        "from_str": from_obj.strftime("%Y-%m-%d %H:%M"),
        "to_str": to_obj.strftime("%Y-%m-%d %H:%M"),
        "andamal": andamal,
        "newsletter_optin": bool(newsletter_optin),
    }

    pdf_bytes = build_loan_pdf(flat)

    structured = {
        "utlanare": {
            "namn": utlanare_namn,
            "pnr": utlanare_pnr,
            "adress": utlanare_adress,
            "tel": utlanare_tel,
            "epost": utlanare_epost,
        },
        "lantagare": {
            "namn": lantagare_namn,
            "pnr": lantagare_pnr,
            "adress": lantagare_adress,
            "tel": lantagare_tel,
            "epost": lantagare_epost,
        },
        "fordon": {
            "modell": fordon_modell,
            "regnr": flat["fordon_regnr"],
        },
        "period": {
            "from_str": flat["from_str"],
            "to_str": flat["to_str"],
        },
        "andamal": andamal,
        "newsletter_optin": flat["newsletter_optin"],
    }

    agreement = {
        "agreement_id": agreement_id,
        "created_utc": flat["created_utc"],
        "updated_utc": flat["created_utc"],
        "data": structured,
        "flat": flat,
        "pdf_b64": base64.b64encode(pdf_bytes).decode("utf-8"),
        "is_paid": False,
        "stripe_session_id": None,
        "delivered": False,
        "delivery_mode": None,
        "oneflow_contract_id": None,
        "oneflow_published": False,
        "oneflow_status": None,
        "signed_pdf_b64": None,
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

    ctx = page_ctx(
        request,
        "/lana-bil-till-skuldsatt/review",
        "Granska uppgifter | HP Juridik",
        "Granska och välj Gratis eller Premium.",
    )
    ctx.update({"agreement_id": agreement_id, "data": agreement["data"], "error": None})
    return templates.TemplateResponse("pages/lana_bil_review.html", ctx)


@app.post("/lana-bil-till-skuldsatt/review")
def lana_bil_review_post(
    request: Request,
    plan: str = Form(...),
    confirm_correct: Optional[str] = Form(None),
    disclaimer_accept: Optional[str] = Form(None),
):
    agreement_id = request.session.get("agreement_id")
    agreement = load_agreement(agreement_id)
    if not agreement_id or not agreement:
        return RedirectResponse(url="/lana-bil-till-skuldsatt", status_code=303)

    if not (confirm_correct and disclaimer_accept):
        ctx = page_ctx(request, "/lana-bil-till-skuldsatt/review", "Granska uppgifter | HP Juridik", "Granska.")
        ctx.update(
            {
                "agreement_id": agreement_id,
                "data": agreement["data"],
                "error": "Du måste kryssa i båda rutorna för att fortsätta.",
            }
        )
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


@app.get("/checkout-success", response_class=HTMLResponse)
def checkout_success(request: Request):
    ctx = page_ctx(request, "/checkout-success", "Tack för din betalning | HP Juridik", "Betalning mottagen.")
    return templates.TemplateResponse("pages/checkout_success.html", ctx)


@app.get("/checkout-cancel", response_class=HTMLResponse)
def checkout_cancel(request: Request):
    ctx = page_ctx(request, "/checkout-cancel", "Betalning avbruten | HP Juridik", "Betalningen avbröts.")
    return templates.TemplateResponse("pages/checkout_cancel.html", ctx)


# ------------------------------------------------------------------------------
# Stripe Webhook
# ------------------------------------------------------------------------------
@app.post("/stripe/webhook")
@app.post("/stripe/webhook/")
async def stripe_webhook(request: Request):
    print("=== STRIPE WEBHOOK HIT ===", utc_iso())

    if not STRIPE_WEBHOOK_SECRET:
        raise HTTPException(status_code=500, detail="STRIPE_WEBHOOK_SECRET saknas")

    payload = await request.body()
    sig_header = request.headers.get("stripe-signature")

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

    if event_type not in ("checkout.session.completed", "checkout.session.async_payment_succeeded"):
        return PlainTextResponse("ok", status_code=200)

    session_obj = event["data"]["object"]
    session_id = session_obj.get("id")
    metadata = session_obj.get("metadata") or {}
    agreement_id = metadata.get("agreement_id")
    payment_status = session_obj.get("payment_status")

    if payment_status != "paid":
        print("Not paid yet -> no delivery.")
        return PlainTextResponse("ok", status_code=200)

    if not agreement_id:
        safe_send_email(
            [LEAD_INBOX],
            "Stripe ALERT: saknar agreement_id",
            f"session_id={session_id}\nmetadata={metadata}",
        )
        return PlainTextResponse("ok", status_code=200)

    agreement = load_agreement(agreement_id)
    if not agreement:
        msg = f"Stripe PAID men agreement saknas.\nagreement_id={agreement_id}\nsession_id={session_id}\nmetadata={metadata}"
        print("WARNING:", msg)
        safe_send_email([LEAD_INBOX], "Stripe ALERT: agreement saknas (persistens)", msg)
        return PlainTextResponse("ok", status_code=200)

    if agreement.get("is_paid") and agreement.get("oneflow_contract_id"):
        return PlainTextResponse("ok", status_code=200)

    agreement["stripe_session_id"] = agreement.get("stripe_session_id") or session_id
    agreement["is_paid"] = True
    save_agreement(agreement)

    try:
        if ONEFLOW_ENABLED:
            deliver_premium_oneflow(agreement_id, agreement, stripe_session_id=session_id)
            print("Premium Oneflow delivered OK:", agreement_id)
        else:
            deliver_premium_pdf_fallback(agreement_id, agreement, stripe_session_id=session_id)
            print("Premium PDF delivered (no Oneflow):", agreement_id)
    except Exception as e:
        err_txt = f"Premium delivery failed agreement_id={agreement_id} session_id={session_id}: {repr(e)}"
        print(err_txt)
        safe_send_email([LEAD_INBOX], "Delivery ERROR i Stripe webhook", err_txt)
        return PlainTextResponse("delivery error", status_code=500)

    return PlainTextResponse("ok", status_code=200)


# ------------------------------------------------------------------------------
# Oneflow webhook
# ------------------------------------------------------------------------------
@app.post("/oneflow/webhook")
@app.post("/oneflow/webhook/")
async def oneflow_webhook(request: Request):
    body = await request.body()

    if not verify_oneflow_webhook(dict(request.headers)):
        return PlainTextResponse("invalid signature", status_code=400)

    try:
        payload = json.loads(body.decode("utf-8"))
    except Exception:
        return PlainTextResponse("bad json", status_code=400)

    contract_id = oneflow_extract_contract_id(payload)
    event_type = oneflow_extract_event_type(payload)

    print("Oneflow webhook:", event_type, "contract_id:", contract_id)

    if not contract_id:
        return PlainTextResponse("ok", status_code=200)

    agreement = find_agreement_by_contract_id(contract_id)
    if not agreement:
        print("Oneflow webhook: agreement not found for contract_id", contract_id)
        return PlainTextResponse("ok", status_code=200)

    agreement["oneflow_status"] = event_type or agreement.get("oneflow_status") or "webhook_received"
    save_agreement(agreement)

    should_check_signed = any(
        marker in (event_type or "")
        for marker in ["sign", "signed", "completed", "contract"]
    )

    if should_check_signed or not event_type:
        try:
            contract = oneflow_get_contract(contract_id)
            if oneflow_contract_is_signed(contract):
                finalize_signed_oneflow_contract(agreement)
                print("Oneflow signed + delivered:", agreement["agreement_id"])
        except Exception as e:
            err_txt = f"Oneflow finalize failed agreement_id={agreement['agreement_id']} contract_id={contract_id}: {repr(e)}"
            print(err_txt)
            safe_send_email([LEAD_INBOX], "Oneflow finalize ERROR", err_txt)

    return PlainTextResponse("ok", status_code=200)


# ------------------------------------------------------------------------------
# Health
# ------------------------------------------------------------------------------
@app.get("/healthz", response_class=PlainTextResponse)
def healthz():
    return "ok"
