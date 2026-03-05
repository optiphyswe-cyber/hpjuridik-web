from __future__ import annotations

import os
import json
import uuid
import time
import sqlite3
import hashlib
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, Optional, Tuple, List

import requests
import stripe

from fastapi import FastAPI, Request, HTTPException, Form
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.templating import Jinja2Templates

import smtplib
from email.message import EmailMessage

# ----------------------------
# Config
# ----------------------------

BASE_URL = os.getenv("BASE_URL", "https://www.hpjuridik.se").rstrip("/")
PREMIUM_PRICE_ORE = int(os.getenv("PREMIUM_PRICE_ORE", "300"))

# Email (SMTP)
SMTP_HOST = os.getenv("SMTP_HOST", "")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER = os.getenv("SMTP_USER", "")
SMTP_PASS = os.getenv("SMTP_PASS", "")
MAIL_FROM = os.getenv("MAIL_FROM", SMTP_USER or "no-reply@hpjuridik.se")
LEAD_INBOX = os.getenv("LEAD_INBOX", "")  # t.ex. info@hpjuridik.se

# Stripe
STRIPE_SECRET_KEY = os.getenv("STRIPE_SECRET_KEY", "")
STRIPE_WEBHOOK_SECRET = os.getenv("STRIPE_WEBHOOK_SECRET", "")
stripe.api_key = STRIPE_SECRET_KEY

# Oneflow
ONEFLOW_API_TOKEN = os.getenv("ONEFLOW_API_TOKEN", "")
ONEFLOW_BASE_URL = os.getenv("ONEFLOW_BASE_URL", "https://api.oneflow.com/v1").rstrip("/")
ONEFLOW_TEMPLATE_ID = os.getenv("ONEFLOW_TEMPLATE_ID", "")  # t.ex. 13789463
ONEFLOW_WORKSPACE_ID = os.getenv("ONEFLOW_WORKSPACE_ID", "")  # valfritt
ONEFLOW_USER_EMAIL = os.getenv("ONEFLOW_USER_EMAIL", "")  # valfritt men ofta bra
ONEFLOW_WEBHOOK_SIGN_KEY = os.getenv("ONEFLOW_WEBHOOK_SIGN_KEY", "")  # behövs för webhook-verifiering

DB_PATH = os.getenv("DB_PATH", "agreements.sqlite3")

# ----------------------------
# App / Templates
# ----------------------------

app = FastAPI()

# Om du har /static och /templates i projektet så används de automatiskt.
# Om du inte har dem ännu, kan du kommentera bort dessa rader.
if os.path.isdir("static"):
    app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates") if os.path.isdir("templates") else None


# ----------------------------
# Utils
# ----------------------------

def utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()

def now_ts() -> int:
    return int(time.time())

def require_env(name: str, value: str) -> None:
    if not value:
        raise HTTPException(status_code=500, detail=f"{name} saknas i env")

def safe_send_email(to_list: List[str], subject: str, body: str, attachments: Optional[List[Tuple[str, bytes, str]]] = None) -> Tuple[bool, Optional[str]]:
    """
    attachments: list of (filename, bytes, mime_type)
    """
    try:
        if not SMTP_HOST or not SMTP_USER or not SMTP_PASS:
            return False, "SMTP env saknas (SMTP_HOST/SMTP_USER/SMTP_PASS)"
        msg = EmailMessage()
        msg["From"] = MAIL_FROM
        msg["To"] = ", ".join([x for x in to_list if x])
        msg["Subject"] = subject
        msg.set_content(body)

        if attachments:
            for filename, content, mime in attachments:
                maintype, subtype = (mime.split("/", 1) + ["octet-stream"])[:2]
                msg.add_attachment(content, maintype=maintype, subtype=subtype, filename=filename)

        with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as s:
            s.starttls()
            s.login(SMTP_USER, SMTP_PASS)
            s.send_message(msg)
        return True, None
    except Exception as e:
        return False, repr(e)

def read_file_bytes(path: str) -> Optional[bytes]:
    try:
        with open(path, "rb") as f:
            return f.read()
    except Exception:
        return None


# ----------------------------
# SQLite storage
# ----------------------------

def db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db() -> None:
    with db() as conn:
        conn.execute("""
        CREATE TABLE IF NOT EXISTS agreements (
            id TEXT PRIMARY KEY,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            data_json TEXT NOT NULL,

            customer_email TEXT,
            customer_name TEXT,

            stripe_session_id TEXT,
            stripe_paid_at TEXT,

            oneflow_contract_id TEXT,
            oneflow_participant_id TEXT,
            oneflow_access_link TEXT,
            oneflow_signed_at TEXT,

            delivered INTEGER DEFAULT 0,
            signed_pdf_sent INTEGER DEFAULT 0
        )
        """)
        conn.commit()

init_db()

def save_agreement(agreement_id: str, data: Dict[str, Any], customer_email: str = "", customer_name: str = "") -> None:
    now = utc_iso()
    with db() as conn:
        conn.execute("""
        INSERT INTO agreements (id, created_at, updated_at, data_json, customer_email, customer_name)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
          updated_at=excluded.updated_at,
          data_json=excluded.data_json,
          customer_email=excluded.customer_email,
          customer_name=excluded.customer_name
        """, (agreement_id, now, now, json.dumps(data, ensure_ascii=False), customer_email, customer_name))
        conn.commit()

def load_agreement(agreement_id: str) -> Optional[Dict[str, Any]]:
    with db() as conn:
        row = conn.execute("SELECT * FROM agreements WHERE id=?", (agreement_id,)).fetchone()
        if not row:
            return None
        out = dict(row)
        out["data"] = json.loads(row["data_json"])
        return out

def update_agreement_fields(agreement_id: str, **fields: Any) -> None:
    if not fields:
        return
    parts = []
    vals = []
    for k, v in fields.items():
        parts.append(f"{k}=?")
        vals.append(v)
    parts.append("updated_at=?")
    vals.append(utc_iso())
    vals.append(agreement_id)
    with db() as conn:
        conn.execute(f"UPDATE agreements SET {', '.join(parts)} WHERE id=?", vals)
        conn.commit()


# ----------------------------
# Oneflow client
# ----------------------------

class OneflowError(RuntimeError):
    pass

def oneflow_headers() -> Dict[str, str]:
    require_env("ONEFLOW_API_TOKEN", ONEFLOW_API_TOKEN)
    h = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "x-oneflow-api-token": ONEFLOW_API_TOKEN,
    }
    # Vissa konton kräver user email-header för vissa endpoints
    if ONEFLOW_USER_EMAIL:
        h["x-oneflow-user-email"] = ONEFLOW_USER_EMAIL
    return h

def oneflow_get_default_workspace_id() -> Optional[str]:
    if ONEFLOW_WORKSPACE_ID:
        return ONEFLOW_WORKSPACE_ID
    # försök hämta första workspace via API
    try:
        r = requests.get(f"{ONEFLOW_BASE_URL}/workspaces", headers=oneflow_headers(), timeout=20)
        if r.status_code >= 300:
            return None
        data = r.json()
        # brukar vara {"workspaces":[...]} eller lista
        if isinstance(data, dict) and "workspaces" in data and data["workspaces"]:
            return str(data["workspaces"][0].get("id") or data["workspaces"][0].get("workspace_id"))
        if isinstance(data, list) and data:
            return str(data[0].get("id") or data[0].get("workspace_id"))
        return None
    except Exception:
        return None

def oneflow_create_contract_from_template(
    template_id: str,
    contract_name: str,
    counterparty_name: str,
    counterparty_email: str,
) -> Tuple[str, str]:
    """
    Returns (contract_id, counterparty_participant_id)
    """
    require_env("ONEFLOW_TEMPLATE_ID", template_id)
    ws_id = oneflow_get_default_workspace_id()

    body: Dict[str, Any] = {
        "name": contract_name,
        "template_id": int(template_id),
        "parties": [
            {
                "name": counterparty_name or "Motpart",
                "participants": [
                    {
                        "name": counterparty_name or "Motpart",
                        "email": counterparty_email,
                        # Oneflow avgör signing per template/roll, men vi skapar en deltagare
                    }
                ],
            }
        ],
    }
    if ws_id:
        body["workspace_id"] = int(ws_id)

    r = requests.post(f"{ONEFLOW_BASE_URL}/contracts/create", headers=oneflow_headers(), data=json.dumps(body), timeout=30)
    if r.status_code >= 300:
        raise OneflowError(f"create contract failed: {r.status_code} {r.text}")

    contract = r.json()
    contract_id = str(contract.get("id") or contract.get("contract_id"))
    if not contract_id:
        raise OneflowError(f"create contract: kunde inte läsa contract id: {contract}")

    # hitta counterparty participant id (för access link)
    participant_id = ""
    parties = contract.get("parties") or []
    for p in parties:
        for part in (p.get("participants") or []):
            if (part.get("email") or "").lower() == (counterparty_email or "").lower():
                participant_id = str(part.get("id") or part.get("participant_id"))
                break
        if participant_id:
            break

    # fallback: hämta kontraktet och leta där
    if not participant_id:
        r2 = requests.get(f"{ONEFLOW_BASE_URL}/contracts/{contract_id}", headers=oneflow_headers(), timeout=20)
        if r2.status_code < 300:
            c2 = r2.json()
            for p in (c2.get("parties") or []):
                for part in (p.get("participants") or []):
                    if (part.get("email") or "").lower() == (counterparty_email or "").lower():
                        participant_id = str(part.get("id") or part.get("participant_id"))
                        break
                if participant_id:
                    break

    if not participant_id:
        raise OneflowError("Kunde inte hitta participant_id för motpart (behövs för access link)")

    return contract_id, participant_id

def oneflow_set_data_fields(contract_id: str, fields: Dict[str, str]) -> None:
    """
    fields = {external_key: value}
    """
    if not fields:
        return
    payload = [{"external_key": k, "value": v} for k, v in fields.items()]
    r = requests.put(
        f"{ONEFLOW_BASE_URL}/contracts/{contract_id}/data_fields",
        headers=oneflow_headers(),
        data=json.dumps(payload),
        timeout=30,
    )
    if r.status_code >= 300:
        raise OneflowError(f"set data fields failed: {r.status_code} {r.text}")

def oneflow_create_access_link(contract_id: str, participant_id: str) -> str:
    r = requests.post(
        f"{ONEFLOW_BASE_URL}/contracts/{contract_id}/participants/{participant_id}/access_link",
        headers=oneflow_headers(),
        timeout=20,
    )
    if r.status_code >= 300:
        raise OneflowError(f"access link failed: {r.status_code} {r.text}")
    data = r.json()
    link = data.get("access_link")
    if not link:
        raise OneflowError(f"access link saknas i svar: {data}")
    return str(link)

def oneflow_download_contract_pdf(contract_id: str) -> bytes:
    # "Download a contract file" – enklaste: GET /contracts/{id}/files och ladda ner senaste PDF
    r = requests.get(f"{ONEFLOW_BASE_URL}/contracts/{contract_id}/files", headers=oneflow_headers(), timeout=30)
    if r.status_code >= 300:
        raise OneflowError(f"list files failed: {r.status_code} {r.text}")
    files = r.json()
    # brukar vara {"files":[...]} eller lista
    if isinstance(files, dict) and "files" in files:
        files = files["files"]
    if not isinstance(files, list) or not files:
        raise OneflowError("Inga filer på kontraktet än")
    # välj senaste PDF
    pdf = None
    for f in files:
        if (f.get("content_type") or "").lower() == "application/pdf" or str(f.get("name","")).lower().endswith(".pdf"):
            pdf = f
            break
    if not pdf:
        pdf = files[0]
    file_id = str(pdf.get("id") or pdf.get("file_id"))
    if not file_id:
        raise OneflowError(f"Kunde inte läsa file id: {pdf}")

    r2 = requests.get(f"{ONEFLOW_BASE_URL}/contracts/{contract_id}/files/{file_id}/download", headers=oneflow_headers(), timeout=60)
    if r2.status_code >= 300:
        raise OneflowError(f"download failed: {r2.status_code} {r2.text}")
    return r2.content


# ----------------------------
# Stripe
# ----------------------------

def create_stripe_checkout_session(agreement_id: str) -> stripe.checkout.Session:
    require_env("STRIPE_SECRET_KEY", STRIPE_SECRET_KEY)

    success = f"{BASE_URL}/checkout-success?agreement_id={agreement_id}"
    cancel = f"{BASE_URL}/checkout-cancel?agreement_id={agreement_id}"

    session = stripe.checkout.Session.create(
        mode="payment",
        success_url=success,
        cancel_url=cancel,
        line_items=[{
            "price_data": {
                "currency": "sek",
                "product_data": {"name": "Premium – bilutlåningsavtal (Oneflow-signering)"},
                "unit_amount": PREMIUM_PRICE_ORE,
            },
            "quantity": 1,
        }],
        metadata={"agreement_id": agreement_id},
        payment_intent_data={"metadata": {"agreement_id": agreement_id}},
    )
    return session


# ----------------------------
# Agreement logic
# ----------------------------

def agreement_to_oneflow_fields(data: Dict[str, Any]) -> Dict[str, str]:
    """
    Mappa dina formulärfält -> Oneflow datafält (external_key).
    Du har redan skapat:
      fordon_regnr, lantagare_adress, lantagare_namn, from_str, to_str, utlanare_adress, utlanare_namn, andamal
    """
    def s(x: Any) -> str:
        return "" if x is None else str(x)

    return {
        "utlanare_namn": s(data.get("utlanare_namn")),
        "utlanare_adress": s(data.get("utlanare_adress")),
        "lantagare_namn": s(data.get("lantagare_namn")),
        "lantagare_adress": s(data.get("lantagare_adress")),
        "fordon_regnr": s(data.get("fordon_regnr")),
        "from_str": s(data.get("from_str")),
        "to_str": s(data.get("to_str")),
        "andamal": s(data.get("andamal")),
    }

def deliver_premium(agreement_id: str) -> None:
    """
    Idempotent leverans:
      - skapar Oneflow kontrakt + datafält + access link
      - skickar mail med kvitto + signeringslänk
    """
    row = load_agreement(agreement_id)
    if not row:
        raise RuntimeError(f"Agreement saknas: {agreement_id}")

    if row.get("delivered"):
        return

    data = row["data"]
    customer_email = row.get("customer_email") or (data.get("kund_email") or data.get("email") or "")
    customer_name = row.get("customer_name") or (data.get("kund_namn") or data.get("name") or data.get("lantagare_namn") or "Kund")

    if not customer_email:
        # vi kan fortfarande skapa kontrakt, men mail kan inte skickas
        customer_email = ""

    # 1) Skapa Oneflow kontrakt från template
    contract_name = f"Bilutlåningsavtal – {customer_name} – {agreement_id[:8]}"
    contract_id, participant_id = oneflow_create_contract_from_template(
        template_id=ONEFLOW_TEMPLATE_ID,
        contract_name=contract_name,
        counterparty_name=customer_name,
        counterparty_email=customer_email or "no-reply@example.com",  # Oneflow kräver email – se upp!
    )

    # 2) Sätt datafält
    fields = agreement_to_oneflow_fields(data)
    oneflow_set_data_fields(contract_id, fields)

    # 3) Skapa access link för motpart
    access_link = oneflow_create_access_link(contract_id, participant_id)

    update_agreement_fields(
        agreement_id,
        oneflow_contract_id=contract_id,
        oneflow_participant_id=participant_id,
        oneflow_access_link=access_link,
        delivered=1,
    )

    # 4) Maila kvitto + signeringslänk
    body = (
        f"Tack för din betalning.\n\n"
        f"För att signera bilutlåningsavtalet, använd länken nedan:\n"
        f"{access_link}\n\n"
        f"Orderreferens: {agreement_id}\n"
        f"Belopp: {PREMIUM_PRICE_ORE/100:.2f} SEK\n\n"
        f"/HP Juridik"
    )

    to_list = [customer_email] if customer_email else []
    cc_list = [LEAD_INBOX] if LEAD_INBOX else []
    recipients = [x for x in (to_list + cc_list) if x]

    ok, err = safe_send_email(
        recipients,
        "Premium – bilutlåningsavtal (signering)",
        body,
        attachments=None,  # vill du bifoga utkast-PDF här kan vi lägga till det
    )
    if not ok:
        # vi vill inte krascha webhook pga mail
        print("ALERT email failed:", err)


# ----------------------------
# Routes
# ----------------------------

@app.get("/healthz", response_class=PlainTextResponse)
def healthz():
    return "ok"

@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    if templates:
        return templates.TemplateResponse("pages/index.html", {"request": request, "title": "HP Juridik"})
    return HTMLResponse("<h1>HP Juridik</h1><p>/lana-bil-till-skuldsatt</p>")

@app.get("/lana-bil-till-skuldsatt", response_class=HTMLResponse)
async def form_page(request: Request):
    if templates:
        return templates.TemplateResponse("pages/lana_bil.html", {"request": request, "title": "Låna bil"})
    return HTMLResponse("<h2>Form saknas (templates/pages/lana_bil.html)</h2>")

@app.post("/lana-bil-till-skuldsatt")
async def form_submit(
    request: Request,
    utlanare_namn: str = Form(""),
    utlanare_adress: str = Form(""),
    lantagare_namn: str = Form(""),
    lantagare_adress: str = Form(""),
    fordon_regnr: str = Form(""),
    from_str: str = Form(""),
    to_str: str = Form(""),
    andamal: str = Form(""),
    kund_email: str = Form(""),
    premium: Optional[str] = Form(None),  # t.ex. "1" om premium vald
):
    agreement_id = str(uuid.uuid4())
    data = {
        "utlanare_namn": utlanare_namn,
        "utlanare_adress": utlanare_adress,
        "lantagare_namn": lantagare_namn,
        "lantagare_adress": lantagare_adress,
        "fordon_regnr": fordon_regnr,
        "from_str": from_str,
        "to_str": to_str,
        "andamal": andamal,
        "kund_email": kund_email,
        "premium": bool(premium),
    }
    save_agreement(agreement_id, data, customer_email=kund_email, customer_name=lantagare_namn or kund_email)

    if premium:
        session = create_stripe_checkout_session(agreement_id)
        update_agreement_fields(agreement_id, stripe_session_id=session.get("id"))
        return RedirectResponse(url=session.url, status_code=303)

    # Gratisflöde: här kan du göra din gamla leverans (t.ex. maila PDF direkt)
    return RedirectResponse(url=f"/review?agreement_id={agreement_id}", status_code=303)

@app.get("/review", response_class=HTMLResponse)
async def review(request: Request, agreement_id: str):
    row = load_agreement(agreement_id)
    if not row:
        raise HTTPException(status_code=404, detail="agreement not found")
    if templates:
        return templates.TemplateResponse("pages/review.html", {"request": request, "agreement": row["data"], "agreement_id": agreement_id})
    return HTMLResponse(f"<pre>{json.dumps(row['data'], ensure_ascii=False, indent=2)}</pre>")

@app.get("/checkout-success", response_class=HTMLResponse)
async def checkout_success(request: Request, agreement_id: str):
    # OBS: Leverans sker via Stripe webhook – denna sida är bara tack-sida.
    if templates:
        return templates.TemplateResponse("pages/checkout_success.html", {"request": request, "agreement_id": agreement_id})
    return HTMLResponse("<h3>Tack! Vi skickar signeringslänk via e-post.</h3>")

@app.get("/checkout-cancel", response_class=HTMLResponse)
async def checkout_cancel(request: Request, agreement_id: str):
    if templates:
        return templates.TemplateResponse("pages/checkout_cancel.html", {"request": request, "agreement_id": agreement_id})
    return HTMLResponse("<h3>Avbrutet</h3>")

# Stripe webhook (premium leverans)
@app.post("/stripe/webhook")
async def stripe_webhook(request: Request):
    print("=== STRIPE WEBHOOK HIT ===", utc_iso())

    require_env("STRIPE_WEBHOOK_SECRET", STRIPE_WEBHOOK_SECRET)

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
        print("Stripe verify failed:", repr(e))
        return PlainTextResponse(f"invalid signature: {type(e).__name__}", status_code=400)

    etype = event.get("type")
    print("Stripe event:", etype)

    if etype == "checkout.session.completed":
        session_obj = event["data"]["object"]
        session_id = session_obj.get("id")
        metadata = session_obj.get("metadata") or {}
        agreement_id = metadata.get("agreement_id")

        payment_status = session_obj.get("payment_status")
        status = session_obj.get("status")
        print("checkout session:", session_id, "agreement_id:", agreement_id, "status:", status, "payment_status:", payment_status)

        if not agreement_id:
            # skicka alert till lead inbox
            ok, err = safe_send_email(
                [LEAD_INBOX] if LEAD_INBOX else [],
                "Stripe ALERT: saknar agreement_id",
                f"session_id={session_id}\nmetadata={metadata}\n",
            )
            if not ok:
                print("ALERT email failed:", err)
            return PlainTextResponse("ok", status_code=200)

        row = load_agreement(agreement_id)
        if not row:
            ok, err = safe_send_email(
                [LEAD_INBOX] if LEAD_INBOX else [],
                "Stripe ALERT: agreement saknas (persistens)",
                f"agreement_id={agreement_id}\nsession_id={session_id}\nmetadata={metadata}\n",
            )
            if not ok:
                print("ALERT email failed:", err)
            return PlainTextResponse("ok", status_code=200)

        # Idempotens: om redan levererad -> ok
        if row.get("delivered"):
            return PlainTextResponse("ok", status_code=200)

        # markera betald
        update_agreement_fields(agreement_id, stripe_session_id=session_id, stripe_paid_at=utc_iso())

        # Leverera premium (Oneflow)
        try:
            deliver_premium(agreement_id)
        except Exception as e:
            # maila fel men returnera 200 så Stripe inte spammar (du kan välja 500 om du vill retries)
            msg = f"Premium delivery failed\nagreement_id={agreement_id}\nsession_id={session_id}\nerr={repr(e)}\n"
            print("WARNING:", msg)
            ok, err = safe_send_email([LEAD_INBOX] if LEAD_INBOX else [], "Premium delivery failed", msg)
            if not ok:
                print("ALERT email failed:", err)
            return PlainTextResponse("ok", status_code=200)

        return PlainTextResponse("ok", status_code=200)

    return PlainTextResponse("ok", status_code=200)


# ----------------------------
# Oneflow webhook (signerat)
# ----------------------------

def verify_oneflow_webhook(request_body: bytes, headers: Dict[str, str]) -> bool:
    """
    Oneflow webhook signering: docs använder callbackId + signKey (SHA1).
    Header brukar innehålla: x-oneflow-callback-id och x-oneflow-signature (kan variera).
    """
    if not ONEFLOW_WEBHOOK_SIGN_KEY:
        return True  # om du inte kör signering ännu
    callback_id = headers.get("x-oneflow-callback-id") or headers.get("X-Oneflow-Callback-Id") or ""
    signature = headers.get("x-oneflow-signature") or headers.get("X-Oneflow-Signature") or ""
    if not callback_id or not signature:
        return False
    raw = (callback_id + ONEFLOW_WEBHOOK_SIGN_KEY).encode("utf-8")
    expected = hashlib.sha1(raw).hexdigest()
    return expected == signature

@app.post("/oneflow/webhook")
async def oneflow_webhook(request: Request):
    body = await request.body()
    if not verify_oneflow_webhook(body, dict(request.headers)):
        return PlainTextResponse("invalid signature", status_code=400)

    try:
        payload = json.loads(body.decode("utf-8"))
    except Exception:
        return PlainTextResponse("bad json", status_code=400)

    # payload format: event -> contract_id etc (varierar beroende på Oneflow)
    # Vi plockar ut contract id robust:
    contract_id = (
        str(payload.get("contract_id") or payload.get("id") or "")
        or str(((payload.get("contract") or {}) .get("id") or ""))
    )
    event_type = payload.get("type") or payload.get("event_type") or payload.get("event") or ""
    print("Oneflow webhook:", event_type, "contract_id:", contract_id)

    if not contract_id:
        return PlainTextResponse("ok", status_code=200)

    # hitta agreement via contract_id
    with db() as conn:
        row = conn.execute("SELECT id, signed_pdf_sent FROM agreements WHERE oneflow_contract_id=?", (contract_id,)).fetchone()
    if not row:
        return PlainTextResponse("ok", status_code=200)

    agreement_id = row["id"]
    if row["signed_pdf_sent"]:
        return PlainTextResponse("ok", status_code=200)

    # Försök ladda ner PDF (om ännu inte finns -> ok)
    try:
        pdf_bytes = oneflow_download_contract_pdf(contract_id)
    except Exception as e:
        print("Oneflow download not ready:", repr(e))
        return PlainTextResponse("ok", status_code=200)

    agr = load_agreement(agreement_id)
    data = agr["data"] if agr else {}
    customer_email = (agr.get("customer_email") if agr else "") or data.get("kund_email") or ""
    customer_name = (agr.get("customer_name") if agr else "") or data.get("lantagare_namn") or "Kund"

    subject = "Signerad handling – bilutlåningsavtal"
    body_txt = (
        f"Hej!\n\nHär kommer den signerade handlingen.\n\n"
        f"Referens: {agreement_id}\n\n/HP Juridik"
    )
    recipients = [x for x in [customer_email, LEAD_INBOX] if x]

    ok, err = safe_send_email(
        recipients,
        subject,
        body_txt,
        attachments=[(f"bilutlaningsavtal_{agreement_id[:8]}.pdf", pdf_bytes, "application/pdf")],
    )
    if not ok:
        print("signed pdf email failed:", err)

    update_agreement_fields(agreement_id, oneflow_signed_at=utc_iso(), signed_pdf_sent=1)
    return PlainTextResponse("ok", status_code=200)
