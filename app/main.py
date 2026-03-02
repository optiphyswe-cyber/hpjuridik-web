import os
import io
import json
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, Optional, List

import httpx
import stripe
from fastapi import FastAPI, Request, Form, HTTPException
from fastapi.responses import (
    HTMLResponse,
    PlainTextResponse,
    StreamingResponse,
)
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import RedirectResponse

# PDF (ReportLab)
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle
from reportlab.lib import colors


# =============================================================================
# App + templates
# =============================================================================

app = FastAPI()

# Render health checks often use HEAD /
@app.head("/", include_in_schema=False)
def head_root():
    return PlainTextResponse("ok")


# robust paths (works on Render)
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(BASE_DIR, "static")
TEMPLATES_DIR = os.path.join(BASE_DIR, "templates")

app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
templates = Jinja2Templates(directory=TEMPLATES_DIR)


# =============================================================================
# Environment / settings
# =============================================================================

CANONICAL_HOST = os.getenv("CANONICAL_HOST", "www.hpjuridik.se").strip().lower()
SITE_URL = os.getenv("SITE_URL", f"https://{CANONICAL_HOST}").rstrip("/")
BASE_URL = os.getenv("BASE_URL", SITE_URL).rstrip("/")

# Postmark (API, not SMTP)
POSTMARK_SERVER_TOKEN = os.getenv("POSTMARK_SERVER_TOKEN", "").strip()
MAIL_FROM = os.getenv("MAIL_FROM", "lanabil@hpjuridik.se").strip()
LEAD_INBOX = os.getenv("LEAD_INBOX", "lanabil@hpjuridik.se").strip()
CONTACT_TO = os.getenv("CONTACT_TO", "hp@hpjuridik.se").strip()  # for contact form

# Stripe
STRIPE_SECRET_KEY = os.getenv("STRIPE_SECRET_KEY", "").strip()
STRIPE_WEBHOOK_SECRET = os.getenv("STRIPE_WEBHOOK_SECRET", "").strip()
stripe.api_key = STRIPE_SECRET_KEY

# Storage (MVP)
AGREEMENTS_DIR = os.getenv("AGREEMENTS_DIR", "/tmp/agreements")
os.makedirs(AGREEMENTS_DIR, exist_ok=True)


# =============================================================================
# Company info (single source of truth)
# =============================================================================

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


# =============================================================================
# Middleware: force https + force canonical host
# =============================================================================

class CanonicalRedirectMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        proto = (request.headers.get("x-forwarded-proto") or request.url.scheme).lower()
        host = (request.headers.get("x-forwarded-host") or request.url.hostname or "").lower()

        path = request.url.path
        query = request.url.query
        suffix = f"?{query}" if query else ""

        if proto != "https":
            target = f"https://{host}{path}{suffix}"
            return RedirectResponse(url=target, status_code=301)

        if host and host != CANONICAL_HOST:
            target = f"https://{CANONICAL_HOST}{path}{suffix}"
            return RedirectResponse(url=target, status_code=301)

        return await call_next(request)

app.add_middleware(CanonicalRedirectMiddleware)


# =============================================================================
# Helpers: SEO + context
# =============================================================================

def seo(path: str, title: str, description: str):
    canonical_url = f"{SITE_URL}{path}"
    return {
        "title": title,
        "description": description,
        "canonical": canonical_url,
        "robots": "index, follow",
    }

def page_ctx(request: Request, path: str, title: str, desc: str):
    return {
        "request": request,
        "seo": seo(path, title, desc),
        "company": COMPANY,
    }


# =============================================================================
# Helpers: storage (MVP /tmp)
# =============================================================================

def _agreement_path(agreement_id: str) -> str:
    return os.path.join(AGREEMENTS_DIR, f"{agreement_id}.json")

def save_agreement(agreement_id: str, data: Dict[str, Any]) -> None:
    tmp_path = _agreement_path(agreement_id) + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
    os.replace(tmp_path, _agreement_path(agreement_id))

def load_agreement(agreement_id: str) -> Dict[str, Any]:
    p = _agreement_path(agreement_id)
    if not os.path.exists(p):
        raise HTTPException(status_code=404, detail="agreement_id not found")
    with open(p, "r", encoding="utf-8") as f:
        return json.load(f)


# =============================================================================
# Helpers: normalization + parsing
# =============================================================================

def norm_regnr(s: str) -> str:
    return (s or "").replace(" ", "").upper()

def parse_dt_local(s: str) -> datetime:
    """
    HTML datetime-local -> 'YYYY-MM-DDTHH:MM'
    We treat it as local time; store ISO string as given (no timezone math).
    """
    s = (s or "").strip()
    if not s:
        raise ValueError("missing datetime")
    # fromisoformat handles YYYY-MM-DDTHH:MM
    return datetime.fromisoformat(s)

def sv_dt(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d kl. %H:%M")


# =============================================================================
# Postmark email (API)
# =============================================================================

async def postmark_send(to: str, subject: str, text_body: str, html_body: Optional[str] = None, reply_to: Optional[str] = None) -> None:
    if not POSTMARK_SERVER_TOKEN:
        raise RuntimeError("POSTMARK_SERVER_TOKEN saknas (måste vara Server API token).")

    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "X-Postmark-Server-Token": POSTMARK_SERVER_TOKEN,
    }
    payload: Dict[str, Any] = {
        "From": MAIL_FROM,
        "To": to,
        "Subject": subject,
        "TextBody": text_body,
    }
    if reply_to:
        payload["ReplyTo"] = reply_to
    if html_body:
        payload["HtmlBody"] = html_body

    async with httpx.AsyncClient(timeout=20) as client:
        r = await client.post("https://api.postmarkapp.com/email", headers=headers, json=payload)
        r.raise_for_status()


# =============================================================================
# PDF: Låna bil (ReportLab)
# =============================================================================

def _safe(s: str) -> str:
    return (s or "").strip()

def build_loan_pdf(
    *,
    utlanare: dict,
    lantagare: dict,
    fordon: dict,
    period: dict,
    andamal: str,
    ort: str = "Lund",
) -> bytes:
    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf,
        pagesize=A4,
        leftMargin=18 * mm,
        rightMargin=18 * mm,
        topMargin=16 * mm,
        bottomMargin=16 * mm,
        title="Tillfälligt låneavtal – bil",
        author=COMPANY.get("brand", ""),
    )

    styles = getSampleStyleSheet()
    title = ParagraphStyle("Title", parent=styles["Title"], fontSize=18, leading=22, spaceAfter=10)
    h = ParagraphStyle("H", parent=styles["Heading2"], fontSize=12.5, leading=15, spaceBefore=10, spaceAfter=6)
    body = ParagraphStyle("Body", parent=styles["BodyText"], fontSize=10.5, leading=14, spaceAfter=6)
    small = ParagraphStyle("Small", parent=styles["BodyText"], fontSize=9.5, leading=12.5, spaceAfter=4)

    def P(text: str, st=body):
        text = _safe(text).replace("\n", "<br/>")
        return Paragraph(text, st)

    story: List[Any] = []
    story.append(Paragraph("TILLFÄLLIGT LÅNEAVTAL – BIL", title))
    story.append(P("Detta avtal upprättas för att tydliggöra villkoren för ett tillfälligt lån av fordon.", small))

    # Parter
    story.append(Paragraph("1. Parter", h))
    tdata = [
        ["Utlånare", ""],
        ["Namn", _safe(utlanare.get("namn"))],
        ["Personnr (valfritt)", _safe(utlanare.get("pnr"))],
        ["Adress", _safe(utlanare.get("adress"))],
        ["Telefon", _safe(utlanare.get("tel"))],
        ["E-post", _safe(utlanare.get("epost"))],
        ["", ""],
        ["Låntagare", ""],
        ["Namn", _safe(lantagare.get("namn"))],
        ["Personnr (valfritt)", _safe(lantagare.get("pnr"))],
        ["Adress", _safe(lantagare.get("adress"))],
        ["Telefon", _safe(lantagare.get("tel"))],
        ["E-post", _safe(lantagare.get("epost"))],
    ]
    table = Table(tdata, colWidths=[55 * mm, 120 * mm])
    table.setStyle(
        TableStyle(
            [
                ("FONTNAME", (0, 0), (-1, -1), "Helvetica"),
                ("FONTSIZE", (0, 0), (-1, -1), 10),
                ("BACKGROUND", (0, 0), (-1, 0), colors.whitesmoke),
                ("BACKGROUND", (0, 7), (-1, 7), colors.whitesmoke),
                ("LINEBELOW", (0, 0), (-1, 0), 0.5, colors.grey),
                ("LINEBELOW", (0, 7), (-1, 7), 0.5, colors.grey),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
            ]
        )
    )
    story.append(table)

    # Fordon
    story.append(Paragraph("2. Fordon", h))
    story.append(P(f"Märke/Modell: {_safe(fordon.get('modell'))}"))
    story.append(P(f"Registreringsnummer: {_safe(fordon.get('regnr'))}"))

    # Period
    story.append(Paragraph("3. Låneperiod", h))
    story.append(P(f"Från: {_safe(period.get('from_str'))}"))
    story.append(P(f"Till: {_safe(period.get('to_str'))}"))

    # Ändamål
    story.append(Paragraph("4. Ändamål", h))
    story.append(P(_safe(andamal) or "—"))

    # Villkor / friskrivning (kort)
    story.append(Paragraph("5. Ansvar och villkor (översikt)", h))
    story.append(
        P(
            "Låntagaren ansvarar för fordonet under låneperioden och ska ersätta skador som uppstår genom vårdslöshet "
            "eller otillåten användning. Parterna ansvarar själva för att kontrollera försäkring, körkortsbehörighet och "
            "övriga relevanta förutsättningar.",
            body,
        )
    )

    # Underskrifter
    story.append(Spacer(1, 8))
    story.append(Paragraph("6. Underskrifter", h))
    story.append(
        P(
            f"Ort och datum: {ort}, {datetime.now().strftime('%Y-%m-%d')}\n\n"
            "Utlånare: ____________________________\n\n"
            "Låntagare: ____________________________",
            body,
        )
    )

    doc.build(story)
    return buf.getvalue()


# =============================================================================
# Stripe helpers
# =============================================================================

def create_checkout_session(agreement_id: str) -> str:
    if not STRIPE_SECRET_KEY:
        raise RuntimeError("STRIPE_SECRET_KEY saknas i Render env.")
    session = stripe.checkout.Session.create(
        mode="payment",
        line_items=[{
            "price_data": {
                "currency": "sek",
                "product_data": {"name": "Premium – Låna bil till skuldsatt"},
                "unit_amount": 15000,  # 150 kr
            },
            "quantity": 1,
        }],
        metadata={"agreement_id": agreement_id},
        success_url=f"{BASE_URL}/checkout-success?session_id={{CHECKOUT_SESSION_ID}}",
        cancel_url=f"{BASE_URL}/checkout-cancel",
    )
    return session.url


# =============================================================================
# Core routes
# =============================================================================

@app.get("/healthz")
def healthz():
    return {"ok": True}


@app.get("/", response_class=HTMLResponse)
def home(request: Request):
    return templates.TemplateResponse(
        "pages/home.html",
        page_ctx(request, "/", "HP Juridik", "Juridisk hjälp och dokument."),
    )


@app.get("/tjanster", response_class=HTMLResponse)
@app.get("/services", response_class=HTMLResponse)
def services(request: Request):
    return templates.TemplateResponse(
        "pages/services.html",
        page_ctx(request, "/services", "Tjänster | HP Juridik", "Tjänster och juridisk hjälp."),
    )


@app.get("/villkor", response_class=HTMLResponse)
@app.get("/terms", response_class=HTMLResponse)
def terms(request: Request):
    return templates.TemplateResponse(
        "pages/terms.html",
        page_ctx(request, "/terms", "Villkor | HP Juridik", "Villkor."),
    )


@app.get("/kontakta-oss", response_class=HTMLResponse)
@app.get("/contact", response_class=HTMLResponse)
def contact(request: Request):
    return templates.TemplateResponse(
        "pages/contact.html",
        page_ctx(request, "/contact", "Kontakt | HP Juridik", "Kontakta HP Juridik."),
    )


@app.post("/kontakta-oss", response_class=HTMLResponse)
@app.post("/contact", response_class=HTMLResponse)
async def contact_submit(
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
        "NY KONTAKTFÖRFRÅGAN (HPJURIDIK.SE)\n"
        "====================================\n\n"
        f"Namn: {namn}\n"
        f"E-post: {epost}\n"
        f"Telefon: {telefon or 'Ej angivet'}\n\n"
        "MEDDELANDE\n"
        "------------------------------------\n"
        f"{meddelande}\n\n"
        "TEKNISK INFO\n"
        "------------------------------------\n"
        f"Tid: {ts}\n"
        f"IP: {ip}\n"
        f"User-Agent: {ua}\n"
    )

    try:
        await postmark_send(CONTACT_TO, subject, body, reply_to=epost)
        ok = True
        err = None
    except Exception as e:
        ok = False
        err = str(e)

    ctx = page_ctx(request, "/contact", "Kontakt | HP Juridik", "Kontakta HP Juridik.")
    ctx.update({"sent_ok": ok, "sent_error": err})
    return templates.TemplateResponse("pages/contact.html", ctx)


# =============================================================================
# Låna bil – form → review → free/paid
# =============================================================================

@app.get("/lana-bil-till-skuldsatt", response_class=HTMLResponse)
def lana_bil_form(request: Request):
    return templates.TemplateResponse(
        "pages/lana_bil.html",
        page_ctx(
            request,
            "/lana-bil-till-skuldsatt",
            "Låna bil till skuldsatt | HP Juridik",
            "Skapa tillfälligt låneavtal för bil.",
        ),
    )


@app.post("/lana-bil-till-skuldsatt", response_class=HTMLResponse)
@app.post("/lana-bil-till-skuldsatt/review", response_class=HTMLResponse)
def lana_bil_review(
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
    fordon_modell: str = Form(...),
    fordon_regnr: str = Form(...),
    # Period
    from_dt: str = Form(...),
    to_dt: str = Form(...),
    # Övrigt
    andamal: str = Form(""),
    disclaimer_accept: Optional[str] = Form(None),
    newsletter_optin: Optional[str] = Form(None),
):
    # validate & normalize
    try:
        from_obj = parse_dt_local(from_dt)
        to_obj = parse_dt_local(to_dt)
    except Exception:
        raise HTTPException(status_code=400, detail="Ogiltigt datumformat.")

    if to_obj <= from_obj:
        raise HTTPException(status_code=400, detail="Slutdatum måste vara efter startdatum.")

    regnr = norm_regnr(fordon_regnr)

    agreement_id = str(uuid.uuid4())
    ip = request.client.host if request.client else "unknown"
    ua = request.headers.get("user-agent", "unknown")

    payload = {
        "id": agreement_id,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "status": "draft",
        "ip": ip,
        "user_agent": ua,
        "form_payload": {
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
                "regnr": regnr,
            },
            "period": {
                "from_raw": from_dt,
                "to_raw": to_dt,
                "from_str": sv_dt(from_obj),
                "to_str": sv_dt(to_obj),
            },
            "andamal": andamal,
            "disclaimer_accept": bool(disclaimer_accept),
            "newsletter_optin": bool(newsletter_optin),
        },
    }

    save_agreement(agreement_id, payload)

    ctx = page_ctx(
        request,
        "/lana-bil-till-skuldsatt",
        "Granska uppgifter | HP Juridik",
        "Granska uppgifter innan du fortsätter.",
    )
    ctx.update({
        "agreement_id": agreement_id,
        "data": payload["form_payload"],
    })
    return templates.TemplateResponse("pages/lana_bil_review.html", ctx)


@app.post("/lana-bil-till-skuldsatt/free")
async def lana_bil_free(
    request: Request,
    agreement_id: str = Form(...),
    confirm_correct: Optional[str] = Form(None),
    disclaimer_accept: Optional[str] = Form(None),
):
    if not confirm_correct:
        raise HTTPException(status_code=400, detail="Du måste bekräfta att uppgifterna är korrekta.")
    if not disclaimer_accept:
        raise HTTPException(status_code=400, detail="Du måste acceptera friskrivningen.")

    ag = load_agreement(agreement_id)
    ag["status"] = "free_downloaded"
    ag["confirm_correct_at"] = datetime.now(timezone.utc).isoformat()
    ag["disclaimer_accepted_at"] = datetime.now(timezone.utc).isoformat()
    save_agreement(agreement_id, ag)

    fp = ag["form_payload"]
    pdf_bytes = build_loan_pdf(
        utlanare=fp["utlanare"],
        lantagare=fp["lantagare"],
        fordon=fp["fordon"],
        period=fp["period"],
        andamal=fp.get("andamal", ""),
    )

    # Lead-mail ONLY (no PDFs to parties)
    subject = "Lead: Låna bil till skuldsatt (Gratis nedladdning)"
    body = (
        "NY LEAD (GRATIS)\n"
        "=================\n\n"
        f"Agreement ID: {agreement_id}\n"
        f"Utlånare: {fp['utlanare']['namn']} – {fp['utlanare']['epost']}\n"
        f"Låntagare: {fp['lantagare']['namn']} – {fp['lantagare']['epost']}\n"
        f"Regnr: {fp['fordon']['regnr']}\n"
        f"Period: {fp['period']['from_str']} → {fp['period']['to_str']}\n\n"
        f"Newsletter opt-in: {fp.get('newsletter_optin', False)}\n"
        f"IP: {ag.get('ip')}\n"
        f"UA: {ag.get('user_agent')}\n"
    )
    try:
        await postmark_send(LEAD_INBOX, subject, body)
    except Exception:
        # Don't block download if lead mail fails; log via print
        print("Postmark lead mail failed for agreement:", agreement_id)

    filename = "laneavtal-bil.pdf"
    return StreamingResponse(
        io.BytesIO(pdf_bytes),
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.post("/lana-bil-till-skuldsatt/paid")
def lana_bil_paid(
    agreement_id: str = Form(...),
    confirm_correct: Optional[str] = Form(None),
    disclaimer_accept: Optional[str] = Form(None),
):
    if not confirm_correct:
        raise HTTPException(status_code=400, detail="Du måste bekräfta att uppgifterna är korrekta.")
    if not disclaimer_accept:
        raise HTTPException(status_code=400, detail="Du måste acceptera friskrivningen.")

    ag = load_agreement(agreement_id)
    ag["status"] = "paid_pending"
    ag["confirm_correct_at"] = datetime.now(timezone.utc).isoformat()
    ag["disclaimer_accepted_at"] = datetime.now(timezone.utc).isoformat()
    save_agreement(agreement_id, ag)

    checkout_url = create_checkout_session(agreement_id)
    return RedirectResponse(checkout_url, status_code=303)


@app.get("/checkout-success", response_class=HTMLResponse)
def checkout_success(request: Request):
    # webhook does the real work
    try:
        return templates.TemplateResponse(
            "pages/checkout_success.html",
            page_ctx(request, "/checkout-success", "Tack!", "Betalning mottagen."),
        )
    except Exception:
        return HTMLResponse("<h1>Tack!</h1><p>Betalning mottagen. (Webhooken sköter resten.)</p>")


@app.get("/checkout-cancel", response_class=HTMLResponse)
def checkout_cancel(request: Request):
    try:
        return templates.TemplateResponse(
            "pages/checkout_cancel.html",
            page_ctx(request, "/checkout-cancel", "Avbrutet", "Betalning avbruten."),
        )
    except Exception:
        return HTMLResponse("<h1>Avbrutet</h1><p>Betalningen avbröts.</p>")


# =============================================================================
# Stripe webhook
# =============================================================================

@app.post("/stripe/webhook")
async def stripe_webhook(request: Request):
    payload = await request.body()
    sig = request.headers.get("stripe-signature", "")

    if not STRIPE_WEBHOOK_SECRET:
        raise HTTPException(status_code=500, detail="STRIPE_WEBHOOK_SECRET saknas.")

    try:
        event = stripe.Webhook.construct_event(payload, sig, STRIPE_WEBHOOK_SECRET)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid webhook signature")

    if event["type"] == "checkout.session.completed":
        session = event["data"]["object"]
        if session.get("payment_status") == "paid":
            agreement_id = (session.get("metadata") or {}).get("agreement_id")
            session_id = session.get("id")

            if agreement_id:
                try:
                    ag = load_agreement(agreement_id)
                    ag["status"] = "paid"
                    ag["stripe_session_id"] = session_id
                    ag["paid_at"] = datetime.now(timezone.utc).isoformat()
                    save_agreement(agreement_id, ag)

                    # Internal premium notification (Scrive later)
                    fp = ag["form_payload"]
                    subject = "Premium: Betalning mottagen (Stripe)"
                    body = (
                        "PREMIUM BETALD ✅\n"
                        "=================\n\n"
                        f"Agreement ID: {agreement_id}\n"
                        f"Stripe session: {session_id}\n"
                        f"Utlånare: {fp['utlanare']['namn']} – {fp['utlanare']['epost']}\n"
                        f"Låntagare: {fp['lantagare']['namn']} – {fp['lantagare']['epost']}\n"
                        f"Regnr: {fp['fordon']['regnr']}\n"
                        f"Period: {fp['period']['from_str']} → {fp['period']['to_str']}\n"
                    )
                    try:
                        await postmark_send(LEAD_INBOX, subject, body)
                    except Exception:
                        print("Postmark premium notify failed for:", agreement_id)

                except Exception as e:
                    print("Webhook update failed:", str(e))

            print("PAID ✅ agreement_id=", agreement_id, "session_id=", session_id)

    return {"ok": True}
