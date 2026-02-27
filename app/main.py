from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
import os

app = FastAPI()
app.mount("/static", StaticFiles(directory="app/static"), name="static")
templates = Jinja2Templates(directory="app/templates")

SITE_URL = os.getenv("SITE_URL", "https://hpjuridik-web.onrender.com").rstrip("/")
NOINDEX = os.getenv("NOINDEX", "1") == "1"  # staging default: noindex

def seo(path: str, title: str, description: str):
    return {
        "title": title,
        "description": description,
        "canonical": f"{SITE_URL}{path}",
        "robots": "noindex, nofollow" if NOINDEX else "index, follow",
    }

# Home: allow GET + HEAD (Render pingar ibland HEAD)
@app.api_route("/", methods=["GET", "HEAD"], response_class=HTMLResponse)
def home(request: Request):
    return templates.TemplateResponse(
        "pages/home.html",
        {"request": request, "seo": seo("/", "HP Juridik – 20 min gratis rådgivning",
                                        "Personlig, trygg och värdeskapande juridik för privatpersoner och företag.")}
    )

@app.get("/om-oss", response_class=HTMLResponse)
def about(request: Request):
    return templates.TemplateResponse(
        "pages/page.html",
        {"request": request,
         "seo": seo("/om-oss", "Om oss – HP Juridik", "Lär känna HP Juridik och hur vi arbetar."),
         "heading": "Om oss",
         "lead": "Kort beskrivning om byrån och hur du arbetar.",
         "body": "<p>Här fyller du på med din text.</p>"}
    )

@app.get("/tjanster", response_class=HTMLResponse)
def services(request: Request):
    return templates.TemplateResponse(
        "pages/page.html",
        {"request": request,
         "seo": seo("/tjanster", "Tjänster – HP Juridik", "Juridisk rådgivning för privatpersoner och företag."),
         "heading": "Tjänster",
         "lead": "Här listar du dina tjänster. Sen kan varje tjänst bli en egen sida/ett flöde.",
         "body": """
           <ul>
             <li><strong>Avtalsrätt</strong> – granskning och upprättande</li>
             <li><strong>Familjerätt</strong> – bodelning, samboavtal, vårdnad</li>
             <li><strong>Arbetsrätt</strong> – rådgivning och tvister</li>
             <li><strong>Fordringar</strong> – krav och process</li>
           </ul>
         """}
    )

@app.get("/cases", response_class=HTMLResponse)
def cases(request: Request):
    return templates.TemplateResponse(
        "pages/page.html",
        {"request": request,
         "seo": seo("/cases", "Cases – HP Juridik", "Exempel på uppdrag och resultat."),
         "heading": "Cases",
         "lead": "Lägg in 3–6 korta exempel. Du kan anonymisera.",
         "body": "<p>Kommer snart.</p>"}
    )

@app.get("/kontakta-oss", response_class=HTMLResponse)
def contact(request: Request):
    return templates.TemplateResponse(
        "pages/page.html",
        {"request": request,
         "seo": seo("/kontakta-oss", "Kontakta oss – HP Juridik", "Kontakta oss för rådgivning."),
         "heading": "Kontakta oss",
         "lead": "Skicka ett meddelande så återkommer vi.",
         "body": """
           <form class="form" method="post" action="/kontakta-oss">
             <label>Namn<br><input name="namn" required></label>
             <label>E-post<br><input name="epost" type="email" required></label>
             <label>Meddelande<br><textarea name="meddelande" rows="5" required></textarea></label>
             <button class="btn" type="submit">Skicka</button>
           </form>
         """}
    )

@app.post("/kontakta-oss", response_class=HTMLResponse)
def contact_submit(request: Request):
    # MVP: bara bekräfta. Sen kopplar du e-post/CRM.
    return templates.TemplateResponse(
        "pages/page.html",
        {"request": request,
         "seo": seo("/kontakta-oss", "Tack – HP Juridik", "Tack för ditt meddelande."),
         "heading": "Tack!",
         "lead": "Vi har tagit emot ditt meddelande och återkommer så snart vi kan.",
         "body": "<p>Du kan också ringa eller mejla direkt om du vill.</p>"}
    )

@app.get("/gdpr", response_class=HTMLResponse)
def gdpr(request: Request):
    return templates.TemplateResponse(
        "pages/page.html",
        {"request": request,
         "seo": seo("/gdpr", "GDPR – HP Juridik", "Information om personuppgifter och integritet."),
         "heading": "GDPR",
         "lead": "Information om hur personuppgifter hanteras.",
         "body": "<p>Fyll på med din GDPR-text.</p>"}
    )

@app.get("/allmanna-villkor", response_class=HTMLResponse)
def terms(request: Request):
    return templates.TemplateResponse(
        "pages/page.html",
        {"request": request,
         "seo": seo("/allmanna-villkor", "Allmänna villkor – HP Juridik", "Villkor för tjänster och rådgivning."),
         "heading": "Allmänna villkor",
         "lead": "Villkor för tjänster och rådgivning.",
         "body": "<p>Fyll på med dina villkor.</p>"}
    )
