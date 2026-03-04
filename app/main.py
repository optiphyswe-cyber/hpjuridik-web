import os
import io
import uuid
import base64
import smtplib
from datetime import datetime, timezone
from typing import Dict, Any, List, Optional

import stripe
from fastapi import FastAPI, Request, Form, Header, HTTPException
from fastapi.responses import HTMLResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware
from starlette.responses import RedirectResponse, Response

# ReportLab PDF
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle
from reportlab.lib import colors

# -------------------------
# App + templates
# -------------------------
app = FastAPI()
app.mount("/static", StaticFiles(directory="app/static"), name="static")
templates = Jinja2Templates(directory="app/templates")

# -------------------------
# ENV / Settings
# -------------------------
BASE_URL = os.getenv("BASE_URL", "http://localhost:8000").rstrip("/")
SESSION_SECRET = os.getenv("SESSION_SECRET", "dev-change-me")

SMTP_HOST = os.getenv("SMTP_HOST", "")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER = os.getenv("SMTP_USER", "")
SMTP_PASS = os.getenv("SMTP_PASS", "")

POSTMARK_SERVER_TOKEN = os.getenv("POSTMARK_SERVER_TOKEN", "").strip()  # (valfritt)
MAIL_FROM = os.getenv("MAIL_FROM", (SMTP_USER or "lanabil@hpjuridik.se"))
CONTACT_TO = os.getenv("CONTACT_TO", "hp@hpjuridik.se")
LEAD_INBOX = os.getenv("LEAD_INBOX", "lanabil@hpjuridik.se")

STRIPE_SECRET_KEY = os.getenv("STRIPE_SECRET_KEY", "").strip()
STRIPE_WEBHOOK_SECRET = os.getenv("STRIPE_WEBHOOK_SECRET", "").strip()
PREMIUM_PRICE_ORE = int(os.getenv("PREMIUM_PRICE_ORE", "15000"))  # 15000 = 150 kr

# Canonical host (valfritt, men ni hade detta innan)
CANONICAL_HOST = os.getenv("CANONICAL_HOST", "www.hpjuridik.se").strip().lower()
SITE_URL = os.getenv("SITE_URL", f"https://{CANONICAL_HOST}").rstrip("/")

app.add_middleware(SessionMiddleware, secret_key=SESSION_SECRET)

if STRIPE_SECRET_KEY:
    stripe.api_key = STRIPE_SECRET_KEY

# -------------------------
# Company info
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
# In-memory Agreement store (MVP)
# -------------------------
AGREEMENTS: Dict[str, Dict[str, Any]] = {}

def utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()

def page_ctx(request: Request, path: str, title: str, description: str):
    canonical = f"{SITE_URL}{path if path.startswith('/') else '/' + path}"
    return {
        "request": request,
        "company": COMPANY,
        "seo": {
            "title": title,
            "description": description,
            "canonical": canonical,
            "robots": "index, follow",
        },
        "path": path,
    }

# -------------------------
# Email helpers (SMTP; Postmark kan kopplas in senare)
# -------------------------
def smtp_send(to_emails: List[str], subject: str, text: str, pdf_bytes: Optional[bytes] = None):
    from email.message import EmailMessage

    msg = EmailMessage()
    msg["From"] = MAIL_FROM
    msg["To"] = ", ".join([e for e in to_emails if e])
    msg["Subject"] = subject
    msg.set_content(text)

    if pdf_bytes:
        msg.add_attachment(pdf_bytes, maintype="application", subtype="pdf", filename="avtal.pdf")

    if not SMTP_HOST or not SMTP_USER or not SMTP_PASS:
        # Om SMTP inte är satt: faila tydligt
        raise RuntimeError("SMTP settings missing (SMTP_HOST/SMTP_USER/SMTP_PASS).")

    with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
        server.starttls()
        server.login(SMTP_USER, SMTP_PASS)
        server.send_message(msg)

def send_email(to_emails: List[str], subject: str, text: str, pdf_bytes: Optional[bytes] = None):
    """
    MVP: SMTP.
    (Ni har POSTMARK_SERVER_TOKEN i env – men er nuvarande kod kör SMTP.
     Vill ni byta till Postmark nu så säger du till, så ger jag Postmark-varianten.)
    """
    smtp_send(to_emails, subject, text, pdf_bytes)

# -------------------------
# PDF generator (ReportLab)
# -------------------------
def build_loan_pdf(data: Dict[str, Any]) -> bytes:
    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf,
        pagesize=A4,
        leftMargin=18 * mm,
        rightMargin=18 * mm,
        topMargin=18 * mm,
        bottomMargin=18 * mm,
    )
    styles = getSampleStyleSheet()
    h1 = ParagraphStyle("H1", parent=styles["Heading1"], fontSize=16, leading=20, spaceAfter=10)
    p = ParagraphStyle("P", parent=styles["BodyText"], fontSize=10.5, leading=14)

    story = []
    story.append(Paragraph("Tillfälligt låneavtal – bil", h1))
    story.append(Paragraph(f"Avtals-ID: {data.get('agreement_id','')}", p))
    story.append(Paragraph(f"Skapat (UTC): {data.get('created_utc','')}", p))
    story.append(Spacer(1, 8))

    def row(label, value):
        return [Paragraph(f"<b>{label}</b>", p), Paragraph(str(value or ""), p)]

    table_data = [
        row("Utlånare – namn", data.get("utlanare_namn")),
        row("Utlånare – personnummer", data.get("utlanare_pnr")),
        row("Utlånare – adress", data.get("utlanare_adress")),
        row("Utlånare – telefon", data.get("utlanare_tel")),
        row("Utlånare – e-post", data.get("utlanare_epost")),
        row("Låntagare – namn", data.get("lantagare_namn")),
        row("Låntagare – personnummer", data.get("lantagare_pnr")),
        row("Låntagare – adress", data.get("lantagare_adress")),
        row("Låntagare – telefon", data.get("lantagare_tel")),
        row("Låntagare – e-post", data.get("lantagare_epost")),
        row("Fordon – märke/modell", data.get("bil_marke_modell")),
        row("Fordon – reg.nr", data.get("bil_regnr")),
        row("Avtalsperiod – från", data.get("from_dt")),
        row("Avtalsperiod – till", data.get("to_dt")),
        row("Ändamål/syfte", data.get("andamal")),
    ]

    t = Table(table_data, colWidths=[55 * mm, 110 * mm])
    t.setStyle(
        TableStyle(
            [
                ("BOX", (0, 0), (-1, -1), 0.8, colors.black),
                ("INNERGRID", (0, 0), (-1, -1), 0.4, colors.grey),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("BACKGROUND", (0, 0), (-1, 0), colors.whitesmoke),
                ("LEFTPADDING", (0, 0), (-1, -1), 6),
                ("RIGHTPADDING", (0, 0), (-1, -1), 6),
                ("TOPPADDING", (0, 0), (-1, -1), 4),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
            ]
        )
    )
    story.append(t)
    story.append(Spacer(1, 12))

    story.append(Paragraph("<b>Friskrivning</b>", p))
    story.append(
        Paragraph(
            "Detta dokument är ett standardiserat bevisunderlag baserat på angivna uppgifter. "
            "HP Juridik lämnar ingen garanti för att avtalet godtas av Kronofogden, domstol eller annan part. "
            "Myndighetsbedömningar sker alltid utifrån en helhetsprövning.",
            p,
        )
    )
    story.append(Spacer(1, 18))
    story.append(Paragraph("Signaturer:", p))
    story.append(Spacer(1, 24))
    story.append(Paragraph("______________________________", p))
    story.append(Paragraph("Utlånare", p))
    story.append(Spacer(1, 18))
    story.append(Paragraph("______________________________", p))
    story.append(Paragraph("Låntagare", p))

    doc.build(story)
    return buf.getvalue()

# -------------------------
# Routes
# -------------------------
@app.get("/", response_class=HTMLResponse)
def home(request: Request):
    ctx = page_ctx(request, "/", "HP Juridik | Juridisk rådgivning", "Personlig och trygg juridisk rådgivning.")
    ctx.update({
        "sent": request.query_params.get("sent") == "1",   # kontakt skickad
        "free_ok": request.query_params.get("free") == "1",
        "premium_ok": request.query_params.get("premium") == "1",
        "error": None,
    })
    return templates.TemplateResponse("pages/home.html", ctx)

@app.get("/kontakta-oss", response_class=HTMLResponse)
def contact_page(request: Request):
    ctx = page_ctx(request, "/kontakta-oss", "Kontakt | HP Juridik", "Kontakta HP Juridik.")
    ctx.update({"sent": False, "error": None})
    return templates.TemplateResponse("pages/contact.html", ctx)

# Kontakt: ni vill stanna på home (ingen redirect till /kontakta-oss)
@app.post("/kontakta-oss", response_class=HTMLResponse)
def contact_submit(
    request: Request,
    name: str = Form(""),
    email: str = Form(""),
    phone: str = Form(""),
    message: str = Form(""),
):
    subject = "HP Juridik | Ny kontaktförfrågan från webb"
    body = (
        f"NY KONTAKTFÖRFRÅGAN (HPJURIDIK.SE)\n\n"
        f"Namn: {name}\n"
        f"E-post: {email}\n"
        f"Telefon: {phone}\n\n"
        f"MEDDELANDE\n"
        f"------------------------------\n"
        f"{message}\n\n"
        f"Teknisk info\n"
        f"------------------------------\n"
        f"Tid (UTC): {utc_iso()}\n"
        f"IP: {request.client.host if request.client else ''}\n"
        f"User-Agent: {request.headers.get('user-agent','')}\n\n"
        f"Signatur\n"
        f"------------------------------\n"
        f"{COMPANY['signature_name']} // {COMPANY['brand']}\n"
        f"{COMPANY['phone']}\n"
        f"{COMPANY['website']}\n"
        f"{COMPANY['address']}\n"
        f"{COMPANY['company']}\n"
        f"{COMPANY['orgnr']}\n"
    )

    try:
        send_email([CONTACT_TO], subject, body, pdf_bytes=None)
    except Exception as e:
        ctx = page_ctx(request, "/", "HP Juridik | Juridisk rådgivning", "Personlig och trygg juridisk rådgivning.")
        ctx.update({"sent": False, "error": f"Kunde inte skicka mail: {e}"})
        return templates.TemplateResponse("pages/home.html", ctx, status_code=500)

    # rendera home med tack-box (som du vill)
    ctx = page_ctx(request, "/", "HP Juridik | Juridisk rådgivning", "Personlig och trygg juridisk rådgivning.")
    ctx.update({"sent": True, "error": None})
    return templates.TemplateResponse("pages/home.html", ctx)

@app.get("/lana-bil-till-skuldsatt", response_class=HTMLResponse)
def lana_bil_form(request: Request):
    ctx = page_ctx(
        request,
        "/lana-bil-till-skuldsatt",
        "Låna bil till skuldsatt | HP Juridik",
        "Skapa avtal – granska, välj Gratis eller Premium.",
    )
    ctx.update({"error": None})
    return templates.TemplateResponse("pages/lana_bil.html", ctx)

@app.post("/lana-bil-till-skuldsatt")
def lana_bil_submit(
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
    from_dt: str = Form(...),  # "YYYY-MM-DDTHH:MM" från datetime-local
    to_dt: str = Form(...),
    andamal: str = Form(...),
    disclaimer_accept: Optional[str] = Form(None),
):
    if not disclaimer_accept:
        ctx = page_ctx(
            request,
            "/lana-bil-till-skuldsatt",
            "Låna bil till skuldsatt | HP Juridik",
            "Skapa avtal – granska, välj Gratis eller Premium.",
        )
        ctx.update({"error": "Du måste godkänna friskrivningen för att fortsätta."})
        return templates.TemplateResponse("pages/lana_bil.html", ctx, status_code=400)

    bil_regnr_norm = "".join((bil_regnr or "").split()).upper()

    try:
        # datetime-local ger "2026-03-04T09:13" (fromisoformat klarar det)
        datetime.fromisoformat(from_dt)
        datetime.fromisoformat(to_dt)
    except ValueError:
        ctx = page_ctx(request, "/lana-bil-till-skuldsatt", "Låna bil till skuldsatt | HP Juridik", "Skapa avtal.")
        ctx.update({"error": "Ogiltigt datum/tid-format."})
        return templates.TemplateResponse("pages/lana_bil.html", ctx, status_code=400)

    if to_dt <= from_dt:
        ctx = page_ctx(request, "/lana-bil-till-skuldsatt", "Låna bil till skuldsatt | HP Juridik", "Skapa avtal.")
        ctx.update({"error": "Till-datum/tid måste vara efter Från-datum/tid."})
        return templates.TemplateResponse("pages/lana_bil.html", ctx, status_code=400)

    agreement_id = str(uuid.uuid4())
    data = {
        "agreement_id": agreement_id,
        "created_utc": utc_iso(),
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
        "bil_regnr": bil_regnr_norm,
        "from_dt": from_dt.replace("T", " "),
        "to_dt": to_dt.replace("T", " "),
        "andamal": andamal,
    }

    pdf_bytes = build_loan_pdf(data)

    AGREEMENTS[agreement_id] = {
        "agreement_id": agreement_id,
        "created_utc": data["created_utc"],
        "data": data,
        "pdf_b64": base64.b64encode(pdf_bytes).decode("utf-8"),
        "is_paid": False,
        "stripe_session_id": None,
        "delivered": False,
    }

    request.session["agreement_id"] = agreement_id
    return RedirectResponse(url="/lana-bil-till-skuldsatt/review", status_code=303)

@app.get("/lana-bil-till-skuldsatt/review", response_class=HTMLResponse)
def lana_bil_review_get(request: Request):
    agreement_id = request.session.get("agreement_id")
    if not agreement_id or agreement_id not in AGREEMENTS:
        return RedirectResponse(url="/lana-bil-till-skuldsatt", status_code=303)

    agreement = AGREEMENTS[agreement_id]
    ctx = page_ctx(
        request,
        "/lana-bil-till-skuldsatt/review",
        "Granska uppgifter | Låna bil till skuldsatt",
        "Granska och välj Gratis eller Premium.",
    )
    ctx.update({"agreement": agreement, "error": None})
    return templates.TemplateResponse("pages/lana_bil_review.html", ctx)

@app.post("/lana-bil-till-skuldsatt/review")
def lana_bil_review_post(request: Request, plan: str = Form(...)):
    agreement_id = request.session.get("agreement_id")
    if not agreement_id or agreement_id not in AGREEMENTS:
        return RedirectResponse(url="/lana-bil-till-skuldsatt", status_code=303)

    agreement = AGREEMENTS[agreement_id]
    data = agreement["data"]
    pdf_bytes = base64.b64decode(agreement["pdf_b64"])

    lender_email = data.get("utlanare_epost")
    borrower_email = data.get("lantagare_epost")

    if plan == "free":
        # Kund-PDF -> båda
        send_email(
            [lender_email, borrower_email],
            "Tillfälligt låneavtal – bil (PDF)",
            "Här kommer ert avtal som PDF. /HP Juridik",
            pdf_bytes=pdf_bytes,
        )

        # Lead -> lanabil@
        send_email(
            [LEAD_INBOX],
            "Lead: Låna bil till skuldsatt (FREE)",
            f"agreement_id: {agreement_id}\n\n{data}",
            pdf_bytes=None,
        )

        agreement["delivered"] = True
        return RedirectResponse(url="/?free=1", status_code=303)

    if plan == "premium":
        if not STRIPE_SECRET_KEY:
            raise HTTPException(status_code=500, detail="STRIPE_SECRET_KEY saknas.")

        session = stripe.checkout.Session.create(
            mode="payment",
            payment_method_types=["card"],
            line_items=[
                {
                    "price_data": {
                        "currency": "sek",
                        "product_data": {"name": "Premium – Låna bil till skuldsatt"},
                        "unit_amount": PREMIUM_PRICE_ORE,
                    },
                    "quantity": 1,
                }
            ],
            metadata={"agreement_id": agreement_id},
            success_url=f"{BASE_URL}/checkout-success?agreement_id={agreement_id}",
            cancel_url=f"{BASE_URL}/checkout-cancel?agreement_id={agreement_id}",
        )

        agreement["stripe_session_id"] = session.id
        return RedirectResponse(url=session.url, status_code=303)

    raise HTTPException(status_code=400, detail="Invalid plan")

@app.post("/stripe/webhook")
async def stripe_webhook(request: Request, stripe_signature: str = Header(None)):
    if not STRIPE_WEBHOOK_SECRET:
        raise HTTPException(status_code=500, detail="STRIPE_WEBHOOK_SECRET saknas.")

    payload = await request.body()

    try:
        event = stripe.Webhook.construct_event(
            payload=payload, sig_header=stripe_signature, secret=STRIPE_WEBHOOK_SECRET
        )
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Webhook error: {e}")

    if event["type"] == "checkout.session.completed":
        session = event["data"]["object"]
        agreement_id = (session.get("metadata") or {}).get("agreement_id")

        if agreement_id and agreement_id in AGREEMENTS:
            agreement = AGREEMENTS[agreement_id]
            if not agreement.get("delivered"):
                agreement["is_paid"] = True

                data = agreement["data"]
                pdf_bytes = base64.b64decode(agreement["pdf_b64"])
                lender_email = data.get("utlanare_epost")
                borrower_email = data.get("lantagare_epost")

                # Kund-PDF -> båda
                send_email(
                    [lender_email, borrower_email],
                    "Premium – ert avtal (PDF)",
                    "Tack för er betalning. Här kommer ert avtal som PDF. /HP Juridik",
                    pdf_bytes=pdf_bytes,
                )

                # Lead -> lanabil@
                send_email(
                    [LEAD_INBOX],
                    "Lead: Låna bil till skuldsatt (PREMIUM)",
                    f"agreement_id: {agreement_id}\n\n{data}\n\nstripe_session_id: {agreement.get('stripe_session_id')}",
                    pdf_bytes=None,
                )

                agreement["delivered"] = True

    return PlainTextResponse("ok")

@app.get("/checkout-success", response_class=HTMLResponse)
def checkout_success(request: Request):
    ctx = page_ctx(request, "/checkout-success", "Tack | HP Juridik", "Tack för din betalning.")
    ctx.update({"agreement_id": request.query_params.get("agreement_id")})
    return templates.TemplateResponse("pages/checkout_success.html", ctx)

@app.get("/checkout-cancel", response_class=HTMLResponse)
def checkout_cancel(request: Request):
    ctx = page_ctx(request, "/checkout-cancel", "Avbrutet | HP Juridik", "Betalningen avbröts.")
    ctx.update({"agreement_id": request.query_params.get("agreement_id")})
    return templates.TemplateResponse("pages/checkout_cancel.html", ctx)

@app.get("/healthz", response_class=PlainTextResponse)
def healthz():
    return "ok"
