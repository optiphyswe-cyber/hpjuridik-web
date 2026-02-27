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

def seo(path: str, title: str, description: str):
    return {
        "title": title,
        "description": description,
        "canonical": f"{SITE_URL}{path}",
        "robots": "noindex, nofollow" if NOINDEX else "index, follow",
    }

# -------------------------
# Email helpers
# -------------------------
def build_email_body(namn: str, epost: str, telefon: str, meddelande: str, request: Request) -> str:
    ts = datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M")
    ip = request.client.host if request.client else "unknown"
    ua = request.headers.get("user-agent", "unknown")

    telefon_txt = telefon if telefon else "Ej angivet"

    return (
        "Ny kontaktförfrågan via HP Juridik\n"
        "---------------------------------\n\n"
        f"Namn: {namn}\n"
        f"E-post: {epost}\n"
        f"Telefon: {telefon_txt}\n\n"
        "Meddelande:\n"
        f"{meddelande}\n\n"
        "---------------------------------\n"
        f"Tid: {ts}\n"
        f"IP: {ip}\n"
        f"User-Agent: {ua}\n"
    )

def send_contact_email(namn: str, epost: str, telefon: str, meddelande: str, request: Request) -> None:
    # Kräver att du satt SMTP_* i Render Environment
    if not (SMTP_HOST and SMTP_USER and SMTP_PASS):
        raise RuntimeError("SMTP är inte konfigurerat (saknar SMTP_HOST/SMTP_USER/SMTP_PASS i Render).")

    subject = f"Kontaktförfrågan: {namn}"
    body = build_email_body(namn, epost, telefon, meddelande, request)

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
@app.api_route("/", methods=["GET", "HEAD"], response_class=HTMLResponse)
def home(request: Request):
    return templates.TemplateResponse(
        "pages/home.html",
        {
            "request": request,
            "seo": seo(
                "/",
                "HP Juridik – 20 min gratis rådgivning",
                "Personlig, trygg och värdeskapande juridik för privatpersoner och företag.",
            ),
        },
    )

@app.get("/tjanster", response_class=HTMLResponse)
def services(request: Request):
    return templates.TemplateResponse(
        "pages/services.html",
        {
            "request": request,
            "seo": seo(
                "/tjanster",
                "Tjänster – HP Juridik",
                "Juridisk rådgivning för privatpersoner och företag.",
            ),
        },
    )

@app.get("/om-oss", response_class=HTMLResponse)
def about(request: Request):
    return templates.TemplateResponse(
        "pages/page.html",
        {
            "request": request,
            "seo": seo("/om-oss", "Om oss – HP Juridik", "Lär känna HP Juridik och hur vi arbetar."),
            "heading": "Om oss",
            "lead": "Personlig, trygg och värdeskapande juridik.",
            "body": "<p>Fyll på med din presentation här.</p>",
        },
    )

@app.get("/cases", response_class=HTMLResponse)
def cases(request: Request):
    return templates.TemplateResponse(
        "pages/page.html",
        {
            "request": request,
            "seo": seo("/cases", "Cases – HP Juridik", "Exempel på uppdrag och resultat."),
            "heading": "Cases",
            "lead": "Exempel på uppdrag (kan anonymiseras).",
            "body": "<p>Kommer snart.</p>",
        },
    )

@app.get("/gdpr", response_class=HTMLResponse)
def gdpr(request: Request):
    return templates.TemplateResponse(
        "pages/page.html",
        {
            "request": request,
            "seo": seo("/gdpr", "GDPR – HP Juridik", "Information om personuppgifter och integritet."),
            "heading": "GDPR",
            "lead": "Information om hur personuppgifter hanteras.",
            "body": "<p>Fyll på med din GDPR-text.</p>",
        },
    )

@app.get("/allmanna-villkor", response_class=HTMLResponse)
def terms(request: Request):
    return templates.TemplateResponse(
        "pages/page.html",
        {
            "request": request,
            "seo": seo("/allmanna-villkor", "Allmänna villkor – HP Juridik", "Villkor för tjänster och rådgivning."),
            "heading": "Allmänna villkor",
            "lead": "Villkor för tjänster och rådgivning.",
            "body": "<p>Fyll på med dina villkor.</p>",
        },
    )

@app.get("/kontakta-oss", response_class=HTMLResponse)
def contact(request: Request):
    return templates.TemplateResponse(
        "pages/contact.html",
        {
            "request": request,
            "seo": seo("/kontakta-oss", "Kontakta oss – HP Juridik", "Kontakta oss för rådgivning."),
            "sent": False,
            "error": None,
        },
    )

@app.post("/kontakta-oss", response_class=HTMLResponse)
def contact_submit(
    request: Request,
    namn: str = Form(...),
    epost: str = Form(...),
    meddelande: str = Form(...),
    telefon: str = Form(""),              # valfritt (funkar även om formuläret inte har fältet)
    website: str = Form("", required=False),  # honeypot spam-skydd
):
    # spam -> låtsas OK
    if website:
        return templates.TemplateResponse(
            "pages/contact.html",
            {"request": request, "seo": seo("/kontakta-oss", "Kontakta oss – HP Juridik", ""), "sent": True, "error": None},
        )

    error = None
    try:
        send_contact_email(namn, epost, telefon, meddelande, request)
    except Exception as e:
        error = str(e)

    return templates.TemplateResponse(
        "pages/contact.html",
        {
            "request": request,
            "seo": seo("/kontakta-oss", "Kontakta oss – HP Juridik", "Kontakta oss för rådgivning."),
            "sent": error is None,
            "error": error,
        },
    )

@app.get("/healthz", response_class=PlainTextResponse)
def healthz():
    return "ok"
