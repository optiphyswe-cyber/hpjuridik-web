"""HP Juridik – FastAPI app (stable)

Drop-in replacement for `app/main.py`.

Goals
- All public pages render (/, /kontakta-oss, /tjanster, /villkor, /lana-bil-till-skuldsatt, etc.)
- Contact form works reliably (POST accepted on both /kontakta-oss and /contact).
  Requirement from you: after submit, user stays on home (pages/home.html) with a success box.
- "Låna bil" flow works:
    GET /lana-bil-till-skuldsatt -> form
    POST /lana-bil-till-skuldsatt -> review page
    GET /lana-bil-till-skuldsatt/review -> redirects back to form (prevents 405)
    GET /lana-bil-till-skuldsatt/pdf/{agreement_id} -> downloads PDF
    POST /lana-bil-till-skuldsatt/free -> sends internal lead email
- Stripe premium checkout + webhook
- Oneflow (optional) after payment: create contract from template and email both parties

Environment variables expected (Render -> Environment):

# Mail (Postmark)
POSTMARK_SERVER_TOKEN=...
MAIL_FROM=lanabil@hpjuridik.se
LEAD_INBOX=lanabil@hpjuridik.se        # where leads/internal notifications go
CONTACT_TO=info@hpjuridik.se           # where contact form goes (optional)

# Stripe
STRIPE_SECRET_KEY=sk_live_...
STRIPE_WEBHOOK_SECRET=whsec_...
STRIPE_PRICE_ID_PREMIUM=price_...      # Stripe Price (150 SEK etc.)
PUBLIC_BASE_URL=https://www.hpjuridik.se

# DB (optional; if missing we fall back to in-memory)
DATABASE_URL=postgresql+psycopg://...

# Oneflow (optional)
ONEFLOW_API_TOKEN=...
ONEFLOW_WORKSPACE_ID=123
ONEFLOW_TEMPLATE_ID=456
ONEFLOW_WEBHOOK_SECRET=...             # optional

"""

from __future__ import annotations

import base64
import hashlib
import hmac
import io
import json
import os
import secrets
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any, Dict, Optional

import requests
import stripe
from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.pdfgen import canvas

# Optional DB
try:
    from sqlalchemy import Column, DateTime, String, Text, create_engine
    from sqlalchemy.orm import declarative_base, sessionmaker

    SQLA_AVAILABLE = True
except Exception:
    SQLA_AVAILABLE = False


# -----------------------------------------------------------------------------
# Config
# -----------------------------------------------------------------------------
POSTMARK_SERVER_TOKEN = os.getenv("POSTMARK_SERVER_TOKEN", "").strip()
MAIL_FROM = os.getenv("MAIL_FROM", "").strip() or "lanabil@hpjuridik.se"
LEAD_INBOX = os.getenv("LEAD_INBOX", "").strip() or MAIL_FROM
CONTACT_TO = os.getenv("CONTACT_TO", "").strip() or ""  # if empty: fallback to LEAD_INBOX

STRIPE_SECRET_KEY = os.getenv("STRIPE_SECRET_KEY", "").strip()
STRIPE_WEBHOOK_SECRET = os.getenv("STRIPE_WEBHOOK_SECRET", "").strip()
STRIPE_PRICE_ID_PREMIUM = os.getenv("STRIPE_PRICE_ID_PREMIUM", "").strip()
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "").strip() or "https://www.hpjuridik.se"

DATABASE_URL = os.getenv("DATABASE_URL", "").strip()

ONEFLOW_API_TOKEN = os.getenv("ONEFLOW_API_TOKEN", "").strip()
ONEFLOW_WORKSPACE_ID = os.getenv("ONEFLOW_WORKSPACE_ID", "").strip()
ONEFLOW_TEMPLATE_ID = os.getenv("ONEFLOW_TEMPLATE_ID", "").strip()
ONEFLOW_WEBHOOK_SECRET = os.getenv("ONEFLOW_WEBHOOK_SECRET", "").strip()


# -----------------------------------------------------------------------------
# App + templates + static
# -----------------------------------------------------------------------------
app = FastAPI()

# Render sends HEAD requests to / as health checks; FastAPI otherwise returns 405.
@app.head("/", include_in_schema=False)
def _head_root() -> Response:
    return Response(status_code=200)

# Project layout in your repo:
# hpjuridik-web/
#   app/
#     main.py
#     static/
#     templates/
BASE_DIR = os.path.dirname(__file__)
TEMPLATES_DIR = os.path.join(BASE_DIR, "templates")
STATIC_DIR = os.path.join(BASE_DIR, "static")

# Mount static if folder exists
if os.path.isdir(STATIC_DIR):
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

templates = Jinja2Templates(directory=TEMPLATES_DIR)


def page_ctx(request: Request, path: str, title: str, description: str = "") -> Dict[str, Any]:
    return {
        "request": request,
        "path": path,
        "title": title,
        "description": description,
    }


# -----------------------------------------------------------------------------
# Models
# -----------------------------------------------------------------------------
@dataclass
class Agreement:
    id: str
    created_at: datetime

    # Utlånare
    utlanare_namn: str
    utlanare_pnr: str
    utlanare_adress: str
    utlanare_tel: str
    utlanare_epost: str

    # Låntagare
    lantagare_namn: str
    lantagare_pnr: str
    lantagare_adress: str
    lantagare_tel: str
    lantagare_epost: str

    # Fordon
    bil_marke_modell: str
    bil_regnr: str

    # Period
    from_dt: str
    to_dt: str
    andamal: str

    # Marketing
    newsletter_optin: str = "false"

    # Stripe
    stripe_session_id: str = ""
    stripe_payment_status: str = ""

    # Oneflow
    oneflow_contract_id: str = ""
    oneflow_link_utlanare: str = ""
    oneflow_link_lantagare: str = ""


# -----------------------------------------------------------------------------
# Storage (DB if available, otherwise in-memory)
# -----------------------------------------------------------------------------
_agreements_mem: Dict[str, Agreement] = {}

if SQLA_AVAILABLE and DATABASE_URL:
    Base = declarative_base()

    class AgreementRow(Base):
        __tablename__ = "agreements"

        id = Column(String(64), primary_key=True)
        created_at = Column(DateTime(timezone=True), nullable=False)

        payload_json = Column(Text, nullable=False)  # store dataclass dict as JSON

    engine = create_engine(DATABASE_URL, pool_pre_ping=True)
    SessionLocal = sessionmaker(bind=engine, autocommit=False, autoflush=False)

    try:
        Base.metadata.create_all(engine)
    except Exception:
        # Don't crash startup if DB is not reachable; app still runs.
        pass


def _agreement_to_json(a: Agreement) -> str:
    d = asdict(a)
    d["created_at"] = a.created_at.isoformat()
    return json.dumps(d, ensure_ascii=False)


def _agreement_from_json(s: str) -> Agreement:
    d = json.loads(s)
    d["created_at"] = datetime.fromisoformat(d["created_at"])
    return Agreement(**d)


def db_upsert_agreement(a: Agreement) -> None:
    if SQLA_AVAILABLE and DATABASE_URL:
        try:
            with SessionLocal() as db:
                row = db.get(AgreementRow, a.id)
                if row is None:
                    row = AgreementRow(id=a.id, created_at=a.created_at, payload_json=_agreement_to_json(a))
                    db.add(row)
                else:
                    row.created_at = a.created_at
                    row.payload_json = _agreement_to_json(a)
                db.commit()
            return
        except Exception:
            # Fallback to memory if DB temporarily fails
            _agreements_mem[a.id] = a
            return

    _agreements_mem[a.id] = a


def db_get_agreement(agreement_id: str) -> Optional[Agreement]:
    if SQLA_AVAILABLE and DATABASE_URL:
        try:
            with SessionLocal() as db:
                row = db.get(AgreementRow, agreement_id)
                if row is None:
                    return None
                return _agreement_from_json(row.payload_json)
        except Exception:
            return _agreements_mem.get(agreement_id)

    return _agreements_mem.get(agreement_id)


# -----------------------------------------------------------------------------
# Utilities
# -----------------------------------------------------------------------------
def safe_id(prefix: str = "a") -> str:
    return f"{prefix}_{secrets.token_hex(12)}"


def _require_env(name: str, value: str) -> None:
    if not value:
        raise RuntimeError(f"Missing environment variable: {name}")


# -----------------------------------------------------------------------------
# Email (Postmark)
# -----------------------------------------------------------------------------
def postmark_send(*, to: str, subject: str, body_text: str, reply_to: str = "", attachments: Optional[list] = None) -> None:
    _require_env("POSTMARK_SERVER_TOKEN", POSTMARK_SERVER_TOKEN)

    payload: Dict[str, Any] = {
        "From": MAIL_FROM,
        "To": to,
        "Subject": subject,
        "TextBody": body_text,
        "MessageStream": "outbound",
    }
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
        timeout=25,
    )
    if r.status_code >= 300:
        raise RuntimeError(f"Postmark error {r.status_code}: {r.text[:800]}")


def pdf_attachment(filename: str, pdf_bytes: bytes) -> dict:
    return {
        "Name": filename,
        "Content": base64.b64encode(pdf_bytes).decode("ascii"),
        "ContentType": "application/pdf",
    }


# -----------------------------------------------------------------------------
# PDF generation (ReportLab)
# -----------------------------------------------------------------------------
def make_pdf_bytes(a: Agreement) -> bytes:
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    w, h = A4

    def write(x_mm: float, y_mm: float, text: str, size: int = 11):
        c.setFont("Helvetica", size)
        c.drawString(x_mm * mm, h - y_mm * mm, text)

    y = 22
    write(20, y, "LÅNEAVTAL – BIL (TILLFÄLLIGT LÅN)", 16)
    y += 8
    write(20, y, f"Avtals-ID: {a.id}", 10)
    y += 6
    write(20, y, f"Skapat: {a.created_at.astimezone(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}", 10)

    y += 12
    write(20, y, "1. UTLÅNARE (ÄGARE)", 13)
    y += 7
    write(20, y, f"Namn: {a.utlanare_namn}")
    y += 6
    if a.utlanare_pnr:
        write(20, y, f"Personnummer: {a.utlanare_pnr}"); y += 6
    write(20, y, f"Adress: {a.utlanare_adress}"); y += 6
    write(20, y, f"Telefon: {a.utlanare_tel}"); y += 6
    write(20, y, f"E-post: {a.utlanare_epost}")

    y += 10
    write(20, y, "2. LÅNTAGARE (SKULDSATT)", 13)
    y += 7
    write(20, y, f"Namn: {a.lantagare_namn}")
    y += 6
    if a.lantagare_pnr:
        write(20, y, f"Personnummer: {a.lantagare_pnr}"); y += 6
    write(20, y, f"Adress: {a.lantagare_adress}"); y += 6
    write(20, y, f"Telefon: {a.lantagare_tel}"); y += 6
    write(20, y, f"E-post: {a.lantagare_epost}")

    y += 10
    write(20, y, "3. FORDON", 13)
    y += 7
    write(20, y, f"Märke/Modell: {a.bil_marke_modell}"); y += 6
    write(20, y, f"Registreringsnummer: {a.bil_regnr}")

    y += 10
    write(20, y, "4. AVTALSPERIOD", 13)
    y += 7
    write(20, y, f"Från: {a.from_dt}"); y += 6
    write(20, y, f"Till: {a.to_dt}")

    y += 10
    write(20, y, "5. ÄNDAMÅL / SYFTE", 13)
    y += 7
    c.setFont("Helvetica", 11)
    text = c.beginText(20 * mm, h - y * mm)
    for line in (a.andamal or "").splitlines() or [a.andamal or ""]:
        text.textLine(line)
    c.drawText(text)

    y += 40
    write(20, y, "Detta dokument är ett standardiserat bevisunderlag baserat på inmatade uppgifter.", 9)
    y += 5
    write(20, y, "HP Juridik lämnar ingen garanti att dokumentet godtas av Kronofogden, domstol eller annan part.", 9)

    c.showPage()
    c.save()
    return buf.getvalue()


# -----------------------------------------------------------------------------
# Oneflow (optional)
# -----------------------------------------------------------------------------
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
        return str(r.json().get("id"))

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
        return str(data.get("access_link") or data.get("url") or data.get("link"))


def oneflow_verify_signature(raw_body: bytes, signature_header: str, secret: str) -> bool:
    if not secret:
        return True
    if not signature_header:
        return False
    expected = hmac.new(secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature_header.strip())


# -----------------------------------------------------------------------------
# Routes – Pages
# -----------------------------------------------------------------------------
@app.get("/", response_class=HTMLResponse)
def home(request: Request):
    return templates.TemplateResponse("pages/home.html", page_ctx(request, "/", "HP Juridik"))


@app.get("/tjanster", response_class=HTMLResponse)
@app.get("/services", response_class=HTMLResponse)
def services(request: Request):
    return templates.TemplateResponse("pages/services.html", page_ctx(request, "/tjanster", "Tjänster | HP Juridik"))


@app.get("/villkor", response_class=HTMLResponse)
@app.get("/terms", response_class=HTMLResponse)
def terms(request: Request):
    return templates.TemplateResponse("pages/terms.html", page_ctx(request, "/villkor", "Villkor | HP Juridik"))


@app.get("/kontakta-oss", response_class=HTMLResponse)
@app.get("/contact", response_class=HTMLResponse)
def contact_page(request: Request):
    return templates.TemplateResponse("pages/contact.html", page_ctx(request, "/kontakta-oss", "Kontakt | HP Juridik"))


# -----------------------------------------------------------------------------
# Contact form – POST
# Requirement: user stays on HOME after submit (not redirected to /kontakta-oss)
# Support both /contact and /kontakta-oss because templates may use either.
# -----------------------------------------------------------------------------
@app.post("/contact", response_class=HTMLResponse)
@app.post("/kontakta-oss", response_class=HTMLResponse)
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
        "NY KONTAKTFÖRFRÅGAN (HPJURIDIK.SE)\n\n"
        f"Tid: {ts}\n"
        f"Namn: {namn}\n"
        f"E-post: {epost}\n"
        f"Telefon: {telefon or '-'}\n\n"
        f"Meddelande:\n{meddelande}\n\n"
        f"---\nIP: {ip}\nUA: {ua}\n"
    )

    to_addr = (CONTACT_TO or LEAD_INBOX or MAIL_FROM).strip()

    ok, err = True, ""
    try:
        postmark_send(to=to_addr, subject=subject, body_text=body, reply_to=epost)
    except Exception as e:
        ok, err = False, str(e)

    ctx = page_ctx(request, "/", "HP Juridik")
    # Flags you can show in home.html (or ignore if your template doesn't use them)
    ctx.update({"contact_ok": ok, "contact_error": err, "contact_name": namn})
    return templates.TemplateResponse("pages/home.html", ctx)


# -----------------------------------------------------------------------------
# Låna bil – form -> review
# -----------------------------------------------------------------------------
@app.get("/lana-bil-till-skuldsatt", response_class=HTMLResponse)
def lana_bil_form(request: Request):
    return templates.TemplateResponse(
        "pages/lana_bil.html",
        page_ctx(request, "/lana-bil-till-skuldsatt", "Låna bil till skuldsatt | HP Juridik"),
    )


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
    # Checkboxes (required in your template)
    disclaimer_accept: str = Form(...),
    marketing_accept: str = Form(""),
):
    agreement_id = safe_id("agr")
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

    ctx = page_ctx(request, "/lana-bil-till-skuldsatt/review", "Granska avtal | HP Juridik")
    ctx.update({"agreement_id": agreement_id, "agreement": a, "premium_price_sek": 150})
    return templates.TemplateResponse("pages/lana_bil_review.html", ctx)


# Prevent 405/validation errors if someone opens /review directly in browser
@app.get("/lana-bil-till-skuldsatt/review", response_class=HTMLResponse)
def lana_bil_review_get(_: Request):
    return RedirectResponse("/lana-bil-till-skuldsatt", status_code=302)


@app.get("/lana-bil-till-skuldsatt/pdf/{agreement_id}")
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
def lana_bil_free_send(request: Request, agreement_id: str = Form(...)):
    a = db_get_agreement(agreement_id)
    if not a:
        return Response(content="Not Found", status_code=404)

    ok, err = True, ""
    try:
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
        postmark_send(to=LEAD_INBOX, subject=subject, body_text=body)
    except Exception as e:
        ok, err = False, str(e)

    ctx = page_ctx(request, "/lana-bil-till-skuldsatt/review", "Granska avtal | HP Juridik")
    ctx.update({"agreement_id": agreement_id, "agreement": a, "free_sent_ok": ok, "free_sent_error": err, "premium_price_sek": 150})
    return templates.TemplateResponse("pages/lana_bil_review.html", ctx)


# -----------------------------------------------------------------------------
# Stripe
# -----------------------------------------------------------------------------
@app.post("/stripe/create-checkout-session")
def stripe_create_checkout_session(agreement_id: str = Form(...)):
    _require_env("STRIPE_SECRET_KEY", STRIPE_SECRET_KEY)
    _require_env("STRIPE_PRICE_ID_PREMIUM", STRIPE_PRICE_ID_PREMIUM)

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
    a.stripe_payment_status = session.get("payment_status") or ""
    db_upsert_agreement(a)

    return RedirectResponse(session.url, status_code=303)


@app.get("/checkout-success", response_class=HTMLResponse)
def checkout_success(request: Request):
    return templates.TemplateResponse(
        "pages/checkout_success.html",
        page_ctx(request, "/checkout-success", "Tack! | HP Juridik"),
    )


@app.post("/stripe/webhook")
async def stripe_webhook(request: Request):
    _require_env("STRIPE_WEBHOOK_SECRET", STRIPE_WEBHOOK_SECRET)

    payload = await request.body()
    sig_header = request.headers.get("stripe-signature", "")

    try:
        stripe.api_key = STRIPE_SECRET_KEY or None
        event = stripe.Webhook.construct_event(payload=payload, sig_header=sig_header, secret=STRIPE_WEBHOOK_SECRET)
    except Exception as e:
        return Response(f"Webhook error: {e}", status_code=400)

    if event.get("type") == "checkout.session.completed":
        session = event["data"]["object"]
        agreement_id = ((session.get("metadata") or {}) or {}).get("agreement_id")
        if agreement_id:
            a = db_get_agreement(agreement_id)
            if a:
                a.stripe_payment_status = session.get("payment_status") or "paid"
                db_upsert_agreement(a)
                try:
                    _handle_premium_paid(a)
                except Exception:
                    # do not block webhook retries
                    pass

    return Response(status_code=200)


def _handle_premium_paid(a: Agreement) -> None:
    """After successful payment:
    - Create Oneflow contract (if configured)
    - Email both parties with PDF attached + unique signing link (if available)
    """

    pdf_bytes = make_pdf_bytes(a)
    attach = [pdf_attachment(f"laneavtal-bil-{a.id}.pdf", pdf_bytes)]

    link_utlanare = ""
    link_lantagare = ""

    if ONEFLOW_API_TOKEN and ONEFLOW_WORKSPACE_ID and ONEFLOW_TEMPLATE_ID:
        of = OneflowClient(ONEFLOW_API_TOKEN)
        contract_id = of.create_contract_from_template(
            name=f"Låneavtal bil – {a.bil_regnr} ({a.id})",
            workspace_id=ONEFLOW_WORKSPACE_ID,
            template_id=ONEFLOW_TEMPLATE_ID,
        )

        party1 = of.create_party(contract_id=contract_id, name=a.utlanare_namn)
        p1 = of.create_participant(contract_id=contract_id, party_id=party1, name=a.utlanare_namn, email=a.utlanare_epost)

        party2 = of.create_party(contract_id=contract_id, name=a.lantagare_namn)
        p2 = of.create_participant(contract_id=contract_id, party_id=party2, name=a.lantagare_namn, email=a.lantagare_epost)

        of.publish_contract(contract_id=contract_id)
        link_utlanare = of.create_access_link(contract_id=contract_id, participant_id=p1)
        link_lantagare = of.create_access_link(contract_id=contract_id, participant_id=p2)

        a.oneflow_contract_id = contract_id
        a.oneflow_link_utlanare = link_utlanare
        a.oneflow_link_lantagare = link_lantagare
        db_upsert_agreement(a)

    subject = "HP Juridik – Låneavtal (bil)" + (" + signering" if (link_utlanare or link_lantagare) else "")

    intro = (
        "Här kommer ert låneavtal som PDF.\n\n"
        "Om signeringslänk saknas: signering är inte konfigurerad i systemet ännu.\n"
    )

    body_utl = intro + (f"\nSignera här (Utlånare): {link_utlanare}\n" if link_utlanare else "")
    body_lan = intro + (f"\nSignera här (Låntagare): {link_lantagare}\n" if link_lantagare else "")

    postmark_send(to=a.utlanare_epost, subject=subject, body_text=body_utl, attachments=attach)
    postmark_send(to=a.lantagare_epost, subject=subject, body_text=body_lan, attachments=attach)

    # Internal receipt
    internal = (
        "NY PREMIUM-ORDER\n"
        "==============\n\n"
        f"Agreement ID: {a.id}\n"
        f"Utlånare: {a.utlanare_namn} - {a.utlanare_epost}\n"
        f"Låntagare: {a.lantagare_namn} - {a.lantagare_epost}\n"
        f"Oneflow contract: {a.oneflow_contract_id or '-'}\n"
    )
    postmark_send(to=LEAD_INBOX, subject=f"Premium: Låna bil (betald) – {a.id}", body_text=internal)


# -----------------------------------------------------------------------------
# Oneflow webhook (optional)
# -----------------------------------------------------------------------------
@app.post("/oneflow/webhook")
async def oneflow_webhook(request: Request):
    raw = await request.body()
    sig = request.headers.get("x-oneflow-signature", "")
    if not oneflow_verify_signature(raw, sig, ONEFLOW_WEBHOOK_SECRET):
        return Response("invalid signature", status_code=401)
    return Response(status_code=200)
