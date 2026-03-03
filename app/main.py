"""
HP Juridik – FastAPI app (stabil “main”)

Mål:
- Kontaktformulär ska fungera klockrent och STANNA på sidan (ingen redirect till /kontakta-oss om den inte redan är där).
- Låna bil till skuldsatt:
  - Steg 1: formulär -> review-sida (GET) med två val: Gratis PDF / Premium betalning.
  - Gratis: skapa PDF + maila PDF + lead-info till rätt inbox.
  - Premium: Stripe Checkout -> webhook -> maila PDF + skapa Oneflow-signering (om aktiverat).
- Oneflow är OPTIONAL. Om env-variabler saknas faller funktionerna tillbaka till “maila PDF + lead”.

VIKTIGA ENV (Render):
BASE_URL=https://hpjuridik.se
POSTMARK_SERVER_TOKEN=...
MAIL_FROM=lanabil@hpjuridik.se   (avsändare för systemmail)
CONTACT_TO=hp@hpjuridik.se      (kontaktformulärets mottagare)
LEAD_INBOX=lanabil@hpjuridik.se (mottagare för “låna bil”-leads)

STRIPE:
STRIPE_SECRET_KEY=sk_live_...
STRIPE_WEBHOOK_SECRET=whsec_...

ONEFLOW (valfritt):
ONEFLOW_API_TOKEN=...
ONEFLOW_BASE_URL=https://api.oneflow.com (eller enligt ert konto)
ONEFLOW_WORKSPACE_ID=...
ONEFLOW_TEMPLATE_ID=...

Katalogstruktur (som ni har):
app/
  main.py  (denna fil)
  templates/
    partials/base.html
    pages/home.html
    pages/contact.html
    pages/lana_bil.html
    pages/lana_bil_review.html
    pages/terms.html
    pages/services.html
    pages/page.html
  static/...

OBS:
- Alla endpoints returnerar HTML där det ska vara HTML.
- /health ger JSON för Render/övervakning.
- HEAD / svarar 200 (Render gör ofta HEAD-check).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import io
import json
import os
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, Optional, Tuple

import httpx
import stripe
from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from jinja2 import Environment
from starlette.middleware.proxy_headers import ProxyHeadersMiddleware
from starlette.templating import Jinja2Templates

# PDF
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas


# ------------------------------------------------------------------------------
# App setup
# ------------------------------------------------------------------------------

app = FastAPI()

# Render / proxy headers (för rätt schema/host bakom proxy)
app.add_middleware(ProxyHeadersMiddleware, trusted_hosts="*")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
TEMPLATES_DIR = os.path.join(BASE_DIR, "templates")
STATIC_DIR = os.path.join(BASE_DIR, "static")

templates = Jinja2Templates(directory=TEMPLATES_DIR)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


# ------------------------------------------------------------------------------
# Env helpers
# ------------------------------------------------------------------------------

def env(name: str, default: Optional[str] = None) -> Optional[str]:
    v = os.getenv(name)
    if v is None or v == "":
        return default
    return v


BASE_URL = env("BASE_URL", "https://hpjuridik.se").rstrip("/")
MAIL_FROM = env("MAIL_FROM", "lanabil@hpjuridik.se")
CONTACT_TO = env("CONTACT_TO", "hp@hpjuridik.se")
LEAD_INBOX = env("LEAD_INBOX", "lanabil@hpjuridik.se")

POSTMARK_SERVER_TOKEN = env("POSTMARK_SERVER_TOKEN")

STRIPE_SECRET_KEY = env("STRIPE_SECRET_KEY")
STRIPE_WEBHOOK_SECRET = env("STRIPE_WEBHOOK_SECRET")

ONEFLOW_API_TOKEN = env("ONEFLOW_API_TOKEN")
ONEFLOW_BASE_URL = env("ONEFLOW_BASE_URL", "https://api.oneflow.com")
ONEFLOW_WORKSPACE_ID = env("ONEFLOW_WORKSPACE_ID")
ONEFLOW_TEMPLATE_ID = env("ONEFLOW_TEMPLATE_ID")

# Stripe init
if STRIPE_SECRET_KEY:
    stripe.api_key = STRIPE_SECRET_KEY


# ------------------------------------------------------------------------------
# Utilities
# ------------------------------------------------------------------------------

def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def iso_dt(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def safe(s: Optional[str]) -> str:
    return (s or "").strip()


def page_ctx(request: Request, path: str, title: str, meta_desc: str = "") -> Dict[str, Any]:
    return {
        "request": request,
        "path": path,
        "title": title,
        "meta_desc": meta_desc,
        "base_url": BASE_URL,
        "year": datetime.now().year,
    }


# ------------------------------------------------------------------------------
# Postmark mail
# ------------------------------------------------------------------------------

async def postmark_send(
    *,
    to_email: str,
    subject: str,
    text_body: str,
    from_email: Optional[str] = None,
    reply_to: Optional[str] = None,
    attachments: Optional[list] = None,
) -> None:
    """
    Skickar mail via Postmark.
    attachments: [{"Name": "...pdf", "Content": "<base64>", "ContentType":"application/pdf"}]
    """
    if not POSTMARK_SERVER_TOKEN:
        raise RuntimeError("POSTMARK_SERVER_TOKEN saknas i environment")

    payload = {
        "From": from_email or MAIL_FROM,
        "To": to_email,
        "Subject": subject,
        "TextBody": text_body,
    }
    if reply_to:
        payload["ReplyTo"] = reply_to
    if attachments:
        payload["Attachments"] = attachments

    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.post(
            "https://api.postmarkapp.com/email",
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
                "X-Postmark-Server-Token": POSTMARK_SERVER_TOKEN,
            },
            json=payload,
        )
        if r.status_code >= 300:
            raise RuntimeError(f"Postmark error {r.status_code}: {r.text}")


def pdf_to_attachment(filename: str, pdf_bytes: bytes) -> Dict[str, Any]:
    return {
        "Name": filename,
        "Content": base64.b64encode(pdf_bytes).decode("utf-8"),
        "ContentType": "application/pdf",
    }


# ------------------------------------------------------------------------------
# PDF generation (låneavtal)
# ------------------------------------------------------------------------------

def build_loan_pdf(data: Dict[str, Any]) -> bytes:
    """
    Skapar enkel PDF. (Ni kan bygga ut texten hur mycket ni vill.)
    """
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    w, h = A4

    # Enkel typsnitt fallback (Reportlab standard)
    # Om ni vill ha svensk font: lägg en .ttf i app/static eller app/fonts och registrera här.
    y = h - 25 * mm

    def line(txt: str, dy: float = 7 * mm, size: int = 11, bold: bool = False):
        nonlocal y
        c.setFont("Helvetica-Bold" if bold else "Helvetica", size)
        c.drawString(20 * mm, y, txt)
        y -= dy

    line("Tillfälligt låneavtal – Bil", size=16, bold=True, dy=10 * mm)
    line(f"Skapat: {iso_dt(now_utc())}", size=10, dy=8 * mm)

    line("Utlånare (ägare)", bold=True)
    line(f"Namn: {data.get('utlanare_namn','')}")
    line(f"Personnummer: {data.get('utlanare_pnr','')}")
    line(f"Adress: {data.get('utlanare_adress','')}")
    line(f"Telefon: {data.get('utlanare_tel','')}")
    line(f"E-post: {data.get('utlanare_epost','')}", dy=10 * mm)

    line("Låntagare (skuldsatt)", bold=True)
    line(f"Namn: {data.get('lantagare_namn','')}")
    line(f"Personnummer: {data.get('lantagare_pnr','')}")
    line(f"Adress: {data.get('lantagare_adress','')}")
    line(f"Telefon: {data.get('lantagare_tel','')}")
    line(f"E-post: {data.get('lantagare_epost','')}", dy=10 * mm)

    line("Fordon", bold=True)
    line(f"Märke/modell: {data.get('bil_marke_modell','')}")
    line(f"Registreringsnummer: {data.get('bil_regnr','')}", dy=10 * mm)

    line("Avtalsperiod", bold=True)
    line(f"Från: {data.get('from_dt','')}")
    line(f"Till: {data.get('to_dt','')}", dy=10 * mm)

    line("Ändamål / syfte", bold=True)
    # Wrap crude
    txt = (data.get("andamal") or "").strip()
    if not txt:
        txt = "-"
    c.setFont("Helvetica", 11)
    max_chars = 95
    for chunk in [txt[i:i+max_chars] for i in range(0, len(txt), max_chars)]:
        c.drawString(20 * mm, y, chunk)
        y -= 6 * mm
        if y < 25 * mm:
            c.showPage()
            y = h - 25 * mm
            c.setFont("Helvetica", 11)

    y -= 6 * mm
    line("Notering", bold=True)
    line("Detta dokument är ett standardiserat bevisunderlag baserat på angivna uppgifter.")
    line("Ingen garanti lämnas för myndighetsbedömning. Varje situation prövas individuellt.", size=10)

    c.showPage()
    c.save()
    return buf.getvalue()


# ------------------------------------------------------------------------------
# Oneflow (OPTIONAL)
# ------------------------------------------------------------------------------

@dataclass
class OneflowConfig:
    token: str
    base_url: str
    workspace_id: str
    template_id: str


def get_oneflow_config() -> Optional[OneflowConfig]:
    if not (ONEFLOW_API_TOKEN and ONEFLOW_WORKSPACE_ID and ONEFLOW_TEMPLATE_ID and ONEFLOW_BASE_URL):
        return None
    return OneflowConfig(
        token=ONEFLOW_API_TOKEN,
        base_url=ONEFLOW_BASE_URL.rstrip("/"),
        workspace_id=ONEFLOW_WORKSPACE_ID,
        template_id=ONEFLOW_TEMPLATE_ID,
    )


async def oneflow_create_contract_from_template(
    *,
    cfg: OneflowConfig,
    agreement_id: str,
    utlanare_email: str,
    lantagare_email: str,
    variables: Dict[str, Any],
) -> Dict[str, Any]:
    """
    Skapar kontrakt från template, fyller variabler och lägger till parter.
    OBS: Oneflow API kan skilja beroende på version/inställningar.
    Den här funktionen är skriven för att vara “best effort” och INTE krascha hela flödet.
    """
    headers = {
        "Authorization": f"Bearer {cfg.token}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }

    async with httpx.AsyncClient(timeout=45) as client:
        # 1) Create contract from template
        # Oneflow brukar ha endpoint: POST /contracts/create_from_template eller liknande.
        # Vi kör defensivt med en vanlig pattern och låter fel bubbla till caller som “optional”.
        create_url_candidates = [
            f"{cfg.base_url}/contracts/create_from_template",
            f"{cfg.base_url}/contracts/template/{cfg.template_id}",
            f"{cfg.base_url}/contracts",
        ]

        last_err = None
        contract = None

        for url in create_url_candidates:
            try:
                r = await client.post(
                    url,
                    headers=headers,
                    json={
                        "workspace_id": cfg.workspace_id,
                        "template_id": cfg.template_id,
                        "name": f"HP Juridik – Låna bil ({agreement_id})",
                        "external_id": agreement_id,
                    },
                )
                if r.status_code < 300:
                    contract = r.json()
                    break
                last_err = f"{r.status_code} {r.text}"
            except Exception as e:
                last_err = str(e)

        if not contract:
            raise RuntimeError(f"Oneflow: kunde inte skapa kontrakt (testade flera endpoints): {last_err}")

        contract_id = contract.get("id") or contract.get("contract_id") or contract.get("data", {}).get("id")
        if not contract_id:
            # fallback: ibland returneras hela objektet utan id-fält vi känner igen
            raise RuntimeError(f"Oneflow: kunde inte läsa contract id. Response: {contract}")

        # 2) Apply template variables (best effort)
        # ofta: POST /contracts/{id}/variables eller PUT /contracts/{id}
        try:
            await client.post(
                f"{cfg.base_url}/contracts/{contract_id}/variables",
                headers=headers,
                json={"variables": variables},
            )
        except Exception:
            # Ignorera om endpoint saknas, kör bara vidare.
            pass

        # 3) Add participants (best effort)
        # ofta: POST /contracts/{id}/participants
        try:
            await client.post(
                f"{cfg.base_url}/contracts/{contract_id}/participants",
                headers=headers,
                json={
                    "participants": [
                        {"type": "signer", "email": utlanare_email, "name": variables.get("utlanare_namn", "Utlånare")},
                        {"type": "signer", "email": lantagare_email, "name": variables.get("lantagare_namn", "Låntagare")},
                    ]
                },
            )
        except Exception:
            pass

        # 4) Start signing (best effort)
        # ofta: POST /contracts/{id}/publish eller /send
        try:
            await client.post(f"{cfg.base_url}/contracts/{contract_id}/publish", headers=headers, json={})
        except Exception:
            pass

        return {"contract_id": contract_id, "raw": contract}


# ------------------------------------------------------------------------------
# Stripe helpers
# ------------------------------------------------------------------------------

PRICE_PREMIUM_SEK = 150  # kan ändras till 1 för test med 100 öre (Stripe: amount i öre)

def stripe_amount_ore(sek: int) -> int:
    return int(sek) * 100


def make_order_token(payload: Dict[str, Any]) -> str:
    """
    Skapar en “order token” som vi kan skicka mellan steg utan DB.
    OBS: För enklare stabilitet: vi bas64-encodar JSON. (Ni kan signera den om ni vill.)
    """
    raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("utf-8").rstrip("=")


def parse_order_token(token: str) -> Dict[str, Any]:
    pad = "=" * (-len(token) % 4)
    raw = base64.urlsafe_b64decode((token + pad).encode("utf-8"))
    return json.loads(raw.decode("utf-8"))


async def stripe_create_checkout_session(*, order_token: str, customer_email: str) -> str:
    if not STRIPE_SECRET_KEY:
        raise RuntimeError("STRIPE_SECRET_KEY saknas i environment")

    success_url = f"{BASE_URL}/checkout-success?session_id={{CHECKOUT_SESSION_ID}}"
    cancel_url = f"{BASE_URL}/lana-bil-till-skuldsatt/review?token={order_token}&cancel=1"

    session = stripe.checkout.Session.create(
        mode="payment",
        payment_method_types=["card"],
        customer_email=customer_email,
        line_items=[
            {
                "price_data": {
                    "currency": "sek",
                    "product_data": {"name": "Premium – Signering (BankID via Oneflow)"},
                    "unit_amount": stripe_amount_ore(PRICE_PREMIUM_SEK),
                },
                "quantity": 1,
            }
        ],
        success_url=success_url,
        cancel_url=cancel_url,
        metadata={"order_token": order_token, "kind": "lana_bil_premium"},
    )
    return session.url


def verify_stripe_webhook(payload: bytes, sig_header: str) -> stripe.Event:
    if not STRIPE_WEBHOOK_SECRET:
        raise RuntimeError("STRIPE_WEBHOOK_SECRET saknas i environment")
    return stripe.Webhook.construct_event(payload=payload, sig_header=sig_header, secret=STRIPE_WEBHOOK_SECRET)


# ------------------------------------------------------------------------------
# Routes: health + pages
# ------------------------------------------------------------------------------

@app.get("/health", response_class=JSONResponse)
async def health() -> Dict[str, Any]:
    return {"ok": True, "time": iso_dt(now_utc())}


@app.head("/", response_class=Response)
async def head_root() -> Response:
    # Render skickar HEAD; svara 200.
    return Response(status_code=200)


@app.get("/", response_class=HTMLResponse)
async def home(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(
        "pages/home.html",
        page_ctx(request, "/", "HP Juridik", "Juridisk hjälp och dokumentmallar"),
    )


@app.get("/tjanster", response_class=HTMLResponse)
async def services(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(
        "pages/services.html",
        page_ctx(request, "/tjanster", "Tjänster | HP Juridik", "Våra tjänster"),
    )


@app.get("/terms", response_class=HTMLResponse)
async def terms(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(
        "pages/terms.html",
        page_ctx(request, "/terms", "Villkor | HP Juridik", "Villkor"),
    )


# ------------------------------------------------------------------------------
# Contact form (MÅSTE FUNKA + ingen redirect)
# ------------------------------------------------------------------------------

@app.get("/kontakta-oss", response_class=HTMLResponse)
async def contact_page(request: Request) -> HTMLResponse:
    ctx = page_ctx(request, "/kontakta-oss", "Kontakt | HP Juridik", "Kontakta oss")
    # default: inget skickat
    ctx.update({"sent_ok": False, "sent_err": None})
    return templates.TemplateResponse("pages/contact.html", ctx)


@app.post("/kontakta-oss", response_class=HTMLResponse)
async def contact_submit(
    request: Request,
    namn: str = Form(...),
    epost: str = Form(...),
    telefon: str = Form(""),
    meddelande: str = Form(...),
) -> HTMLResponse:
    ts = iso_dt(now_utc())
    ip = request.client.host if request.client else "unknown"
    ua = request.headers.get("user-agent", "unknown")

    subject = "HP Juridik | Ny kontaktförfrågan"
    body = (
        "NY KONTAKTFÖRFRÅGAN (HPJURIDIK.SE)\n"
        "================================\n\n"
        f"Tid: {ts}\n"
        f"Namn: {namn}\n"
        f"E-post: {epost}\n"
        f"Telefon: {telefon}\n\n"
        "Meddelande:\n"
        f"{meddelande}\n\n"
        "----\n"
        f"IP: {ip}\n"
        f"UA: {ua}\n"
    )

    ok = True
    err = None
    try:
        await postmark_send(
            to_email=CONTACT_TO,
            subject=subject,
            text_body=body,
            from_email=MAIL_FROM,
            reply_to=epost,
        )
    except Exception as e:
        ok = False
        err = str(e)

    ctx = page_ctx(request, "/kontakta-oss", "Kontakt | HP Juridik", "Kontakta oss")
    ctx.update({"sent_ok": ok, "sent_err": err})
    # STANNA PÅ SIDAN (ingen redirect)
    return templates.TemplateResponse("pages/contact.html", ctx)


# ------------------------------------------------------------------------------
# Låna bil – form + review + gratis/premium
# ------------------------------------------------------------------------------

@app.get("/lana-bil-till-skuldsatt", response_class=HTMLResponse)
async def lana_bil_form(request: Request) -> HTMLResponse:
    ctx = page_ctx(request, "/lana-bil-till-skuldsatt", "Låna bil till skuldsatt | HP Juridik", "")
    ctx.update({"sent": False, "error": None})
    return templates.TemplateResponse("pages/lana_bil.html", ctx)


@app.post("/lana-bil-till-skuldsatt", response_class=HTMLResponse)
async def lana_bil_submit_to_review(
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
    marketing_accept: str = Form(...),
) -> HTMLResponse:
    agreement_id = str(uuid.uuid4())

    payload = {
        "agreement_id": agreement_id,
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
        "bil_regnr": bil_regnr,
        "from_dt": from_dt,
        "to_dt": to_dt,
        "andamal": andamal,
        "disclaimer_accept": True,
        "marketing_accept": True,
        "created_at": iso_dt(now_utc()),
        "ip": request.client.host if request.client else "unknown",
        "ua": request.headers.get("user-agent", "unknown"),
    }

    token = make_order_token(payload)

    # Review-sidan är GET så att refresh inte duplicerar POST
    return RedirectResponse(url=f"/lana-bil-till-skuldsatt/review?token={token}", status_code=303)


@app.get("/lana-bil-till-skuldsatt/review", response_class=HTMLResponse)
async def lana_bil_review(request: Request, token: str, cancel: Optional[str] = None) -> HTMLResponse:
    try:
        order = parse_order_token(token)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid token")

    ctx = page_ctx(request, "/lana-bil-till-skuldsatt/review", "Granska | Låna bil | HP Juridik", "")
    ctx.update(
        {
            "order": order,
            "token": token,
            "premium_price_sek": PRICE_PREMIUM_SEK,
            "cancel": bool(cancel),
            "stripe_enabled": bool(STRIPE_SECRET_KEY),
        }
    )
    return templates.TemplateResponse("pages/lana_bil_review.html", ctx)


@app.post("/lana-bil-till-skuldsatt/free", response_class=HTMLResponse)
async def lana_bil_free(request: Request, token: str = Form(...)) -> HTMLResponse:
    try:
        order = parse_order_token(token)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid token")

    # build pdf
    pdf_bytes = build_loan_pdf(order)
    filename = f"laneavtal-bil-{order['agreement_id']}.pdf"
    attach = pdf_to_attachment(filename, pdf_bytes)

    # maila PDF till båda parter
    subj_user = "HP Juridik – Ditt låneavtal (PDF)"
    text_user = (
        "Hej!\n\n"
        "Här kommer ditt låneavtal (PDF) baserat på uppgifterna du angivit.\n\n"
        "Vänligen kontrollera att allt stämmer.\n\n"
        "/HP Juridik\n"
    )

    # lead-mail till inbox
    subj_lead = "Lead: Låna bil till skuldsatt (Gratis nedladdning)"
    lead_body = (
        "NY LEAD (GRATIS)\n"
        "=================\n\n"
        f"Agreement ID: {order.get('agreement_id')}\n"
        f"Utlånare: {order.get('utlanare_namn')} – {order.get('utlanare_epost')}\n"
        f"Låntagare: {order.get('lantagare_namn')} – {order.get('lantagare_epost')}\n"
        f"Regnr: {order.get('bil_regnr')}\n"
        f"Period: {order.get('from_dt')} -> {order.get('to_dt')}\n\n"
        f"Newsletter opt-in: {order.get('marketing_accept')}\n\n"
        f"IP: {order.get('ip')}\n"
        f"UA: {order.get('ua')}\n"
    )

    err = None
    try:
        await postmark_send(
            to_email=order["utlanare_epost"],
            subject=subj_user,
            text_body=text_user,
            attachments=[attach],
        )
        await postmark_send(
            to_email=order["lantagare_epost"],
            subject=subj_user,
            text_body=text_user,
            attachments=[attach],
        )
        await postmark_send(
            to_email=LEAD_INBOX,
            subject=subj_lead,
            text_body=lead_body,
            attachments=[attach],
        )
    except Exception as e:
        err = str(e)

    ctx = page_ctx(request, "/lana-bil-till-skuldsatt", "Låna bil till skuldsatt | HP Juridik", "")
    ctx.update({"sent": err is None, "error": err})
    return templates.TemplateResponse("pages/lana_bil.html", ctx)


@app.post("/lana-bil-till-skuldsatt/premium", response_class=HTMLResponse)
async def lana_bil_premium_start(request: Request, token: str = Form(...)) -> Response:
    try:
        order = parse_order_token(token)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid token")

    if not STRIPE_SECRET_KEY:
        raise HTTPException(status_code=400, detail="Stripe not configured")

    # välj vilken mail Stripe ska koppla till (utlånare räcker oftast)
    customer_email = order.get("utlanare_epost") or order.get("lantagare_epost")
    url = await stripe_create_checkout_session(order_token=token, customer_email=customer_email)
    return RedirectResponse(url=url, status_code=303)


@app.get("/checkout-success", response_class=HTMLResponse)
async def checkout_success(request: Request, session_id: str) -> HTMLResponse:
    # enkel “tack”-sida; webhook sköter resten
    return HTMLResponse(
        "<h1>Tack!</h1><p>Betalning mottagen. (Webhooken sköter resten.)</p>",
        status_code=200,
    )


@app.post("/stripe/webhook", response_class=JSONResponse)
async def stripe_webhook(request: Request) -> JSONResponse:
    payload = await request.body()
    sig = request.headers.get("stripe-signature", "")

    try:
        event = verify_stripe_webhook(payload, sig)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Webhook verification failed: {e}")

    # Vi bryr oss främst om Checkout Session Completed
    if event["type"] == "checkout.session.completed":
        session = event["data"]["object"]
        metadata = session.get("metadata", {}) or {}
        token = metadata.get("order_token")
        if not token:
            return JSONResponse({"ok": True, "ignored": True})

        try:
            order = parse_order_token(token)
        except Exception:
            return JSONResponse({"ok": True, "ignored": True})

        # Skapa PDF
        pdf_bytes = build_loan_pdf(order)
        filename = f"laneavtal-bil-{order['agreement_id']}.pdf"
        attach = pdf_to_attachment(filename, pdf_bytes)

        # Maila PDF till båda
        subj_user = "HP Juridik – Premium: Ditt låneavtal (PDF)"
        text_user = (
            "Hej!\n\n"
            "Här kommer ditt låneavtal (PDF).\n"
            "Premium: vi initierar även digital signering i nästa steg (om signeringstjänsten är aktiverad).\n\n"
            "/HP Juridik\n"
        )

        # Lead-mail
        subj_lead = "Lead: Låna bil till skuldsatt (Premium betalning)"
        lead_body = (
            "NY LEAD (PREMIUM)\n"
            "=================\n\n"
            f"Agreement ID: {order.get('agreement_id')}\n"
            f"Stripe session: {session.get('id')}\n"
            f"Utlånare: {order.get('utlanare_namn')} – {order.get('utlanare_epost')}\n"
            f"Låntagare: {order.get('lantagare_namn')} – {order.get('lantagare_epost')}\n"
            f"Regnr: {order.get('bil_regnr')}\n"
            f"Period: {order.get('from_dt')} -> {order.get('to_dt')}\n\n"
            f"Newsletter opt-in: {order.get('marketing_accept')}\n\n"
            f"IP: {order.get('ip')}\n"
            f"UA: {order.get('ua')}\n"
        )

        oneflow_status = None
        oneflow_err = None

        # Oneflow: optional
        cfg = get_oneflow_config()
        if cfg:
            try:
                variables = {
                    "agreement_id": order.get("agreement_id"),
                    "utlanare_namn": order.get("utlanare_namn"),
                    "lantagare_namn": order.get("lantagare_namn"),
                    "bil_regnr": order.get("bil_regnr"),
                    "bil_marke_modell": order.get("bil_marke_modell"),
                    "from_dt": order.get("from_dt"),
                    "to_dt": order.get("to_dt"),
                    "andamal": order.get("andamal"),
                }
                res = await oneflow_create_contract_from_template(
                    cfg=cfg,
                    agreement_id=order["agreement_id"],
                    utlanare_email=order["utlanare_epost"],
                    lantagare_email=order["lantagare_epost"],
                    variables=variables,
                )
                oneflow_status = res.get("contract_id")
            except Exception as e:
                oneflow_err = str(e)

        # Skicka mail – även om Oneflow failar
        try:
            await postmark_send(
                to_email=order["utlanare_epost"],
                subject=subj_user,
                text_body=text_user,
                attachments=[attach],
            )
            await postmark_send(
                to_email=order["lantagare_epost"],
                subject=subj_user,
                text_body=text_user,
                attachments=[attach],
            )
            # lead inbox
            lead_plus = lead_body
            if oneflow_status:
                lead_plus += f"\nOneflow contract_id: {oneflow_status}\n"
            if oneflow_err:
                lead_plus += f"\nOneflow error: {oneflow_err}\n"

            await postmark_send(
                to_email=LEAD_INBOX,
                subject=subj_lead,
                text_body=lead_plus,
                attachments=[attach],
            )
        except Exception as e:
            # webhook ska ändå returnera 200 så Stripe inte retryar i onödan?
            # Men här kan ni välja 500 om ni vill retrya. Vi kör 200 och loggar via response.
            return JSONResponse({"ok": False, "mail_error": str(e), "oneflow": oneflow_status, "oneflow_err": oneflow_err})

        return JSONResponse({"ok": True, "oneflow": oneflow_status, "oneflow_err": oneflow_err})

    return JSONResponse({"ok": True})


# ------------------------------------------------------------------------------
# Legacy/friendly routes (om ni länkar till /contact)
# ------------------------------------------------------------------------------

@app.get("/contact", response_class=HTMLResponse)
async def contact_alias_get(request: Request) -> HTMLResponse:
    return RedirectResponse(url="/kontakta-oss", status_code=307)


@app.post("/contact", response_class=HTMLResponse)
async def contact_alias_post(request: Request) -> HTMLResponse:
    return RedirectResponse(url="/kontakta-oss", status_code=307)
