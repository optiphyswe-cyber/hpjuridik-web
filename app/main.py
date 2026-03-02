import os
import io
import json
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, Optional, List

import httpx
import stripe
from fastapi import FastAPI, Request, Form, HTTPException
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import RedirectResponse

# PDF
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer
from reportlab.lib.styles import getSampleStyleSheet

# =============================================================================
# App setup
# =============================================================================

app = FastAPI()

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(BASE_DIR, "static")
TEMPLATES_DIR = os.path.join(BASE_DIR, "templates")

app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
templates = Jinja2Templates(directory=TEMPLATES_DIR)

# =============================================================================
# ENV
# =============================================================================

SITE_URL = os.getenv("SITE_URL", "https://www.hpjuridik.se").rstrip("/")

POSTMARK_SERVER_TOKEN = os.getenv("POSTMARK_SERVER_TOKEN", "").strip()
MAIL_FROM = os.getenv("MAIL_FROM", "lanabil@hpjuridik.se").strip()
LEAD_INBOX = os.getenv("LEAD_INBOX", "lanabil@hpjuridik.se").strip()
CONTACT_TO = os.getenv("CONTACT_TO", "hp@hpjuridik.se").strip()

STRIPE_SECRET_KEY = os.getenv("STRIPE_SECRET_KEY", "").strip()
STRIPE_WEBHOOK_SECRET = os.getenv("STRIPE_WEBHOOK_SECRET", "").strip()
stripe.api_key = STRIPE_SECRET_KEY

# Oneflow
ONEFLOW_API_TOKEN = os.getenv("ONEFLOW_API_TOKEN", "").strip()
ONEFLOW_WORKSPACE_ID = os.getenv("ONEFLOW_WORKSPACE_ID", "").strip()
ONEFLOW_TEMPLATE_ID = os.getenv("ONEFLOW_TEMPLATE_ID", "").strip()

AGREEMENTS_DIR = "/tmp/agreements"
os.makedirs(AGREEMENTS_DIR, exist_ok=True)

# =============================================================================
# Helpers
# =============================================================================

def agreement_path(aid: str):
    return os.path.join(AGREEMENTS_DIR, f"{aid}.json")

def save_agreement(aid: str, data: Dict[str, Any]):
    with open(agreement_path(aid), "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)

def load_agreement(aid: str):
    p = agreement_path(aid)
    if not os.path.exists(p):
        raise HTTPException(404, "Agreement not found")
    with open(p, "r", encoding="utf-8") as f:
        return json.load(f)

async def postmark_send(to: str, subject: str, text_body: str):
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "X-Postmark-Server-Token": POSTMARK_SERVER_TOKEN,
    }
    payload = {
        "From": MAIL_FROM,
        "To": to,
        "Subject": subject,
        "TextBody": text_body,
    }
    async with httpx.AsyncClient() as client:
        r = await client.post("https://api.postmarkapp.com/email", headers=headers, json=payload)
        r.raise_for_status()

# =============================================================================
# PDF builder
# =============================================================================

def build_pdf(data: dict) -> bytes:
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4)
    styles = getSampleStyleSheet()
    story = []

    story.append(Paragraph("TILLFÄLLIGT LÅNEAVTAL – BIL", styles["Title"]))
    story.append(Spacer(1, 12))

    story.append(Paragraph(f"Utlånare: {data['utlanare']['namn']}", styles["Normal"]))
    story.append(Paragraph(f"Låntagare: {data['lantagare']['namn']}", styles["Normal"]))
    story.append(Paragraph(f"Regnr: {data['fordon']['regnr']}", styles["Normal"]))
    story.append(Paragraph(f"Period: {data['period']['from']} → {data['period']['to']}", styles["Normal"]))
    story.append(Spacer(1, 20))

    story.append(Paragraph("Underskrifter:", styles["Heading2"]))
    story.append(Spacer(1, 40))

    doc.build(story)
    return buf.getvalue()

# =============================================================================
# Oneflow integration
# =============================================================================

async def create_oneflow_document(agreement_id: str, agreement: dict):
    if not ONEFLOW_API_TOKEN:
        raise RuntimeError("ONEFLOW_API_TOKEN saknas")

    headers = {
        "Authorization": f"Bearer {ONEFLOW_API_TOKEN}",
        "Content-Type": "application/json",
    }

    fp = agreement["form_payload"]

    payload = {
        "workspace_id": ONEFLOW_WORKSPACE_ID,
        "template_id": ONEFLOW_TEMPLATE_ID,
        "title": f"Låneavtal bil – {fp['fordon']['regnr']}",
        "participants": [
            {"name": fp["utlanare"]["namn"], "email": fp["utlanare"]["epost"], "role": "Signer"},
            {"name": fp["lantagare"]["namn"], "email": fp["lantagare"]["epost"], "role": "Signer"},
        ],
    }

    async with httpx.AsyncClient() as client:
        r = await client.post("https://api.oneflow.com/v1/documents", headers=headers, json=payload)
        r.raise_for_status()
        return r.json()

# =============================================================================
# Contact
# =============================================================================

@app.get("/contact", response_class=HTMLResponse)
def contact(request: Request):
    return templates.TemplateResponse("pages/contact.html", {"request": request})

@app.post("/contact", response_class=HTMLResponse)
async def contact_submit(
    request: Request,
    namn: str = Form(...),
    epost: str = Form(...),
    telefon: str = Form(""),
    meddelande: str = Form(...)
):
    subject = f"HP Juridik | Ny kontaktförfrågan från {namn}"
    body = f"""
Namn: {namn}
E-post: {epost}
Telefon: {telefon}
Meddelande:
{meddelande}
"""
    try:
        await postmark_send(CONTACT_TO, subject, body)
        sent = True
    except Exception as e:
        print("Mail error:", e)
        sent = False

    return templates.TemplateResponse(
        "pages/contact.html",
        {"request": request, "sent_ok": sent}
    )

# =============================================================================
# Stripe webhook
# =============================================================================

@app.post("/stripe/webhook")
async def stripe_webhook(request: Request):
    payload = await request.body()
    sig = request.headers.get("stripe-signature")

    event = stripe.Webhook.construct_event(payload, sig, STRIPE_WEBHOOK_SECRET)

    if event["type"] == "checkout.session.completed":
        session = event["data"]["object"]
        if session["payment_status"] == "paid":
            agreement_id = session["metadata"]["agreement_id"]
            ag = load_agreement(agreement_id)

            ag["status"] = "paid"
            save_agreement(agreement_id, ag)

            try:
                oneflow_response = await create_oneflow_document(agreement_id, ag)
                ag["oneflow"] = oneflow_response
                save_agreement(agreement_id, ag)
            except Exception as e:
                print("Oneflow error:", e)

    return {"ok": True}
