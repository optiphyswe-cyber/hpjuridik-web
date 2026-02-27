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
# Environment / settings
# -------------------------
SITE_URL = os.getenv("SITE_URL", "https://hpjuridik-web.onrender.com").rstrip("/")
NOINDEX = os.getenv("NOINDEX", "1") == "1"

SMTP_HOST = os.getenv("SMTP_HOST", "")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER = os.getenv("SMTP_USER", "")
SMTP_PASS = os.getenv("SMTP_PASS", "")
CONTACT_TO = os.getenv("CONTACT_TO", "hp@hpjuridik.se")

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


def seo(path: str, title: str, description: str):
    return {
        "title": title,
        "description": description,
        "canonical": f"{SITE_URL}{path}",
        "robots": "noindex, nofollow" if NOINDEX else "index, follow",
    }


def page_ctx(request: Request, path: str, title: str, desc: str):
    return {
        "request": request,
        "seo": seo(path, title, desc),
        "company": COMPANY,
    }


# -------------------------
# Email helpers
# -------------------------
def build_email_body(
    namn: str,
    epost: str,
    telefon: str,
    meddelande: str,
    request: Request,
) -> str:
    ts = datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M")
    ip = request.client.host if request.client else "unknown"
    ua = request.headers.get("user-agent", "unknown")

    telefon_txt = telefon.strip() if telefon and telefon.strip() else "Ej angivet"

    return (
        "NY KONTAKTFÖRFRÅGAN (HPJURIDIK.SE)\n"
        "====================================\n\n"
        f"Namn: {namn}\n"
        f"E-post: {epost}\n"
        f"Telefon: {telefon_txt}\n\n"
        "MEDDELANDE\n"
        "------------------------------------\n"
        f"{meddelande}\n\n"
        "TEKNISK INFO\n"
        "------------------------------------\n"
        f"Tid: {ts}\n"
        f"IP: {ip}\n"
        f"User-Agent: {ua}\n\n"
        "SIGNATUR\n"
        "------------------------------------\n"
        "Mvh // HP\n"
        f"{COMPANY['phone']}\n"
        f"{COMPANY['website']}\n"
        f"{COMPANY['address']}\n"
        f"{COMPANY['company']}\n"
        f"{COMPANY['orgnr']}\n"
    )


def send_contact_email(
    namn: str,
    epost: str,
    telefon: str,
    meddelande: str,
    request: Request,
) -> None:
    # Kräver Render env vars:
    # SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASS, CONTACT_TO
    if not (SMTP_HOST and SMTP_USER and SMTP_PASS):
        raise RuntimeError(
            "SMTP är inte konfigurerat (saknar SMTP_HOST/SMTP_USER/SMTP_PASS i Render)."
        )

    subject = f"HP Juridik | Ny kontaktförfrågan från {namn}"
    body = build_email_body(namn, epost, telefon, meddelande, request)

    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = subject

    # From måste ofta matcha SMTP_USER för att levereras bra
    msg["From"] = formataddr((COMPANY["brand"], SMTP_USER))
    msg["To"] = CONTACT_TO

    # Reply-To så du kan svara direkt till klienten
    msg["Reply-To"] = epost

    with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=20) as server:
        server.starttls()
        server.login(SMTP_USER, SMTP_PASS)
        server.sendmail(SMTP_USER, [CONTACT_TO], msg.as_string())


# -------------------------
# Routes
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
    ctx = page_ctx(
        request,
        "/gdpr",
        "GDPR – HP Juridik",
        "Information om personuppgifter och integritet.",
    )
    return templates.TemplateResponse("pages/gdpr.html", ctx)


@app.get("/allmanna-villkor", response_class=HTMLResponse)
def terms(request: Request):
    ctx = page_ctx(
        request,
        "/allmanna-villkor",
        "Allmänna villkor – HP Juridik",
        "Villkor för tjänster och rådgivning.",
    )
    return templates.TemplateResponse("pages/terms.html", ctx)


@app.post("/kontakta-oss", response_class=HTMLResponse)
def contact_submit(
    request: Request,
    namn: str = Form(...),
    epost: str = Form(...),
    meddelande: str = Form(...),
    telefon: str = Form(""),
    website: str = Form("", required=False),  # honeypot spam-skydd
):
    # Spam -> låtsas OK utan att skicka
    if website:
        ctx = page_ctx(
            request,
            "/",
            "HP Juridik – 20 min gratis rådgivning",
            "Personlig, trygg och värdeskapande juridik för privatpersoner och företag.",
        )
        ctx.update({"sent": True, "error": None})
        return templates.TemplateResponse("pages/home.html", ctx)

    error = None
    try:
        send_contact_email(namn, epost, telefon, meddelande, request)
    except Exception as e:
        error = str(e)

    # Returnera startsidan (one-page) med status-rutor i kontaktsektionen
    ctx = page_ctx(
        request,
        "/",
        "HP Juridik – 20 min gratis rådgivning",
        "Personlig, trygg och värdeskapande juridik för privatpersoner och företag.",
    )
    ctx.update({"sent": error is None, "error": error})
    return templates.TemplateResponse("pages/home.html", ctx)


@app.get("/healthz", response_class=PlainTextResponse)
def healthz():
    return "ok"
