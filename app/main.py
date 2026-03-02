"""
HP Juridik – FastAPI app (Render-stable)

Drop-in replacement for `app/main.py`.

Implements:
- Stable routing for Render (GET/HEAD /, trailing slash safety)
- Public pages (Jinja2 templates)
- Contact form (stays on same page, emails CONTACT_TO with Reply-To = user)
- Låna bil flow (DB-backed when DATABASE_URL exists; otherwise in-memory fallback):
    POST /lana-bil-till-skuldsatt/start -> create agreement + redirect to review/{id}
    GET  /lana-bil-till-skuldsatt/review/{id} -> review page
    POST /lana-bil-till-skuldsatt/{id}/free -> lead email + PDF download
    POST /lana-bil-till-skuldsatt/{id}/checkout -> Stripe Checkout redirect
    POST /stripe/webhook -> verifies signature; on checkout.session.completed: marks paid, emails PDF to both parties + internal inbox
- PDF generation via ReportLab

Environment variables (Render):
# Postmark
POSTMARK_SERVER_TOKEN=...
MAIL_FROM=lanabil@hpjuridik.se
CONTACT_TO=hp@hpjuridik.se
LEAD_INBOX=lanabil@hpjuridik.se

# Stripe
STRIPE_SECRET_KEY=sk_live_...
STRIPE_WEBHOOK_SECRET=whsec_...
STRIPE_PRICE_ID_PREMIUM=price_...
PUBLIC_BASE_URL=https://hpjuridik.se   # or https://www.hpjuridik.se (must match domain)

# Optional DB (recommended)
DATABASE_URL=postgresql+psycopg://...

# Optional (future)
ONEFLOW_API_TOKEN=...
ONEFLOW_WORKSPACE_ID=...
ONEFLOW_TEMPLATE_ID=...
ONEFLOW_WEBHOOK_SECRET=...
"""

from __future__ import annotations

import base64
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

# Optional DB (recommended)
try:
    from sqlalchemy import Boolean, Column, DateTime, String, Text, create_engine, select
    from sqlalchemy.orm import Session, declarative_base

    SQLA_AVAILABLE = True
except Exception:
    SQLA_AVAILABLE = False


# -----------------------------------------------------------------------------
# Config
# -----------------------------------------------------------------------------
POSTMARK_SERVER_TOKEN = os.getenv("POSTMARK_SERVER_TOKEN", "").strip()
MAIL_FROM = (os.getenv("MAIL_FROM", "").strip() or "lanabil@hpjuridik.se").strip()
CONTACT_TO = (os.getenv("CONTACT_TO", "").strip() or "hp@hpjuridik.se").strip()
LEAD_INBOX = (os.getenv("LEAD_INBOX", "").strip() or MAIL_FROM).strip()

STRIPE_SECRET_KEY = os.getenv("STRIPE_SECRET_KEY", "").strip()
STRIPE_WEBHOOK_SECRET = os.getenv("STRIPE_WEBHOOK_SECRET", "").strip()
STRIPE_PRICE_ID_PREMIUM = os.getenv("STRIPE_PRICE_ID_PREMIUM", "").strip()
PUBLIC_BASE_URL = (os.getenv("PUBLIC_BASE_URL", "").strip() or "https://hpjuridik.se").strip().rstrip("/")

DATABASE_URL = os.getenv("DATABASE_URL", "").strip()


def _require_env(name: str, value: str) -> None:
    if not value:
        raise RuntimeError(f"Missing environment variable: {name}")


def safe_id(prefix: str = "agr") -> str:
    return f"{prefix}_{secrets.token_hex(12)}"


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


# -----------------------------------------------------------------------------
# App + templates + static
# -----------------------------------------------------------------------------
app = FastAPI()

# Render often sends HEAD / as a health check.
@app.head("/", include_in_schema=False)
def _head_root() -> Response:
    return Response(status_code=200)


BASE_DIR = os.path.dirname(__file__)
TEMPLATES_DIR = os.path.join(BASE_DIR, "templates")
STATIC_DIR = os.path.join(BASE_DIR, "static")

if os.path.isdir(STATIC_DIR):
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

templates = Jinja2Templates(directory=TEMPLATES_DIR)


def page_ctx(request: Request, path: str, title: str, description: str = "") -> Dict[str, Any]:
    return {"request": request, "path": path, "title": title, "description": description}


# -----------------------------------------------------------------------------
# Domain model
# -----------------------------------------------------------------------------
@dataclass
class Agreement:
    id: str
    created_at: datetime
    status: str  # draft | free_downloaded | checkout_created | paid | emailed
    emailed_at: str = ""  # ISO string for idempotency marker

    # Utlånare
    utlanare_namn: str = ""
    utlanare_pnr: str = ""
    utlanare_adress: str = ""
    utlanare_tel: str = ""
    utlanare_epost: str = ""

    # Låntagare
    lantagare_namn: str = ""
    lantagare_pnr: str = ""
    lantagare_adress: str = ""
    lantagare_tel: str = ""
    lantagare_epost: str = ""

    # Fordon
    bil_marke_modell: str = ""
    bil_regnr: str = ""

    # Period
    from_dt: str = ""
    to_dt: str = ""
    andamal: str = ""

    # Marketing
    newsletter_optin: str = "false"

    # Stripe
    stripe_session_id: str = ""
    stripe_payment_status: str = ""


# -----------------------------------------------------------------------------
# Storage (DB if available, otherwise in-memory)
# -----------------------------------------------------------------------------
_agreements_mem: Dict[str, Agreement] = {}

Base = declarative_base() if SQLA_AVAILABLE else None

if SQLA_AVAILABLE and DATABASE_URL:
    class AgreementRow(Base):  # type: ignore[misc,valid-type]
        __tablename__ = "agreements"

        id = Column(String(64), primary_key=True)
        created_at = Column(DateTime(timezone=True), nullable=False)
        status = Column(String(32), nullable=False)
        emailed_at = Column(String(64), nullable=False, default="")

        stripe_session_id = Column(String(128), nullable=False, default="")
        stripe_payment_status = Column(String(32), nullable=False, default="")

        payload_json = Column(Text, nullable=False)  # full Agreement as JSON

        # simple marker for idempotency
        is_emailed = Column(Boolean, nullable=False, default=False)

    engine = create_engine(DATABASE_URL, pool_pre_ping=True)

    try:
        Base.metadata.create_all(engine)  # type: ignore[union-attr]
    except Exception:
        # Don't crash startup if DB temporarily unavailable
        pass


def _agreement_to_json(a: Agreement) -> str:
    d = asdict(a)
    d["created_at"] = a.created_at.isoformat()
    return json.dumps(d, ensure_ascii=False)


def _agreement_from_json(s: str) -> Agreement:
    d = json.loads(s)
    d["created_at"] = datetime.fromisoformat(d["created_at"])
    return Agreement(**d)


def upsert_agreement(a: Agreement) -> None:
    # DB path
    if SQLA_AVAILABLE and DATABASE_URL:
        try:
            with Session(engine) as db:  # type: ignore[name-defined]
                row = db.get(AgreementRow, a.id)  # type: ignore[name-defined]
                if row is None:
                    row = AgreementRow(  # type: ignore[name-defined]
                        id=a.id,
                        created_at=a.created_at,
                        status=a.status,
                        emailed_at=a.emailed_at or "",
                        stripe_session_id=a.stripe_session_id or "",
                        stripe_payment_status=a.stripe_payment_status or "",
                        payload_json=_agreement_to_json(a),
                        is_emailed=bool(a.emailed_at),
                    )
                    db.add(row)
                else:
                    row.created_at = a.created_at
                    row.status = a.status
                    row.emailed_at = a.emailed_at or ""
                    row.stripe_session_id = a.stripe_session_id or ""
                    row.stripe_payment_status = a.stripe_payment_status or ""
                    row.payload_json = _agreement_to_json(a)
                    row.is_emailed = bool(a.emailed_at)
                db.commit()
            return
        except Exception:
            # fallback to mem if DB fails
            _agreements_mem[a.id] = a
            return

    _agreements_mem[a.id] = a


def get_agreement(agreement_id: str) -> Optional[Agreement]:
    if SQLA_AVAILABLE and DATABASE_URL:
        try:
            with Session(engine) as db:  # type: ignore[name-defined]
                row = db.get(AgreementRow, agreement_id)  # type: ignore[name-defined]
                if not row:
                    return None
                return _agreement_from_json(row.payload_json)
        except Exception:
            return _agreements_mem.get(agreement_id)

    return _agreements_mem.get(agreement_id)


def get_agreement_by_session(session_id: str) -> Optional[Agreement]:
    if not session_id:
        return None

    if SQLA_AVAILABLE and DATABASE_URL:
        try:
            with Session(engine) as db:  # type: ignore[name-defined]
                stmt = select(AgreementRow).where(AgreementRow.stripe_session_id == session_id)  # type: ignore[name-defined]
                row = db.execute(stmt).scalars().first()
                if not row:
                    return None
                return _agreement_from_json(row.payload_json)
        except Exception:
            # fallback memory
            for a in _agreements_mem.values():
                if a.stripe_session_id == session_id:
                    return a
            return None

    for a in _agreements_mem.values():
        if a.stripe_session_id == session_id:
            return a
    return None


# -----------------------------------------------------------------------------
# Email (Postmark)
# -----------------------------------------------------------------------------
def postmark_send(
    *,
    to: str,
    subject: str,
    body_text: str,
    reply_to: str = "",
    attachments: Optional[list] = None,
) -> None:
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
# PDF generation (ReportLab) – includes standard terms + signature lines
# -----------------------------------------------------------------------------
def make_pdf_bytes(a: Agreement) -> bytes:
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    w, h = A4

    def write(x_mm: float, y_mm: float, text: str, size: int = 11):
        c.setFont("Helvetica", size)
        c.drawString(x_mm * mm, h - y_mm * mm, text)

    def write_multiline(x_mm: float, y_mm: float, text: str, size: int = 11, leading: int = 14):
        c.setFont("Helvetica", size)
        t = c.beginText(x_mm * mm, h - y_mm * mm)
        t.setLeading(leading)
        for line in (text or "").splitlines():
            t.textLine(line)
        c.drawText(t)

    y = 18
    write(20, y, "LÅNEAVTAL – BIL (TILLFÄLLIGT LÅN)", 16)
    y += 8
    write(20, y, f"Avtals-ID: {a.id}", 10)
    y += 5
    write(20, y, f"Avtalsdatum: {a.created_at.astimezone(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}", 10)

    y += 10
    write(20, y, "1. PARTER", 13)
    y += 7
    write(20, y, "UTLÅNARE (ÄGARE)", 11)
    y += 6
    write(25, y, f"Namn: {a.utlanare_namn}"); y += 5
    if a.utlanare_pnr:
        write(25, y, f"Personnummer: {a.utlanare_pnr}"); y += 5
    write(25, y, f"Adress: {a.utlanare_adress}"); y += 5
    write(25, y, f"Telefon: {a.utlanare_tel}"); y += 5
    write(25, y, f"E-post: {a.utlanare_epost}"); y += 7

    write(20, y, "LÅNTAGARE (SKULDSATT)", 11)
    y += 6
    write(25, y, f"Namn: {a.lantagare_namn}"); y += 5
    if a.lantagare_pnr:
        write(25, y, f"Personnummer: {a.lantagare_pnr}"); y += 5
    write(25, y, f"Adress: {a.lantagare_adress}"); y += 5
    write(25, y, f"Telefon: {a.lantagare_tel}"); y += 5
    write(25, y, f"E-post: {a.lantagare_epost}"); y += 8

    write(20, y, "2. FORDON", 13)
    y += 7
    write(25, y, f"Märke/Modell: {a.bil_marke_modell}"); y += 5
    write(25, y, f"Registreringsnummer: {a.bil_regnr}"); y += 8

    write(20, y, "3. AVTALSPERIOD", 13)
    y += 7
    write(25, y, f"Från: {a.from_dt}"); y += 5
    write(25, y, f"Till: {a.to_dt}"); y += 8

    write(20, y, "4. ÄNDAMÅL / SYFTE", 13)
    y += 7
    write_multiline(25, y, a.andamal, 11, 14)
    y += 22

    write(20, y, "5. STANDARDVILLKOR", 13)
    y += 7
    terms = (
        "a) Äganderätten till fordonet kvarstår hos utlånaren.\n"
        "b) Låntagaren får endast använda fordonet för angivet ändamål och under avtalsperioden.\n"
        "c) Låntagaren ansvarar för böter, avgifter och skador som uppstår under låneperioden, om inte annat avtalas.\n"
        "d) Fordonet ska återlämnas i väsentligen samma skick som vid utlämning (normalt slitage undantaget).\n"
        "e) Parterna ansvarar för att gällande försäkring finns. Eventuella självrisker regleras mellan parterna.\n"
        "f) Detta avtal utgör ett bevisunderlag som kan användas vid kontakt med myndigheter eller tredje part."
    )
    write_multiline(25, y, terms, 10, 13)
    y += 45

    write(20, y, "6. UNDERSKRIFTER", 13)
    y += 10
    write(20, y, "______________________________", 11)
    write(120, y, "______________________________", 11)
    y += 5
    write(20, y, "Utlånare (namnteckning)", 10)
    write(120, y, "Låntagare (namnteckning)", 10)

    y += 12
    write(20, y, "HP Juridik | hpjuridik.se | Kontakt: hp@hpjuridik.se", 9)

    c.showPage()
    c.save()
    return buf.getvalue()


# -----------------------------------------------------------------------------
# Routes – Pages (GET)
# -----------------------------------------------------------------------------
@app.get("/", response_class=HTMLResponse)
def home(request: Request):
    return templates.TemplateResponse("pages/home.html", page_ctx(request, "/", "HP Juridik"))


@app.get("/kontakta-oss", response_class=HTMLResponse)
@app.get("/kontakta-oss/", response_class=HTMLResponse)
def contact_page(request: Request):
    return templates.TemplateResponse("pages/contact.html", page_ctx(request, "/kontakta-oss", "Kontakt | HP Juridik"))


@app.get("/tjanster", response_class=HTMLResponse)
@app.get("/tjanster/", response_class=HTMLResponse)
@app.get("/services", response_class=HTMLResponse)
@app.get("/services/", response_class=HTMLResponse)
def services(request: Request):
    return templates.TemplateResponse("pages/services.html", page_ctx(request, "/tjanster", "Tjänster | HP Juridik"))


@app.get("/villkor", response_class=HTMLResponse)
@app.get("/villkor/", response_class=HTMLResponse)
@app.get("/terms", response_class=HTMLResponse)
@app.get("/terms/", response_class=HTMLResponse)
def terms(request: Request):
    return templates.TemplateResponse("pages/terms.html", page_ctx(request, "/villkor", "Villkor | HP Juridik"))


@app.get("/lana-bil-till-skuldsatt", response_class=HTMLResponse)
@app.get("/lana-bil-till-skuldsatt/", response_class=HTMLResponse)
def lana_bil_form(request: Request):
    return templates.TemplateResponse(
        "pages/lana_bil.html",
        page_ctx(request, "/lana-bil-till-skuldsatt", "Låna bil till skuldsatt | HP Juridik"),
    )


@app.get("/lana-bil-till-skuldsatt/review/{agreement_id}", response_class=HTMLResponse)
@app.get("/lana-bil-till-skuldsatt/review/{agreement_id}/", response_class=HTMLResponse)
def lana_bil_review_page(request: Request, agreement_id: str):
    a = get_agreement(agreement_id)
    if not a:
        return RedirectResponse("/lana-bil-till-skuldsatt", status_code=302)

    ctx = page_ctx(request, f"/lana-bil-till-skuldsatt/review/{agreement_id}", "Granska avtal | HP Juridik")
    ctx.update({"agreement": a, "agreement_id": agreement_id})
    return templates.TemplateResponse("pages/lana_bil_review.html", ctx)


@app.get("/checkout-success", response_class=HTMLResponse)
@app.get("/checkout-success/", response_class=HTMLResponse)
def checkout_success(request: Request, session_id: str = ""):
    ctx = page_ctx(request, "/checkout-success", "Tack! | HP Juridik")
    ctx.update({"session_id": session_id})
    return templates.TemplateResponse("pages/checkout_success.html", ctx)


@app.get("/checkout-cancel", response_class=HTMLResponse)
@app.get("/checkout-cancel/", response_class=HTMLResponse)
def checkout_cancel(request: Request):
    return templates.TemplateResponse("pages/checkout_cancel.html", page_ctx(request, "/checkout-cancel", "Avbruten betalning | HP Juridik"))


# -----------------------------------------------------------------------------
# Contact form (POST) – stays on SAME PAGE, shows success box
# -----------------------------------------------------------------------------
@app.post("/contact", response_class=HTMLResponse)
@app.post("/contact/", response_class=HTMLResponse)
@app.post("/kontakta-oss", response_class=HTMLResponse)
@app.post("/kontakta-oss/", response_class=HTMLResponse)
async def contact_submit(
    request: Request,
    namn: str = Form(...),
    epost: str = Form(...),
    telefon: str = Form(""),
    meddelande: str = Form(...),
):
    ts = now_utc().strftime("%Y-%m-%d %H:%M UTC")
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

    ok, err = True, ""
    try:
        postmark_send(to=CONTACT_TO, subject=subject, body_text=body, reply_to=epost)
    except Exception as e:
        ok, err = False, str(e)

    ctx = page_ctx(request, "/kontakta-oss", "Kontakt | HP Juridik")
    ctx.update({"sent": ok, "error": err, "name": namn})
    return templates.TemplateResponse("pages/contact.html", ctx)


# -----------------------------------------------------------------------------
# Låna bil – Start (POST) -> creates agreement + redirect to review/{id}
# -----------------------------------------------------------------------------
@app.post("/lana-bil-till-skuldsatt/start")
@app.post("/lana-bil-till-skuldsatt/start/")
async def lana_bil_start(
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
    # Checkboxes
    disclaimer_accept: str = Form(...),
    marketing_accept: str = Form(""),
):
    agreement_id = safe_id("agr")
    a = Agreement(
        id=agreement_id,
        created_at=now_utc(),
        status="draft",
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
    upsert_agreement(a)

    return RedirectResponse(f"/lana-bil-till-skuldsatt/review/{agreement_id}", status_code=303)


# -----------------------------------------------------------------------------
# Free (POST) – lead email + direct PDF download
# -----------------------------------------------------------------------------
@app.post("/lana-bil-till-skuldsatt/{agreement_id}/free")
@app.post("/lana-bil-till-skuldsatt/{agreement_id}/free/")
def lana_bil_free_download(request: Request, agreement_id: str):
    a = get_agreement(agreement_id)
    if not a:
        return Response("Not Found", status_code=404)

    # Generate PDF
    pdf_bytes = make_pdf_bytes(a)
    filename = f"laneavtal-bil-{agreement_id}.pdf"

    # Lead email (best-effort; download should still work even if mail fails)
    ts = now_utc().strftime("%Y-%m-%d %H:%M UTC")
    ip = request.client.host if request.client else "unknown"
    ua = request.headers.get("user-agent", "unknown")

    subject = "Lead: Låna bil till skuldsatt (Gratis)"
    body = (
        "NY LEAD (GRATIS)\n"
        "================\n\n"
        f"Tid: {ts}\n"
        f"Agreement ID: {a.id}\n"
        f"Status: free\n\n"
        f"Utlånare: {a.utlanare_namn} | {a.utlanare_epost} | {a.utlanare_tel}\n"
        f"Låntagare: {a.lantagare_namn} | {a.lantagare_epost} | {a.lantagare_tel}\n"
        f"Bil: {a.bil_marke_modell} | {a.bil_regnr}\n"
        f"Period: {a.from_dt} -> {a.to_dt}\n"
        f"Newsletter opt-in: {a.newsletter_optin}\n\n"
        f"---\nIP: {ip}\nUA: {ua}\n"
    )

    try:
        postmark_send(
            to=LEAD_INBOX,
            subject=subject,
            body_text=body,
            attachments=[pdf_attachment(filename, pdf_bytes)],
        )
    except Exception:
        pass

    # Update status
    if a.status == "draft":
        a.status = "free_downloaded"
        upsert_agreement(a)

    return StreamingResponse(
        io.BytesIO(pdf_bytes),
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# -----------------------------------------------------------------------------
# Premium (POST) – create Stripe Checkout session + redirect
# -----------------------------------------------------------------------------
@app.post("/lana-bil-till-skuldsatt/{agreement_id}/checkout")
@app.post("/lana-bil-till-skuldsatt/{agreement_id}/checkout/")
def lana_bil_create_checkout(agreement_id: str):
    _require_env("STRIPE_SECRET_KEY", STRIPE_SECRET_KEY)
    _require_env("STRIPE_PRICE_ID_PREMIUM", STRIPE_PRICE_ID_PREMIUM)

    a = get_agreement(agreement_id)
    if not a:
        return Response("Not Found", status_code=404)

    stripe.api_key = STRIPE_SECRET_KEY

    session = stripe.checkout.Session.create(
        mode="payment",
        line_items=[{"price": STRIPE_PRICE_ID_PREMIUM, "quantity": 1}],
        success_url=f"{PUBLIC_BASE_URL}/checkout-success?session_id={{CHECKOUT_SESSION_ID}}",
        cancel_url=f"{PUBLIC_BASE_URL}/checkout-cancel",
        metadata={"agreement_id": agreement_id},
    )

    a.stripe_session_id = session["id"]
    a.status = "checkout_created"
    a.stripe_payment_status = session.get("payment_status") or ""
    upsert_agreement(a)

    return RedirectResponse(session.url, status_code=303)


# -----------------------------------------------------------------------------
# Stripe webhook (POST) – idempotent, signature-verified
# -----------------------------------------------------------------------------
@app.post("/stripe/webhook")
@app.post("/stripe/webhook/")
async def stripe_webhook(request: Request):
    _require_env("STRIPE_WEBHOOK_SECRET", STRIPE_WEBHOOK_SECRET)

    payload = await request.body()
    sig_header = request.headers.get("stripe-signature", "")

    try:
        stripe.api_key = STRIPE_SECRET_KEY or None
        event = stripe.Webhook.construct_event(payload=payload, sig_header=sig_header, secret=STRIPE_WEBHOOK_SECRET)
    except Exception as e:
        return Response(f"Webhook error: {e}", status_code=400)

    event_type = event.get("type", "")

    if event_type == "checkout.session.completed":
        session = event["data"]["object"]
        session_id = session.get("id", "")
        agreement_id = ((session.get("metadata") or {}) or {}).get("agreement_id", "")

        a = get_agreement(agreement_id) if agreement_id else get_agreement_by_session(session_id)

        if not a:
            # Acknowledge anyway (Stripe will retry; but if agreement truly missing, retries won't help)
            return Response(status_code=200)

        # Idempotency: if we've already emailed, stop.
        if a.emailed_at:
            return Response(status_code=200)

        # Mark paid
        a.status = "paid"
        a.stripe_payment_status = session.get("payment_status") or "paid"
        a.stripe_session_id = a.stripe_session_id or session_id
        upsert_agreement(a)

        # Do the heavy work best-effort; never fail webhook ACK
        try:
            _handle_premium_paid(a)
        except Exception:
            pass

    return Response(status_code=200)


def _handle_premium_paid(a: Agreement) -> None:
    """After successful payment: generate PDF + email both parties + internal receipt.
    Must be idempotent (guarded by a.emailed_at).
    """
    # Re-fetch to reduce races
    fresh = get_agreement(a.id) or a
    if fresh.emailed_at:
        return

    pdf_bytes = make_pdf_bytes(fresh)
    filename = f"laneavtal-bil-{fresh.id}.pdf"
    attach = [pdf_attachment(filename, pdf_bytes)]

    subject_parties = "HP Juridik – Låneavtal (bil)"
    body_common = (
        "Här kommer ert låneavtal som PDF.\n\n"
        "Nästa steg: (Premium) Signering kommer via Oneflow i ett senare steg.\n"
        "Spara PDF:en för er dokumentation.\n"
    )

    # Email both parties
    postmark_send(to=fresh.utlanare_epost, subject=subject_parties, body_text=body_common, attachments=attach)
    postmark_send(to=fresh.lantagare_epost, subject=subject_parties, body_text=body_common, attachments=attach)

    # Internal notification
    internal = (
        "NY PREMIUM-ORDER\n"
        "==============\n\n"
        f"Agreement ID: {fresh.id}\n"
        f"Stripe session: {fresh.stripe_session_id or '-'}\n"
        f"Payment status: {fresh.stripe_payment_status or '-'}\n\n"
        f"Utlånare: {fresh.utlanare_namn} | {fresh.utlanare_epost} | {fresh.utlanare_tel}\n"
        f"Låntagare: {fresh.lantagare_namn} | {fresh.lantagare_epost} | {fresh.lantagare_tel}\n"
        f"Bil: {fresh.bil_marke_modell} | {fresh.bil_regnr}\n"
        f"Period: {fresh.from_dt} -> {fresh.to_dt}\n"
        f"Newsletter opt-in: {fresh.newsletter_optin}\n"
    )
    postmark_send(to=LEAD_INBOX, subject=f"Premium: Låna bil (betald) – {fresh.id}", body_text=internal, attachments=attach)

    # Mark emailed (idempotency flag)
    fresh.status = "emailed"
    fresh.emailed_at = now_utc().isoformat()
    upsert_agreement(fresh)
