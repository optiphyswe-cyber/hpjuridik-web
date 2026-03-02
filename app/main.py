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

# Optional DB (app funkar utan DB)
try:
    from sqlalchemy import Column, DateTime, String, Text, create_engine, select
    from sqlalchemy.orm import Session, declarative_base

    SQLA_AVAILABLE = True
except Exception:
    SQLA_AVAILABLE = False


# ---------------- Config ----------------
POSTMARK_SERVER_TOKEN = os.getenv("POSTMARK_SERVER_TOKEN", "").strip()
MAIL_FROM = (os.getenv("MAIL_FROM", "").strip() or "lanabil@hpjuridik.se").strip()
CONTACT_TO = (os.getenv("CONTACT_TO", "").strip() or "hp@hpjuridik.se").strip()
LEAD_INBOX = (os.getenv("LEAD_INBOX", "").strip() or MAIL_FROM).strip()

STRIPE_SECRET_KEY = os.getenv("STRIPE_SECRET_KEY", "").strip()
STRIPE_WEBHOOK_SECRET = os.getenv("STRIPE_WEBHOOK_SECRET", "").strip()
STRIPE_PRICE_ID_PREMIUM = (os.getenv("STRIPE_PRICE_ID_PREMIUM", "") or os.getenv("STRIPE_PRICE_ID", "")).strip()

PUBLIC_BASE_URL = (os.getenv("PUBLIC_BASE_URL", "") or os.getenv("BASE_URL", "") or "https://hpjuridik.se").strip().rstrip("/")

DATABASE_URL = os.getenv("DATABASE_URL", "").strip()


def _require_env(name: str, value: str) -> None:
    if not value:
        raise RuntimeError(f"Missing environment variable: {name}")


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def safe_id(prefix: str = "agr") -> str:
    return f"{prefix}_{secrets.token_hex(12)}"


# ---------------- App ----------------
app = FastAPI()

@app.head("/", include_in_schema=False)
def head_root() -> Response:
    return Response(status_code=200)

@app.get("/healthz", include_in_schema=False)
def healthz() -> Dict[str, bool]:
    return {"ok": True}


BASE_DIR = os.path.dirname(__file__)
TEMPLATES_DIR = os.path.join(BASE_DIR, "templates")
STATIC_DIR = os.path.join(BASE_DIR, "static")

if os.path.isdir(STATIC_DIR):
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

templates = Jinja2Templates(directory=TEMPLATES_DIR)

def ctx(request: Request, path: str, title: str) -> Dict[str, Any]:
    return {"request": request, "path": path, "title": title}


# ---------------- Data ----------------
@dataclass
class Agreement:
    id: str
    created_at: datetime

    utlanare_namn: str
    utlanare_pnr: str
    utlanare_adress: str
    utlanare_tel: str
    utlanare_epost: str

    lantagare_namn: str
    lantagare_pnr: str
    lantagare_adress: str
    lantagare_tel: str
    lantagare_epost: str

    bil_marke_modell: str
    bil_regnr: str

    from_dt: str
    to_dt: str
    andamal: str

    newsletter_optin: str = "false"

    stripe_session_id: str = ""
    stripe_payment_status: str = ""
    emailed_at: str = ""  # idempotency


_mem: Dict[str, Agreement] = {}

Base = declarative_base() if SQLA_AVAILABLE else None
if SQLA_AVAILABLE and DATABASE_URL:
    class AgreementRow(Base):  # type: ignore[misc,valid-type]
        __tablename__ = "agreements"
        id = Column(String(64), primary_key=True)
        created_at = Column(DateTime(timezone=True), nullable=False)
        payload_json = Column(Text, nullable=False)
        stripe_session_id = Column(String(128), nullable=False, default="")
        stripe_payment_status = Column(String(32), nullable=False, default="")
        emailed_at = Column(String(64), nullable=False, default="")

    engine = create_engine(DATABASE_URL, pool_pre_ping=True)
    try:
        Base.metadata.create_all(engine)  # type: ignore[union-attr]
    except Exception:
        pass


def _to_json(a: Agreement) -> str:
    d = asdict(a)
    d["created_at"] = a.created_at.isoformat()
    return json.dumps(d, ensure_ascii=False)

def _from_json(s: str) -> Agreement:
    d = json.loads(s)
    d["created_at"] = datetime.fromisoformat(d["created_at"])
    return Agreement(**d)

def save_agreement(a: Agreement) -> None:
    if SQLA_AVAILABLE and DATABASE_URL:
        try:
            with Session(engine) as db:  # type: ignore[name-defined]
                row = db.get(AgreementRow, a.id)  # type: ignore[name-defined]
                if row is None:
                    row = AgreementRow(  # type: ignore[name-defined]
                        id=a.id, created_at=a.created_at, payload_json=_to_json(a),
                        stripe_session_id=a.stripe_session_id or "",
                        stripe_payment_status=a.stripe_payment_status or "",
                        emailed_at=a.emailed_at or "",
                    )
                    db.add(row)
                else:
                    row.created_at = a.created_at
                    row.payload_json = _to_json(a)
                    row.stripe_session_id = a.stripe_session_id or ""
                    row.stripe_payment_status = a.stripe_payment_status or ""
                    row.emailed_at = a.emailed_at or ""
                db.commit()
            return
        except Exception:
            _mem[a.id] = a
            return
    _mem[a.id] = a

def load_agreement(agreement_id: str) -> Optional[Agreement]:
    if SQLA_AVAILABLE and DATABASE_URL:
        try:
            with Session(engine) as db:  # type: ignore[name-defined]
                row = db.get(AgreementRow, agreement_id)  # type: ignore[name-defined]
                return _from_json(row.payload_json) if row else None
        except Exception:
            return _mem.get(agreement_id)
    return _mem.get(agreement_id)

def load_by_session(session_id: str) -> Optional[Agreement]:
    if not session_id:
        return None
    if SQLA_AVAILABLE and DATABASE_URL:
        try:
            with Session(engine) as db:  # type: ignore[name-defined]
                stmt = select(AgreementRow).where(AgreementRow.stripe_session_id == session_id)  # type: ignore[name-defined]
                row = db.execute(stmt).scalars().first()
                return _from_json(row.payload_json) if row else None
        except Exception:
            pass
    for a in _mem.values():
        if a.stripe_session_id == session_id:
            return a
    return None


# ---------------- Postmark ----------------
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
    return {"Name": filename, "Content": base64.b64encode(pdf_bytes).decode("ascii"), "ContentType": "application/pdf"}


# ---------------- PDF ----------------
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
        for line in (text or "").splitlines() or [""]:
            t.textLine(line)
        c.drawText(t)

    y = 18
    write(20, y, "LÅNEAVTAL – BIL (TILLFÄLLIGT LÅN)", 16)
    y += 8
    write(20, y, f"Avtals-ID: {a.id}", 10)
    y += 5
    write(20, y, f"Avtalsdatum: {a.created_at.astimezone(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}", 10)

    y += 10
    write(20, y, "1. PARTER", 13); y += 7
    write(20, y, "UTLÅNARE (ÄGARE)", 11); y += 6
    write(25, y, f"Namn: {a.utlanare_namn}"); y += 5
    if a.utlanare_pnr:
        write(25, y, f"Personnummer: {a.utlanare_pnr}"); y += 5
    write(25, y, f"Adress: {a.utlanare_adress}"); y += 5
    write(25, y, f"Telefon: {a.utlanare_tel}"); y += 5
    write(25, y, f"E-post: {a.utlanare_epost}"); y += 7

    write(20, y, "LÅNTAGARE (SKULDSATT)", 11); y += 6
    write(25, y, f"Namn: {a.lantagare_namn}"); y += 5
    if a.lantagare_pnr:
        write(25, y, f"Personnummer: {a.lantagare_pnr}"); y += 5
    write(25, y, f"Adress: {a.lantagare_adress}"); y += 5
    write(25, y, f"Telefon: {a.lantagare_tel}"); y += 5
    write(25, y, f"E-post: {a.lantagare_epost}"); y += 8

    write(20, y, "2. FORDON", 13); y += 7
    write(25, y, f"Märke/Modell: {a.bil_marke_modell}"); y += 5
    write(25, y, f"Registreringsnummer: {a.bil_regnr}"); y += 8

    write(20, y, "3. AVTALSPERIOD", 13); y += 7
    write(25, y, f"Från: {a.from_dt}"); y += 5
    write(25, y, f"Till: {a.to_dt}"); y += 8

    write(20, y, "4. ÄNDAMÅL / SYFTE", 13); y += 7
    write_multiline(25, y, a.andamal, 11, 14)
    y += 22

    write(20, y, "5. STANDARDVILLKOR", 13); y += 7
    terms = (
        "a) Äganderätten till fordonet kvarstår hos utlånaren.\n"
        "b) Låntagaren får endast använda fordonet för angivet ändamål och under avtalsperioden.\n"
        "c) Låntagaren ansvarar för böter, avgifter och skador som uppstår under låneperioden.\n"
        "d) Fordonet ska återlämnas i väsentligen samma skick (normalt slitage undantaget).\n"
        "e) Parterna ansvarar för att gällande försäkring finns. Eventuella självrisker regleras mellan parterna.\n"
    )
    write_multiline(25, y, terms, 10, 13)
    y += 45

    write(20, y, "6. UNDERSKRIFTER", 13); y += 10
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


# ---------------- Routes: pages ----------------
@app.get("/", response_class=HTMLResponse)
def home(request: Request):
    return templates.TemplateResponse("pages/home.html", ctx(request, "/", "HP Juridik"))

@app.get("/kontakta-oss", response_class=HTMLResponse)
@app.get("/contact", response_class=HTMLResponse)
def contact_page(request: Request):
    return templates.TemplateResponse("pages/contact.html", ctx(request, "/kontakta-oss", "Kontakt | HP Juridik"))

@app.get("/tjanster", response_class=HTMLResponse)
@app.get("/services", response_class=HTMLResponse)
def services_page(request: Request):
    return templates.TemplateResponse("pages/services.html", ctx(request, "/tjanster", "Tjänster | HP Juridik"))

@app.get("/villkor", response_class=HTMLResponse)
@app.get("/terms", response_class=HTMLResponse)
def terms_page(request: Request):
    return templates.TemplateResponse("pages/terms.html", ctx(request, "/villkor", "Villkor | HP Juridik"))

@app.get("/lana-bil-till-skuldsatt", response_class=HTMLResponse)
def lana_bil_form(request: Request):
    return templates.TemplateResponse("pages/lana_bil.html", ctx(request, "/lana-bil-till-skuldsatt", "Låna bil till skuldsatt | HP Juridik"))

@app.get("/lana-bil-till-skuldsatt/review", response_class=HTMLResponse)
def lana_bil_review_get(_: Request):
    return RedirectResponse("/lana-bil-till-skuldsatt", status_code=302)


# ---------------- Contact POST (no redirect) ----------------
@app.post("/contact", response_class=HTMLResponse)
@app.post("/kontakta-oss", response_class=HTMLResponse)
async def contact_submit(
    request: Request,
    namn: str = Form(...),
    epost: str = Form(...),
    telefon: str = Form(""),
    meddelande: str = Form(...),
):
    subject = f"HP Juridik | Ny kontaktförfrågan från {namn}"
    body = (
        "NY KONTAKTFÖRFRÅGAN (HPJURIDIK.SE)\n\n"
        f"Tid: {now_utc().strftime('%Y-%m-%d %H:%M UTC')}\n"
        f"Namn: {namn}\nE-post: {epost}\nTelefon: {telefon or '-'}\n\n"
        f"Meddelande:\n{meddelande}\n"
    )

    ok, err = True, ""
    try:
        postmark_send(to=CONTACT_TO, subject=subject, body_text=body, reply_to=epost)
    except Exception as e:
        ok, err = False, str(e)

    # Returnerar HTML direkt -> ingen redirect
    c = ctx(request, "/kontakta-oss", "Kontakt | HP Juridik")
    c.update({"sent": ok, "error": err, "name": namn})
    return templates.TemplateResponse("pages/contact.html", c)


# ---------------- Låna bil POST (kompatibel med dina actions) ----------------
@app.post("/lana-bil-till-skuldsatt", response_class=HTMLResponse)
@app.post("/lana-bil-till-skuldsatt/review", response_class=HTMLResponse)
@app.post("/lana-bil-till-skuldsatt/start", response_class=HTMLResponse)
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

    disclaimer_accept: str = Form(...),
    marketing_accept: str = Form(""),
):
    agreement_id = safe_id("agr")
    a = Agreement(
        id=agreement_id,
        created_at=now_utc(),
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
    save_agreement(a)

    c = ctx(request, "/lana-bil-till-skuldsatt/review", "Granska avtal | HP Juridik")
    c.update({"agreement": a, "agreement_id": agreement_id, "premium_price_sek": 150})
    return templates.TemplateResponse("pages/lana_bil_review.html", c)


# ---------------- Gratis: lead + PDF download ----------------
@app.post("/lana-bil-till-skuldsatt/free")
def lana_bil_free(request: Request, agreement_id: str = Form(...)):
    a = load_agreement(agreement_id)
    if not a:
        return Response("Not Found", status_code=404)

    pdf_bytes = make_pdf_bytes(a)
    filename = f"laneavtal-bil-{a.id}.pdf"

    # lead mail (best effort)
    try:
        postmark_send(
            to=LEAD_INBOX,
            subject="Lead: Låna bil till skuldsatt (Gratis)",
            body_text=f"Agreement ID: {a.id}\nUtlånare: {a.utlanare_namn} ({a.utlanare_epost})\nLåntagare: {a.lantagare_namn} ({a.lantagare_epost})\nRegnr: {a.bil_regnr}\n",
            attachments=[pdf_attachment(filename, pdf_bytes)],
        )
    except Exception:
        pass

    return StreamingResponse(
        io.BytesIO(pdf_bytes),
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# ---------------- Premium: checkout + webhook (valfritt) ----------------
@app.post("/lana-bil-till-skuldsatt/checkout")
def lana_bil_checkout(agreement_id: str = Form(...)):
    _require_env("STRIPE_SECRET_KEY", STRIPE_SECRET_KEY)
    _require_env("STRIPE_PRICE_ID_PREMIUM", STRIPE_PRICE_ID_PREMIUM)

    a = load_agreement(agreement_id)
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
    a.stripe_payment_status = session.get("payment_status") or ""
    save_agreement(a)

    return RedirectResponse(session.url, status_code=303)


@app.get("/checkout-success", response_class=HTMLResponse)
def checkout_success(request: Request, session_id: str = ""):
    return templates.TemplateResponse("pages/checkout_success.html", {**ctx(request, "/checkout-success", "Tack! | HP Juridik"), "session_id": session_id})

@app.get("/checkout-cancel", response_class=HTMLResponse)
def checkout_cancel(request: Request):
    return templates.TemplateResponse("pages/checkout_cancel.html", ctx(request, "/checkout-cancel", "Avbruten betalning | HP Juridik"))


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
        session_id = session.get("id", "")
        agreement_id = ((session.get("metadata") or {}) or {}).get("agreement_id", "")

        a = load_agreement(agreement_id) if agreement_id else load_by_session(session_id)
        if not a:
            return Response(status_code=200)

        if a.emailed_at:
            return Response(status_code=200)

        a.stripe_payment_status = session.get("payment_status") or "paid"
        a.emailed_at = now_utc().isoformat()
        save_agreement(a)

        # maila PDF till båda + intern kopia (best effort)
        try:
            pdf_bytes = make_pdf_bytes(a)
            fn = f"laneavtal-bil-{a.id}.pdf"
            attach = [pdf_attachment(fn, pdf_bytes)]
            postmark_send(to=a.utlanare_epost, subject="HP Juridik – Låneavtal (bil)", body_text="Här kommer ert låneavtal som PDF.", attachments=attach)
            postmark_send(to=a.lantagare_epost, subject="HP Juridik – Låneavtal (bil)", body_text="Här kommer ert låneavtal som PDF.", attachments=attach)
            postmark_send(to=LEAD_INBOX, subject=f"Premium: Låna bil (betald) – {a.id}", body_text=f"Agreement ID: {a.id}", attachments=attach)
        except Exception:
            pass

    return Response(status_code=200)
