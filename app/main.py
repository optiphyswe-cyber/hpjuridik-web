import os
import smtplib
from email.mime.text import MIMEText
from email.utils import formataddr

from fastapi import FastAPI, Request, Form
from fastapi.responses import HTMLResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

# ... (din setup som vanligt)

SMTP_HOST = os.getenv("SMTP_HOST", "")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER = os.getenv("SMTP_USER", "")
SMTP_PASS = os.getenv("SMTP_PASS", "")
CONTACT_TO = os.getenv("CONTACT_TO", "")

def send_contact_email(namn: str, epost: str, meddelande: str):
    if not (SMTP_HOST and SMTP_USER and SMTP_PASS and CONTACT_TO):
        raise RuntimeError("SMTP env vars saknas")

    subject = f"Nytt meddelande från hpjuridik.se: {namn}"
    body = f"Namn: {namn}\nE-post: {epost}\n\nMeddelande:\n{meddelande}\n"

    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = subject
    msg["From"] = formataddr(("HP Juridik", SMTP_USER))
    msg["To"] = CONTACT_TO
    msg["Reply-To"] = epost

    with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=20) as server:
        server.starttls()
        server.login(SMTP_USER, SMTP_PASS)
        server.sendmail(SMTP_USER, [CONTACT_TO], msg.as_string())

@app.get("/kontakta-oss", response_class=HTMLResponse)
def contact(request: Request):
    return templates.TemplateResponse(
        "pages/contact.html",
        {"request": request, "seo": seo("/kontakta-oss", "Kontakta oss – HP Juridik", "Kontakta oss för rådgivning."), "sent": False}
    )

@app.post("/kontakta-oss", response_class=HTMLResponse)
def contact_submit(
    request: Request,
    namn: str = Form(...),
    epost: str = Form(...),
    meddelande: str = Form(...),
    website: str = Form("", required=False),  # honeypot
):
    # Spam: om honeypot fylld -> låtsas OK
    if website:
        return templates.TemplateResponse(
            "pages/contact.html",
            {"request": request, "seo": seo("/kontakta-oss", "Kontakta oss – HP Juridik", ""), "sent": True}
        )

    send_contact_email(namn, epost, meddelande)

    return templates.TemplateResponse(
        "pages/contact.html",
        {"request": request, "seo": seo("/kontakta-oss", "Tack – HP Juridik", "Tack för ditt meddelande."), "sent": True}
    )
