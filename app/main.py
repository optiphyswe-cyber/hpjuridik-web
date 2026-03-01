import os
import io
import json
import hmac
import hashlib
from uuid import uuid4, UUID
from datetime import datetime, timezone

import stripe
import httpx

from fastapi import FastAPI, Request, Form, Depends, HTTPException
from fastapi.responses import (
    HTMLResponse,
    PlainTextResponse,
    StreamingResponse,
    Response,
    RedirectResponse,
)
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.base import BaseHTTPMiddleware

# PDF (ReportLab) - du har redan detta i requirements
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle
from reportlab.lib import colors

# DB (SQLAlchemy)
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker, DeclarativeBase, Mapped, mapped_column
from sqlalchemy.dialects.postgresql import UUID as PG_UUID, JSONB
from sqlalchemy import String, DateTime, Boolean

# -------------------------
# App + templates
# -------------------------
app = FastAPI()
app.mount("/static", StaticFiles(directory="app/static"), name="static")
templates = Jinja2Templates(directory="app/templates")

# -------------------------
# Environment / settings
# -------------------------
CANONICAL_HOST = os.getenv("CANONICAL_HOST", "www.hpjuridik.se").strip().lower()
SITE_URL = os.getenv("SITE_URL", f"https://{CANONICAL_HOST}").rstrip("/")

ENV = os.getenv("ENV", "development")

# Stripe
STRIPE_SECRET_KEY = os.getenv("STRIPE_SECRET_KEY", "")
STRIPE_WEBHOOK_SECRET = os.getenv("STRIPE_WEBHOOK_SECRET", "")
STRIPE_PRICE_SEK_150 = os.getenv("STRIPE_PRICE_SEK_150", "")  # optional, annars amount inline

# Postmark
POSTMARK_SERVER_TOKEN = os.getenv("POSTMARK_SERVER_TOKEN", "")
EMAIL_FROM = os.getenv("EMAIL_FROM", "lanabil@hpjuridik.se")
EMAIL_LEAD_TO = os.getenv("EMAIL_LEAD_TO", "lanabil@hpjuridik.se")

# Signicat (stub – du fyller endpoints efter din tenant/produkt)
SIGNICAT_BASE_URL = os.getenv("SIGNICAT_BASE_URL", "")
SIGNICAT_CLIENT_ID = os.getenv("SIGNICAT_CLIENT_ID", "")
SIGNICAT_CLIENT_SECRET = os.getenv("SIGNICAT_CLIENT_SECRET", "")
SIGNICAT_WEBHOOK_SECRET = os.getenv("SIGNICAT_WEBHOOK_SECRET", "")  # om du vill ha enkel header/hmac check

# DB
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()

# -------------------------
# Company info (single source of truth)
# -------------------------
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

# -------------------------
# Middleware: force https + force canonical host (www)
# -------------------------
class CanonicalRedirectMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        proto = (request.headers.get("x-forwarded-proto") or request.url.scheme).lower()
        host = (request.headers.get("x-forwarded-host") or request.url.hostname or "").lower()

        path = request.url.path
        query = request.url.query
        suffix = f"?{query}" if query else ""

        if proto != "https" and ENV != "development":
            target = f"https://{host}{path}{suffix}"
            return RedirectResponse(url=target, status_code=301)

        if host and host != CANONICAL_HOST and ENV != "development":
            target = f"https://{CANONICAL_HOST}{path}{suffix}"
            return RedirectResponse(url=target, status_code=301)

        return await call_next(request)

app.add_middleware(CanonicalRedirectMiddleware)

# -------------------------
# SEO helpers
# -------------------------
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

# -------------------------
# DB setup
# -------------------------
class Base(DeclarativeBase):
    pass

class Agreement(Base):
    __tablename__ = "agreements"

    # Postgres UUID om möjligt, annars faller vi tillbaka till String
    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid4)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc))

    status: Mapped[str] = mapped_column(String, nullable=False, default="draft")
    form_payload: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)

    stripe_session_id: Mapped[str | None] = mapped_column(String, nullable=True)

    sign_provider: Mapped[str | None] = mapped_column(String, nullable=True)
    sign_provider_id: Mapped[str | None] = mapped_column(String, nullable=True)
    sign_url: Mapped[str | None] = mapped_column(String, nullable=True)

    signed_pdf_path: Mapped[str | None] = mapped_column(String, nullable=True)
    audit_log_path: Mapped[str | None] = mapped_column(String, nullable=True)

    ip: Mapped[str | None] = mapped_column(String, nullable=True)
    user_agent: Mapped[str | None] = mapped_column(String, nullable=True)

    disclaimer_accepted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    confirm_correct_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    newsletter_optin: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL saknas. Skapa Render Postgres och sätt DATABASE_URL i env.")

engine = create_engine(DATABASE_URL, pool_pre_ping=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)

def init_db():
    Base.metadata.create_all(bind=engine)

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

init_db()

# -------------------------
# Email (Postmark API)
# -------------------------
POSTMARK_URL = "https://api.postmarkapp.com/email"

async def send_email_postmark(to_email: str, subject: str, html_body: str, text_body: str | None = None):
    if not POSTMARK_SERVER_TOKEN:
        raise RuntimeError("POSTMARK_SERVER_TOKEN saknas (Render env).")

    payload = {
        "From": EMAIL_FROM,
        "To": to_email,
        "Subject": subject,
        "HtmlBody": html_body,
    }
    if text_body:
        payload["TextBody"] = text_body

    headers = {
        "X-Postmark-Server-Token": POSTMARK_SERVER_TOKEN,
        "Accept": "application/json",
        "Content-Type": "application/json",
    }

    async with httpx.AsyncClient(timeout=20) as client:
        r = await client.post(POSTMARK_URL, json=payload, headers=headers)
        r.raise_for_status()
        return r.json()

async def send_lead_email(html_body: str):
    return await send_email_postmark(
        to_email=EMAIL_LEAD_TO,
        subject="Lead – Låna bil till skuldsatt",
        html_body=html_body,
    )

# -------------------------
# Stripe helpers
# -------------------------
stripe.api_key = STRIPE_SECRET_KEY

def create_checkout_session(*, agreement_id: str) -> stripe.checkout.Session:
    if not STRIPE_SECRET_KEY:
        raise RuntimeError("STRIPE_SECRET_KEY saknas.")

    success_url = f"{SITE_URL}/checkout-success?session_id={{CHECKOUT_SESSION_ID}}"
    cancel_url = f"{SITE_URL}/lana-bil-till-skuldsatt"

    if STRIPE_PRICE_SEK_150:
        line_items = [{"price": STRIPE_PRICE_SEK_150, "quantity": 1}]
    else:
        line_items = [{
            "price_data": {
                "currency": "sek",
                "product_data": {"name": "Premium – Låna bil till skuldsatt"},
                "unit_amount": 15000,  # 150 kr i ören
            },
            "quantity": 1
        }]

    session = stripe.checkout.Session.create(
        mode="payment",
        success_url=success_url,
        cancel_url=cancel_url,
        line_items=line_items,
        metadata={"agreement_id": agreement_id},
    )
    return session

# -------------------------
# Signicat (stub) helpers
# -------------------------
async def signicat_get_access_token() -> str:
    """
    TODO: Exakta token-endpointen kan skilja per Signicat produkt/tenant.
    Vanligt är client_credentials mot /oauth/connect/token.
    """
    if not (SIGNICAT_BASE_URL and SIGNICAT_CLIENT_ID and SIGNICAT_CLIENT_SECRET):
        raise RuntimeError("Signicat env saknas (SIGNICAT_BASE_URL/CLIENT_ID/CLIENT_SECRET).")

    token_url = f"{SIGNICAT_BASE_URL.rstrip('/')}/oauth/connect/token"
    data = {
        "grant_type": "client_credentials",
        "client_id": SIGNICAT_CLIENT_ID,
        "client_secret": SIGNICAT_CLIENT_SECRET,
    }

    async with httpx.AsyncClient(timeout=25) as client:
        r = await client.post(token_url, data=data)
        r.raise_for_status()
        return r.json()["access_token"]

async def signicat_create_signing(*, agreement_id: str, lender_email: str) -> dict:
    """
    TODO: Anpassa endpoint + payload enligt din Signicat eSign/Sign setup.
    Returnera: {"provider_id": "...", "sign_url": "..."}
    """
    token = await signicat_get_access_token()

    callback_url = f"{SITE_URL}/sign/webhook"
    # Exempelpayload (måste troligen ändras)
    payload = {
        "externalReference": agreement_id,
        "callbackUrl": callback_url,
        "signer": {"email": lender_email, "method": "bankid"},
    }

    url = f"{SIGNICAT_BASE_URL.rstrip('/')}/signing/orders"  # EXEMPEL
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.post(url, json=payload, headers={"Authorization": f"Bearer {token}"})
        r.raise_for_status()
        data = r.json()

    provider_id = data.get("id") or data.get("orderId") or ""
    sign_url = data.get("signUrl") or data.get("url") or ""
    if not sign_url:
        raise RuntimeError("Signicat svar saknar sign_url (mappa korrekt från API-responsen).")

    return {"provider_id": provider_id, "sign_url": sign_url}

async def signicat_fetch_signed_artifacts(*, provider_id: str) -> dict:
    """
    TODO: Anpassa endpoints för att hämta signerad PDF + audit log.
    """
    token = await signicat_get_access_token()

    pdf_url = f"{SIGNICAT_BASE_URL.rstrip('/')}/signing/orders/{provider_id}/document"  # EXEMPEL
    audit_url = f"{SIGNICAT_BASE_URL.rstrip('/')}/signing/orders/{provider_id}/audit"    # EXEMPEL

    async with httpx.AsyncClient(timeout=30) as client:
        pdf_r = await client.get(pdf_url, headers={"Authorization": f"Bearer {token}"})
        pdf_r.raise_for_status()
        audit_r = await client.get(audit_url, headers={"Authorization": f"Bearer {token}"})
        audit_r.raise_for_status()

    return {"pdf_bytes": pdf_r.content, "audit_bytes": audit_r.content}

def verify_signicat_webhook(body: bytes, headers: dict) -> bool:
    """
    Enkel verifiering:
    - Om SIGNICAT_WEBHOOK_SECRET är satt: kräver header 'x-signicat-secret' == secret
    - Du kan byta till HMAC-signatur om din Signicat webhook stödjer det.
    """
    if not SIGNICAT_WEBHOOK_SECRET:
        return True
    return headers.get("x-signicat-secret") == SIGNICAT_WEBHOOK_SECRET

# -------------------------
# PDF helpers (din befintliga generator, oförändrad)
# -------------------------
def _safe(s: str) -> str:
    return (s or "").strip()

def _sv_dt(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d kl. %H:%M")

def _sv_date(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d")

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

    story = []
    story.append(Paragraph("TILLFÄLLIGT LÅNEAVTAL – BIL", title))
    story.append(P("Detta avtal upprättas för att tydliggöra villkoren för ett tidsbegränsat lån av fordon.", small))
    story.append(Spacer(1, 6))

    story.append(Paragraph("Parter", h))

    ut_pnr = f", personnummer: {_safe(utlanare.get('pnr'))}" if _safe(utlanare.get("pnr")) else ""
    la_pnr = f", personnummer: {_safe(lantagare.get('pnr'))}" if _safe(lantagare.get("pnr")) else ""

    story.append(P(f"<b>Utlånare (ägare):</b> {_safe(utlanare.get('namn'))}{ut_pnr}", body))
    story.append(P(f"Adress: {_safe(utlanare.get('adress'))}", body))
    story.append(P(f"Telefon: {_safe(utlanare.get('tel'))} &nbsp;&nbsp; E-post: {_safe(utlanare.get('epost'))}", body))
    story.append(Spacer(1, 4))
    story.append(P(f"<b>Låntagare (skuldsatt):</b> {_safe(lantagare.get('namn'))}{la_pnr}", body))
    story.append(P(f"Adress: {_safe(lantagare.get('adress'))}", body))
    story.append(P(f"Telefon: {_safe(lantagare.get('tel'))} &nbsp;&nbsp; E-post: {_safe(lantagare.get('epost'))}", body))

    story.append(Paragraph("Fordon", h))
    story.append(P(f"Märke och modell: {_safe(fordon.get('marke_modell'))}", body))
    story.append(P(f"Registreringsnummer: {_safe(fordon.get('regnr'))}", body))
    if _safe(fordon.get("agare")):
        story.append(P(f"Ägare: {_safe(fordon.get('agare'))}", body))

    story.append(Paragraph("Avtalsperiod", h))
    from_dt = period["from"]
    to_dt = period["to"]
    story.append(P(f"Från: {_sv_dt(from_dt)}", body))
    story.append(P(f"Till: {_sv_dt(to_dt)}", body))

    story.append(Paragraph("Ändamål med lånet", h))
    story.append(P(andamal, body))

    story.append(Paragraph("Bakgrund och syfte med avtalet", h))
    story.append(
        P(
            "Syftet med detta avtal är att dokumentera att utlåningen är tillfällig, att fordonet fortsatt tillhör utlånaren "
            "och att låntagaren nyttjar fordonet inom ramen för nedanstående villkor. Avtalet kan användas som underlag "
            "för att visa att fordonet inte överlåtits utan endast lånats ut under begränsad tid.",
            body,
        )
    )
    story.append(P("Detta avtal är ett standardiserat bevisunderlag baserat på parternas uppgifter.", small))

    story.append(Paragraph("Villkor", h))
    villkor = [
        "Lånet avser endast ovan angivet fordon och gäller enbart under den angivna avtalsperioden.",
        "Låntagaren ansvarar för att fordonet hanteras varsamt och enligt gällande trafik- och försäkringsvillkor.",
        "Låntagaren ansvarar för kostnader som uppstår under låneperioden (bränsle, trängselskatt, parkeringsavgifter/böter m.m.) om inte annat avtalas skriftligen.",
        "Skador eller fel som uppstår under låneperioden ska omedelbart meddelas utlånaren. Låntagaren ansvarar för skador som uppstår genom vårdslöshet eller felaktig användning.",
        "Fordonet får inte överlåtas, lånas ut i andra hand, hyras ut eller användas för olagliga ändamål.",
        "Utlånaren har rätt att återkalla lånet i förtid vid misstanke om missbruk eller vid väsentligt avtalsbrott.",
    ]
    for i, v in enumerate(villkor, start=1):
        story.append(P(f"<b>{i}.</b> {v}", body))

    story.append(Paragraph("Kopior", h))
    story.append(P("Avtalet upprättas i två (2) likalydande exemplar där parterna erhåller varsitt.", body))

    story.append(Spacer(1, 6))
    today = datetime.now(timezone.utc).astimezone()
    story.append(P(f"Ort: {_safe(ort)}", body))
    story.append(P(f"Datum: {_sv_date(today)}", body))

    story.append(Spacer(1, 14))
    sig_data = [
        ["______________________________", "______________________________"],
        ["Utlånare (ägare)", "Låntagare (skuldsatt)"],
        [f"Namn: {_safe(utlanare.get('namn'))}", f"Namn: {_safe(lantagare.get('namn'))}"],
    ]
    sig_table = Table(sig_data, colWidths=[85 * mm, 85 * mm])
    sig_table.setStyle(
        TableStyle(
            [
                ("FONTNAME", (0, 0), (-1, -1), "Helvetica"),
                ("FONTSIZE", (0, 0), (-1, -1), 10),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
                ("TOPPADDING", (0, 0), (-1, -1), 2),
                ("ALIGN", (0, 0), (-1, -1), "LEFT"),
                ("LINEBELOW", (0, 0), (0, 0), 0, colors.white),
                ("LINEBELOW", (1, 0), (1, 0), 0, colors.white),
            ]
        )
    )
    story.append(sig_table)

    doc.build(story)
    return buf.getvalue()

# -------------------------
# SEO endpoints
# -------------------------
@app.get("/robots.txt", response_class=PlainTextResponse)
def robots_txt():
    return f"User-agent: *\nAllow: /\nSitemap: {SITE_URL}/sitemap.xml\n"

@app.get("/sitemap.xml")
def sitemap_xml():
    urls = [
        f"{SITE_URL}/",
        f"{SITE_URL}/gdpr",
        f"{SITE_URL}/allmanna-villkor",
        f"{SITE_URL}/lana-bil-till-skuldsatt",
    ]
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    xml = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">',
    ]
    for u in urls:
        xml.append("<url>")
        xml.append(f"<loc>{u}</loc>")
        xml.append(f"<lastmod>{now}</lastmod>")
        xml.append("</url>")
    xml.append("</urlset>")
    return Response(content="\n".join(xml), media_type="application/xml")

# -------------------------
# Routes (befintliga sidor)
# -------------------------
@app.api_route("/", methods=["GET", "HEAD"], response_class=HTMLResponse)
def home(request: Request):
    ctx = page_ctx(
        request,
        "/",
        "HP Juridik – 20 min gratis rådgivning",
        "Personlig, trygg och värdeskapande juridik för privatpersoner och företag.",
    )
    return templates.TemplateResponse("pages/home.html", ctx)

@app.get("/gdpr", response_class=HTMLResponse)
def gdpr(request: Request):
    ctx = page_ctx(request, "/gdpr", "GDPR – HP Juridik", "Information om personuppgifter och integritet.")
    return templates.TemplateResponse("pages/gdpr.html", ctx)

@app.get("/allmanna-villkor", response_class=HTMLResponse)
def terms(request: Request):
    ctx = page_ctx(request, "/allmanna-villkor", "Allmänna villkor – HP Juridik", "Villkor för tjänster och rådgivning.")
    return templates.TemplateResponse("pages/terms.html", ctx)

# -------------------------
# Låna bil – Form + Review + Gratis/Premium
# -------------------------
@app.get("/lana-bil-till-skuldsatt", response_class=HTMLResponse)
def lana_bil_form(request: Request):
    ctx = page_ctx(
        request,
        "/lana-bil-till-skuldsatt",
        "Låna bil till skuldsatt | HP Juridik",
        "Skapa ett tillfälligt låneavtal för bil som PDF.",
    )
    ctx.update({"sent": False, "error": None})
    return templates.TemplateResponse("pages/lana_bil.html", ctx)

def normalize_regnr(regnr: str) -> str:
    return "".join((regnr or "").split()).upper()

def parse_iso(dt_str: str) -> datetime:
    return datetime.fromisoformat(dt_str)

@app.post("/lana-bil-till-skuldsatt/review", response_class=HTMLResponse)
def lana_bil_review(
    request: Request,
    db=Depends(get_db),

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

    # Övrigt
    andamal: str = Form(...),
    disclaimer_accept: str = Form(None),
    newsletter_optin: str = Form(None),
):
    if not disclaimer_accept:
        ctx = page_ctx(
            request,
            "/lana-bil-till-skuldsatt",
            "Låna bil till skuldsatt | HP Juridik",
            "Skapa ett tillfälligt låneavtal för bil som PDF.",
        )
        ctx.update({"sent": False, "error": "Du måste godkänna friskrivningsvillkoret för att fortsätta."})
        return templates.TemplateResponse("pages/lana_bil.html", ctx, status_code=400)

    # validera datum
    try:
        from_dt_obj = parse_iso(from_dt)
        to_dt_obj = parse_iso(to_dt)
    except Exception:
        raise HTTPException(400, "Ogiltigt datum/tid-format.")
    if to_dt_obj <= from_dt_obj:
        raise HTTPException(400, "Till-datum/tid måste vara efter Från-datum/tid.")

    payload = {
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
            "marke_modell": bil_marke_modell,
            "regnr": normalize_regnr(bil_regnr),
            "agare": utlanare_namn,
        },
        "period": {"from_dt": from_dt, "to_dt": to_dt},
        "andamal": andamal,
        "disclaimer_accept": True,
        "newsletter_optin": bool(newsletter_optin),
    }

    agreement = Agreement(
        status="draft",
        form_payload=payload,
        ip=request.client.host if request.client else None,
        user_agent=request.headers.get("user-agent"),
        newsletter_optin=bool(newsletter_optin),
    )
    db.add(agreement)
    db.commit()
    db.refresh(agreement)

    ctx = page_ctx(
        request,
        "/lana-bil-till-skuldsatt",
        "Granska uppgifter | HP Juridik",
        "Granska uppgifterna innan du laddar ner eller betalar.",
    )
    ctx.update({"payload": payload, "agreement_id": str(agreement.id)})
    return templates.TemplateResponse("pages/lana_bil_review.html", ctx)

@app.post("/lana-bil-till-skuldsatt/free")
async def lana_bil_free(
    request: Request,
    agreement_id: str = Form(...),
    confirm_correct: str = Form(None),
    disclaimer_accept: str = Form(None),
    db=Depends(get_db),
):
    if not confirm_correct:
        raise HTTPException(400, "Du måste bekräfta att uppgifterna är korrekta.")
    if not disclaimer_accept:
        raise HTTPException(400, "Du måste acceptera friskrivningen.")

    agreement = db.get(Agreement, UUID(agreement_id))
    if not agreement:
        raise HTTPException(404, "Avtal hittades inte.")

    payload = agreement.form_payload or {}

    # Skapa PDF
    try:
        from_dt_obj = parse_iso(payload["period"]["from_dt"])
        to_dt_obj = parse_iso(payload["period"]["to_dt"])
    except Exception:
        raise HTTPException(400, "Ogiltiga datum i avtalspayload.")

    pdf_bytes = build_loan_pdf(
        utlanare=payload["utlanare"],
        lantagare=payload["lantagare"],
        fordon=payload["fordon"],
        period={"from": from_dt_obj, "to": to_dt_obj},
        andamal=payload.get("andamal", ""),
        ort="Lund",
    )

    # Skicka lead-mail internt (utan PDF)
    utl = payload.get("utlanare", {})
    lat = payload.get("lantagare", {})
    html = f"""
      <h3>Ny lead: Låna bil till skuldsatt (Gratis)</h3>
      <ul>
        <li>Utlånare: {utl.get('namn')} ({utl.get('epost')})</li>
        <li>Låntagare: {lat.get('namn')} ({lat.get('epost')})</li>
        <li>Tid: {datetime.now(timezone.utc).isoformat()}Z</li>
        <li>IP: {agreement.ip}</li>
        <li>User-Agent: {agreement.user_agent}</li>
        <li>Newsletter opt-in: {agreement.newsletter_optin}</li>
        <li>Agreement ID: {agreement_id}</li>
      </ul>
    """
    await send_lead_email(html)

    agreement.status = "free_downloaded"
    agreement.confirm_correct_at = datetime.now(timezone.utc)
    agreement.disclaimer_accepted_at = datetime.now(timezone.utc)
    db.commit()

    filename = "laneavtal-bil.pdf"
    return StreamingResponse(
        io.BytesIO(pdf_bytes),
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )

@app.post("/lana-bil-till-skuldsatt/paid")
def lana_bil_paid(
    agreement_id: str = Form(...),
    confirm_correct: str = Form(None),
    disclaimer_accept: str = Form(None),
    db=Depends(get_db),
):
    if not confirm_correct:
        raise HTTPException(400, "Du måste bekräfta att uppgifterna är korrekta.")
    if not disclaimer_accept:
        raise HTTPException(400, "Du måste acceptera friskrivningen.")

    agreement = db.get(Agreement, UUID(agreement_id))
    if not agreement:
        raise HTTPException(404, "Avtal hittades inte.")

    session = create_checkout_session(agreement_id=agreement_id)
    agreement.stripe_session_id = session.id
    agreement.confirm_correct_at = datetime.now(timezone.utc)
    agreement.disclaimer_accepted_at = datetime.now(timezone.utc)
    db.commit()

    return RedirectResponse(url=session.url, status_code=303)

@app.get("/checkout-success", response_class=HTMLResponse)
def checkout_success(request: Request, session_id: str | None = None):
    ctx = page_ctx(request, "/checkout-success", "Tack!", "Betalningen är mottagen. Signering skickas via e-post.")
    ctx.update({"session_id": session_id})
    return templates.TemplateResponse("pages/checkout_success.html", ctx)

# -------------------------
# Stripe webhook (MÅSTE vara sanningen)
# -------------------------
@app.post("/stripe/webhook")
async def stripe_webhook(request: Request, db=Depends(get_db)):
    if not STRIPE_WEBHOOK_SECRET:
        raise HTTPException(500, "STRIPE_WEBHOOK_SECRET saknas.")

    body = await request.body()
    sig_header = request.headers.get("stripe-signature", "")

    try:
        event = stripe.Webhook.construct_event(body, sig_header, STRIPE_WEBHOOK_SECRET)
    except Exception as e:
        raise HTTPException(400, f"Invalid webhook signature: {e}")

    if event["type"] == "checkout.session.completed":
        session = event["data"]["object"]
        if session.get("payment_status") != "paid":
            return {"ok": True}

        agreement_id = (session.get("metadata") or {}).get("agreement_id")
        if not agreement_id:
            raise HTTPException(400, "agreement_id saknas i Stripe metadata.")

        agreement = db.get(Agreement, UUID(agreement_id))
        if not agreement:
            raise HTTPException(404, "Agreement hittades inte.")

        # Idempotens
        if agreement.status in ("signing", "signed"):
            return {"ok": True}

        agreement.status = "paid"
        db.commit()

        payload = agreement.form_payload or {}
        lender_email = (payload.get("utlanare") or {}).get("epost")
        if not lender_email:
            agreement.status = "failed"
            db.commit()
            raise HTTPException(400, "Utlånarens e-post saknas i payload.")

        # Starta Signicat signering
        signing = await signicat_create_signing(agreement_id=str(agreement.id), lender_email=lender_email)

        agreement.sign_provider = "signicat"
        agreement.sign_provider_id = signing.get("provider_id")
        agreement.sign_url = signing.get("sign_url")
        agreement.status = "signing"
        db.commit()

        sign_url = agreement.sign_url
        html = f"""
          <p>Tack! Här är signeringslänken (BankID) för utlånaren:</p>
          <p><a href="{sign_url}">{sign_url}</a></p>
          <p>Avtals-ID: {agreement.id}</p>
        """
        await send_email_postmark(
            to_email=lender_email,
            subject="Signera avtalet (BankID)",
            html_body=html,
        )

    return {"ok": True}

# -------------------------
# Signicat webhook (signed)
# -------------------------
@app.post("/sign/webhook")
async def sign_webhook(request: Request, db=Depends(get_db)):
    body = await request.body()
    headers = {k.lower(): v for k, v in request.headers.items()}

    if not verify_signicat_webhook(body, headers):
        raise HTTPException(400, "Invalid Signicat webhook verification.")

    data = json.loads(body.decode("utf-8") or "{}")

    # TODO: mappa enligt din Signicat webhook payload
    status = (data.get("status") or data.get("event") or "").lower()
    agreement_id = data.get("externalReference") or data.get("agreement_id") or data.get("reference")
    provider_id = data.get("id") or data.get("orderId") or data.get("provider_id")

    if not agreement_id:
        raise HTTPException(400, "agreement_id saknas i sign webhook payload.")

    agreement = db.get(Agreement, UUID(str(agreement_id)))
    if not agreement:
        raise HTTPException(404, "Agreement hittades inte.")

    # Tolkning: signed/completed
    if "signed" in status or status == "completed":
        if not provider_id:
            provider_id = agreement.sign_provider_id
        if not provider_id:
            raise HTTPException(400, "provider_id saknas för att hämta signerade artefakter.")

        artifacts = await signicat_fetch_signed_artifacts(provider_id=provider_id)

        os.makedirs("/tmp/signed_artifacts", exist_ok=True)
        pdf_path = f"/tmp/signed_artifacts/{agreement.id}.pdf"
        audit_path = f"/tmp/signed_artifacts/{agreement.id}.audit"

        with open(pdf_path, "wb") as f:
            f.write(artifacts["pdf_bytes"])
        with open(audit_path, "wb") as f:
            f.write(artifacts["audit_bytes"])

        agreement.signed_pdf_path = pdf_path
        agreement.audit_log_path = audit_path
        agreement.status = "signed"
        db.commit()

        # VALFRITT: maila signerad PDF till båda (MVP: mailar notis)
        payload = agreement.form_payload or {}
        lender_email = (payload.get("utlanare") or {}).get("epost")
        borrower_email = (payload.get("lantagare") or {}).get("epost")

        if lender_email:
            await send_email_postmark(lender_email, "Avtalet är signerat", "<p>Avtalet är nu signerat.</p>")
        if borrower_email:
            await send_email_postmark(borrower_email, "Avtalet är signerat", "<p>Avtalet är nu signerat.</p>")

    return {"ok": True}

# -------------------------
# (Valfritt) Download signerad PDF
# -------------------------
@app.get("/agreements/{agreement_id}/signed.pdf")
def download_signed_pdf(agreement_id: str, db=Depends(get_db)):
    agreement = db.get(Agreement, UUID(agreement_id))
    if not agreement or agreement.status != "signed" or not agreement.signed_pdf_path:
        raise HTTPException(404, "Signerad PDF finns inte.")
    try:
        with open(agreement.signed_pdf_path, "rb") as f:
            pdf_bytes = f.read()
    except FileNotFoundError:
        raise HTTPException(404, "Signerad PDF saknas på disk.")
    return Response(content=pdf_bytes, media_type="application/pdf")

# -------------------------
# Health
# -------------------------
@app.get("/healthz", response_class=PlainTextResponse)
def healthz():
    return "ok"
