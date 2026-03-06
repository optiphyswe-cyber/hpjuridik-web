from __future__ import annotations

import os
import io
import json
import uuid
import base64
import hmac
import hashlib
import smtplib
from email.message import EmailMessage
from datetime import datetime, timezone
from typing import Any, Dict, Optional, List

import requests
import stripe
from fastapi import FastAPI, Request, Form, HTTPException
from fastapi.responses import HTMLResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware
from starlette.responses import RedirectResponse

from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.lib import colors
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle


# ------------------------------------------------------------------------------
# App
# ------------------------------------------------------------------------------
app = FastAPI(redirect_slashes=False)
app.mount("/static", StaticFiles(directory="app/static"), name="static")
templates = Jinja2Templates(directory="app/templates")

SESSION_SECRET = os.getenv("SESSION_SECRET", "change-me-now")
app.add_middleware(SessionMiddleware, secret_key=SESSION_SECRET)


# ------------------------------------------------------------------------------
# Environment
# ------------------------------------------------------------------------------
BASE_URL = os.getenv("BASE_URL", "http://localhost:10000").rstrip("/")
SITE_URL = os.getenv("SITE_URL", "https://www.hpjuridik.se").rstrip("/")

MAIL_FROM = os.getenv("MAIL_FROM", "hp@hpjuridik.se")
CONTACT_TO = os.getenv("CONTACT_TO", "hp@hpjuridik.se")
CONTACT_FROM = os.getenv("CONTACT_FROM", MAIL_FROM)
LEAD_INBOX = os.getenv("LEAD_INBOX", "hp@hpjuridik.se")

SMTP_HOST = os.getenv("SMTP_HOST", "")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER = os.getenv("SMTP_USER", "")
SMTP_PASS = os.getenv("SMTP_PASS", "")

STRIPE_SECRET_KEY = os.getenv("STRIPE_SECRET_KEY", "")
STRIPE_WEBHOOK_SECRET = os.getenv("STRIPE_WEBHOOK_SECRET", "")
PREMIUM_PRICE_ORE = int(os.getenv("PREMIUM_PRICE_ORE", "15000"))

ONEFLOW_API_TOKEN = os.getenv("ONEFLOW_API_TOKEN", "")
ONEFLOW_BASE_URL = os.getenv("ONEFLOW_BASE_URL", "https://api.oneflow.com/v1").rstrip("/")
ONEFLOW_TEMPLATE_ID = os.getenv("ONEFLOW_TEMPLATE_ID", "")
ONEFLOW_WORKSPACE_ID = os.getenv("ONEFLOW_WORKSPACE_ID", "")
ONEFLOW_WEBHOOK_SIGN_KEY = os.getenv("ONEFLOW_WEBHOOK_SIGN_KEY", "")
ONEFLOW_USER_EMAIL = os.getenv("ONEFLOW_USER_EMAIL", "")

AGREEMENTS_DIR = os.getenv("AGREEMENTS_DIR", "/tmp/hpjuridik_agreements")

if STRIPE_SECRET_KEY:
    stripe.api_key = STRIPE_SECRET_KEY

ONEFLOW_ENABLED = bool(
    ONEFLOW_API_TOKEN
    and ONEFLOW_TEMPLATE_ID
    and ONEFLOW_WORKSPACE_ID
    and ONEFLOW_USER_EMAIL
)

COMPANY = {
    "brand": "HP Juridik",
    "signature_name": "HP",
    "phone": "076-317 12 84",
    "email": "hp@hpjuridik.se",
    "website": "hpjuridik.se",
    "address": "Karl XI gata 21, 222 20 Lund",
    "company": "Subsidiaritet i Lund AB",
    "orgnr": "559365-2018",
}


# ------------------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------------------
def utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def log(*parts: Any) -> None:
    try:
        print("[HPJ]", *parts, flush=True)
    except Exception:
        pass


def ensure_dir(path: str) -> None:
    os.makedirs(path, exist_ok=True)


def agreement_path(agreement_id: str) -> str:
    ensure_dir(AGREEMENTS_DIR)
    return os.path.join(AGREEMENTS_DIR, f"{agreement_id}.json")


def save_agreement(agreement: Dict[str, Any]) -> None:
    ensure_dir(AGREEMENTS_DIR)
    agreement["updated_at"] = utc_iso()
    with open(agreement_path(agreement["agreement_id"]), "w", encoding="utf-8") as f:
        json.dump(agreement, f, ensure_ascii=False, indent=2)


def load_agreement(agreement_id: Optional[str]) -> Optional[Dict[str, Any]]:
    if not agreement_id:
        return None
    path = agreement_path(agreement_id)
    if not os.path.exists(path):
        return None
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def find_agreement_by_contract_id(contract_id: str) -> Optional[Dict[str, Any]]:
    ensure_dir(AGREEMENTS_DIR)
    for filename in os.listdir(AGREEMENTS_DIR):
        if not filename.endswith(".json"):
            continue
        full = os.path.join(AGREEMENTS_DIR, filename)
        try:
            with open(full, "r", encoding="utf-8") as f:
                agreement = json.load(f)
            if str(agreement.get("oneflow_contract_id") or "") == str(contract_id):
                return agreement
        except Exception:
            continue
    return None


def normalize_regnr(value: str) -> str:
    return "".join((value or "").split()).upper()


def page_ctx(request: Request, path: str, title: str, description: str) -> Dict[str, Any]:
    canonical = f"{SITE_URL}{path}"
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


def safe_send_email(
    to_list: List[str],
    subject: str,
    body: str,
    pdf_bytes: Optional[bytes] = None,
    pdf_filename: str = "avtal.pdf",
    reply_to: Optional[str] = None,
    from_email: Optional[str] = None,
) -> tuple[bool, Optional[str]]:
    try:
        recipients = [x.strip() for x in to_list if x and x.strip()]
        if not recipients:
            return False, "No recipients"
        if not SMTP_HOST or not SMTP_USER or not SMTP_PASS:
            return False, "SMTP env missing"

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
# PDF
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
    story.append(Paragraph("Bilutlåningsavtal", h1))
    story.append(Paragraph(f"Avtals-ID: {flat.get('agreement_id', '')}", p))
    story.append(Paragraph(f"Skapat: {flat.get('created_at', '')}", p))
    story.append(Spacer(1, 10))

    def row(label: str, value: Any):
        return [Paragraph(f"<b>{label}</b>", p), Paragraph(str(value or ""), p)]

    data = [
        row("Utlånare namn", flat.get("utlanare_namn")),
        row("Utlånare adress", flat.get("utlanare_adress")),
        row("Utlånare e-post", flat.get("utlanare_epost")),
        row("Låntagare namn", flat.get("lantagare_namn")),
        row("Låntagare adress", flat.get("lantagare_adress")),
        row("Låntagare e-post", flat.get("lantagare_epost")),
        row("Fordon", flat.get("fordon_modell")),
        row("Registreringsnummer", flat.get("fordon_regnr")),
        row("Från", flat.get("from_str")),
        row("Till", flat.get("to_str")),
        row("Ändamål", flat.get("andamal")),
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
    story.append(
        Paragraph(
            "Detta är ett standardiserat dokument baserat på de uppgifter som användaren lämnat.",
            p,
        )
    )

    doc.build(story)
    return buf.getvalue()


# ------------------------------------------------------------------------------
# Oneflow
# ------------------------------------------------------------------------------
class OneflowError(RuntimeError):
    pass


def oneflow_headers() -> Dict[str, str]:
    if not ONEFLOW_API_TOKEN:
        raise OneflowError("ONEFLOW_API_TOKEN missing")
    if not ONEFLOW_USER_EMAIL:
        raise OneflowError("ONEFLOW_USER_EMAIL missing")

    return {
        "x-oneflow-api-token": ONEFLOW_API_TOKEN,
        "x-oneflow-user-email": ONEFLOW_USER_EMAIL,
        "Content-Type": "application/json",
    }


def oneflow_create_contract_from_template(agreement: Dict[str, Any]) -> Dict[str, Any]:
    payload = {
        "workspace_id": int(ONEFLOW_WORKSPACE_ID),
        "template_id": int(ONEFLOW_TEMPLATE_ID),
        "name": f"Bilutlåningsavtal {agreement['agreement_id']}",
    }

    log("ONEFLOW create payload:", json.dumps(payload, ensure_ascii=False))

    r = requests.post(
        f"{ONEFLOW_BASE_URL}/contracts/create",
        headers=oneflow_headers(),
        json=payload,
        timeout=30,
    )

    log("ONEFLOW create status:", r.status_code)
    log("ONEFLOW create body:", r.text)

    if r.status_code >= 300:
        raise OneflowError(f"Oneflow create failed {r.status_code}: {r.text}")

    return r.json()


def oneflow_publish_contract(contract_id: str) -> None:
    payload = {
        "subject": "Signera avtal via BankID",
        "message": "Ni har fått ett avtal för signering via Oneflow.",
    }

    log("ONEFLOW publish payload:", json.dumps(payload, ensure_ascii=False))

    r = requests.post(
        f"{ONEFLOW_BASE_URL}/contracts/{contract_id}/publish",
        headers=oneflow_headers(),
        json=payload,
        timeout=30,
    )

    log("ONEFLOW publish status:", r.status_code)
    log("ONEFLOW publish body:", r.text)

    if r.status_code >= 300:
        raise OneflowError(f"Oneflow publish failed {r.status_code}: {r.text}")


def oneflow_get_contract(contract_id: str) -> Dict[str, Any]:
    r = requests.get(
        f"{ONEFLOW_BASE_URL}/contracts/{contract_id}",
        headers=oneflow_headers(),
        timeout=30,
    )

    log("ONEFLOW get contract status:", r.status_code)
    log("ONEFLOW get contract body:", r.text[:3000])

    if r.status_code >= 300:
        raise OneflowError(f"Oneflow get contract failed {r.status_code}: {r.text}")

    return r.json()


def oneflow_get_parties(contract_id: str) -> List[Dict[str, Any]]:
    r = requests.get(
        f"{ONEFLOW_BASE_URL}/contracts/{contract_id}/parties",
        headers=oneflow_headers(),
        timeout=30,
    )

    log("ONEFLOW parties status:", r.status_code)
    log("ONEFLOW parties body:", r.text[:3000])

    if r.status_code >= 300:
        raise OneflowError(f"Oneflow parties failed {r.status_code}: {r.text}")

    data = r.json()
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        if isinstance(data.get("parties"), list):
            return data["parties"]
        if isinstance(data.get("data"), list):
            return data["data"]
    return []


def extract_external_participants(parties: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    results: List[Dict[str, Any]] = []

    for party in parties:
        if party.get("my_party") is True:
            continue

        # individual party
        participant = party.get("participant")
        if isinstance(participant, dict):
            email = str(participant.get("email") or "").strip().lower()
            pid = participant.get("id")
            if pid:
                results.append(
                    {
                        "participant_id": int(pid),
                        "email": email,
                        "name": str(participant.get("name") or ""),
                        "party_name": str(party.get("name") or ""),
                    }
                )

        # company party
        participants = party.get("participants")
        if isinstance(participants, list):
            for item in participants:
                if not isinstance(item, dict):
                    continue
                email = str(item.get("email") or "").strip().lower()
                pid = item.get("id")
                if pid:
                    results.append(
                        {
                            "participant_id": int(pid),
                            "email": email,
                            "name": str(item.get("name") or ""),
                            "party_name": str(party.get("name") or ""),
                        }
                    )

    return results


def oneflow_create_access_link(contract_id: str, participant_id: int) -> str:
    r = requests.post(
        f"{ONEFLOW_BASE_URL}/contracts/{contract_id}/participants/{participant_id}/access_link",
        headers=oneflow_headers(),
        json={},
        timeout=30,
    )

    log("ONEFLOW access_link status:", r.status_code)
    log("ONEFLOW access_link body:", r.text[:3000])

    if r.status_code >= 300:
        raise OneflowError(f"Oneflow access_link failed {r.status_code}: {r.text}")

    data = r.json()
    candidates = [
        data.get("url"),
        data.get("href"),
        data.get("link"),
        data.get("access_link"),
        (data.get("_links") or {}).get("self"),
        ((data.get("_links") or {}).get("access_link") or {}).get("href"),
    ]
    for item in candidates:
        if isinstance(item, str) and item.strip():
            return item.strip()

    raise OneflowError(f"Could not extract access link from Oneflow response: {data}")


def oneflow_download_signed_pdf(contract_id: str) -> bytes:
    r = requests.get(
        f"{ONEFLOW_BASE_URL}/contracts/{contract_id}/files",
        headers=oneflow_headers(),
        timeout=30,
    )

    log("ONEFLOW files status:", r.status_code)
    log("ONEFLOW files body:", r.text[:3000])

    if r.status_code >= 300:
        raise OneflowError(f"Oneflow files failed {r.status_code}: {r.text}")

    files = r.json()
    if isinstance(files, dict) and "files" in files:
        files = files["files"]

    if not isinstance(files, list) or not files:
        raise OneflowError("No files found")

    pdf_file = None
    for item in files:
        name = str(item.get("name") or "").lower()
        content_type = str(item.get("content_type") or "").lower()
        if name.endswith(".pdf") or content_type == "application/pdf":
            pdf_file = item
            break

    if not pdf_file:
        pdf_file = files[0]

    file_id = str(pdf_file.get("id") or pdf_file.get("file_id") or "")
    if not file_id:
        raise OneflowError("Missing file id")

    r2 = requests.get(
        f"{ONEFLOW_BASE_URL}/contracts/{contract_id}/files/{file_id}/download",
        headers=oneflow_headers(),
        timeout=60,
    )

    log("ONEFLOW download status:", r2.status_code)

    if r2.status_code >= 300:
        raise OneflowError(f"Oneflow download failed {r2.status_code}: {r2.text}")

    return r2.content


def verify_oneflow_webhook(headers: Dict[str, str]) -> bool:
    if not ONEFLOW_WEBHOOK_SIGN_KEY:
        return True

    callback_id = headers.get("x-oneflow-callback-id") or headers.get("X-Oneflow-Callback-Id") or ""
    signature = headers.get("x-oneflow-signature") or headers.get("X-Oneflow-Signature") or ""

    if not callback_id or not signature:
        log("ONEFLOW webhook missing signature headers -> allow during integration")
        return True

    expected = hashlib.sha1((callback_id + ONEFLOW_WEBHOOK_SIGN_KEY).encode("utf-8")).hexdigest()
    return hmac.compare_digest(expected, signature)


def extract_contract_id(payload: Dict[str, Any]) -> str:
    possible = [
        payload.get("contract_id"),
        payload.get("id"),
        (payload.get("contract") or {}).get("id"),
        (payload.get("data") or {}).get("contract_id"),
        ((payload.get("data") or {}).get("contract") or {}).get("id"),
    ]
    for item in possible:
        if item:
            return str(item)
    return ""


def contract_is_signed(contract: Dict[str, Any]) -> bool:
    raw = json.dumps(contract, ensure_ascii=False).lower()
    markers = [
        '"state":"signed"',
        '"status":"signed"',
        '"lifecycle_state":"signed"',
        '"fully_signed":true',
        '"is_signed":true',
    ]
    return any(marker in raw for marker in markers)


# ------------------------------------------------------------------------------
# Delivery
# ------------------------------------------------------------------------------
def deliver_free(agreement: Dict[str, Any]) -> None:
    if agreement.get("delivered"):
        return

    pdf_bytes = base64.b64decode(agreement["pdf_b64"])
    flat = agreement["flat"]

    ok, err = safe_send_email(
        [flat.get("utlanare_epost"), flat.get("lantagare_epost")],
        "Bilutlåningsavtal – PDF",
        "Här kommer ert bilutlåningsavtal som PDF.\n\n/HP Juridik",
        pdf_bytes=pdf_bytes,
        pdf_filename=f"bilutlaningsavtal-{agreement['agreement_id']}.pdf",
    )
    if not ok:
        raise RuntimeError(err)

    agreement["delivered"] = True
    agreement["delivery_mode"] = "free_pdf"
    save_agreement(agreement)
    log("FREE delivered:", agreement["agreement_id"])


def deliver_premium_fallback(agreement: Dict[str, Any], stripe_session_id: str) -> None:
    pdf_bytes = base64.b64decode(agreement["pdf_b64"])
    flat = agreement["flat"]

    ok, err = safe_send_email(
        [flat.get("utlanare_epost"), flat.get("lantagare_epost")],
        "Bilutlåningsavtal – PDF",
        "Oneflow är inte aktivt. Här kommer PDF-versionen.\n\n/HP Juridik",
        pdf_bytes=pdf_bytes,
        pdf_filename=f"bilutlaningsavtal-{agreement['agreement_id']}.pdf",
    )
    if not ok:
        raise RuntimeError(err)

    agreement["is_paid"] = True
    agreement["stripe_session_id"] = stripe_session_id
    agreement["delivered"] = True
    agreement["delivery_mode"] = "premium_pdf_fallback"
    agreement["oneflow_status"] = "fallback_pdf"
    save_agreement(agreement)
    log("PREMIUM fallback delivered:", agreement["agreement_id"])


def send_oneflow_access_link_emails(agreement: Dict[str, Any], contract_id: str) -> None:
    flat = agreement["flat"]
    target_emails = [
        str(flat.get("utlanare_epost") or "").strip().lower(),
        str(flat.get("lantagare_epost") or "").strip().lower(),
    ]

    parties = oneflow_get_parties(contract_id)
    external_participants = extract_external_participants(parties)

    links_by_email: Dict[str, str] = {}
    for participant in external_participants:
        email = participant["email"]
        if not email or email in links_by_email:
            continue
        try:
            link = oneflow_create_access_link(contract_id, participant["participant_id"])
            links_by_email[email] = link
        except Exception as e:
            log("ONEFLOW access_link failed for participant:", participant, repr(e))

    any_link_sent = False
    for email in target_emails:
        if not email:
            continue
        link = links_by_email.get(email)
        if not link:
            continue

        ok, err = safe_send_email(
            [email],
            "Bilutlåningsavtal – signera via Oneflow",
            (
                "Tack för betalningen.\n\n"
                "Här är er personliga signeringslänk till avtalet i Oneflow:\n\n"
                f"{link}\n\n"
                f"Avtals-ID: {agreement['agreement_id']}\n\n"
                "/HP Juridik"
            ),
        )
        if ok:
            any_link_sent = True
        else:
            log("ONEFLOW access link mail failed:", email, err)

    if not any_link_sent:
        ok, err = safe_send_email(
            [flat.get("utlanare_epost"), flat.get("lantagare_epost")],
            "Bilutlåningsavtal – signering via Oneflow",
            (
                "Tack för betalningen.\n\n"
                "Avtalet har nu skapats i Oneflow och skickats för digital signering.\n\n"
                "Oneflow skickar eller tillhandahåller signeringslänk separat.\n\n"
                f"Avtals-ID: {agreement['agreement_id']}\n\n"
                "/HP Juridik"
            ),
        )
        if not ok:
            log("ONEFLOW fallback info mail failed:", err)


def deliver_premium_oneflow(agreement: Dict[str, Any], stripe_session_id: str) -> None:
    existing_contract_id = str(agreement.get("oneflow_contract_id") or "").strip()
    if existing_contract_id:
        log("ONEFLOW already exists for agreement:", agreement["agreement_id"], existing_contract_id)

        agreement["is_paid"] = True
        agreement["stripe_session_id"] = stripe_session_id
        if not agreement.get("oneflow_status"):
            agreement["oneflow_status"] = "published"
        if not agreement.get("delivery_mode"):
            agreement["delivery_mode"] = "oneflow"
        save_agreement(agreement)
        return

    log("ONEFLOW start agreement:", agreement["agreement_id"])

    contract = oneflow_create_contract_from_template(agreement)

    contract_id = str(
        contract.get("id")
        or (contract.get("contract") or {}).get("id")
        or ""
    )

    if not contract_id:
        raise OneflowError(f"Missing contract id from Oneflow: {contract}")

    log("ONEFLOW contract_id:", contract_id)

    oneflow_publish_contract(contract_id)

    agreement["is_paid"] = True
    agreement["stripe_session_id"] = stripe_session_id
    agreement["oneflow_contract_id"] = contract_id
    agreement["oneflow_published"] = True
    agreement["oneflow_status"] = "published"
    agreement["delivery_mode"] = "oneflow"
    agreement["oneflow_error"] = None
    save_agreement(agreement)

    try:
        send_oneflow_access_link_emails(agreement, contract_id)
    except Exception as e:
        log("ONEFLOW access-link flow failed:", repr(e))
        flat = agreement["flat"]
        ok, err = safe_send_email(
            [flat.get("utlanare_epost"), flat.get("lantagare_epost")],
            "Bilutlåningsavtal – signering via Oneflow",
            (
                "Tack för betalningen.\n\n"
                "Avtalet har nu skapats i Oneflow och skickats för digital signering.\n\n"
                "Om signeringslänken inte kommit fram ännu, kontrollera skräppost eller invänta Oneflow-mejl.\n\n"
                f"Avtals-ID: {agreement['agreement_id']}\n\n"
                "/HP Juridik"
            ),
        )
        if not ok:
            log("ONEFLOW generic info mail failed:", err)

    log("ONEFLOW delivered OK:", agreement["agreement_id"], "contract_id:", contract_id)


def finalize_signed_contract(agreement: Dict[str, Any]) -> None:
    if agreement.get("signed_pdf_b64") and agreement.get("delivered"):
        return

    contract_id = str(agreement.get("oneflow_contract_id") or "")
    if not contract_id:
        raise OneflowError("Agreement missing oneflow_contract_id")

    pdf_bytes = oneflow_download_signed_pdf(contract_id)
    agreement["signed_pdf_b64"] = base64.b64encode(pdf_bytes).decode("utf-8")
    agreement["oneflow_status"] = "signed"
    agreement["delivered"] = True
    save_agreement(agreement)

    flat = agreement["flat"]
    ok, err = safe_send_email(
        [flat.get("utlanare_epost"), flat.get("lantagare_epost")],
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

    log("ONEFLOW signed PDF sent:", agreement["agreement_id"])


# ------------------------------------------------------------------------------
# Routes
# ------------------------------------------------------------------------------
@app.get("/", response_class=HTMLResponse)
def home(request: Request):
    ctx = page_ctx(request, "/", "HP Juridik", "HP Juridik")
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
    ctx = page_ctx(request, "/kontakta-oss", "Kontakt | HP Juridik", "Kontakta HP Juridik")
    ctx.update({"sent": False, "error": None})
    return templates.TemplateResponse("pages/contact.html", ctx)


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
        return RedirectResponse(url="/?sent=1", status_code=303)

    body = (
        "NY KONTAKTFÖRFRÅGAN\n\n"
        f"Namn: {namn}\n"
        f"E-post: {epost}\n"
        f"Telefon: {telefon}\n\n"
        f"Meddelande:\n{meddelande}\n\n"
        f"Tid: {utc_iso()}\n"
    )

    ok, err = safe_send_email(
        [CONTACT_TO],
        "HP Juridik | Ny kontaktförfrågan",
        body,
        reply_to=epost or None,
        from_email=CONTACT_FROM,
    )

    if not ok:
        ctx = page_ctx(request, "/kontakta-oss", "Kontakt | HP Juridik", "Kontakta HP Juridik")
        ctx.update({"sent": False, "error": err})
        return templates.TemplateResponse("pages/contact.html", ctx, status_code=500)

    return RedirectResponse(url="/?sent=1", status_code=303)


@app.post("/contact", response_class=HTMLResponse)
def contact_alias(
    request: Request,
    website: str = Form(""),
    namn: str = Form(""),
    epost: str = Form(""),
    telefon: str = Form(""),
    meddelande: str = Form(""),
):
    return contact_submit(request, website, namn, epost, telefon, meddelande)


@app.get("/lana-bil-till-skuldsatt", response_class=HTMLResponse)
def lana_bil_form(request: Request):
    ctx = page_ctx(
        request,
        "/lana-bil-till-skuldsatt",
        "Låna bil till skuldsatt | HP Juridik",
        "Skapa bilutlåningsavtal",
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
        ctx = page_ctx(
            request,
            "/lana-bil-till-skuldsatt",
            "Låna bil till skuldsatt | HP Juridik",
            "Skapa bilutlåningsavtal",
        )
        ctx.update({"error": "Du måste godkänna friskrivningen."})
        return templates.TemplateResponse("pages/lana_bil.html", ctx, status_code=400)

    try:
        from_obj = datetime.fromisoformat(from_dt)
        to_obj = datetime.fromisoformat(to_dt)
    except ValueError:
        ctx = page_ctx(
            request,
            "/lana-bil-till-skuldsatt",
            "Låna bil till skuldsatt | HP Juridik",
            "Skapa bilutlåningsavtal",
        )
        ctx.update({"error": "Ogiltigt datumformat."})
        return templates.TemplateResponse("pages/lana_bil.html", ctx, status_code=400)

    if to_obj <= from_obj:
        ctx = page_ctx(
            request,
            "/lana-bil-till-skuldsatt",
            "Låna bil till skuldsatt | HP Juridik",
            "Skapa bilutlåningsavtal",
        )
        ctx.update({"error": "Till-datum måste vara efter från-datum."})
        return templates.TemplateResponse("pages/lana_bil.html", ctx, status_code=400)

    agreement_id = str(uuid.uuid4())

    flat = {
        "agreement_id": agreement_id,
        "created_at": utc_iso(),
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

    agreement = {
        "agreement_id": agreement_id,
        "created_at": flat["created_at"],
        "updated_at": flat["created_at"],
        "flat": flat,
        "data": {
            "utlanare": {
                "namn": utlanare_namn,
                "adress": utlanare_adress,
                "epost": utlanare_epost,
                "tel": utlanare_tel,
                "pnr": utlanare_pnr,
            },
            "lantagare": {
                "namn": lantagare_namn,
                "adress": lantagare_adress,
                "epost": lantagare_epost,
                "tel": lantagare_tel,
                "pnr": lantagare_pnr,
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
            "newsletter_optin": bool(newsletter_optin),
        },
        "pdf_b64": base64.b64encode(pdf_bytes).decode("utf-8"),
        "is_paid": False,
        "stripe_session_id": None,
        "delivered": False,
        "delivery_mode": None,
        "oneflow_contract_id": None,
        "oneflow_published": False,
        "oneflow_status": None,
        "oneflow_error": None,
        "signed_pdf_b64": None,
    }

    save_agreement(agreement)
    request.session["agreement_id"] = agreement_id
    log("AGREEMENT created:", agreement_id)
    return RedirectResponse(url="/lana-bil-till-skuldsatt/review", status_code=303)


@app.get("/lana-bil-till-skuldsatt/review", response_class=HTMLResponse)
def lana_bil_review_get(request: Request):
    agreement_id = request.session.get("agreement_id")
    agreement = load_agreement(agreement_id)
    if not agreement:
        return RedirectResponse(url="/lana-bil-till-skuldsatt", status_code=303)

    ctx = page_ctx(request, "/lana-bil-till-skuldsatt/review", "Granska uppgifter | HP Juridik", "Granska avtal")
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

    if not agreement:
        return RedirectResponse(url="/lana-bil-till-skuldsatt", status_code=303)

    if not confirm_correct or not disclaimer_accept:
        ctx = page_ctx(request, "/lana-bil-till-skuldsatt/review", "Granska uppgifter | HP Juridik", "Granska avtal")
        ctx.update({"agreement_id": agreement_id, "data": agreement["data"], "error": "Du måste kryssa i båda rutorna."})
        return templates.TemplateResponse("pages/lana_bil_review.html", ctx, status_code=400)

    if plan == "free":
        try:
            deliver_free(agreement)
            return RedirectResponse(url="/?free=1", status_code=303)
        except Exception as e:
            ctx = page_ctx(request, "/lana-bil-till-skuldsatt/review", "Granska uppgifter | HP Juridik", "Granska avtal")
            ctx.update({"agreement_id": agreement_id, "data": agreement["data"], "error": f"Gratisleverans misslyckades: {e}"})
            return templates.TemplateResponse("pages/lana_bil_review.html", ctx, status_code=500)

    if plan == "premium":
        if not STRIPE_SECRET_KEY:
            raise HTTPException(status_code=500, detail="STRIPE_SECRET_KEY missing")

        session = stripe.checkout.Session.create(
            mode="payment",
            payment_method_types=["card"],
            line_items=[
                {
                    "price_data": {
                        "currency": "sek",
                        "product_data": {"name": "Premium – Oneflow signering"},
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
        log("STRIPE checkout created:", session.id, "agreement:", agreement_id)
        return RedirectResponse(url=session.url, status_code=303)

    raise HTTPException(status_code=400, detail="Invalid plan")


@app.get("/checkout-success", response_class=HTMLResponse)
def checkout_success(request: Request):
    ctx = page_ctx(request, "/checkout-success", "Tack för din betalning | HP Juridik", "Betalning mottagen")
    return templates.TemplateResponse("pages/checkout_success.html", ctx)


@app.get("/checkout-cancel", response_class=HTMLResponse)
def checkout_cancel(request: Request):
    ctx = page_ctx(request, "/checkout-cancel", "Betalning avbruten | HP Juridik", "Betalningen avbröts")
    return templates.TemplateResponse("pages/checkout_cancel.html", ctx)


# ------------------------------------------------------------------------------
# Stripe webhook
# ------------------------------------------------------------------------------
@app.post("/stripe/webhook")
@app.post("/stripe/webhook/")
async def stripe_webhook(request: Request):
    log("=== STRIPE WEBHOOK HIT ===")

    if not STRIPE_WEBHOOK_SECRET:
        log("ERROR: STRIPE_WEBHOOK_SECRET missing")
        return PlainTextResponse("missing webhook secret", status_code=500)

    payload = await request.body()
    sig_header = request.headers.get("stripe-signature")

    if not sig_header:
        log("ERROR: missing stripe signature")
        return PlainTextResponse("missing stripe signature", status_code=400)

    try:
        event = stripe.Webhook.construct_event(payload, sig_header, STRIPE_WEBHOOK_SECRET)
    except Exception as e:
        log("ERROR: invalid stripe signature:", repr(e))
        return PlainTextResponse(f"invalid signature: {e}", status_code=400)

    event_type = event.get("type")
    log("Stripe event_type:", event_type)

    if event_type not in ("checkout.session.completed", "checkout.session.async_payment_succeeded"):
        log("INFO: ignoring event type")
        return PlainTextResponse("ok", status_code=200)

    session_obj = event["data"]["object"]
    session_id = session_obj.get("id")
    payment_status = session_obj.get("payment_status")
    metadata = session_obj.get("metadata") or {}
    agreement_id = metadata.get("agreement_id")

    log("session_id:", session_id)
    log("payment_status:", payment_status)
    log("agreement_id:", agreement_id)

    if payment_status != "paid":
        log("INFO: payment not paid yet")
        return PlainTextResponse("ok", status_code=200)

    if not agreement_id:
        log("ERROR: agreement_id missing in metadata")
        safe_send_email([LEAD_INBOX], "Stripe error", f"Missing agreement_id for session {session_id}")
        return PlainTextResponse("ok", status_code=200)

    agreement = load_agreement(agreement_id)
    if not agreement:
        log("ERROR: agreement not found:", agreement_id)
        safe_send_email([LEAD_INBOX], "Stripe error", f"Agreement not found: {agreement_id}")
        return PlainTextResponse("ok", status_code=200)

    try:
        if ONEFLOW_ENABLED:
            log("INFO: ONEFLOW enabled -> start premium delivery")
            deliver_premium_oneflow(agreement, session_id)
            log("INFO: deliver_premium_oneflow OK")
        else:
            log("INFO: ONEFLOW disabled -> fallback PDF")
            deliver_premium_fallback(agreement, session_id)
            log("INFO: deliver_premium_fallback OK")
    except Exception as e:
        agreement["is_paid"] = True
        agreement["stripe_session_id"] = session_id
        agreement["oneflow_status"] = "error"
        agreement["oneflow_error"] = repr(e)
        save_agreement(agreement)

        log("ERROR: premium delivery failed:", repr(e))
        safe_send_email(
            [LEAD_INBOX],
            "Stripe premium delivery failed",
            f"agreement_id={agreement_id}\nsession_id={session_id}\nerror={repr(e)}",
        )
        return PlainTextResponse(f"delivery error: {e}", status_code=500)

    return PlainTextResponse("ok", status_code=200)


# ------------------------------------------------------------------------------
# Oneflow webhook
# ------------------------------------------------------------------------------
@app.post("/oneflow/webhook")
@app.post("/oneflow/webhook/")
async def oneflow_webhook(request: Request):
    log("=== ONEFLOW WEBHOOK HIT ===")

    if not verify_oneflow_webhook(dict(request.headers)):
        log("ERROR: invalid Oneflow signature")
        return PlainTextResponse("invalid signature", status_code=400)

    body = await request.body()
    try:
        payload = json.loads(body.decode("utf-8"))
    except Exception:
        log("ERROR: bad Oneflow json")
        return PlainTextResponse("bad json", status_code=400)

    log("ONEFLOW webhook payload:", json.dumps(payload, ensure_ascii=False)[:4000])

    contract_id = extract_contract_id(payload)
    log("ONEFLOW webhook contract_id:", contract_id)

    if not contract_id:
        return PlainTextResponse("ok", status_code=200)

    agreement = find_agreement_by_contract_id(contract_id)
    if not agreement:
        log("ONEFLOW agreement not found for contract:", contract_id)
        return PlainTextResponse("ok", status_code=200)

    try:
        contract = oneflow_get_contract(contract_id)
        if contract_is_signed(contract):
            finalize_signed_contract(agreement)
        else:
            agreement["oneflow_status"] = "webhook_received"
            save_agreement(agreement)
            log("ONEFLOW webhook received but contract not signed yet")
    except Exception as e:
        agreement["oneflow_status"] = "error"
        agreement["oneflow_error"] = repr(e)
        save_agreement(agreement)

        log("ONEFLOW finalize failed:", repr(e))
        safe_send_email(
            [LEAD_INBOX],
            "Oneflow finalize failed",
            f"agreement_id={agreement['agreement_id']}\ncontract_id={contract_id}\nerror={repr(e)}",
        )

    return PlainTextResponse("ok", status_code=200)


# ------------------------------------------------------------------------------
# Health
# ------------------------------------------------------------------------------
@app.get("/healthz", response_class=PlainTextResponse)
def healthz():
    return "ok"
