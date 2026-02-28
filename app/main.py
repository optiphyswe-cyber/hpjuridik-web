import os
import io
import smtplib
import base64
from datetime import datetime, timezone
from email.mime.text import MIMEText
from email.message import EmailMessage
from email.utils import formataddr

import httpx
from fastapi import FastAPI, Request, Form
from fastapi.responses import HTMLResponse, PlainTextResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

# PDF (ReportLab)
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
# Environment / settings
# -------------------------
SITE_URL = os.getenv("SITE_URL", "https://hpjuridik-web.onrender.com").rstrip("/")
NOINDEX = os.getenv("NOINDEX", "1") == "1"

# On Render, SMTP is often blocked. Prefer an email API (Postmark).
# Set EMAIL_PROVIDER=postmark (recommended) OR smtp
EMAIL_PROVIDER = os.getenv("EMAIL_PROVIDER", "postmark").lower().strip()

# Postmark (recommended)
POSTMARK_TOKEN = os.getenv("POSTMARK_TOKEN", "")
POSTMARK_FROM = os.getenv("POSTMARK_FROM", "")  # e.g. "HP Juridik <info@hpjuridik.se>"

# SMTP (fallback)
SMTP_HOST = os.getenv("SMTP_HOST", "")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER = os.getenv("SMTP_USER", "")
SMTP_PASS = os.getenv("SMTP_PASS", "")

CONTACT_TO = os.getenv("CONTACT_TO", "hp@hpjuridik.se")
LEADS_INBOX = os.getenv("LEADS_INBOX", "lanabil@hpjuridik.se")

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


def _now_local_str() -> str:
    return datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M")


# -------------------------
# Email helpers
# -------------------------
def send_email_postmark(*, to_emails: list[str], subject: str, body_text: str, attachments: list[dict] | None = None) -> None:
    """
    Send via Postmark API (recommended on Render).
    Requires:
      POSTMARK_TOKEN
      POSTMARK_FROM (e.g. "HP Juridik <info@hpjuridik.se>")
    """
    if not POSTMARK_TOKEN or not POSTMARK_FROM:
        raise RuntimeError("Postmark saknar config: POSTMARK_TOKEN och POSTMARK_FROM måste vara satta.")

    clean = [e.strip() for e in (to_emails or []) if e and e.strip()]
    if not clean:
        raise RuntimeError("Inga giltiga mottagaradresser angivna.")

    payload = {
        "From": POSTMARK_FROM,
        "To": ", ".join(clean),
        "Subject": subject,
        "TextBody": body_text,
        "MessageStream": "outbound",
    }
    if attachments:
        payload["Attachments"] = attachments

    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "X-Postmark-Server-Token": POSTMARK_TOKEN,
    }

    with httpx.Client(timeout=20) as client:
        r = client.post("https://api.postmarkapp.com/email", headers=headers, json=payload)
        if r.status_code >= 300:
            raise RuntimeError(f"Postmark error {r.status_code}: {r.text}")


def send_email_smtp(*, to_emails: list[str], subject: str, body_text: str, pdf_bytes: bytes | None = None, filename: str | None = None) -> None:
    """SMTP fallback."""
    if not (SMTP_HOST and SMTP_USER and SMTP_PASS):
        raise RuntimeError("SMTP är inte konfigurerat (saknar SMTP_HOST/SMTP_USER/SMTP_PASS).")

    clean = [e.strip() for e in (to_emails or []) if e and e.strip()]
    if not clean:
        raise RuntimeError("Inga giltiga mottagaradresser angivna.")

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = formataddr((COMPANY["brand"], SMTP_USER))
    msg["To"] = ", ".join(clean)
    msg.set_content(body_text)

    if pdf_bytes and filename:
        msg.add_attachment(pdf_bytes, maintype="application", subtype="pdf", filename=filename)

    with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=20) as smtp:
        smtp.starttls()
        smtp.login(SMTP_USER, SMTP_PASS)
        smtp.send_message(msg)


def send_agreement_email(*, to_emails: list[str], subject: str, body_text: str, pdf_bytes: bytes, filename: str = "laneavtal-bil.pdf") -> None:
    """Skickar avtals-PDF som bilaga till angivna mottagare."""
    if EMAIL_PROVIDER == "postmark":
        attachments = [{
            "Name": filename,
            "Content": base64.b64encode(pdf_bytes).decode("ascii"),
            "ContentType": "application/pdf",
        }]
        return send_email_postmark(to_emails=to_emails, subject=subject, body_text=body_text, attachments=attachments)

    if EMAIL_PROVIDER == "smtp":
        return send_email_smtp(to_emails=to_emails, subject=subject, body_text=body_text, pdf_bytes=pdf_bytes, filename=filename)

    raise RuntimeError("Ogiltigt EMAIL_PROVIDER. Använd 'postmark' eller 'smtp'.")


def build_email_body(namn: str, epost: str, telefon: str, meddelande: str, request: Request) -> str:
    ts = _now_local_str()
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


def send_contact_email(namn: str, epost: str, telefon: str, meddelande: str, request: Request) -> None:
    subject = f"HP Juridik | Ny kontaktförfrågan från {namn}"
    body = build_email_body(namn, epost, telefon, meddelande, request)

    if EMAIL_PROVIDER == "postmark":
        return send_email_postmark(to_emails=[CONTACT_TO], subject=subject, body_text=body)

    # SMTP fallback
    if not (SMTP_HOST and SMTP_USER and SMTP_PASS):
        raise RuntimeError("SMTP är inte konfigurerat (saknar SMTP_HOST/SMTP_USER/SMTP_PASS i Render).")

    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = subject
    msg["From"] = formataddr((COMPANY["brand"], SMTP_USER))
    msg["To"] = CONTACT_TO
    msg["Reply-To"] = epost

    with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=20) as server:
        server.starttls()
        server.login(SMTP_USER, SMTP_PASS)
        server.sendmail(SMTP_USER, [CONTACT_TO], msg.as_string())

def notify_lead_inbox(
    *,
    request: Request,
    utlanare_epost: str,
    lantagare_epost: str,
    utlanare_namn: str,
    lantagare_namn: str,
    marketing_accept: bool,
) -> None:
    """
    Skickar ett internt 'lead'-mail till din inkorg så att du får e-postadresserna.
    Standard: LEADS_INBOX (default lanabil@hpjuridik.se)
    """
    ip = request.client.host if request.client else "unknown"
    ua = request.headers.get("user-agent", "unknown")
    ts = _now_local_str()

    subject = "NYTT LEAD – Låna bil (gratis)"
    body = (
        "NYTT LEAD (Låna bil – gratis)\n"
        "====================================\n\n"
        f"Tid: {ts}\n"
        f"IP: {ip}\n"
        f"User-Agent: {ua}\n\n"
        f"Utlånare: {utlanare_namn}\n"
        f"Utlånare e-post: {utlanare_epost}\n\n"
        f"Låntagare: {lantagare_namn}\n"
        f"Låntagare e-post: {lantagare_epost}\n\n"
        f"Samtycke nyhetsutskick: {'JA' if marketing_accept else 'NEJ'}\n"
    )

    # Inga bilagor behövs här – bara en intern notis.
    if EMAIL_PROVIDER == "postmark":
        return send_email_postmark(to_emails=[LEADS_INBOX], subject=subject, body_text=body)
    # SMTP fallback
    return send_email_smtp(to_emails=[LEADS_INBOX], subject=subject, body_text=body)




# -------------------------
# PDF helpers (Låna bil)
# -------------------------
def _safe(s: str) -> str:
    return (s or "").strip()


def _sv_dt(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d kl. %H:%M")


def _sv_date(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d")


def build_loan_pdf(
    *,
    utlanare: dict,
    lantagare: dict,
    fordon: dict,
    period: dict,
    andamal: str,
    ort: str = "Lund",
) -> bytes:
    """Skapar ett tillfälligt låneavtal – bil och returnerar PDF som bytes."""
    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf,
        pagesize=A4,
        leftMargin=18 * mm,
        rightMargin=18 * mm,
        topMargin=16 * mm,
        bottomMargin=16 * mm,
        title="Tillfälligt låneavtal – bil",
        author=COMPANY.get("brand", ""),
    )

    styles = getSampleStyleSheet()

    title = ParagraphStyle("Title", parent=styles["Title"], fontSize=18, leading=22, spaceAfter=10)
    h = ParagraphStyle("H", parent=styles["Heading2"], fontSize=12.5, leading=15, spaceBefore=10, spaceAfter=6)
    body = ParagraphStyle("Body", parent=styles["BodyText"], fontSize=10.5, leading=14, spaceAfter=6)
    small = ParagraphStyle("Small", parent=styles["BodyText"], fontSize=9.5, leading=12.5, spaceAfter=4)

    def P(text: str, st=body):
        text = _safe(text).replace("\n", "<br/>")
        return Paragraph(text, st)

    story = []

    # 1) Titel
    story.append(Paragraph("TILLFÄLLIGT LÅNEAVTAL – BIL", title))
    story.append(P("Detta avtal upprättas för att tydliggöra villkoren för ett tidsbegränsat lån av fordon.", small))
    story.append(Spacer(1, 6))

    # 2) Parter
    story.append(Paragraph("Parter", h))

    ut_pnr = f", personnummer: {_safe(utlanare.get('pnr'))}" if _safe(utlanare.get("pnr")) else ""
    la_pnr = f", personnummer: {_safe(lantagare.get('pnr'))}" if _safe(lantagare.get("pnr")) else ""

    story.append(P(f"<b>Utlånare (ägare):</b> {_safe(utlanare.get('namn'))}{ut_pnr}", body))
    story.append(P(f"Adress: {_safe(utlanare.get('adress'))}", body))
    story.append(P(f"Telefon: {_safe(utlanare.get('tel'))} &nbsp;&nbsp; E-post: {_safe(utlanare.get('epost'))}", body))
    story.append(Spacer(1, 4))
    story.append(P(f"<b>Låntagare (skuldsatt):</b> {_safe(lantagare.get('namn'))}{la_pnr}", body))
    story.append(P(f"Adress: {_safe(lantagare.get('adress'))}", body))
    story.append(P(f"Telefon: {_safe(lantagare.get('tel'))} &nbsp;&nbsp; E-post: {_safe(lantagare.get('epost'))}", body))

    # 3) Fordon
    story.append(Paragraph("Fordon", h))
    story.append(P(f"Märke och modell: {_safe(fordon.get('marke_modell'))}", body))
    story.append(P(f"Registreringsnummer: {_safe(fordon.get('regnr'))}", body))
    if _safe(fordon.get("agare")):
        story.append(P(f"Ägare: {_safe(fordon.get('agare'))}", body))

    # 4) Avtalsperiod
    story.append(Paragraph("Avtalsperiod", h))
    from_dt = period["from"]
    to_dt = period["to"]
    story.append(P(f"Från: {_sv_dt(from_dt)}", body))
    story.append(P(f"Till: {_sv_dt(to_dt)}", body))

    # 5) Ändamål
    story.append(Paragraph("Ändamål med lånet", h))
    story.append(P(andamal, body))

    # 6) Bakgrund och syfte
    story.append(Paragraph("Bakgrund och syfte med avtalet", h))
    story.append(P(
        "Syftet med detta avtal är att dokumentera att utlåningen är tillfällig, att fordonet fortsatt tillhör utlånaren "
        "och att låntagaren nyttjar fordonet inom ramen för nedanstående villkor. Avtalet kan användas som underlag "
        "för att visa att fordonet inte överlåtits utan endast lånats ut under begränsad tid.",
        body
    ))
    story.append(P(
        "Detta avtal är ett standardiserat bevisunderlag baserat på parternas uppgifter och innebär ingen garanti för visst utfall vid myndighetsprövning eller tvist.",
        small
    ))

    # 7) Villkor 1–6
    story.append(Paragraph("Villkor", h))
    villkor = [
        "Lånet avser endast ovan angivet fordon och gäller enbart under den angivna avtalsperioden.",
        "Låntagaren ansvarar för att fordonet hanteras varsamt och enligt gällande trafik- och försäkringsvillkor.",
        "Låntagaren ansvarar för kostnader som uppstår under låneperioden, såsom bränsle, trängselskatt, parkeringsavgifter/böter och liknande, om inte annat avtalas skriftligen.",
        "Skador eller fel som uppstår under låneperioden ska omedelbart meddelas utlånaren. Låntagaren ansvarar för skador som uppstår genom vårdslöshet eller felaktig användning.",
        "Fordonet får inte överlåtas, lånas ut i andra hand, hyras ut eller användas för olagliga ändamål.",
        "Utlånaren har rätt att återkalla lånet i förtid vid misstanke om missbruk eller vid väsentligt avtalsbrott. Låntagaren ska då utan dröjsmål återlämna fordonet.",
    ]
    for i, v in enumerate(villkor, start=1):
        story.append(P(f"<b>{i}.</b> {v}", body))

    # 8) Kopior
    story.append(Paragraph("Kopior", h))
    story.append(P("Avtalet upprättas i två (2) likalydande exemplar där parterna erhåller varsitt.", body))

    # 9) Ort & datum
    story.append(Spacer(1, 6))
    today = datetime.now(timezone.utc).astimezone()
    story.append(P(f"Ort: {_safe(ort)}", body))
    story.append(P(f"Datum: {_sv_date(today)}", body))

    # 10) Underskrifter
    story.append(Spacer(1, 14))
    sig_data = [
        ["______________________________", "______________________________"],
        ["Utlånare (ägare)", "Låntagare (skuldsatt)"],
        [f"Namn: {_safe(utlanare.get('namn'))}", f"Namn: {_safe(lantagare.get('namn'))}"],
    ]
    sig_table = Table(sig_data, colWidths=[85 * mm, 85 * mm])
    sig_table.setStyle(TableStyle([
        ("FONTNAME", (0, 0), (-1, -1), "Helvetica"),
        ("FONTSIZE", (0, 0), (-1, -1), 10),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
        ("TOPPADDING", (0, 0), (-1, -1), 2),
        ("ALIGN", (0, 0), (-1, -1), "LEFT"),
        ("LINEBELOW", (0, 0), (0, 0), 0, colors.white),
        ("LINEBELOW", (1, 0), (1, 0), 0, colors.white),
    ]))
    story.append(sig_table)

    doc.build(story)
    return buf.getvalue()


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

    ctx = page_ctx(
        request,
        "/",
        "HP Juridik – 20 min gratis rådgivning",
        "Personlig, trygg och värdeskapande juridik för privatpersoner och företag.",
    )
    ctx.update({"sent": error is None, "error": error})
    return templates.TemplateResponse("pages/home.html", ctx)


# --- Låna bil till skuldsatt ---
@app.get("/lana-bil-till-skuldsatt", response_class=HTMLResponse)
def lana_bil_form(request: Request):
    ctx = page_ctx(
        request,
        "/lana-bil-till-skuldsatt",
        "Låna bil till skuldsatt | HP Juridik",
        "Skapa ett tillfälligt låneavtal för bil som PDF.",
    )
    ctx.update({"sent": False, "error": None})
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
    from_dt: str = Form(...),
    to_dt: str = Form(...),
    andamal: str = Form(...),
    disclaimer_accept: str = Form(None),
    marketing_accept: str = Form(None),
):
    # Måste godkänna friskrivningsvillkor innan PDF skapas
    if not disclaimer_accept:
        ctx = page_ctx(
            request,
            "/lana-bil-till-skuldsatt",
            "Låna bil till skuldsatt | HP Juridik",
            "Skapa ett tillfälligt låneavtal för bil som PDF.",
        )
        ctx.update({"sent": False, "error": "Du måste godkänna friskrivningsvillkoret för att fortsätta."})
        return templates.TemplateResponse("pages/lana_bil.html", ctx, status_code=400)

    # logga godkännandet
    client_ip = request.client.host if request.client else "unknown"
    accepted_at = datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")
    ua = request.headers.get("user-agent", "unknown")
    
    # För gratisflödet: samtycke att spara e-post för nyhetsutskick (obligatoriskt enligt din önskan)
    if not marketing_accept:
        ctx = page_ctx(
            request,
            "/lana-bil-till-skuldsatt",
            "Låna bil till skuldsatt | HP Juridik",
            "Skapa ett tillfälligt låneavtal för bil som PDF.",
        )
        ctx.update({"sent": False, "error": "Du måste godkänna nyhetsutskick för att fortsätta (gratisversion)."})
        return templates.TemplateResponse("pages/lana_bil.html", ctx, status_code=400)

print(f"[lana-bil] disclaimer accepted at={accepted_at} ip={client_ip} ua={ua}")

    # Normalisera regnr: uppercase + inga mellanslag
    bil_regnr_norm = "".join((bil_regnr or "").split()).upper()

    # datetime-local -> YYYY-MM-DDTHH:MM
    try:
        from_dt_obj = datetime.fromisoformat(from_dt)
        to_dt_obj = datetime.fromisoformat(to_dt)
    except ValueError:
        ctx = page_ctx(
            request,
            "/lana-bil-till-skuldsatt",
            "Låna bil till skuldsatt | HP Juridik",
            "Skapa ett tillfälligt låneavtal för bil som PDF.",
        )
        ctx.update({"sent": False, "error": "Ogiltigt datum/tid-format."})
        return templates.TemplateResponse("pages/lana_bil.html", ctx, status_code=400)

    if to_dt_obj <= from_dt_obj:
        ctx = page_ctx(
            request,
            "/lana-bil-till-skuldsatt",
            "Låna bil till skuldsatt | HP Juridik",
            "Skapa ett tillfälligt låneavtal för bil som PDF.",
        )
        ctx.update({"sent": False, "error": "Till-datum/tid måste vara efter Från-datum/tid."})
        return templates.TemplateResponse("pages/lana_bil.html", ctx, status_code=400)

    # Bygg PDF
    pdf_bytes = build_loan_pdf(
        utlanare={
            "namn": utlanare_namn,
            "pnr": utlanare_pnr,
            "adress": utlanare_adress,
            "tel": utlanare_tel,
            "epost": utlanare_epost,
        },
        lantagare={
            "namn": lantagare_namn,
            "pnr": lantagare_pnr,
            "adress": lantagare_adress,
            "tel": lantagare_tel,
            "epost": lantagare_epost,
        },
        fordon={
            "marke_modell": bil_marke_modell,
            "regnr": bil_regnr_norm,
            "agare": utlanare_namn,
        },
        period={"from": from_dt_obj, "to": to_dt_obj},
        andamal=andamal,
        ort="Lund",
    )

    # Skicka mail till båda parter innan vi returnerar PDF
    subject = "Låneavtal bil – PDF"
    body_text = (
        "Hej!\n\n"
        "Här kommer ert tillfälliga låneavtal för bil som PDF.\n\n"
        "Observera: Avtalet är ett bevisunderlag och innebär ingen garanti för visst utfall vid myndighetsprövning.\n\n"
        f"Skapat: {_now_local_str()}\n\n"
        "Vänligen,\nHP Juridik"
    )

    print(f"[lana-bil] attempting email provider={EMAIL_PROVIDER} to={utlanare_epost},{lantagare_epost}")
    try:
        send_agreement_email(
            to_emails=[utlanare_epost, lantagare_epost],
            subject=subject,
            body_text=body_text,
            pdf_bytes=pdf_bytes,
            filename="laneavtal-bil.pdf",
        )
        print("[lana-bil] email sent OK")
        # Skicka även en intern notis till din inkorg med e-postadresserna (lead)
        try:
            notify_lead_inbox(
                request=request,
                utlanare_epost=utlanare_epost,
                lantagare_epost=lantagare_epost,
                utlanare_namn=utlanare_namn,
                lantagare_namn=lantagare_namn,
                marketing_accept=True,
            )
            print(f"[lana-bil] lead sent to inbox={LEADS_INBOX}")
        except Exception as e2:
            # ska inte stoppa kunden – bara logga
            print("[lana-bil] lead FAILED:", repr(e2))

    except Exception as e:
        print("[lana-bil] email FAILED:", repr(e))
        ctx = page_ctx(
            request,
            "/lana-bil-till-skuldsatt",
            "Låna bil till skuldsatt | HP Juridik",
            "Skapa ett tillfälligt låneavtal för bil som PDF.",
        )
        ctx.update({"sent": False, "error": f"PDF skapades men kunde inte mejlas: {e}"})
        return templates.TemplateResponse("pages/lana_bil.html", ctx, status_code=500)

    filename = "laneavtal-bil.pdf"
    return StreamingResponse(
        io.BytesIO(pdf_bytes),
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.get("/healthz", response_class=PlainTextResponse)
def healthz():
    return "ok"
