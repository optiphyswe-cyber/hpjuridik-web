import os
import stripe
import httpx
from datetime import datetime, timezone

from fastapi import FastAPI, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

# =========================
# App setup
# =========================

app = FastAPI()

app.mount("/static", StaticFiles(directory="app/static"), name="static")
templates = Jinja2Templates(directory="app/templates")

# =========================
# Environment
# =========================

POSTMARK_SERVER_TOKEN = os.getenv("POSTMARK_SERVER_TOKEN")
LEAD_INBOX = os.getenv("LEAD_INBOX")
STRIPE_SECRET_KEY = os.getenv("STRIPE_SECRET_KEY")

stripe.api_key = STRIPE_SECRET_KEY

BASE_URL = "https://hpjuridik.se"


# =========================
# Helper: Postmark
# =========================

async def send_email(subject: str, body: str):
    if not POSTMARK_SERVER_TOKEN:
        print("POSTMARK_SERVER_TOKEN saknas")
        return

    async with httpx.AsyncClient() as client:
        await client.post(
            "https://api.postmarkapp.com/email",
            headers={
                "X-Postmark-Server-Token": POSTMARK_SERVER_TOKEN,
                "Accept": "application/json",
            },
            json={
                "From": LEAD_INBOX,
                "To": LEAD_INBOX,
                "Subject": subject,
                "TextBody": body,
            },
        )


# =========================
# Home
# =========================

@app.get("/", response_class=HTMLResponse)
def home(request: Request):
    return templates.TemplateResponse("pages/home.html", {"request": request})


# =========================
# CONTACT
# =========================

@app.get("/contact", response_class=HTMLResponse)
@app.get("/kontakta-oss", response_class=HTMLResponse)
def contact_get(request: Request):
    return templates.TemplateResponse(
        "pages/contact.html",
        {"request": request, "sent": False}
    )


@app.post("/contact", response_class=HTMLResponse)
@app.post("/kontakta-oss", response_class=HTMLResponse)
async def contact_post(
    request: Request,
    namn: str = Form(...),
    epost: str = Form(...),
    telefon: str = Form(""),
    meddelande: str = Form(...)
):
    ts = datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M")

    body = f"""
NY KONTAKTFÖRFRÅGAN

Namn: {namn}
E-post: {epost}
Telefon: {telefon}
Meddelande:
{meddelande}

Tid: {ts}
"""

    try:
        await send_email("Ny kontaktförfrågan – HP Juridik", body)
        sent = True
    except Exception as e:
        print(e)
        sent = False

    return templates.TemplateResponse(
        "pages/contact.html",
        {"request": request, "sent": sent}
    )


# =========================
# LÅNA BIL – FORM
# =========================

@app.get("/lana-bil-till-skuldsatt", response_class=HTMLResponse)
def lana_bil_form(request: Request):
    return templates.TemplateResponse(
        "pages/lana_bil.html",
        {"request": request}
    )


# =========================
# REVIEW (POST ONLY)
# =========================

@app.get("/lana-bil-till-skuldsatt/review")
def review_redirect():
    return RedirectResponse("/lana-bil-till-skuldsatt")


@app.post("/lana-bil-till-skuldsatt/review", response_class=HTMLResponse)
def lana_bil_review(
    request: Request,
    utlanare_namn: str = Form(...),
    utlanare_epost: str = Form(...),
    lantagare_namn: str = Form(...),
    lantagare_epost: str = Form(...),
    bil_marke_modell: str = Form(...),
    bil_regnr: str = Form(...),
    andamal: str = Form(...)
):
    data = {
        "utlanare_namn": utlanare_namn,
        "utlanare_epost": utlanare_epost,
        "lantagare_namn": lantagare_namn,
        "lantagare_epost": lantagare_epost,
        "bil_marke_modell": bil_marke_modell,
        "bil_regnr": bil_regnr,
        "andamal": andamal,
    }

    return templates.TemplateResponse(
        "pages/lana_bil_review.html",
        {"request": request, **data}
    )


# =========================
# GRATIS
# =========================

@app.post("/lana-bil-gratis")
async def lana_bil_gratis(
    utlanare_namn: str = Form(...),
    utlanare_epost: str = Form(...),
    lantagare_namn: str = Form(...),
    lantagare_epost: str = Form(...),
    bil_marke_modell: str = Form(...),
    bil_regnr: str = Form(...),
):
    body = f"""
GRATIS NEDLADDNING

Utlånare: {utlanare_namn} ({utlanare_epost})
Låntagare: {lantagare_namn} ({lantagare_epost})
Bil: {bil_marke_modell}
Regnr: {bil_regnr}
"""

    await send_email("Lead – Låna bil (gratis)", body)

    return RedirectResponse("/lana-bil-till-skuldsatt?success=1", status_code=303)


# =========================
# PREMIUM → STRIPE
# =========================

@app.post("/lana-bil-premium")
def lana_bil_premium():
    session = stripe.checkout.Session.create(
        payment_method_types=["card"],
        line_items=[{
            "price_data": {
                "currency": "sek",
                "product_data": {
                    "name": "Premium låneavtal bil"
                },
                "unit_amount": 100,  # 1 kr test
            },
            "quantity": 1,
        }],
        mode="payment",
        success_url=BASE_URL + "/checkout-success",
        cancel_url=BASE_URL + "/lana-bil-till-skuldsatt",
    )

    return RedirectResponse(session.url, status_code=303)


@app.get("/checkout-success", response_class=HTMLResponse)
def checkout_success(request: Request):
    return templates.TemplateResponse(
        "pages/checkout_success.html",
        {"request": request}
    )
