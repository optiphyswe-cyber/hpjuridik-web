import os
import smtplib
from datetime import datetime, timezone
from email.mime.text import MIMEText
from email.utils import formataddr

from fastapi import FastAPI, Request, Form
from fastapi.responses import HTMLResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

# -------------------------
# App + templates
# -------------------------
app = FastAPI()

app.mount("/static", StaticFiles(directory="app/static"), name="static")
templates = Jinja2Templates(directory="app/templates")

# -------------------------
# Settings (env)
# -------------------------
SITE_URL = os.getenv("SITE_URL", "https://hpjuridik-web.onrender.com").rstrip("/")
NOINDEX = os.getenv("NOINDEX", "1") == "1"

SMTP_HOST = os.getenv("SMTP_HOST", "")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER = os.getenv("SMTP_USER", "")
SMTP_PASS = os.getenv("SMTP_PASS", "")
CONTACT_TO = os.getenv("CONTACT_TO", "hp@hpjuridik.se")

def seo(path: str, title: str, description: str):
    return {
        "title": title,
        "description": description,
        "canonical": f"{SITE_URL}{path}",
        "robots": "noindex, nofollow" if NOINDEX else "index, follow",
    }

# -------------------------
# Email helper
# -------------------------
def build_email_body(namn: str, epost: str, telefon: str, arende: str, meddelande: str, request: Request) -> str:
    ts = datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M")
    ip = request.client.host if request.client else "unknown"
    ua = request.headers.get("user-agent", "unknown")

    telefon_txt = telefon if telefon else "Ej angivet"
    arende_txt = arende if arende else "Ej angivet"

    return (
        "Ny kontaktförfrågan via HP Juridik\n"
        "---------------------------------\n\n"
        f"Namn: {namn}\n"
        f"E-post: {epost}\n"
        f"Telefon: {telefon_txt}\n"
        f"Ärende: {arende_txt}\n\n"
        "Meddelande:\n"
        f"{meddelande}\n\n"
        "---------------------------------\n"
        f"Tid: {ts}\n"
        f"IP: {ip}\n"
        f"User-Agent: {ua}\n"
    )

def send_contact_email(namn: str, epost: str, telefon: str, arende: str, meddelande: str, request: Request) -> None:
    if not (SMTP_HOST and SMTP_USER and SMTP_PASS):
        raise RuntimeError("SMTP env vars saknas: SMTP_HOST/SMTP_USER/SMTP_PASS")

    subject_arende = arende if arende else "Kontaktförfrågan"
    subject = f"{subject_arende}: {namn}"

    body = build_email_body(namn, epost, telefon, arende, meddelande, request)

    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = subject
    msg["From"] = formataddr(("HP Juridik", SMTP_USER))
    msg["To"] = CONTACT_TO
    msg["Reply-To"] = epost  # så du kan svara direkt till kunden

    with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=20) as server:
        server.starttls()
        server.login(SMTP_USER, SMTP_PASS)
        server.sendmail(SMTP_USER, [CONTACT_TO], msg.as_string())

# -------------------------
# Routes
# -------------------------
from fastapi import HTTPException  # lägg i imports om du inte har

@app.get("/kontakta-oss", response_class=HTMLResponse)
def contact(request: Request):
    # Rendera sidan även om seo inte finns eller env saknas
    ctx = {"request": request, "sent": False}
    try:
        ctx["seo"] = seo("/kontakta-oss", "Kontakta oss – HP Juridik", "Kontakta oss för rådgivning.")
    except Exception:
        pass
    return templates.TemplateResponse("pages/contact.html", ctx)

@app.post("/kontakta-oss", response_class=HTMLResponse)
def contact_submit(
    request: Request,
    namn: str = Form(...),
    epost: str = Form(...),
    telefon: str = Form(""),
    meddelande: str = Form(...),
    website: str = Form("", required=False),
):
    # spam -> låtsas OK
    if website:
        return templates.TemplateResponse("pages/contact.html", {"request": request, "sent": True})

    # Försök skicka mail, men om det failar: visa snyggt fel istället för 500
    error = None
    try:
        send_contact_email(namn, epost, telefon, "", meddelande, request)  # om din funktion har arende-parameter: sätt "" här
    except Exception as e:
        error = str(e)

    ctx = {"request": request, "sent": error is None, "error": error}
    try:
        ctx["seo"] = seo("/kontakta-oss", "Kontakta oss – HP Juridik", "Kontakta oss för rådgivning.")
    except Exception:
        pass

    return templates.TemplateResponse("pages/contact.html", ctx)
