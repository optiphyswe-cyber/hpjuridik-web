import os
import smtplib
import io
from datetime import datetime, timezone
from email.mime.text import MIMEText
from email.utils import formataddr

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

    title = ParagraphStyle(
        "Title",
        parent=styles["Title"],
        fontSize=18,
        leading=22,
        spaceAfter=10,
    )
    h = ParagraphStyle(
        "H",
        parent=styles["Heading2"],
        fontSize=12.5,
        leading=15,
        spaceBefore=10,
        spaceAfter=6,
    )
    body = ParagraphStyle(
        "Body",
        parent=styles["BodyText"],
        fontSize=10.5,
        leading=14,
        spaceAfter=6,
    )
    small = ParagraphStyle(
        "Small",
        parent=styles["BodyText"],
        fontSize=9.5,
        leading=12.5,
        spaceAfter=4,
    )

    def P(text: str, st=body):
        text = _safe(text).replace("\n", "<br/>")
        return Paragraph(text, st)

    story = []

    # 1) Titel
    story.append(Paragraph("TILLFÄLLIGT LÅNEAVTAL – BIL", title))
    story.append(
        P(
            "Detta avtal upprättas för att tydliggöra villkoren för ett tidsbegränsat lån av fordon.",
            small,
        )
    )
    story.append(Spacer(1, 6))

    # 2) Parter
    story.append(Paragraph("Parter", h))

    ut_pnr = (
        f", personnummer: {_safe(utlanare.get('pnr'))}" if _safe(utlanare.get("pnr")) else ""
    )
    la_pnr = (
        f", personnummer: {_safe(lantagare.get('pnr'))}" if _safe(lantagare.get("pnr")) else ""
    )

    story.append(P(f"<b>Utlånare (ägare):</b> {_safe(utlanare.get('namn'))}{ut_pnr}", body))
    story.append(P(f"Adress: {_safe(utlanare.get('adress'))}", body))
    story.append(
        P(
            f"Telefon: {_safe(utlanare.get('tel'))} &nbsp;&nbsp; E-post: {_safe(utlanare.get('epost'))}",
            body,
        )
    )
    story.append(Spacer(1, 4))
    story.append(P(f"<b>Låntagare (skuldsatt):</b> {_safe(lantagare.get('namn'))}{la_pnr}", body))
    story.append(P(f"Adress: {_safe(lantagare.get('adress'))}", body))
    story.append(
        P(
            f"Telefon: {_safe(lantagare.get('tel'))} &nbsp;&nbsp; E-post: {_safe(lantagare.get('epost'))}",
            body,
        )
    )

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
    story.append(
        P(
            "Syftet med detta avtal är att dokumentera att utlåningen är tillfällig, att fordonet fortsatt tillhör utlånaren "
            "och att låntagaren nyttjar fordonet inom ramen för nedanstående villkor. Avtalet kan användas som underlag "
            "för att visa att fordonet inte överlåtits utan endast lånats ut under begränsad tid.",
            body,
        )
    )

    # Friskrivning (bevisunderlag, ingen garanti för visst utfall)
    story.append(
        P(
            "Detta avtal är ett standardiserat bevisunderlag baserat på parternas uppgifter och innebär ingen garanti för visst utfall vid myndighetsprövning eller tvist.",
            small,
        )
    )

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
    sig_table.setStyle(
        TableStyle(
            [
                ("FONTNAME", (0, 0), (-1, -1), "Helvetica"),
                ("FONTSIZE", (0, 0), (-1, -1), 10),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
                ("TOPPADDING", (0, 0), (-1, -1), 2),
                ("ALIGN", (0, 0), (-1, -1), "LEFT"),
                ("LINEBELOW", (0, 0), (0, 0), 0, colors.white),
                ("LINEBELOW", (1, 0), (1, 0), 0, colors.white),
            ]
        )
    )
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


# --- NYTT: Låna bil till skuldsatt ---
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

    # (Valfritt men bra) logga godkännandet i serverlogg
    client_ip = request.client.host if request.client else "unknown"
    accepted_at = datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")
    ua = request.headers.get("user-agent", "unknown")
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

    filename = "laneavtal-bil.pdf"
    return StreamingResponse(
        io.BytesIO(pdf_bytes),
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.get("/healthz", response_class=PlainTextResponse)
def healthz():
    return "ok"
