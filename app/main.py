# app/main.py
from __future__ import annotations

import os
import io
import json
import time
import uuid
import base64
import hmac
import hashlib
import sqlite3
import smtplib
from email.message import EmailMessage
from datetime import datetime, timezone
from typing import Optional, Dict, Any, Tuple

import requests
import stripe
from fastapi import FastAPI, Request, Form, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse, PlainTextResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

# PDF (ReportLab)
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas
from reportlab.lib.units import mm


# =============================================================================
# ENV
# =============================================================================
BASE_URL = os.getenv("BASE_URL", "https://www.hpjuridik.se").rstrip("/")

SESSION_SECRET = os.getenv("SESSION_SECRET", "dev-session-secret")

# Email / SMTP
SMTP_HOST = os.getenv("SMTP_HOST", "")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587") or "587")
SMTP_USER = os.getenv("SMTP_USER", "")
SMTP_PASS = os.getenv("SMTP_PASS", "")
MAIL_FROM = os.getenv("MAIL_FROM", SMTP_USER or "no-reply@hpjuridik.se")
LEAD_INBOX = os.getenv("LEAD_INBOX", "hp@hpjuridik.se")
CONTACT_TO = os.getenv("CONTACT_TO", LEAD_INBOX)

# Stripe
STRIPE_SECRET_KEY = os.getenv("STRIPE_SECRET_KEY", "")
STRIPE_WEBHOOK_SECRET = os.getenv("STRIPE_WEBHOOK_SECRET", "")
PREMIUM_PRICE_ORE = int(os.getenv("PREMIUM_PRICE_ORE", "300") or "300")  # 3,00 SEK default for test

# Oneflow
ONEFLOW_API_TOKEN = os.getenv("ONEFLOW_API_TOKEN", "")
ONEFLOW_BASE_URL = os.getenv("ONEFLOW_BASE_URL", "https://api.oneflow.com/v1").rstrip("/")
ONEFLOW_TEMPLATE_ID = os.getenv("ONEFLOW_TEMPLATE_ID", "")  # set to your template ID (e.g. 13789463)
ONEFLOW_WEBHOOK_SIGN_KEY = os.getenv("ONEFLOW_WEBHOOK_SIGN_KEY", "")  # e.g. HPJURIDIK_ONEFLOW_SECRET_2026

# Optional: who should be the internal organizer
ONEFLOW_ORG_NAME = os.getenv("ONEFLOW_ORG_NAME", "HP Juridik")
ONEFLOW_ORG_EMAIL = os.getenv("ONEFLOW_ORG_EMAIL", "hp@hpjuridik.se")

# Where SQLite will live (Render: use /var/data if you have a disk)
DB_PATH = os.getenv("DB_PATH", "/var/data/hpjuridik.sqlite3")


# =============================================================================
# App
# =============================================================================
app = FastAPI()
app.add_middleware(SessionMiddleware, secret_key=SESSION_SECRET)

# Static + templates (matches your repo layout app/static and app/templates)
app.mount("/static", StaticFiles(directory="app/static"), name="static")
templates = Jinja2Templates(directory="app/templates")


# =============================================================================
# Utils
# =============================================================================
def utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def db() -> sqlite3.Connection:
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    conn = db()
    cur = conn.cursor()
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS agreements (
            id TEXT PRIMARY KEY,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,

            plan TEXT NOT NULL,
            customer_name TEXT NOT NULL,
            customer_email TEXT NOT NULL,
            customer_phone TEXT,
            customer_address TEXT,

            borrower_name TEXT NOT NULL,
            borrower_address TEXT NOT NULL,

            lender_name TEXT NOT NULL,
            lender_address TEXT NOT NULL,

            from_str TEXT NOT NULL,
            to_str TEXT NOT NULL,
            purpose TEXT NOT NULL,
            vehicle_regnr TEXT NOT NULL,

            stripe_session_id TEXT,
            stripe_payment_intent TEXT,
            paid_at TEXT,
            delivered_at TEXT,

            oneflow_document_id TEXT,
            oneflow_document_url TEXT,
            oneflow_status TEXT
        )
        """
    )
    conn.commit()
    conn.close()


@app.on_event("startup")
def _startup() -> None:
    init_db()


def safe_send_email(
    to_list: list[str],
    subject: str,
    body_text: str,
    attachments: Optional[list[Tuple[str, bytes, str]]] = None,  # (filename, data, mimetype)
) -> Tuple[bool, str]:
    """
    Returns (ok, err)
    """
    try:
        if not SMTP_HOST or not SMTP_USER or not SMTP_PASS:
            return False, "SMTP not configured (SMTP_HOST/SMTP_USER/SMTP_PASS missing)"

        msg = EmailMessage()
        msg["From"] = MAIL_FROM
        msg["To"] = ", ".join(to_list)
        msg["Subject"] = subject
        msg.set_content(body_text)

        if attachments:
            for filename, data, mimetype in attachments:
                maintype, subtype = mimetype.split("/", 1)
                msg.add_attachment(data, maintype=maintype, subtype=subtype, filename=filename)

        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=20) as s:
            s.starttls()
            s.login(SMTP_USER, SMTP_PASS)
            s.send_message(msg)

        return True, ""
    except Exception as e:
        return False, repr(e)


def page_ctx(request: Request, path: str, title: str = "HP Juridik", description: str = "") -> Dict[str, Any]:
    return {
        "request": request,
        "path": path,
        "title": title,
        "description": description,
        "base_url": BASE_URL,
    }


# =============================================================================
# PDF generation (fallback receipt / preview)
# =============================================================================
FOOTER_TEXT = (
    "076-317 12 84  |  hpjuridik.se  | HP@hpjuridik.se  |  "
    "Karl XI gata 21, 222 20 LUND  |  Subsidiaritet i Lund AB  |  559365-2018"
)


def generate_contract_pdf(agreement: Dict[str, Any]) -> bytes:
    """
    Simple PDF (fallback). Your Oneflow template is the primary signing doc.
    """
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    w, h = A4

    # Header logo if exists in static
    logo_path = "app/static/hp-juridik-logo.png"
    y = h - 35 * mm
    try:
        if os.path.exists(logo_path):
            c.drawImage(logo_path, 75 * mm, h - 30 * mm, width=60 * mm, height=18 * mm, mask="auto")
            y = h - 45 * mm
    except Exception:
        pass

    c.setFont("Helvetica-Bold", 14)
    c.drawString(25 * mm, y, "BILUTLÅNINGSAVTAL")
    y -= 10 * mm

    c.setFont("Helvetica", 10)
    c.drawString(25 * mm, y, "Mellan nedanstående parter har följande avtal träffats.")
    y -= 12 * mm

    def line(label: str, value: str) -> None:
        nonlocal y
        c.setFont("Helvetica-Bold", 10)
        c.drawString(25 * mm, y, label)
        y -= 6 * mm
        c.setFont("Helvetica", 10)
        c.drawString(30 * mm, y, value)
        y -= 10 * mm

    line("UTLÅNARE", f"Namn: {agreement['lender_name']}  |  Adress: {agreement['lender_address']}")
    line("LÅNTAGARE", f"Namn: {agreement['borrower_name']}  |  Adress: {agreement['borrower_address']}")
    line("FORDON", f"Bilens registreringsnummer: {agreement['vehicle_regnr']}")
    line("UTLÅNINGSPERIOD", f"Startdatum: {agreement['from_str']}  |  Slutdatum: {agreement['to_str']}")
    line("ÄNDAMÅL", agreement["purpose"])

    # Footer on every page
    c.setFont("Helvetica", 8)
    c.drawString(15 * mm, 10 * mm, FOOTER_TEXT[:120])
    c.drawString(15 * mm, 6 * mm, FOOTER_TEXT[120:])

    c.showPage()
    c.save()
    return buf.getvalue()


# =============================================================================
# Stripe + Oneflow
# =============================================================================
def require_stripe() -> None:
    if not STRIPE_SECRET_KEY:
        raise HTTPException(status_code=500, detail="STRIPE_SECRET_KEY saknas")
    stripe.api_key = STRIPE_SECRET_KEY


def require_oneflow() -> None:
    if not ONEFLOW_API_TOKEN or not ONEFLOW_TEMPLATE_ID:
        raise HTTPException(status_code=500, detail="Oneflow saknas (ONEFLOW_API_TOKEN/ONEFLOW_TEMPLATE_ID)")


def oneflow_headers() -> Dict[str, str]:
    return {
        "Authorization": f"Bearer {ONEFLOW_API_TOKEN}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


def oneflow_create_from_template(agreement: Dict[str, Any]) -> Dict[str, Any]:
    """
    Creates a Oneflow document from template and sets template data fields.
    This is intentionally defensive because Oneflow accounts differ slightly in endpoints/features.
    """
    require_oneflow()

    # 1) Create document from template
    # Many Oneflow setups support creating a document from a template via /templates/{id}/documents
    # If your tenant uses a different endpoint, you'll see the error in logs.
    url = f"{ONEFLOW_BASE_URL}/templates/{ONEFLOW_TEMPLATE_ID}/documents"
    payload = {
        "name": f"BILUTLÅNINGSAVTAL {agreement['vehicle_regnr']}",
        # Keep metadata we can search for later
        "external_id": agreement["id"],
    }

    r = requests.post(url, headers=oneflow_headers(), data=json.dumps(payload), timeout=30)
    if r.status_code >= 300:
        raise RuntimeError(f"Oneflow create document failed {r.status_code}: {r.text}")

    doc = r.json()
    # Try common keys
    document_id = str(doc.get("id") or doc.get("document_id") or doc.get("data", {}).get("id") or "")
    if not document_id:
        raise RuntimeError(f"Oneflow response missing document id: {doc}")

    # 2) Set template data fields
    # You created external keys like:
    # utlånare_namn -> utlånare_namn
    # utlånare_adress -> utlånare_adress
    # låntagare_namn -> lantagare_namn
    # låntagare_adress -> lantagare_adress
    # fordon_regnr -> fordon_regnr
    # from_str -> from_str
    # to_str -> to_str
    # andamal -> andamal
    fields = {
        "utlanare_namn": agreement["lender_name"],
        "utlanare_adress": agreement["lender_address"],
        "lantagare_namn": agreement["borrower_name"],
        "lantagare_adress": agreement["borrower_address"],
        "fordon_regnr": agreement["vehicle_regnr"],
        "from_str": agreement["from_str"],
        "to_str": agreement["to_str"],
        "andamal": agreement["purpose"],
    }

    # Oneflow has different ways to set fields; common pattern is something like /documents/{id}/data-fields
    # We'll try a best-effort endpoint.
    df_url = f"{ONEFLOW_BASE_URL}/documents/{document_id}/data-fields"
    df_payload = [{"external_key": k, "value": v} for k, v in fields.items()]

    r2 = requests.put(df_url, headers=oneflow_headers(), data=json.dumps(df_payload), timeout=30)
    if r2.status_code >= 300:
        # Not fatal (some tenants use another endpoint); we still proceed but alert you.
        print("WARNING: Oneflow set data-fields failed:", r2.status_code, r2.text)

    # 3) Add counterparty participant (customer) as signer
    # Again endpoints differ; we attempt common participant endpoint.
    participants_url = f"{ONEFLOW_BASE_URL}/documents/{document_id}/participants"
    participant_payload = {
        "name": agreement["customer_name"] or agreement["borrower_name"],
        "email": agreement["customer_email"],
        "role": "counterparty",
        "signatory": True,
        "delivery_channel": "email",
    }
    r3 = requests.post(participants_url, headers=oneflow_headers(), data=json.dumps(participant_payload), timeout=30)
    if r3.status_code >= 300:
        print("WARNING: Oneflow add participant failed:", r3.status_code, r3.text)

    # 4) Send document (start signing)
    send_url = f"{ONEFLOW_BASE_URL}/documents/{document_id}/send"
    r4 = requests.post(send_url, headers=oneflow_headers(), data=json.dumps({}), timeout=30)
    if r4.status_code >= 300:
        print("WARNING: Oneflow send failed:", r4.status_code, r4.text)

    # 5) Try to get a share link
    share_url = f"{ONEFLOW_BASE_URL}/documents/{document_id}"
    r5 = requests.get(share_url, headers=oneflow_headers(), timeout=30)
    doc2 = r5.json() if r5.status_code < 300 else {}
    doc_link = doc2.get("url") or doc2.get("public_url") or doc2.get("share_url") or ""

    return {"document_id": document_id, "document_url": doc_link}


def deliver_premium(agreement_id: str) -> None:
    """
    Called after Stripe webhook (paid). Idempotent.
    - Creates Oneflow doc from template + sends for signing
    - Sends receipt email with fallback PDF attached
    """
    conn = db()
    cur = conn.cursor()
    row = cur.execute("SELECT * FROM agreements WHERE id = ?", (agreement_id,)).fetchone()
    if not row:
        conn.close()
        raise RuntimeError("agreement not found")

    if row["delivered_at"]:
        conn.close()
        print("Already delivered:", agreement_id)
        return

    agreement = dict(row)

    # Create Oneflow signing doc
    doc_id = ""
    doc_url = ""
    try:
        of = oneflow_create_from_template(agreement)
        doc_id = of.get("document_id", "")
        doc_url = of.get("document_url", "")
    except Exception as e:
        # We still send receipt + PDF even if Oneflow failed, but alert internally
        msg = f"Oneflow creation failed for agreement_id={agreement_id}: {e!r}"
        print("ERROR:", msg)
        safe_send_email([LEAD_INBOX], "Oneflow ERROR (premium)", msg)

    # Receipt PDF (fallback / copy)
    pdf_bytes = generate_contract_pdf(agreement)

    # Email receipt to customer + CC lead inbox
    subject = "HP Juridik – Kvitto och avtal"
    body = (
        "Tack för din betalning!\n\n"
        "Här kommer en kopia av avtalet som PDF.\n\n"
    )
    if doc_url:
        body += f"Signering via Oneflow (länk): {doc_url}\n\n"
    else:
        body += "Signering via Oneflow: (kunde inte skapa länk automatiskt – vi återkommer vid behov)\n\n"

    body += "Med vänlig hälsning\nHP Juridik\n"

    to_list = [agreement["customer_email"]]
    ok, err = safe_send_email(
        to_list=to_list,
        subject=subject,
        body_text=body,
        attachments=[("bilutlaningsavtal.pdf", pdf_bytes, "application/pdf")],
    )
    if not ok:
        # fallback alert
        safe_send_email([LEAD_INBOX], "SMTP ERROR – kunde inte skicka kvitto", f"agreement_id={agreement_id}\n{err}")

    cur.execute(
        """
        UPDATE agreements
        SET delivered_at = ?, oneflow_document_id = COALESCE(oneflow_document_id, ?),
            oneflow_document_url = COALESCE(oneflow_document_url, ?),
            oneflow_status = COALESCE(oneflow_status, ?),
            updated_at = ?
        WHERE id = ?
        """,
        (utc_iso(), doc_id or None, doc_url or None, "sent" if doc_id else None, utc_iso(), agreement_id),
    )
    conn.commit()
    conn.close()


# =============================================================================
# Routes – website pages
# =============================================================================
@app.get("/", response_class=HTMLResponse)
def home(request: Request):
    # IMPORTANT: this fixes "hpjuridik.se landar på fel sida"
    return templates.TemplateResponse("pages/home.html", page_ctx(request, "/", "HP Juridik", ""))


@app.get("/contact", response_class=HTMLResponse)
def contact_page(request: Request):
    return templates.TemplateResponse("pages/contact.html", page_ctx(request, "/contact", "Kontakt | HP Juridik", ""))


@app.get("/services", response_class=HTMLResponse)
def services_page(request: Request):
    return templates.TemplateResponse("pages/services.html", page_ctx(request, "/services", "Tjänster | HP Juridik", ""))


@app.get("/terms", response_class=HTMLResponse)
def terms_page(request: Request):
    return templates.TemplateResponse("pages/terms.html", page_ctx(request, "/terms", "Villkor | HP Juridik", ""))


# =============================================================================
# Contact form (simple)
# =============================================================================
@app.post("/contact")
async def contact_submit(
    request: Request,
    name: str = Form(...),
    email: str = Form(...),
    message: str = Form(...),
):
    body = f"Nytt meddelande från webb:\n\nNamn: {name}\nEmail: {email}\n\n{message}\n"
    safe_send_email([CONTACT_TO], "HP Juridik | Ny kontaktförfrågan", body)
    return RedirectResponse(url="/contact", status_code=303)


# =============================================================================
# Flow: Låna bil till skuldsatt -> review -> Stripe checkout -> Stripe webhook -> Oneflow signing
# =============================================================================
@app.get("/lana-bil-till-skuldsaatt", response_class=HTMLResponse)
def lana_bil_form(request: Request):
    return templates.TemplateResponse(
        "pages/lana_bil.html",
        page_ctx(request, "/lana-bil-till-skuldsaatt", "Låna bil till skuldsatt | HP Juridik", ""),
    )


@app.post("/lana-bil-till-skuldsaatt")
async def lana_bil_submit(
    request: Request,
    customer_name: str = Form(...),
    customer_email: str = Form(...),
    customer_phone: str = Form(""),
    customer_address: str = Form(""),

    utlanare_namn: str = Form(...),
    utlanare_adress: str = Form(...),

    lantagare_namn: str = Form(...),
    lantagare_adress: str = Form(...),

    fordon_regnr: str = Form(...),
    from_str: str = Form(...),
    to_str: str = Form(...),
    andamal: str = Form(...),
):
    agreement_id = str(uuid.uuid4())
    now = utc_iso()

    conn = db()
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO agreements (
            id, created_at, updated_at,
            plan, customer_name, customer_email, customer_phone, customer_address,
            borrower_name, borrower_address,
            lender_name, lender_address,
            from_str, to_str, purpose, vehicle_regnr
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            agreement_id, now, now,
            "premium",
            customer_name.strip(), customer_email.strip(), customer_phone.strip(), customer_address.strip(),
            lantagare_namn.strip(), lantagare_adress.strip(),
            utlanare_namn.strip(), utlanare_adress.strip(),
            from_str.strip(), to_str.strip(), andamal.strip(), fordon_regnr.strip(),
        ),
    )
    conn.commit()
    conn.close()

    # remember in session for review
    request.session["agreement_id"] = agreement_id

    return RedirectResponse(url="/lana-bil-till-skuldsaatt/review", status_code=303)


@app.get("/lana-bil-till-skuldsaatt/review", response_class=HTMLResponse)
def lana_bil_review(request: Request):
    agreement_id = request.session.get("agreement_id") or request.query_params.get("agreement_id")
    if not agreement_id:
        return RedirectResponse(url="/lana-bil-till-skuldsaatt", status_code=303)

    conn = db()
    row = conn.execute("SELECT * FROM agreements WHERE id = ?", (agreement_id,)).fetchone()
    conn.close()
    if not row:
        return RedirectResponse(url="/lana-bil-till-skuldsaatt", status_code=303)

    ctx = page_ctx(request, "/lana-bil-till-skuldsaatt/review", "Granska | HP Juridik", "")
    ctx["agreement"] = dict(row)
    ctx["premium_price_ore"] = PREMIUM_PRICE_ORE
    return templates.TemplateResponse("pages/lana_bil_review.html", ctx)


@app.post("/lana-bil-till-skuldsaatt/review")
async def lana_bil_start_checkout(request: Request):
    agreement_id = request.session.get("agreement_id") or request.query_params.get("agreement_id")
    if not agreement_id:
        raise HTTPException(status_code=400, detail="agreement_id saknas")

    conn = db()
    row = conn.execute("SELECT * FROM agreements WHERE id = ?", (agreement_id,)).fetchone()
    conn.close()
    if not row:
        raise HTTPException(status_code=404, detail="agreement hittades ej")

    require_stripe()

    success_url = f"{BASE_URL}/checkout-success?agreement_id={agreement_id}"
    cancel_url = f"{BASE_URL}/checkout-cancel?agreement_id={agreement_id}"

    # IMPORTANT: put agreement_id in metadata so webhook can find it
    session = stripe.checkout.Session.create(
        mode="payment",
        payment_method_types=["card"],
        line_items=[
            {
                "price_data": {
                    "currency": "sek",
                    "product_data": {"name": "Bilutlåningsavtal (premium)"},
                    "unit_amount": PREMIUM_PRICE_ORE,
                },
                "quantity": 1,
            }
        ],
        success_url=success_url,
        cancel_url=cancel_url,
        customer_email=row["customer_email"],
        metadata={"agreement_id": agreement_id},
        payment_intent_data={"metadata": {"agreement_id": agreement_id}},
    )

    # Save stripe session id
    conn = db()
    conn.execute(
        "UPDATE agreements SET stripe_session_id = ?, updated_at = ? WHERE id = ?",
        (session.get("id"), utc_iso(), agreement_id),
    )
    conn.commit()
    conn.close()

    return RedirectResponse(url=session.url, status_code=303)


@app.get("/checkout-success", response_class=HTMLResponse)
def checkout_success(request: Request):
    ctx = page_ctx(request, "/checkout-success", "Tack | HP Juridik", "")
    return templates.TemplateResponse("pages/checkout_success.html", ctx)


@app.get("/checkout-cancel", response_class=HTMLResponse)
def checkout_cancel(request: Request):
    ctx = page_ctx(request, "/checkout-cancel", "Avbrutet | HP Juridik", "")
    return templates.TemplateResponse("pages/checkout_cancel.html", ctx)


# =============================================================================
# Stripe Webhook (premium delivery happens here)
# =============================================================================
@app.post("/stripe/webhook")
async def stripe_webhook(request: Request):
    print("=== STRIPE WEBHOOK HIT ===", utc_iso())

    if not STRIPE_WEBHOOK_SECRET:
        raise HTTPException(status_code=500, detail="STRIPE_WEBHOOK_SECRET saknas")

    payload = await request.body()
    sig_header = request.headers.get("stripe-signature")
    if not sig_header:
        return PlainTextResponse("missing stripe-signature header", status_code=400)

    try:
        event = stripe.Webhook.construct_event(
            payload=payload,
            sig_header=sig_header,
            secret=STRIPE_WEBHOOK_SECRET,
        )
    except Exception as e:
        print("Stripe webhook verify failed:", repr(e))
        return PlainTextResponse("invalid signature", status_code=400)

    event_type = event.get("type")
    print("Stripe event:", event_type)

    if event_type != "checkout.session.completed":
        return PlainTextResponse("ok", status_code=200)

    session_obj = event["data"]["object"]
    session_id = session_obj.get("id")
    payment_status = session_obj.get("payment_status")
    status = session_obj.get("status")

    metadata = session_obj.get("metadata") or {}
    agreement_id = metadata.get("agreement_id")

    print("checkout session:", session_id, "agreement_id:", agreement_id, "status:", status, "payment_status:", payment_status)

    # If metadata is missing, alert and still return 200 so Stripe stops retrying (you can choose 500 if you want retries)
    if not agreement_id:
        msg = (
            "Stripe checkout.session.completed MEN metadata.agreement_id saknas.\n\n"
            f"session_id: {session_id}\n"
            f"metadata: {metadata}\n"
        )
        safe_send_email([LEAD_INBOX], "Stripe ALERT: saknar agreement_id", msg)
        return PlainTextResponse("ok", status_code=200)

    # Mark paid
    conn = db()
    conn.execute(
        """
        UPDATE agreements
        SET paid_at = COALESCE(paid_at, ?),
            stripe_payment_intent = COALESCE(stripe_payment_intent, ?),
            updated_at = ?
        WHERE id = ?
        """,
        (utc_iso(), session_obj.get("payment_intent"), utc_iso(), agreement_id),
    )
    conn.commit()
    conn.close()

    # Deliver (idempotent inside deliver_premium)
    try:
        deliver_premium(agreement_id)
        print("Premium delivered:", agreement_id)
    except Exception as e:
        err_txt = f"Premium delivery failed for agreement_id={agreement_id} session_id={session_id}: {e!r}"
        print(err_txt)
        safe_send_email([LEAD_INBOX], "Delivery ERROR i Stripe webhook", err_txt)
        # Return 500 => Stripe retry (useful for transient errors)
        return PlainTextResponse("delivery error", status_code=500)

    return PlainTextResponse("ok", status_code=200)


# =============================================================================
# Oneflow webhook (status updates after signing)
# =============================================================================
def verify_oneflow_signature(callback_id: str, signature: str) -> bool:
    """
    Oneflow UI says:
    signature = sha1(callback_id + <Sign key>)
    """
    if not ONEFLOW_WEBHOOK_SIGN_KEY:
        return True  # allow if not configured (dev)
    expected = hashlib.sha1((callback_id + ONEFLOW_WEBHOOK_SIGN_KEY).encode("utf-8")).hexdigest()
    return hmac.compare_digest(expected, signature or "")


@app.post("/oneflow/webhook")
async def oneflow_webhook(request: Request):
    """
    Webhook URL set in Oneflow: https://www.hpjuridik.se/oneflow/webhook
    """
    body = await request.body()
    try:
        data = json.loads(body.decode("utf-8"))
    except Exception:
        return PlainTextResponse("invalid json", status_code=400)

    callback_id = str(data.get("callback_id") or data.get("id") or "")
    signature = str(data.get("signature") or "")
    if callback_id and signature and not verify_oneflow_signature(callback_id, signature):
        return PlainTextResponse("invalid signature", status_code=400)

    # Best effort parsing (payloads vary)
    event_type = data.get("event") or data.get("type") or "unknown"
    document_id = (
        str(data.get("document_id") or "")
        or str((data.get("document") or {}).get("id") or "")
    )
    external_id = (
        str(data.get("external_id") or "")
        or str((data.get("document") or {}).get("external_id") or "")
    )
    status = data.get("status") or (data.get("document") or {}).get("status") or ""

    print("Oneflow webhook:", event_type, "document_id:", document_id, "external_id:", external_id, "status:", status)

    # If we stored external_id=agreement_id at creation, we can update agreement
    agreement_id = external_id

    if agreement_id:
        conn = db()
        conn.execute(
            """
            UPDATE agreements
            SET oneflow_document_id = COALESCE(oneflow_document_id, ?),
                oneflow_status = COALESCE(?, oneflow_status),
                updated_at = ?
            WHERE id = ?
            """,
            (document_id or None, status or None, utc_iso(), agreement_id),
        )
        conn.commit()
        conn.close()

    # If you want: when status becomes "signed", email final PDF to both parties.
    # (Downloading signed PDF endpoint differs per tenant, so not hard-coded here.)
    return PlainTextResponse("ok", status_code=200)


# =============================================================================
# Health / misc
# =============================================================================
@app.get("/healthz", response_class=PlainTextResponse)
def healthz():
    return "ok"


@app.head("/", include_in_schema=False)
def head_root():
    # Render health checks sometimes do HEAD /
    return Response(status_code=200)
