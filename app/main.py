import os
import io
import json
import uuid
import hashlib
import datetime as dt
from typing import Optional, Dict, Any, Tuple

import stripe
import httpx
from fastapi import FastAPI, Request, Form, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from sqlalchemy import (
    create_engine, Column, String, DateTime, Boolean, Text
)
from sqlalchemy.orm import declarative_base, sessionmaker
from sqlalchemy.dialects.postgresql import JSONB

# ----------------------------
# Config / env
# ----------------------------
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "https://www.hpjuridik.se").rstrip("/")

POSTMARK_SERVER_TOKEN = os.getenv("POSTMARK_SERVER_TOKEN", "")
MAIL_FROM = os.getenv("MAIL_FROM", "lanabil@hpjuridik.se")
LEAD_INBOX = os.getenv("LEAD_INBOX", "lanabil@hpjuridik.se")

STRIPE_SECRET_KEY = os.getenv("STRIPE_SECRET_KEY", "")
STRIPE_WEBHOOK_SECRET = os.getenv("STRIPE_WEBHOOK_SECRET", "")
PREMIUM_PRICE_ORE = int(os.getenv("PREMIUM_PRICE_ORE", "15000"))  # 150 kr default

# Oneflow (BankID via Oneflow)
ONEFLOW_API_TOKEN = os.getenv("ONEFLOW_API_TOKEN", "")
ONEFLOW_USER_EMAIL = os.getenv("ONEFLOW_USER_EMAIL", "")
ONEFLOW_WORKSPACE_ID = os.getenv("ONEFLOW_WORKSPACE_ID", "")
ONEFLOW_TEMPLATE_ID = os.getenv("ONEFLOW_TEMPLATE_ID", "")
ONEFLOW_SIGN_KEY = os.getenv("ONEFLOW_SIGN_KEY", "")

# DB
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
if not DATABASE_URL:
    # fallback local sqlite (ok för dev)
    DATABASE_URL = "sqlite:///./app.db"

engine = create_engine(
    DATABASE_URL,
    connect_args={"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {},
    pool_pre_ping=True,
)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)
Base = declarative_base()

# ----------------------------
# Models
# ----------------------------
class Agreement(Base):
    __tablename__ = "agreements"

    id = Column(String, primary_key=True)  # uuid
    created_at = Column(DateTime, default=dt.datetime.utcnow, nullable=False)
    status = Column(String, default="draft", nullable=False)

    form_payload = Column(JSONB if "postgresql" in DATABASE_URL else Text, nullable=False)

    ip = Column(String, nullable=True)
    user_agent = Column(String, nullable=True)

    disclaimer_accepted_at = Column(DateTime, nullable=True)
    confirm_correct_at = Column(DateTime, nullable=True)
    newsletter_optin = Column(Boolean, default=False)

    stripe_session_id = Column(String, nullable=True)

    # Oneflow
    oneflow_contract_id = Column(String, nullable=True)
    oneflow_participant_1_id = Column(String, nullable=True)
    oneflow_participant_2_id = Column(String, nullable=True)

    signed_pdf_path = Column(String, nullable=True)


Base.metadata.create_all(bind=engine)

# ----------------------------
# App / templates / static
# ----------------------------
app = FastAPI()
app.mount("/static", StaticFiles(directory="app/static"), name="static")
templates = Jinja2Templates(directory="app/templates")

# Stripe init
if STRIPE_SECRET_KEY:
    stripe.api_key = STRIPE_SECRET_KEY


# ----------------------------
# Helpers
# ----------------------------
def _now_utc() -> dt.datetime:
    return dt.datetime.utcnow()

def _json_load(payload: Any) -> Dict[str, Any]:
    if isinstance(payload, dict):
        return payload
    if payload is None:
        return {}
    return json.loads(payload)

def _json_dump(payload: Dict[str, Any]) -> Any:
    if "postgresql" in DATABASE_URL:
        return payload
    return json.dumps(payload, ensure_ascii=False)

def normalize_regnr(s: str) -> str:
    return "".join((s or "").upper().split())

def parse_dt_local(value: str) -> dt.datetime:
    # input datetime-local: "YYYY-MM-DDTHH:MM"
    return dt.datetime.fromisoformat(value)

async def postmark_send_email(
    to_email: str,
    subject: str,
    text_body: str,
    html_body: Optional[str] = None,
    attachments: Optional[list] = None,
):
    """
    attachments: [{"Name": "...pdf", "Content": base64, "ContentType": "application/pdf"}]
    """
    if not POSTMARK_SERVER_TOKEN:
        raise RuntimeError("POSTMARK_SERVER_TOKEN missing")

    url = "https://api.postmarkapp.com/email"
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "X-Postmark-Server-Token": POSTMARK_SERVER_TOKEN,
    }
    data = {
        "From": MAIL_FROM,
        "To": to_email,
        "Subject": subject,
        "TextBody": text_body,
    }
    if html_body:
        data["HtmlBody"] = html_body
    if attachments:
        data["Attachments"] = attachments

    async with httpx.AsyncClient(timeout=20) as client:
        r = await client.post(url, headers=headers, json=data)
        if r.status_code >= 300:
            raise RuntimeError(f"Postmark error {r.status_code}: {r.text}")

def agreement_to_summary(payload: Dict[str, Any]) -> str:
    return (
        f"Utlånare: {payload.get('utlanare_namn')} ({payload.get('utlanare_epost')})\n"
        f"Låntagare: {payload.get('lantagare_namn')} ({payload.get('lantagare_epost')})\n"
        f"Bil: {payload.get('bil_marke_modell')} / {payload.get('bil_regnr')}\n"
        f"Period: {payload.get('from_dt')} -> {payload.get('to_dt')}\n"
        f"Ändamål: {payload.get('andamal')}\n"
    )

def create_pdf_bytes(payload: Dict[str, Any]) -> bytes:
    # Din PDF-generator kan vara mer avancerad; här är en enkel placeholder.
    # Om du redan har en reportlab-generator i projektet: använd den här istället.
    from reportlab.lib.pagesizes import A4
    from reportlab.pdfgen import canvas

    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    w, h = A4
    y = h - 50

    c.setFont("Helvetica-Bold", 14)
    c.drawString(50, y, "Låneavtal – Låna bil till skuldsatt")
    y -= 30

    c.setFont("Helvetica", 10)
    lines = agreement_to_summary(payload).split("\n")
    for line in lines:
        c.drawString(50, y, line[:110])
        y -= 14

    c.showPage()
    c.save()
    return buf.getvalue()

async def oneflow_request(method: str, path: str, json_body: Optional[dict] = None, files=None) -> dict:
    if not (ONEFLOW_API_TOKEN and ONEFLOW_USER_EMAIL):
        raise RuntimeError("ONEFLOW_API_TOKEN / ONEFLOW_USER_EMAIL missing")

    url = f"https://api.oneflow.com{path}"
    headers = {
        "x-oneflow-api-token": ONEFLOW_API_TOKEN,
        "x-oneflow-user-email": ONEFLOW_USER_EMAIL,
    }

    async with httpx.AsyncClient(timeout=30) as client:
        if files is not None:
            # multipart upload
            r = await client.request(method, url, headers=headers, files=files)
        else:
            r = await client.request(method, url, headers={**headers, "content-type": "application/json"}, json=json_body)

        if r.status_code >= 300:
            raise RuntimeError(f"Oneflow error {r.status_code}: {r.text}")
        return r.json()

async def oneflow_create_contract_and_send(payload: Dict[str, Any], agreement_id: str) -> Tuple[str, str, str, str, str]:
    """
    Returns: (contract_id, p1_id, p2_id, p1_link, p2_link)
    """
    if not (ONEFLOW_WORKSPACE_ID and ONEFLOW_TEMPLATE_ID):
        raise RuntimeError("ONEFLOW_WORKSPACE_ID / ONEFLOW_TEMPLATE_ID missing")

    # 1) Create contract from template
    contract = await oneflow_request(
        "POST",
        "/v1/contracts/create",
        {
            "workspace_id": int(ONEFLOW_WORKSPACE_ID),
            "template_id": int(ONEFLOW_TEMPLATE_ID),
        },
    )
    contract_id = str(contract.get("id"))

    # 2) Upload our PDF as expanded_pdf (template must support PDF section)
    pdf_bytes = create_pdf_bytes(payload)
    files = {
        "type": (None, "expanded_pdf"),
        "file": (f"lanabil-{agreement_id}.pdf", pdf_bytes, "application/pdf"),
    }
    await oneflow_request("POST", f"/v1/contracts/{contract_id}/files", files=files)

    # 3) Get parties (pick first two)
    parties = await oneflow_request("GET", f"/v1/contracts/{contract_id}/parties")
    if not isinstance(parties, list) or len(parties) < 2:
        raise RuntimeError("Oneflow template must contain at least 2 parties (or you need to adjust logic).")

    party1_id = str(parties[0]["id"])
    party2_id = str(parties[1]["id"])

    # 4) Create participants (signatories)
    p1 = await oneflow_request(
        "POST",
        f"/v1/contracts/{contract_id}/parties/{party1_id}/participants",
        {
            "name": payload.get("utlanare_namn"),
            "email": payload.get("utlanare_epost"),
            "role": "signatory",
        },
    )
    p2 = await oneflow_request(
        "POST",
        f"/v1/contracts/{contract_id}/parties/{party2_id}/participants",
        {
            "name": payload.get("lantagare_namn"),
            "email": payload.get("lantagare_epost"),
            "role": "signatory",
        },
    )
    p1_id = str(p1.get("id"))
    p2_id = str(p2.get("id"))

    # 5) Publish contract (send for signing inside Oneflow)
    await oneflow_request("POST", f"/v1/contracts/{contract_id}/publish", {})

    # 6) Create access links for both participants
    link1 = await oneflow_request("POST", f"/v1/contracts/{contract_id}/participants/{p1_id}/access_link", {})
    link2 = await oneflow_request("POST", f"/v1/contracts/{contract_id}/participants/{p2_id}/access_link", {})
    p1_link = link1.get("url") or link1.get("href") or ""
    p2_link = link2.get("url") or link2.get("href") or ""

    return contract_id, p1_id, p2_id, p1_link, p2_link

def oneflow_verify_signature(body: Dict[str, Any]) -> bool:
    """
    Oneflow docs: signature = sha1(callback_id + sign_key)
    """
    if not ONEFLOW_SIGN_KEY:
        # allow if not set (dev), but strongly recommended to set in prod
        return True

    callback_id = body.get("callback_id", "")
    signature = body.get("signature", "")
    if not callback_id or not signature:
        return False

    expected = hashlib.sha1((callback_id + ONEFLOW_SIGN_KEY).encode("utf-8")).hexdigest()
    return expected == signature

async def oneflow_download_signed_pdf(contract_id: str) -> bytes:
    """
    Uses get-contract-files to find a PDF/contract file and downloads it.
    """
    files_list = await oneflow_request("GET", f"/v1/contracts/{contract_id}/files/")
    # Try to find "contract" or "pdf" type
    target = None
    for f in files_list:
        if f.get("type") in ("contract", "pdf"):
            target = f
            break
    if not target:
        # fallback: first file with a download link
        target = files_list[0] if files_list else None
    if not target:
        raise RuntimeError("No files available on Oneflow contract.")

    download_url = target.get("file") or target.get("url") or target.get("href")
    if not download_url:
        raise RuntimeError("No download URL in Oneflow file object.")

    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.get(download_url)
        if r.status_code >= 300:
            raise RuntimeError(f"Failed to download signed PDF: {r.status_code}")
        return r.content


# ----------------------------
# Public pages (keep your site working)
# ----------------------------
@app.get("/", response_class=HTMLResponse)
async def home(request: Request):
    return templates.TemplateResponse("pages/home.html", {"request": request})

@app.get("/contact", response_class=HTMLResponse)
async def contact_get(request: Request):
    return templates.TemplateResponse("pages/contact.html", {"request": request, "sent": False})

@app.post("/contact", response_class=HTMLResponse)
async def contact_post(
    request: Request,
    name: str = Form(...),
    email: str = Form(...),
    message: str = Form(...),
):
    text = f"Kontaktformulär\n\nNamn: {name}\nE-post: {email}\n\nMeddelande:\n{message}\n"
    await postmark_send_email(
        to_email=LEAD_INBOX,
        subject="Kontaktformulär – hpjuridik.se",
        text_body=text,
    )
    return templates.TemplateResponse("pages/contact.html", {"request": request, "sent": True})
# Svensk URL så /kontakta-oss fungerar
@app.get("/kontakta-oss", response_class=HTMLResponse)
def contact_sv(request: Request):
    return templates.TemplateResponse(
        "pages/contact.html",
        page_ctx(request, "/kontakta-oss", "Kontakt | HP Juridik", "Kontakta HP Juridik"),
    )


@app.post("/kontakta-oss", response_class=HTMLResponse)
async def contact_sv_submit(
    request: Request,
    namn: str = Form(...),
    epost: str = Form(...),
    telefon: str = Form(""),
    meddelande: str = Form(...),
):
    ts = datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M")
    ip = request.client.host if request.client else "unknown"
    ua = request.headers.get("user-agent", "unknown")

    subject = f"HP Juridik | Ny kontaktförfrågan från {namn}"

    body = (
        f"NY KONTAKTFÖRFRÅGAN (HPJURIDIK.SE)\n"
        f"----------------------------------\n\n"
        f"Namn: {namn}\n"
        f"E-post: {epost}\n"
        f"Telefon: {telefon or 'Ej angivet'}\n\n"
        f"MEDDELANDE:\n{meddelande}\n\n"
        f"----------------------------------\n"
        f"Tid: {ts}\n"
        f"IP: {ip}\n"
        f"User-Agent: {ua}\n"
    )

    await postmark_send_email(
        to_email=LEAD_INBOX,
        subject=subject,
        text_body=body,
    )

    return templates.TemplateResponse(
        "pages/contact.html",
        {**page_ctx(request, "/kontakta-oss", "Kontakt | HP Juridik", "Kontakta HP Juridik"), "sent": True},
    )
# ----------------------------
# Låna bil – form/review/free/paid
# ----------------------------
@app.get("/lana-bil-till-skuldsatt", response_class=HTMLResponse)
async def lana_bil_form(request: Request):
    return templates.TemplateResponse("pages/lana_bil.html", {"request": request})

@app.post("/lana-bil-till-skuldsatt/review", response_class=HTMLResponse)
async def lana_bil_review(
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

    bil_marke_modell: str = Form(...),
    bil_regnr: str = Form(...),

    from_dt: str = Form(...),
    to_dt: str = Form(...),
    andamal: str = Form(...),

    disclaimer_accept: bool = Form(False),
    newsletter_optin: bool = Form(False),
):
    # Validate times
    from_parsed = parse_dt_local(from_dt)
    to_parsed = parse_dt_local(to_dt)
    if to_parsed <= from_parsed:
        raise HTTPException(status_code=400, detail="Sluttid måste vara efter starttid.")

    payload = {
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

        "bil_marke_modell": bil_marke_modell,
        "bil_regnr": normalize_regnr(bil_regnr),

        "from_dt": from_dt,
        "to_dt": to_dt,
        "andamal": andamal,

        "disclaimer_accept": bool(disclaimer_accept),
        "newsletter_optin": bool(newsletter_optin),
    }

    agreement_id = str(uuid.uuid4())
    ip = request.client.host if request.client else None
    ua = request.headers.get("user-agent")

    db = SessionLocal()
    try:
        db_obj = Agreement(
            id=agreement_id,
            status="draft",
            form_payload=_json_dump(payload),
            ip=ip,
            user_agent=ua,
            disclaimer_accepted_at=_now_utc() if disclaimer_accept else None,
            newsletter_optin=bool(newsletter_optin),
        )
        db.add(db_obj)
        db.commit()
    finally:
        db.close()

    return templates.TemplateResponse(
        "pages/lana_bil_review.html",
        {"request": request, "agreement_id": agreement_id, "data": payload},
    )

@app.post("/lana-bil-till-skuldsatt/free")
async def lana_bil_free(
    request: Request,
    agreement_id: str = Form(...),
    confirm_correct: bool = Form(False),
    disclaimer_accept: bool = Form(False),
):
    if not confirm_correct or not disclaimer_accept:
        raise HTTPException(status_code=400, detail="Du måste bekräfta villkoren och att uppgifterna är korrekta.")

    db = SessionLocal()
    try:
        a: Agreement = db.get(Agreement, agreement_id)
        if not a:
            raise HTTPException(status_code=404, detail="Avtal hittades inte.")
        payload = _json_load(a.form_payload)

        # Send lead email (NO PDF)
        text = (
            "NY LEAD (GRATIS)\n"
            "=================\n\n"
            f"Agreement ID: {agreement_id}\n"
            f"Utlånare: {payload.get('utlanare_namn')} - {payload.get('utlanare_epost')}\n"
            f"Låntagare: {payload.get('lantagare_namn')} - {payload.get('lantagare_epost')}\n"
            f"Regnr: {payload.get('bil_regnr')}\n"
            f"Period: {payload.get('from_dt')} -> {payload.get('to_dt')}\n\n"
            f"Newsletter opt-in: {payload.get('newsletter_optin')}\n"
            f"IP: {a.ip}\n"
            f"UA: {a.user_agent}\n"
        )
        await postmark_send_email(
            to_email=LEAD_INBOX,
            subject="Lead: Låna bil till skuldsatt (Gratis nedladdning)",
            text_body=text,
        )

        a.status = "free_downloaded"
        a.confirm_correct_at = _now_utc()
        a.disclaimer_accepted_at = _now_utc()
        db.commit()

        pdf_bytes = create_pdf_bytes(payload)
        return StreamingResponse(
            io.BytesIO(pdf_bytes),
            media_type="application/pdf",
            headers={"Content-Disposition": f'attachment; filename="lanabil-{agreement_id}.pdf"'},
        )
    finally:
        db.close()

@app.post("/lana-bil-till-skuldsatt/paid")
async def lana_bil_paid(
    request: Request,
    agreement_id: str = Form(...),
    confirm_correct: bool = Form(False),
    disclaimer_accept: bool = Form(False),
):
    if not confirm_correct or not disclaimer_accept:
        raise HTTPException(status_code=400, detail="Du måste bekräfta villkoren och att uppgifterna är korrekta.")

    if not STRIPE_SECRET_KEY:
        raise HTTPException(status_code=500, detail="Stripe är inte konfigurerat.")

    db = SessionLocal()
    try:
        a: Agreement = db.get(Agreement, agreement_id)
        if not a:
            raise HTTPException(status_code=404, detail="Avtal hittades inte.")

        session = stripe.checkout.Session.create(
            mode="payment",
            payment_method_types=["card"],
            line_items=[{
                "price_data": {
                    "currency": "sek",
                    "product_data": {"name": "Premium – Signering av låneavtal (BankID)"},
                    "unit_amount": PREMIUM_PRICE_ORE,
                },
                "quantity": 1,
            }],
            success_url=f"{PUBLIC_BASE_URL}/checkout-success?session_id={{CHECKOUT_SESSION_ID}}",
            cancel_url=f"{PUBLIC_BASE_URL}/lana-bil-till-skuldsatt",
            metadata={"agreement_id": agreement_id},
        )

        a.status = "paid_pending_webhook"
        a.confirm_correct_at = _now_utc()
        a.disclaimer_accepted_at = _now_utc()
        a.stripe_session_id = session["id"]
        db.commit()

        return RedirectResponse(url=session.url, status_code=303)
    finally:
        db.close()

@app.get("/checkout-success", response_class=HTMLResponse)
async def checkout_success(request: Request, session_id: str = ""):
    # IMPORTANT: webhook creates Oneflow + sends emails. Not here.
    return HTMLResponse("<h1>Tack!</h1><p>Betalning mottagen. (Webhooken sköter resten.)</p>")

# ----------------------------
# Stripe webhook
# ----------------------------
@app.post("/stripe/webhook")
async def stripe_webhook(request: Request):
    if not STRIPE_WEBHOOK_SECRET:
        raise HTTPException(status_code=500, detail="STRIPE_WEBHOOK_SECRET saknas.")

    payload = await request.body()
    sig_header = request.headers.get("stripe-signature", "")

    try:
        event = stripe.Webhook.construct_event(payload, sig_header, STRIPE_WEBHOOK_SECRET)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid Stripe signature")

    if event["type"] == "checkout.session.completed":
        session = event["data"]["object"]
        if session.get("payment_status") == "paid":
            agreement_id = (session.get("metadata") or {}).get("agreement_id")
            if agreement_id:
                db = SessionLocal()
                try:
                    a: Agreement = db.get(Agreement, agreement_id)
                    if not a:
                        return PlainTextResponse("ok")

                    # idempotency
                    if a.status in ("signing", "signed"):
                        return PlainTextResponse("ok")

                    payload_data = _json_load(a.form_payload)

                    # Create Oneflow contract + send links
                    contract_id, p1_id, p2_id, link1, link2 = await oneflow_create_contract_and_send(
                        payload_data, agreement_id
                    )

                    a.oneflow_contract_id = contract_id
                    a.oneflow_participant_1_id = p1_id
                    a.oneflow_participant_2_id = p2_id
                    a.status = "signing"
                    db.commit()

                    # Mail sign links to BOTH
                    ut_to = payload_data.get("utlanare_epost")
                    la_to = payload_data.get("lantagare_epost")

                    subject = "Signera låneavtal – HP Juridik"
                    text1 = (
                        "Hej!\n\n"
                        "Här är din signeringslänk för låneavtalet.\n\n"
                        f"Länk: {link1}\n\n"
                        "När båda har signerat får ni avtalet skickat som PDF.\n"
                    )
                    text2 = (
                        "Hej!\n\n"
                        "Här är din signeringslänk för låneavtalet.\n\n"
                        f"Länk: {link2}\n\n"
                        "När båda har signerat får ni avtalet skickat som PDF.\n"
                    )

                    if ut_to:
                        await postmark_send_email(ut_to, subject, text1)
                    if la_to:
                        await postmark_send_email(la_to, subject, text2)

                finally:
                    db.close()

    return PlainTextResponse("ok")

# ----------------------------
# Oneflow webhook (signed)
# ----------------------------
@app.post("/oneflow/webhook")
async def oneflow_webhook(request: Request):
    body = await request.json()

    if not oneflow_verify_signature(body):
        raise HTTPException(status_code=401, detail="Invalid Oneflow signature")

    contract_id = str((body.get("contract") or {}).get("id") or "")
    events = body.get("events") or []

    # Heuristic: treat these as "signed/completed" signals
    signed_types = {
        "contract:signed",
        "contract:sign",
        "contract:complete",
        "contract:completed",
    }
    event_types = {e.get("type") for e in events if isinstance(e, dict)}

    if contract_id and (event_types & signed_types):
        db = SessionLocal()
        try:
            a = db.query(Agreement).filter(Agreement.oneflow_contract_id == contract_id).first()
            if not a:
                return PlainTextResponse("ok")

            # Idempotent
            if a.status == "signed" and a.signed_pdf_path and os.path.exists(a.signed_pdf_path):
                return PlainTextResponse("ok")

            pdf_bytes = await oneflow_download_signed_pdf(contract_id)

            os.makedirs("/tmp/signed", exist_ok=True)
            out_path = f"/tmp/signed/{a.id}.pdf"
            with open(out_path, "wb") as f:
                f.write(pdf_bytes)

            a.signed_pdf_path = out_path
            a.status = "signed"
            db.commit()

            payload_data = _json_load(a.form_payload)
            ut_to = payload_data.get("utlanare_epost")
            la_to = payload_data.get("lantagare_epost")

            # Postmark attachment needs base64
            import base64
            b64 = base64.b64encode(pdf_bytes).decode("ascii")
            attachments = [{
                "Name": f"lanabil-signerat-{a.id}.pdf",
                "Content": b64,
                "ContentType": "application/pdf",
            }]

            subject = "Signerad PDF – Låneavtal (HP Juridik)"
            text = "Hej!\n\nHär kommer ert signerade låneavtal som PDF.\n\nVänligen,\nHP Juridik\n"

            if ut_to:
                await postmark_send_email(ut_to, subject, text, attachments=attachments)
            if la_to:
                await postmark_send_email(la_to, subject, text, attachments=attachments)

        finally:
            db.close()

    return PlainTextResponse("ok")

# ----------------------------
# Optional: download signed pdf endpoint
# ----------------------------
@app.get("/lana-bil-till-skuldsatt/signed/{agreement_id}")
async def download_signed(agreement_id: str):
    db = SessionLocal()
    try:
        a: Agreement = db.get(Agreement, agreement_id)
        if not a or not a.signed_pdf_path or not os.path.exists(a.signed_pdf_path):
            raise HTTPException(status_code=404, detail="Signerad PDF finns inte ännu.")
        return StreamingResponse(
            open(a.signed_pdf_path, "rb"),
            media_type="application/pdf",
            headers={"Content-Disposition": f'attachment; filename="lanabil-signerat-{agreement_id}.pdf"'},
        )
    finally:
        db.close()
