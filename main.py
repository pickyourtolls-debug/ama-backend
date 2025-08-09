import os
import re
import json
import sqlite3
import smtplib
from email.message import EmailMessage
from datetime import datetime, timedelta
from typing import Optional, List, Dict

import requests
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from dotenv import load_dotenv

load_dotenv()

DB_PATH = os.path.join(os.path.dirname(__file__), "amahunter.db")

OXY_USER = os.getenv("OXY_USER")
OXY_PASS = os.getenv("OXY_PASS")
OXY_ENDPOINT = os.getenv("OXY_ENDPOINT", "https://realtime.oxylabs.io/v1/queries")

AFF_TAGS = {
    "FR": os.getenv("AFFILIATE_TAG_FR", ""),
    "DE": os.getenv("AFFILIATE_TAG_DE", ""),
    "BE": os.getenv("AFFILIATE_TAG_BE", ""),
}

COUNTRY_TO_DOMAIN = {
    "FR": "amazon.fr",
    "DE": "amazon.de",
    "BE": "amazon.com.be",
}

COUNTRY_TO_GEO = {
    "FR": "France",
    "DE": "Germany",
    "BE": "Belgium",
}

ALLOWED_COUNTRIES = [
    c.strip() for c in os.getenv("ALLOWED_COUNTRIES", "FR,DE,BE").split(",") if c.strip()
]

# SMTP / Email
SMTP_HOST = os.getenv("SMTP_HOST")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER = os.getenv("SMTP_USER")
SMTP_PASS = os.getenv("SMTP_PASS")
SMTP_FROM = os.getenv("SMTP_FROM", "no-reply@amahunter.online")

app = FastAPI(title="AmaHunter API", version="1.3.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---------------- DB INIT -----------------


def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


with get_conn() as conn:
    cur = conn.cursor()
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS prices (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            asin TEXT NOT NULL,
            country TEXT NOT NULL,
            price REAL NOT NULL,
            currency TEXT NOT NULL,
            captured_at TEXT NOT NULL
        );
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS alerts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            asin TEXT NOT NULL,
            country TEXT NOT NULL,
            target_price REAL NOT NULL,
            email TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        """
    )
    conn.commit()

# -------------- MODELS --------------------


class CompareRequest(BaseModel):
    input: str  # ASIN or URL


class CompareItem(BaseModel):
    country: str
    price: float
    currency: str
    affiliate_link: str


class CompareResponse(BaseModel):
    asin: str
    items: List[CompareItem]


class AlertRequest(BaseModel):
    asin: str
    country: str  # FR/DE/BE or "ANY"
    target_price: float
    email: str


# -------------- HELPERS -------------------

ASIN_RE = re.compile(r"/(dp|gp/product)/([A-Z0-9]{10})|^([A-Z0-9]{10})$")


def extract_asin(user_input: str) -> Optional[str]:
    user_input = user_input.strip()
    m = ASIN_RE.search(user_input)
    if m:
        return m.group(2) or m.group(3)
    return None


def affiliate_link(asin: str, country: str) -> str:
    domain = COUNTRY_TO_DOMAIN[country]
    tag = AFF_TAGS.get(country, "")
    tail = f"?tag={tag}" if tag else ""
    return f"https://{domain}/dp/{asin}{tail}"


def oxylabs_amazon_price(asin: str, country: str) -> Dict:
    """
    1) Essaye Oxylabs en mode structuré (amazon_product + parse:true)
    2) Si pas de prix, fallback HTML (amazon) + parsing
    """
    if not (OXY_USER and OXY_PASS):
        raise HTTPException(status_code=500, detail="Oxylabs credentials missing")

    domain = COUNTRY_TO_DOMAIN[country]  # ex: amazon.fr
    geo = COUNTRY_TO_GEO[country]        # ex: France

    # ---- 1) MODE STRUCTURÉ ----
    payload_parsed = {
        "source": "amazon_product",
        "query": asin,
        "domain": domain,
        "geo_location": geo,
        "parse": True,
    }
    resp = requests.post(
        OXY_ENDPOINT, auth=(OXY_USER, OXY_PASS), json=payload_parsed, timeout=60
    )
    if resp.status_code >= 400:
        raise HTTPException(status_code=502, detail=f"Oxylabs error: {resp.text}")

    data = resp.json()
    content = (data.get("results") or [{}])[0].get("content") or {}

    price = None
    if isinstance(content, dict):
        # chemins fréquents
        price = (content.get("buybox_winner") or {}).get("price") or content.get("price")
        if price is None and "buybox" in content:
            price = (content["buybox"] or {}).get("price")

        # parfois string "EUR 49,99"
        if isinstance(price, str):
            norm = (
                price.replace("\u202f", "")
                .replace("\xa0", "")
                .replace("€", "")
                .replace("EUR", "")
                .replace(".", "")
                .replace(",", ".")
                .strip()
            )
            try:
                price = float(re.findall(r"[0-9]+(?:\.[0-9]{1,2})?", norm)[0])
            except Exception:
                price = None

        if isinstance(price, (int, float)):
            return {"price": float(price), "currency": "EUR"}

    # ---- 2) FALLBACK HTML ----
    url = f"https://{domain}/dp/{asin}"
    payload_html = {"source": "amazon", "url": url, "geo_location": geo}
    resp2 = requests.post(
        OXY_ENDPOINT, auth=(OXY_USER, OXY_PASS), json=payload_html, timeout=60
    )
    if resp2.status_code >= 400:
        raise HTTPException(status_code=502, detail=f"Oxylabs error: {resp2.text}")

    html = (resp2.json().get("results") or [{}])[0].get("content") or ""
   # --- HTML parsing renforcé (ajout JSON-LD) ---
from bs4 import BeautifulSoup
soup = BeautifulSoup(html, "html.parser")

def pick_price_text():
    # 1) pattern moderne
    n = soup.select_one(".a-price .a-offscreen")
    if n and n.get_text(strip=True): return n.get_text(strip=True)
    # 2) apex desktop
    n = soup.select_one("#apex_desktop .a-offscreen")
    if n and n.get_text(strip=True): return n.get_text(strip=True)
    # 3) core price feature
    n = soup.select_one("#corePrice_feature_div .a-offscreen")
    if n and n.get_text(strip=True): return n.get_text(strip=True)
    # 4) anciens IDs
    for sel in ["#priceblock_ourprice", "#priceblock_dealprice", "#priceblock_saleprice"]:
        n = soup.select_one(sel)
        if n and n.get_text(strip=True): return n.get_text(strip=True)
    # 5) fallback large
    n = soup.find("span", {"class": "a-offscreen"})
    if n and n.get_text(strip=True): return n.get_text(strip=True)
    return None

price = None

# A) JSON-LD (schema.org) – très fiable quand présent
for script in soup.find_all("script", type="application/ld+json"):
    try:
        data = json.loads(script.string or "")
        # parfois c'est une liste
        if isinstance(data, list):
            for d in data:
                offer = (d.get("offers") or {})
                val = offer.get("price") if isinstance(offer, dict) else None
                if val:
                    s = str(val).replace("\u202f","").replace("\xa0","").replace(",",".")
                    m = re.search(r"[0-9]+(?:\.[0-9]{1,2})?", s)
                    if m: 
                        price = float(m.group(0))
                        break
        elif isinstance(data, dict):
            offer = (data.get("offers") or {})
            val = offer.get("price") if isinstance(offer, dict) else None
            if val:
                s = str(val).replace("\u202f","").replace("\xa0","").replace(",",".")
                m = re.search(r"[0-9]+(?:\.[0-9]{1,2})?", s)
                if m: 
                    price = float(m.group(0))
    except Exception:
        pass
    if price is not None:
        break

# B) Si JSON-LD pas trouvé, lire le texte affiché
if price is None:
    price_txt = pick_price_text()
    if price_txt:
        norm = (price_txt.replace("\u202f","").replace("\xa0","")
                        .replace("€","").replace("EUR","")
                        .replace(".","").replace(",","."))
        m = re.search(r"[0-9]+(?:\.[0-9]{1,2})?", norm)
        if m:
            price = float(m.group(0))

# C) Fallback ultime : regex directe dans le HTML
if price is None:
    m = re.search(r"([0-9][0-9\.,\s\u00A0\u202F]+)\s?(€|EUR)", html)
    if m:
        num = m.group(1).replace("\u202f","").replace("\xa0","").replace(" ","")
        num = num.replace(".","").replace(",",".")
        try:
            price = float(num)
        except Exception:
            price = None

    # Fallback ultime: regex directe dans le HTML
    if price is None:
        m = re.search(r"([0-9][0-9\.,\s\u00A0\u202F]+)\s?(€|EUR)", html)
        if m:
            num = (
                m.group(1)
                .replace("\u202f", "")
                .replace("\xa0", "")
                .replace(" ", "")
                .replace(".", "")
                .replace(",", ".")
            )
            try:
                price = float(num)
            except Exception:
                price = None

    if price is None:
        raise HTTPException(status_code=404, detail="Price not found")

    return {"price": price, "currency": "EUR"}


# ----- EMAIL SENDER -----


def send_email(to: str, subject: str, html: str):
    if not (SMTP_HOST and SMTP_USER and SMTP_PASS):
        raise HTTPException(status_code=500, detail="SMTP not configured")
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = SMTP_FROM
    msg["To"] = to
    msg.set_content("HTML email")
    msg.add_alternative(html, subtype="html")

    with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as s:
        s.starttls()
        s.login(SMTP_USER, SMTP_PASS)
        s.send_message(msg)


# -------------- ENDPOINTS -----------------


@app.get("/")
def root():
    return {"ok": True, "service": "AmaHunter API"}


@app.post("/compare", response_model=CompareResponse)
def compare(req: CompareRequest):
    asin = extract_asin(req.input)
    if not asin:
        raise HTTPException(status_code=400, detail="ASIN or valid Amazon URL required")

    items: List[CompareItem] = []
    now = datetime.utcnow().isoformat()

    for country in ALLOWED_COUNTRIES:
        try:
            p = oxylabs_amazon_price(asin, country)
            link = affiliate_link(asin, country)
            items.append(
                CompareItem(
                    country=country,
                    price=p["price"],
                    currency=p["currency"],
                    affiliate_link=link,
                )
            )

            # persist history
            with get_conn() as conn:
                conn.execute(
                    "INSERT INTO prices (asin, country, price, currency, captured_at) VALUES (?,?,?,?,?)",
                    (asin, country, p["price"], p["currency"], now),
                )
                conn.commit()
        except HTTPException:
            continue

    items.sort(key=lambda x: x.price)

    if not items:
        raise HTTPException(status_code=404, detail="No prices found for this ASIN")

    return CompareResponse(asin=asin, items=items)


@app.get("/history/{asin}")
def history(asin: str):
    since = (datetime.utcnow() - timedelta(days=30)).isoformat()
    with get_conn() as conn:
        cur = conn.execute(
            "SELECT asin, country, price, currency, captured_at FROM prices WHERE asin=? AND captured_at>=? ORDER BY captured_at ASC",
            (asin, since),
        )
        rows = [dict(r) for r in cur.fetchall()]
    return {"asin": asin, "rows": rows}


@app.post("/alerts")
def create_alert(req: AlertRequest):
    now = datetime.utcnow().isoformat()
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO alerts (asin, country, target_price, email, created_at) VALUES (?,?,?,?,?)",
            (req.asin, req.country, req.target_price, req.email, now),
        )
        conn.commit()
    return {"status": "ok"}


@app.post("/alerts/run")
def run_alerts():
    """Cron-safe endpoint: checks alerts and sends emails if conditions are met."""
    sent = []
    with get_conn() as conn:
        cur = conn.execute("SELECT * FROM alerts")
        alerts = [dict(r) for r in cur.fetchall()]
        for a in alerts:
            try:
                p = oxylabs_amazon_price(a["asin"], a["country"])
                if p["price"] <= a["target_price"]:
                    subject = f"AmaHunter: baisse de prix pour {a['asin']} ({a['country']})"
                    link = affiliate_link(a["asin"], a["country"])
                    html = f"""
                        <h2>Bonne nouvelle 🎉</h2>
                        <p>Le produit <b>{a['asin']}</b> est passé à <b>{p['price']} EUR</b> sur {a['country']}.</p>
                        <p><a href="{link}" target="_blank">Voir sur Amazon</a></p>
                        <hr/>
                        <small>AmaHunter · Les prix peuvent changer à tout moment.</small>
                    """
                    send_email(a["email"], subject, html)
                    sent.append(
                        {
                            "to": a["email"],
                            "asin": a["asin"],
                            "country": a["country"],
                            "price": p["price"],
                        }
                    )
            except Exception:
                continue
    return {"sent": sent}


@app.post("/alerts/test")
def test_email(to: str):
    """Send a test email to validate SMTP setup."""
    send_email(to, "Test AmaHunter SMTP", "<p>Ceci est un test d'envoi SMTP AmaHunter.</p>")
    return {"status": "ok"}


# -------- DEBUG (temporaire) --------------
@app.get("/debug")
def debug(asin: str, country: str = "FR"):
    domain = COUNTRY_TO_DOMAIN[country]
    geo = COUNTRY_TO_GEO[country]
    payload = {
        "source": "amazon_product",
        "query": asin,
        "domain": domain,
        "geo_location": geo,
        "parse": True,
    }
    r = requests.post(OXY_ENDPOINT, auth=(OXY_USER, OXY_PASS), json=payload, timeout=60)
    obj = r.json()
    content = (obj.get("results") or [{}])[0].get("content") or {}
    sample = {k: content.get(k) for k in ["price", "buybox", "buybox_winner", "availability", "title"]}
    return {"country": country, "parsed_keys": sample, "has_content": bool(content)}
