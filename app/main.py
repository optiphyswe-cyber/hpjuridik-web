# app/main.py
from __future__ import annotations

import base64
import json
import logging
import os
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, Optional

import httpx
import stripe
from fastapi import FastAPI, Form, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from jinja2 import Environment
from starlette.middleware.sessions import SessionMiddleware
from starlette.templating import Jinja2Templates

# =========================
# Logging
# =========================
logging.basicConfig(level=logging.INFO)
log = logging.getLogger("hpjuridik")

# =========================
# Config
# =========================
def env(name: str, default: Optional[str] = None) -> str:
    v = os.getenv(name, default)
    if v is None:
        raise RuntimeError(f"Missing required env var: {name}")
    return v

BASE_URL = env("BASE_URL", "https://hpjuridik.se").rstrip("/")
POSTMARK_SERVER_TOKEN = env("POSTMARK_SERVER_TOKEN", "")
MAIL_FROM = env("MAIL_FROM", "lanabil@hpjuridik.se")

# Kontaktformulär ska INTE till lanabil, utan till hp@
CONTACT_TO = env("CONTACT_TO", "hp@hpjuridik.se")

# Lead-inbox för låna-bil-flödet (gratis/premium)
LEAD_INBOX = env("LEAD_INBOX", "lanabil@hpjuridik.se")

# Stripe
STRIPE_SECRET_KEY = env("STRIPE_SECRET_KEY", "")
STRIPE_WEBHOOK_SECRET = env("STRIPE_WEBHOOK_SECRET", "")

# Pris i öre (SEK)
PREMIUM_PRICE_ORE = int(env("PREMIUM_PRICE_ORE", "15000"))  # 150 kr default
CURRENCY = env("CURRENCY", "sek")

# Sessions
SESSION_SECRET = env("SESSION_SECRET", "change-me-in-render")

stripe.api_key = STRIPE_SECRET_KEY

# =========================
# App
# =========================
app = FastAPI()
app.add_middleware(SessionMiddleware, secret_key=SESSION_SECRET, same_site="lax", https_only=True)

# Static/templates
app.mount("/static", StaticFiles(directory="app/static"), name="static")
templates = Jinja2Templates(directory="app/templates")


# =========================
# In-memory storage (enkel & stabil)
# - Byt till DB senare om du vill
# =========================
@dataclass
class Agreement:
    agreement_id: str
    created_utc: str
    data: Dict[str, Any]
    pdf_b64: str  # PDF bytes base64
    is_paid: bool = False
    stripe_session_id: Optional[str] = None

AGREEMENTS: Dict[str, Agreement] = {}


# =========================
# Helpers
# =========================
def now_utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")

def client_ip(request: Request) -> str:
    # Render/reverse proxy: X-Forwarded-For kan finnas
    xff = request.headers.get("x-forwarded-for")
    if xff:
        return xff.split(",")[0].strip()
    if request.client:
        return request.client.host
    return "unknown"

def user_agent(request: Request) -> str:
    return request.headers.get("user-agent", "unknown")

async def postmark_send(
    *,
    to: str,
    subject: str,
    text_body: str,
    from_email: Optional[str] = None,
    reply_to: Optional[str] = None,
    attachments: Optional[list[dict]] = None,
) -> None:
    if not POSTMARK_SERVER_TOKEN:
        raise RuntimeError("POSTMARK_SERVER_TOKEN is not set")

    payload: Dict[str, Any] = {
        "From": from_email or MAIL_FROM,
        "To": to,
        "Subject": subject,
        "TextBody": text_body,
        "MessageStream": "outbound",
    }
    if reply_to:
        payload["ReplyTo"] = reply_to
    if attachments:
        payload["Attachments"] = attachments

    async with httpx.AsyncClient(timeout=20) as client:
        r = await client.post(
            "https://api.postmarkapp.com/email",
            headers={
                "X-Postmark-Server-Token": POSTMARK_SERVER_TOKEN,
                "Content-Type": "application/json",
            },
            content=json.dumps(payload),
        )
        if r.status_code >= 300:
            raise RuntimeError(f"Postmark error {r.status_code}: {r.text}")

def make_pdf_bytes(agreement: Agreement) -> bytes:
    # Minimal men stabil PDF. Du kan fylla på med mer text/klasuler senare.
    from reportlab.lib.pagesizes import A4
    from reportlab.pdfgen import canvas

    buf = bytearray()
    # reportlab vill ha file-like; vi använder BytesIO
    from io import BytesIO
    bio = BytesIO()
    c = canvas.Canvas(bio, pagesize=A4)

    w, h = A4
    y = h - 60

    def line(txt: str, dy: int = 16):
        nonlocal y
        c.drawString(50, y, txt)
        y -= dy

    d = agreement.data
    line("HP Juridik - Tillfälligt låneavtal (bil)")
    line(f"Avtals-ID: {agreement.agreement_id}")
    line(f"Skapat (UTC): {agreement.created_utc}")
    line("")

    line("Utlånare (ägare):")
    line(f"  Namn: {d.get('utlanare_namn','')}")
    line(f"  Personnummer: {d.get('utlanare_pnr','')}")
    line(f"  Adress: {d.get('utlanare_adress','')}")
    line(f"  Telefon: {d.get('utlanare_tel','')}")
    line(f"  E-post: {d.get('utlanare_epost','')}")
    line("")

    line("Låntagare (skuldsatt):")
    line(f"  Namn: {d.get('lantagare_namn','')}")
    line(f"  Personnummer: {d.get('lantagare_pnr','')}")
    line(f"  Adress: {d.get('lantagare_adress','')}")
    line(f"  Telefon: {d.get('lantagare_tel','')}")
    line(f"  E-post: {d.get('lantagare_epost','')}")
    line("")

    line("Fordon:")
    line(f"  Märke/modell: {d.get('bil_marke_modell','')}")
    line(f"  Regnr: {d.get('bil_regnr','')}")
    line("")

    line("Avtalsperiod:")
    line(f"  Från: {d.get('from_dt','')}")
    line(f"  Till: {d.get('to_dt','')}")
    line("")

    line("Ändamål / syfte:")
    # enkel radbrytning
    andamal = (d.get("andamal") or "").strip()
    for chunk in [andamal[i:i+95] for i in range(0, len(andamal), 95)] or [""]:
        line(f"  {chunk}")
    line("")

    line("Standardiserad bekräftelse:")
    line("  Avtalet bygger på användarens uppgifter och är ett bevisunderlag.")
    line("  Ingen garanti lämnas för myndighetsbedömning.")
    line("")

    c.showPage()
    c.save()
    return bio.getvalue()

def pdf_attachment(filename: str, pdf_bytes: bytes) -> dict:
    return {
        "Name": filename,
        "Content": base64.b64encode(pdf_bytes).decode("utf-8"),
        "ContentType": "application/pdf",
    }

def page_ctx(request: Request, path: str, title: str, description: str = "") -> Dict[str, Any]:
    return {
        "request": request,
        "path": path,
        "title": title,
        "description": description,
        "year": datetime.now().year,
    }


# =========================
# Routes: Pages
# =========================
@app.get("/", response_class=HTMLResponse)
async def home(request: Request):
    ctx = page_ctx(request, "/", "HP Juridik", "Juridisk hjälp och dokument")
    # flags för att visa tack-ruta på home efter contact-submit
    ctx.update({
        "contact_sent": request.session.pop("contact_sent", False),
        "contact_error": request.session.pop("contact_error", None),
    })
    return templates.TemplateResponse("pages/home.html", ctx)

@app.get("/kontakta-oss", response_class=HTMLResponse)
async def contact_page(request: Request):
    ctx = page_ctx(request, "/kontakta-oss", "Kontakt | HP Juridik", "Kontakta HP Juridik")
    # (Sidan kan fortfarande finnas, men vi vill inte redirecta hit efter submit)
    ctx.update({
        "sent_ok": False,
        "sent_error": None,
    })
    return templates.TemplateResponse("pages/contact.html", ctx)

# Alias om du redan har form action="/contact"
@app.post("/contact", response_class=HTMLResponse)
@app.post("/kontakta-oss", response_class=HTMLResponse)
async def contact_submit(
    request: Request,
    namn: str = Form(...),
    epost: str = Form(...),
    telefon: str = Form(""),
    meddelande: str = Form(...),
):
    ts = now_utc_iso()
    ip = client_ip(request)
    ua = user_agent(request)

    subject = "HP Juridik | Ny kontaktförfrågan"
    body = (
        "NY KONTAKTFÖRFRÅGAN (hpjuridik.se)\n"
        "============================\n\n"
        f"Tid (UTC): {ts}\n"
        f"Namn: {namn}\n"
        f"E-post: {epost}\n"
        f"Telefon: {telefon}\n\n"
        "Meddelande:\n"
        f"{meddelande}\n\n"
        "Tekniskt:\n"
        f"IP: {ip}\n"
        f"UA: {ua}\n"
    )

    try:
        await postmark_send(
            to=CONTACT_TO,
            subject=subject,
            text_body=body,
            reply_to=epost,  # så du kan svara direkt
        )
        request.session["contact_sent"] = True
        request.session["contact_error"] = None
    except Exception as e:
        log.exception("contact_submit failed")
        request.session["contact_sent"] = False
        request.session["contact_error"] = str(e)

    # Viktigt: rendera HOME utan redirect till /kontakta-oss
    return RedirectResponse(url="/", status_code=303)


# =========================
# Låna bil (form + review + free/premium)
# =========================
@app.get("/lana-bil-till-skuldsatt", response_class=HTMLResponse)
async def lana_bil_form(request: Request):
    ctx = page_ctx(
        request,
        "/lana-bil-till-skuldsatt",
        "Låna bil till skuldsatt | HP Juridik",
        "Skapa tillfälligt låneavtal för bil",
    )
    return templates.TemplateResponse("pages/lana_bil.html", ctx)

@app.post("/lana-bil-till-skuldsatt", response_class=HTMLResponse)
async def lana_bil_submit(
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

    disclaimer_accept: Optional[str] = Form(None),
    marketing_accept: Optional[str] = Form(None),
):
    agreement_id = str(uuid.uuid4())
    data = {
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
        "disclaimer_accept": bool(disclaimer_accept),
        "marketing_accept": bool(marketing_accept),
    }

    agreement = Agreement(
        agreement_id=agreement_id,
        created_utc=now_utc_iso(),
        data=data,
        pdf_b64="",  # set below
    )
    pdf = make_pdf_bytes(agreement)
    agreement.pdf_b64 = base64.b64encode(pdf).decode("utf-8")
    AGREEMENTS[agreement_id] = agreement

    request.session["agreement_id"] = agreement_id
    return RedirectResponse(url="/lana-bil-till-skuldsatt/review", status_code=303)

@app.get("/lana-bil-till-skuldsatt/review", response_class=HTMLResponse)
async def lana_bil_review_get(request: Request):
    agreement_id = request.session.get("agreement_id")
    if not agreement_id or agreement_id not in AGREEMENTS:
        return RedirectResponse(url="/lana-bil-till-skuldsatt", status_code=303)

    ag = AGREEMENTS[agreement_id]
    ctx = page_ctx(request, "/lana-bil-till-skuldsatt/review", "Granska | HP Juridik", "")
    ctx.update({
        "agreement_id": ag.agreement_id,
        "data": ag.data,
        "premium_price_sek": f"{PREMIUM_PRICE_ORE/100:.0f}",
        "msg": request.session.pop("review_msg", None),
        "err": request.session.pop("review_err", None),
    })
    return templates.TemplateResponse("pages/lana_bil_review.html", ctx)

@app.post("/lana-bil-till-skuldsatt/review", response_class=HTMLResponse)
async def lana_bil_review_post(
    request: Request,
    plan: str = Form(...),  # "free" | "premium"
    disclaimer_accept: Optional[str] = Form(None),
    marketing_accept: Optional[str] = Form(None),
):
    agreement_id = request.session.get("agreement_id")
    if not agreement_id or agreement_id not in AGREEMENTS:
        return RedirectResponse(url="/lana-bil-till-skuldsatt", status_code=303)

    ag = AGREEMENTS[agreement_id]

    # krav: båda checkboxar i din UI (som du hade)
    if not disclaimer_accept or not marketing_accept:
        request.session["review_err"] = "Du måste kryssa i båda rutorna för att gå vidare."
        return RedirectResponse(url="/lana-bil-till-skuldsatt/review", status_code=303)

    # uppdatera sparat
    ag.data["disclaimer_accept"] = True
    ag.data["marketing_accept"] = True

    pdf = base64.b64decode(ag.pdf_b64.encode("utf-8"))
    attach = [pdf_attachment(f"laneavtal_{ag.agreement_id}.pdf", pdf)]

    # Gratis: maila PDF till båda + lead inbox
    if plan == "free":
        try:
            subject = "HP Juridik | Låneavtal (bil) - PDF"
            body = (
                "Här kommer ert låneavtal som PDF.\n\n"
                f"Avtals-ID: {ag.agreement_id}\n"
                "Om ni behöver ändra något, skapa ett nytt avtal via hpjuridik.se.\n"
            )
            await postmark_send(
                to=f"{ag.data['utlanare_epost']},{ag.data['lantagare_epost']}",
                subject=subject,
                text_body=body,
                attachments=attach,
            )

            # notifiering till lanabil@ (lead inbox)
            await postmark_send(
                to=LEAD_INBOX,
                subject="Lead: Låna bil till skuldsatt (Gratis nedladdning)",
                text_body=(
                    "NY LEAD (GRATIS)\n"
                    "==============\n\n"
                    f"Agreement ID: {ag.agreement_id}\n"
                    f"Utlånare: {ag.data['utlanare_namn']} - {ag.data['utlanare_epost']}\n"
                    f"Låntagare: {ag.data['lantagare_namn']} - {ag.data['lantagare_epost']}\n"
                    f"Regnr: {ag.data['bil_regnr']}\n"
                    f"Period: {ag.data['from_dt']} -> {ag.data['to_dt']}\n\n"
                    f"Newsletter opt-in: {ag.data.get('marketing_accept')}\n"
                    f"IP: {client_ip(request)}\n"
                    f"UA: {user_agent(request)}\n"
                ),
            )

            request.session["review_msg"] = "Klart! PDF är skickad till båda parter."
        except Exception as e:
            log.exception("free flow failed")
            request.session["review_err"] = f"Något gick fel vid utskick: {e}"

        return RedirectResponse(url="/lana-bil-till-skuldsatt/review", status_code=303)

    # Premium: Stripe Checkout
    if plan == "premium":
        try:
            session = stripe.checkout.Session.create(
                mode="payment",
                currency=CURRENCY,
                line_items=[
                    {
                        "price_data": {
                            "currency": CURRENCY,
                            "product_data": {"name": "Premium: Låneavtal (bil) + nästa steg"},
                            "unit_amount": PREMIUM_PRICE_ORE,
                        },
                        "quantity": 1,
                    }
                ],
                metadata={"agreement_id": ag.agreement_id},
                success_url=f"{BASE_URL}/checkout-success?agreement_id={ag.agreement_id}",
                cancel_url=f"{BASE_URL}/lana-bil-till-skuldsatt/review",
            )
            ag.stripe_session_id = session["id"]
            AGREEMENTS[agreement_id] = ag
            return RedirectResponse(url=session["url"], status_code=303)
        except Exception as e:
            log.exception("stripe session create failed")
            request.session["review_err"] = f"Kunde inte starta betalning: {e}"
            return RedirectResponse(url="/lana-bil-till-skuldsatt/review", status_code=303)

    request.session["review_err"] = "Okänd plan."
    return RedirectResponse(url="/lana-bil-till-skuldsatt/review", status_code=303)


@app.get("/checkout-success", response_class=HTMLResponse)
async def checkout_success(request: Request, agreement_id: str = ""):
    ctx = page_ctx(request, "/checkout-success", "Tack! | HP Juridik", "")
    ctx.update({"agreement_id": agreement_id})
    return templates.TemplateResponse("pages/checkout_success.html", ctx)


# =========================
# Stripe webhook
# =========================
@app.post("/stripe/webhook")
async def stripe_webhook(request: Request):
    payload = await request.body()
    sig = request.headers.get("stripe-signature", "")
    try:
        event = stripe.Webhook.construct_event(payload, sig, STRIPE_WEBHOOK_SECRET)
    except Exception as e:
        log.warning("stripe webhook signature invalid: %s", e)
        return Response(status_code=400)

    event_type = event.get("type")
    data_object = event.get("data", {}).get("object", {})

    # Viktigast: checkout.session.completed
    if event_type == "checkout.session.completed":
        agreement_id = (data_object.get("metadata") or {}).get("agreement_id")
        if agreement_id and agreement_id in AGREEMENTS:
            ag = AGREEMENTS[agreement_id]
            ag.is_paid = True
            AGREEMENTS[agreement_id] = ag

            # Efter betalning: skicka PDF till båda + lead inbox
            try:
                pdf = base64.b64decode(ag.pdf_b64.encode("utf-8"))
                attach = [pdf_attachment(f"laneavtal_{ag.agreement_id}.pdf", pdf)]

                await postmark_send(
                    to=f"{ag.data['utlanare_epost']},{ag.data['lantagare_epost']}",
                    subject="HP Juridik | Premium - Låneavtal (bil) - PDF",
                    text_body=(
                        "Tack! Betalning mottagen.\n\n"
                        "Här kommer ert avtal som PDF.\n"
                        f"Avtals-ID: {ag.agreement_id}\n\n"
                        "Nästa steg (digital signering) kopplas på i nästa iteration.\n"
                    ),
                    attachments=attach,
                )

                await postmark_send(
                    to=LEAD_INBOX,
                    subject="Lead: Låna bil till skuldsatt (Premium betalning)",
                    text_body=(
                        "NY LEAD (PREMIUM)\n"
                        "=================\n\n"
                        f"Agreement ID: {ag.agreement_id}\n"
                        f"Stripe session: {ag.stripe_session_id}\n"
                        f"Utlånare: {ag.data['utlanare_namn']} - {ag.data['utlanare_epost']}\n"
                        f"Låntagare: {ag.data['lantagare_namn']} - {ag.data['lantagare_epost']}\n"
                        f"Regnr: {ag.data['bil_regnr']}\n"
                        f"Period: {ag.data['from_dt']} -> {ag.data['to_dt']}\n\n"
                        f"Newsletter opt-in: {ag.data.get('marketing_accept')}\n"
                    ),
                )
            except Exception:
                log.exception("post-payment email failed")

    return Response(status_code=200)


# =========================
# Health
# =========================
@app.get("/healthz")
async def healthz():
    return {"ok": True}
