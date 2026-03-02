"""
HP Juridik – FastAPI app (Render)

Goals:
- Stable routing (/, /kontakt*, /lana-bil-till-skuldsatt, etc.)
- Contact form sends email via Postmark (works even if SMTP vars are missing)
- Låna bil (avtal) flow:
    - GET form
    - POST -> review page
    - Free download (PDF)
    - Premium checkout (Stripe) + webhook -> trigger Oneflow signing + email both parties
- Minimal DB (SQLite fallback) to avoid losing state between requests

IMPORTANT:
- Put secrets in Render Environment Variables (never hardcode).
- If you previously pasted secrets in chat/logs, rotate them (Stripe + Oneflow + Postmark).
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import io
import json
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, Optional

import requests
import stripe
from fastapi import FastAPI, Form, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

# PDF
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.pdfgen import canvas

# DB (lightweight)
from sqlalchemy import Column, DateTime, Integer, String, Text, create_engine
from sqlalchemy.orm import declarative_base, sessionmaker

# ----------------------------
# Config
# ----------------------------
APP_NAME = "HP Juridik"
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

TEMPLATES_DIR = os.path.join(BASE_DIR, "templates")
STATIC_DIR = os.path.join(BASE_DIR, "static")

# Email
POSTMARK_SERVER_TOKEN = os.getenv("POSTMARK_SERVER_TOKEN", "").strip()
MAIL_FROM = os.getenv("MAIL_FROM", "").strip()  # e.g. lanabil@hpjuridik.se
LEAD_INBOX = os.getenv("LEAD_INBOX", "").strip()  # where leads should go (internal)
CONTACT_TO = os.getenv("CONTACT_TO", "").strip()  # optional override, else LEAD_INBOX

# Stripe
STRIPE_SECRET_KEY = os.getenv("STRIPE_SECRET_KEY", "").strip()
STRIPE_WEBHOOK_SECRET = os.getenv("STRIPE_WEBHOOK_SECRET", "").strip()
STRIPE_PRICE_ID_PREMIUM = os.getenv("STRIPE_PRICE_ID_PREMIUM", "").strip()
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "https://www.hpjuridik.se").rstrip("/")

# Oneflow (signing)
ONEFLOW_API_TOKEN = os.getenv("ONEFLOW_API_TOKEN", "").strip()
ONEFLOW_WORKSPACE_ID = os.getenv("ONEFLOW_WORKSPACE_ID", "").strip()
ONEFLOW_TEMPLATE_ID = os.getenv("ONEFLOW_TEMPLATE_ID", "").strip()
ONEFLOW_WEBHOOK_SECRET = os.getenv("ONEFLOW_WEBHOOK_SECRET", "").strip()  # optional if you enable signature verification

# Database
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
if not DATABASE_URL:
    # Render ephemeral disk is ok for simple state; for production set DATABASE_URL to Postgres.
    DATABASE_URL = "sqlite:////tmp/hpjuridik.db"

engine = create_engine(
    DATABASE_URL,
    connect_args={"check_same_thread": False} if DATABASE_URL.startswith("sqlite:") else {},
    pool_pre_ping=True,
)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)
Base = declarative_base()


class Agreement(Base):
    __tablename__ = "agreements"

    id = Column(String(64), primary_key=True)  # external id
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))

    # parties
    utlanare_namn = Column(String(255))
    utlanare_pnr = Column(String(64))
    utlanare_adress = Column(String(255))
    utlanare_tel = Column(String(64))
    utlanare_epost = Column(String(255))

    lantagare_namn = Column(String(255))
    lantagare_pnr = Column(String(64))
    lantagare_adress = Column(String(255))
    lantagare_tel = Column(String(64))
    lantagare_epost = Column(String(255))

    bil_marke_modell = Column(String(255))
    bil_regnr = Column(String(64))

    from_dt = Column(String(64))
    to_dt = Column(String(64))
    andamal = Column(Text)

    newsletter_optin = Column(String(10), default="false")

    # Stripe/Oneflow
    stripe_session_id = Column(String(255), nullable=True)
    stripe_payment_status = Column(String(64), nullable=True)

    oneflow_contract_id = Column(String(64), nullable=True)
    oneflow_link_utlanare = Column(Text, nullable=True)
    oneflow_link_lantagare = Column(Text, nullable=True)


Base.metadata.create_all(engine)


def db_get_agreement(agreement_id: str) -> Optional[Agreement]:
    with SessionLocal() as db:
        return db.get(Agreement, agreement_id)


def db_upsert_agreement(agreement: Agreement) -> None:
    with SessionLocal() as db:
        db.merge(agreement)
        db.commit()


# ----------------------------
# App / Templates
# ----------------------------
app = FastAPI()

if os.path.isdir(STATIC_DIR):
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

templates = Jinja2Templates(directory=TEMPLATES_DIR)


def page_ctx(request: Request, path: str, title: str, subtitle: str = "") -> Dict[str, Any]:
    return {
        "request": request,
        "path": path,
        "title": title,
        "subtitle": subtitle,
        "app_name": APP_NAME,
        "year": datetime.now().year,
    }


def safe_slug_id() -> str:
    # short random id that is safe in URLs
    import secrets
    return secrets.token_urlsafe(16)


# ----------------------------
# Email via Postmark
# ----------------------------
def postmark_send(
    *,
    to: str,
    subject: str,
    body_text: str,
    body_html: Optional[str] = None,
    reply_to: Optional[str] = None,
    attachments: Optional[list[dict]] = None,
) -> None:
    """
    Sends an email using Postmark's API. Raises for non-200.
    """
    if not POSTMARK_SERVER_TOKEN:
        raise RuntimeError("POSTMARK_SERVER_TOKEN saknas i env")

    if not MAIL_FROM:
        raise RuntimeError("MAIL_FROM saknas i env")

    payload: Dict[str, Any] = {
        "From": MAIL_FROM,
        "To": to,
        "Subject": subject,
        "TextBody": body_text,
    }
    if body_html:
        payload["HtmlBody"] = body_html
    if reply_to:
        payload["ReplyTo"] = reply_to
    if attachments:
        payload["Attachments"] = attachments

    r = requests.post(
        "https://api.postmarkapp.com/email",
        headers={
            "Accept": "application/json",
            "Content-Type": "application/json",
            "X-Postmark-Server-Token": POSTMARK_SERVER_TOKEN,
        },
        data=json.dumps(payload),
        timeout=20,
    )
    if r.status_code >= 300:
        raise RuntimeError(f"Postmark error {r.status_code}: {r.text[:500]}")


def make_pdf_bytes(a: Agreement) -> bytes:
    """
    Simple, robust PDF generator (ReportLab).
    If you later want the exact previous layout, we can replace this
    with your old PDF builder — but this version will not crash.
    """
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    width, height = A4

    def write(x_mm: float, y_mm: float, text: str, size: int = 11):
        c.setFont("Helvetica", size)
        c.drawString(x_mm * mm, height - y_mm * mm, text)

    y = 25
    write(20, y, "Låneavtal – Bil (tillfälligt lån)", 16)
    y += 10
    write(20, y, f"Avtals-ID: {a.id}", 10)
    y += 6
    write(20, y, f"Skapat: {a.created_at.astimezone(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}", 10)

    y += 12
    write(20, y, "Utlånare (ägare)", 13); y += 7
    write(20, y, f"Namn: {a.utlanare_namn}")
    y += 6
    if a.utlanare_pnr:
        write(20, y, f"Personnummer: {a.utlanare_pnr}"); y += 6
    write(20, y, f"Adress: {a.utlanare_adress}"); y += 6
    write(20, y, f"Telefon: {a.utlanare_tel}"); y += 6
    write(20, y, f"E-post: {a.utlanare_epost}"); y += 10

    write(20, y, "Låntagare (skuldsatt)", 13); y += 7
    write(20, y, f"Namn: {a.lantagare_namn}"); y += 6
    if a.lantagare_pnr:
        write(20, y, f"Personnummer: {a.lantagare_pnr}"); y += 6
    write(20, y, f"Adress: {a.lantagare_adress}"); y += 6
    write(20, y, f"Telefon: {a.lantagare_tel}"); y += 6
    write(20, y, f"E-post: {a.lantagare_epost}"); y += 10

    write(20, y, "Fordon", 13); y += 7
    write(20, y, f"Märke/Modell: {a.bil_marke_modell}"); y += 6
    write(20, y, f"Registreringsnummer: {a.bil_regnr}"); y += 10

    write(20, y, "Avtalsperiod", 13); y += 7
    write(20, y, f"Från: {a.from_dt}"); y += 6
    write(20, y, f"Till: {a.to_dt}"); y += 10

    write(20, y, "Ändamål / syfte", 13); y += 7
    c.setFont("Helvetica", 11)
    text_obj = c.beginText(20 * mm, height - y * mm)
    for line in (a.andamal or "").splitlines():
        text_obj.textLine(line)
    c.drawText(text_obj)

    c.showPage()
    c.save()
    return buf.getvalue()


def pdf_attachment(filename: str, pdf_bytes: bytes) -> dict:
    return {
        "Name": filename,
        "Content": base64.b64encode(pdf_bytes).decode("ascii"),
        "ContentType": "application/pdf",
    }


# ----------------------------
# Oneflow (signing) helpers
# ----------------------------
class OneflowClient:
    def __init__(self, token: str):
        self.base = "https://api.oneflow.com"
        self.token = token

    def _headers(self) -> Dict[str, str]:
        return {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    def create_contract_from_template(self, *, name: str, workspace_id: str, template_id: str) -> str:
        """
        Create a contract (draft) based on a template.
        Reference: Oneflow Public API "Create a contract". See developer.oneflow.com
        """
        r = requests.post(
            f"{self.base}/v1/contracts",
            headers=self._headers(),
            data=json.dumps(
                {
                    "name": name,
                    "workspace_id": int(workspace_id),
                    "template_id": int(template_id),
                }
            ),
            timeout=30,
        )
        r.raise_for_status()
        data = r.json()
        # contract id field is commonly "id"
        return str(data.get("id"))

    def create_party(self, *, contract_id: str, name: str) -> str:
        r = requests.post(
            f"{self.base}/v1/contracts/{contract_id}/parties",
            headers=self._headers(),
            data=json.dumps({"name": name}),
            timeout=30,
        )
        r.raise_for_status()
        return str(r.json().get("id"))

    def create_participant(self, *, contract_id: str, party_id: str, name: str, email: str) -> str:
        r = requests.post(
            f"{self.base}/v1/contracts/{contract_id}/parties/{party_id}/participants",
            headers=self._headers(),
            data=json.dumps({"name": name, "email": email}),
            timeout=30,
        )
        r.raise_for_status()
        return str(r.json().get("id"))

    def publish_contract(self, *, contract_id: str) -> None:
        r = requests.post(
            f"{self.base}/v1/contracts/{contract_id}/publish",
            headers=self._headers(),
            timeout=30,
        )
        r.raise_for_status()

    def create_access_link(self, *, contract_id: str, participant_id: str) -> str:
        r = requests.post(
            f"{self.base}/v1/contracts/{contract_id}/participants/{participant_id}/access_link",
            headers=self._headers(),
            timeout=30,
        )
        r.raise_for_status()
        data = r.json()
        # docs refer to "access_link" (string) or "url"
        return str(data.get("access_link") or data.get("url") or data.get("link"))


def oneflow_verify_signature(raw_body: bytes, signature_header: str, secret: str) -> bool:
    """
    Oneflow can sign webhooks. If you configured a webhook secret,
    verify it here. If you didn't, keep ONEFLOW_WEBHOOK_SECRET empty
    and we accept requests (not recommended for production).
    """
    if not secret:
        return True
    if not signature_header:
        return False
    # Common scheme: hex HMAC SHA256 of body
    expected = hmac.new(secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature_header.strip())


# ----------------------------
# Routes: pages
# ----------------------------
@app.get("/", response_class=HTMLResponse)
def home(request: Request):
    # If your home template is pages/home.html, render it.
    return templates.TemplateResponse(
        "pages/home.html",
        page_ctx(request, "/", "HP Juridik", ""),
    )


@app.get("/tjanster", response_class=HTMLResponse)
def services(request: Request):
    return templates.TemplateResponse(
        "pages/services.html",
        page_ctx(request, "/tjanster", "Tjänster | HP Juridik", "Tjänster"),
    )


@app.get("/villkor", response_class=HTMLResponse)
def terms(request: Request):
    return templates.TemplateResponse(
        "pages/terms.html",
        page_ctx(request, "/villkor", "Villkor | HP Juridik", "Villkor"),
    )


# Keep older English routes if your nav still points there
@app.get("/services", response_class=HTMLResponse)
def services_alias(request: Request):
    return services(request)


@app.get("/terms", response_class=HTMLResponse)
def terms_alias(request: Request):
    return terms(request)


@app.get("/kontakta-oss", response_class=HTMLResponse)
@app.get("/contact", response_class=HTMLResponse)
def contact(request: Request):
    # Dedicated contact page
    return templates.TemplateResponse(
        "pages/contact.html",
        page_ctx(request, "/kontakta-oss", "Kontakt | HP Juridik", "Kontakta oss"),
    )


# ----------------------------
# Contact form submission
# Requirement: do NOT redirect to /kontakta-oss; stay on home.html
# ----------------------------
@app.post("/contact", response_class=HTMLResponse)
async def contact_submit(
    request: Request,
    namn: str = Form(...),
    epost: str = Form(...),
    telefon: str = Form(""),
    meddelande: str = Form(...),
):
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    ip = request.client.host if request.client else "unknown"
    ua = request.headers.get("user-agent", "unknown")

    subject = f"HP Juridik | Ny kontaktförfrågan från {namn}"
    body = (
        f"NY KONTAKTFÖRFRÅGAN (HPJURIDIK.SE)\n\n"
        f"Tid: {ts}\n"
        f"Namn: {namn}\n"
        f"E-post: {epost}\n"
        f"Telefon: {telefon or '-'}\n\n"
        f"Meddelande:\n{meddelande}\n\n"
        f"---\nIP: {ip}\nUA: {ua}\n"
    )

    to_addr = (CONTACT_TO or LEAD_INBOX or MAIL_FROM).strip()
    ok, err = True, None
    try:
        postmark_send(to=to_addr, subject=subject, body_text=body, reply_to=epost)
    except Exception as e:
        ok, err = False, str(e)

    # Render HOME with a success box (your home.html should show these flags if you want)
    ctx = page_ctx(request, "/", "HP Juridik", "")
    ctx.update({"contact_ok": ok, "contact_error": err})
    return templates.TemplateResponse("pages/home.html", ctx)


# ----------------------------
# Låna bil: form -> review -> free download / premium
# ----------------------------
@app.get("/lana-bil-till-skuldsatt", response_class=HTMLResponse)
def lana_bil_form(request: Request):
    ctx = page_ctx(request, "/lana-bil-till-skuldsatt", "Låna bil till skuldsatt | HP Juridik", "")
    return templates.TemplateResponse("pages/lana_bil.html", ctx)


@app.post("/lana-bil-till-skuldsatt", response_class=HTMLResponse)
async def lana_bil_review(
    request: Request,
    # Utlånare
    utlanare_namn: str = Form(...),
    utlanare_pnr: str = Form(""),
    utlanare_adress: str = Form(...),
    utlanare_tel: str = Form(...),
    utlanare_epost: str = Form(...),
    # Låntagare
    lantagare_namn: str = Form(...),
    lantagare_pnr: str = Form(""),
    lantagare_adress: str = Form(...),
    lantagare_tel: str = Form(...),
    lantagare_epost: str = Form(...),
    # Fordon
    bil_marke_modell: str = Form(...),
    bil_regnr: str = Form(...),
    # Period
    from_dt: str = Form(...),
    to_dt: str = Form(...),
    andamal: str = Form(...),
    # checkboxes
    disclaimer_accept: str = Form(...),
    marketing_accept: str = Form(""),
):
    # Create agreement
    agreement_id = safe_slug_id()
    a = Agreement(
        id=agreement_id,
        created_at=datetime.now(timezone.utc),
        utlanare_namn=utlanare_namn,
        utlanare_pnr=utlanare_pnr,
        utlanare_adress=utlanare_adress,
        utlanare_tel=utlanare_tel,
        utlanare_epost=utlanare_epost,
        lantagare_namn=lantagare_namn,
        lantagare_pnr=lantagare_pnr,
        lantagare_adress=lantagare_adress,
        lantagare_tel=lantagare_tel,
        lantagare_epost=lantagare_epost,
        bil_marke_modell=bil_marke_modell,
        bil_regnr=bil_regnr,
        from_dt=from_dt,
        to_dt=to_dt,
        andamal=andamal,
        newsletter_optin="true" if marketing_accept else "false",
    )
    db_upsert_agreement(a)

    ctx = page_ctx(request, "/lana-bil-till-skuldsatt/review", "Granska avtal | HP Juridik", "")
    ctx.update({"agreement_id": agreement_id, "agreement": a, "premium_price_sek": 150})
    return templates.TemplateResponse("pages/lana_bil_review.html", ctx)


# If user manually visits /review, don't crash with validation errors
@app.get("/lana-bil-till-skuldsatt/review", response_class=HTMLResponse)
def lana_bil_review_get(request: Request):
    return RedirectResponse(url="/lana-bil-till-skuldsatt", status_code=302)


@app.get("/lana-bil-till-skuldsatt/pdf/{agreement_id}", response_class=Response)
def lana_bil_pdf(agreement_id: str):
    a = db_get_agreement(agreement_id)
    if not a:
        return Response(content="Not Found", status_code=404)

    pdf_bytes = make_pdf_bytes(a)
    filename = f"laneavtal-bil-{agreement_id}.pdf"
    return StreamingResponse(
        io.BytesIO(pdf_bytes),
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.post("/lana-bil-till-skuldsatt/free", response_class=HTMLResponse)
def lana_bil_free_send(
    request: Request,
    agreement_id: str = Form(...),
):
    a = db_get_agreement(agreement_id)
    if not a:
        return Response(content="Not Found", status_code=404)

    # Send internal lead email (free)
    ok, err = True, None
    try:
        to_addr = (LEAD_INBOX or MAIL_FROM).strip()
        subject = "Lead: Låna bil till skuldsatt (Gratis nedladdning)"
        body = (
            "NY LEAD (GRATIS)\n"
            "================\n\n"
            f"Agreement ID: {a.id}\n"
            f"Utlånare: {a.utlanare_namn} - {a.utlanare_epost}\n"
            f"Låntagare: {a.lantagare_namn} - {a.lantagare_epost}\n"
            f"Regnr: {a.bil_regnr}\n"
            f"Period: {a.from_dt} -> {a.to_dt}\n\n"
            f"Newsletter opt-in: {a.newsletter_optin}\n"
        )
        postmark_send(to=to_addr, subject=subject, body_text=body)
    except Exception as e:
        ok, err = False, str(e)

    ctx = page_ctx(request, "/lana-bil-till-skuldsatt/review", "Granska avtal | HP Juridik", "")
    ctx.update(
        {
            "agreement_id": agreement_id,
            "agreement": a,
            "free_sent_ok": ok,
            "free_sent_error": err,
            "premium_price_sek": 150,
        }
    )
    return templates.TemplateResponse("pages/lana_bil_review.html", ctx)


# ----------------------------
# Stripe: premium checkout
# ----------------------------
@app.post("/stripe/create-checkout-session")
def stripe_create_checkout_session(
    agreement_id: str = Form(...),
):
    if not STRIPE_SECRET_KEY or not STRIPE_PRICE_ID_PREMIUM:
        return Response("Stripe is not configured (missing STRIPE_SECRET_KEY/STRIPE_PRICE_ID_PREMIUM)", status_code=500)

    a = db_get_agreement(agreement_id)
    if not a:
        return Response("Not Found", status_code=404)

    stripe.api_key = STRIPE_SECRET_KEY

    session = stripe.checkout.Session.create(
        mode="payment",
        line_items=[{"price": STRIPE_PRICE_ID_PREMIUM, "quantity": 1}],
        success_url=f"{PUBLIC_BASE_URL}/checkout-success?session_id={{CHECKOUT_SESSION_ID}}",
        cancel_url=f"{PUBLIC_BASE_URL}/lana-bil-till-skuldsatt/review",
        metadata={"agreement_id": agreement_id},
    )

    a.stripe_session_id = session["id"]
    a.stripe_payment_status = session.get("payment_status")
    db_upsert_agreement(a)

    return RedirectResponse(session.url, status_code=303)


@app.get("/checkout-success", response_class=HTMLResponse)
def checkout_success(request: Request):
    # Keep it simple — webhook does the heavy lifting
    return templates.TemplateResponse(
        "pages/checkout_success.html",
        page_ctx(request, "/checkout-success", "Tack! | HP Juridik", ""),
    )


@app.post("/stripe/webhook")
async def stripe_webhook(request: Request):
    if not STRIPE_WEBHOOK_SECRET:
        return Response("STRIPE_WEBHOOK_SECRET missing", status_code=500)

    payload = await request.body()
    sig_header = request.headers.get("stripe-signature", "")

    try:
        event = stripe.Webhook.construct_event(
            payload=payload,
            sig_header=sig_header,
            secret=STRIPE_WEBHOOK_SECRET,
        )
    except Exception as e:
        return Response(f"Webhook error: {e}", status_code=400)

    # Handle checkout completion
    if event["type"] == "checkout.session.completed":
        session = event["data"]["object"]
        agreement_id = (session.get("metadata") or {}).get("agreement_id")
        if agreement_id:
            a = db_get_agreement(agreement_id)
            if a:
                a.stripe_payment_status = session.get("payment_status") or "paid"
                db_upsert_agreement(a)
                try:
                    _handle_premium_paid(a)
                except Exception:
                    # Don't fail webhook delivery due to downstream issues
                    pass

    return Response(status_code=200)


def _handle_premium_paid(a: Agreement) -> None:
    """
    Called after successful Stripe payment.
    Creates Oneflow signing contract (if configured),
    and emails PDF + signing links to both parties.
    """
    pdf_bytes = make_pdf_bytes(a)

    # --- Oneflow (optional) ---
    link_utlanare = None
    link_lantagare = None

    if ONEFLOW_API_TOKEN and ONEFLOW_WORKSPACE_ID and ONEFLOW_TEMPLATE_ID:
        of = OneflowClient(ONEFLOW_API_TOKEN)
        contract_id = of.create_contract_from_template(
            name=f"Låneavtal bil – {a.bil_regnr} ({a.id})",
            workspace_id=ONEFLOW_WORKSPACE_ID,
            template_id=ONEFLOW_TEMPLATE_ID,
        )

        # Parties: Utlånare + Låntagare
        party1 = of.create_party(contract_id=contract_id, name=a.utlanare_namn)
        p1 = of.create_participant(contract_id=contract_id, party_id=party1, name=a.utlanare_namn, email=a.utlanare_epost)

        party2 = of.create_party(contract_id=contract_id, name=a.lantagare_namn)
        p2 = of.create_participant(contract_id=contract_id, party_id=party2, name=a.lantagare_namn, email=a.lantagare_epost)

        # Publish then create access links
        of.publish_contract(contract_id=contract_id)
        link_utlanare = of.create_access_link(contract_id=contract_id, participant_id=p1)
        link_lantagare = of.create_access_link(contract_id=contract_id, participant_id=p2)

        a.oneflow_contract_id = contract_id
        a.oneflow_link_utlanare = link_utlanare
        a.oneflow_link_lantagare = link_lantagare
        db_upsert_agreement(a)

    # --- Email both parties ---
    # NOTE: We send 1 email to each person, with PDF attached + their unique signing link (if available).
    common_subject = "HP Juridik – Låneavtal (bil) + signering"
    intro = (
        "Här kommer ert låneavtal som PDF.\n\n"
        "Om signeringslänk saknas: signeringsintegration är ännu inte konfigurerad i systemet.\n"
    )

    attach = [pdf_attachment(f"laneavtal-bil-{a.id}.pdf", pdf_bytes)]

    # Utlånare
    body1 = intro
    if link_utlanare:
        body1 += f"\nSignera här (Utlånare): {link_utlanare}\n"
    postmark_send(
        to=a.utlanare_epost,
        subject=common_subject,
        body_text=body1,
        attachments=attach,
    )

    # Låntagare
    body2 = intro
    if link_lantagare:
        body2 += f"\nSignera här (Låntagare): {link_lantagare}\n"
    postmark_send(
        to=a.lantagare_epost,
        subject=common_subject,
        body_text=body2,
        attachments=attach,
    )

    # Internal notification
    if LEAD_INBOX:
        body_internal = (
            "NY PREMIUM-ORDER\n"
            "==============\n\n"
            f"Agreement ID: {a.id}\n"
            f"Utlånare: {a.utlanare_namn} - {a.utlanare_epost}\n"
            f"Låntagare: {a.lantagare_namn} - {a.lantagare_epost}\n"
            f"Oneflow contract: {a.oneflow_contract_id or '-'}\n"
        )
        postmark_send(to=LEAD_INBOX, subject=f"Premium: Låna bil (betald) – {a.id}", body_text=body_internal)


# ----------------------------
# Oneflow webhook (optional – for signed event)
# ----------------------------
@app.post("/oneflow/webhook")
async def oneflow_webhook(request: Request):
    raw = await request.body()
    sig = request.headers.get("x-oneflow-signature", "")
    if not oneflow_verify_signature(raw, sig, ONEFLOW_WEBHOOK_SECRET):
        return Response("invalid signature", status_code=401)

    # You can parse and act on events here (e.g., when fully signed -> download PDF)
    # For now we just accept.
    return Response(status_code=200)


# ----------------------------
# Health / debug
# ----------------------------
@app.get("/healthz")
def healthz():
    return {"ok": True, "ts": datetime.now(timezone.utc).isoformat()}


# ----------------------------
# Custom 404 to avoid JSON "Not Found" on browser routes
# ----------------------------
@app.exception_handler(404)
async def not_found_handler(request: Request, exc):
    # If you have a pretty 404 template you can render it here.
    # For now, redirect to home.
    return RedirectResponse(url="/", status_code=302)
