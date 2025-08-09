import os
import re
import json
from typing import Optional, List, Dict

import requests
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from dotenv import load_dotenv

# Charge les variables d'env (OXY_USER, OXY_PASS, etc.)
load_dotenv()

OXY_USER = os.getenv("OXY_USER")
OXY_PASS = os.getenv("OXY_PASS")
OXY_ENDPOINT = os.getenv("OXY_ENDPOINT", "https://realtime.oxylabs.io/v1/queries")

# Pays et domaines supportés (on commence par FR uniquement)
COUNTRY_TO_DOMAIN = {"FR": "amazon.fr", "DE": "amazon.de", "BE": "amazon.com.be"}
COUNTRY_TO_GEO = {"FR": "France", "DE": "Germany", "BE": "Belgium"}
ALLOWED_COUNTRIES = [
    c.strip() for c in os.getenv("ALLOWED_COUNTRIES", "FR").split(",") if c.strip()
]

app = FastAPI(title="AmaHunter API (lite)", version="0.1.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], allow_credentials=True,
    allow_methods=["*"], allow_headers=["*"],
)

# -------------------- Modèles --------------------

class CompareRequest(BaseModel):
    input: str  # ASIN ou URL Amazon

class CompareItem(BaseModel):
    country: str
    price: float
    currency: str
    affiliate_link: str

class CompareResponse(BaseModel):
    asin: str
    items: List[CompareItem]

# -------------------- Helpers --------------------

ASIN_RE = re.compile(r"/(dp|gp/product)/([A-Z0-9]{10})|^([A-Z0-9]{10})$")

def extract_asin(user_input: str) -> Optional[str]:
    user_input = user_input.strip()
    m = ASIN_RE.search(user_input)
    if m:
        return m.group(2) or m.group(3)
    return None

def direct_link(asin: str, country: str) -> str:
    # Lien simple sans tag d'affiliation
    return f"https://{COUNTRY_TO_DOMAIN[country]}/dp/{asin}"

def _normalize_price_str(s: str) -> Optional[float]:
    s = s.replace("\u202f", "").replace("\xa0", "").replace("€", "").replace("EUR", "")
    s = s.replace(" ", "").replace(".", "").replace(",", ".")
    m = re.search(r"[0-9]+(?:\.[0-9]{1,2})?", s)
    if not m:
        return None
    try:
        return float(m.group(0))
    except Exception:
        return None

def oxylabs_amazon_price(asin: str, country: str) -> Dict:
    """
    1) Essaye Oxylabs en mode structuré (amazon_product + parse:true)
    2) Si pas de prix, fallback HTML (amazon) + parsing (JSON-LD puis texte affiché)
    """
    if not (OXY_USER and OXY_PASS):
        raise HTTPException(status_code=500, detail="Oxylabs credentials missing")

    domain = COUNTRY_TO_DOMAIN[country]
    geo = COUNTRY_TO_GEO[country]

    # ---- 1) MODE STRUCTURÉ ----
    payload_parsed = {
        "source": "amazon_product",
        "query": asin,
        "domain": domain,
        "geo_location": geo,
        "parse": True,
    }
    r = requests.post(OXY_ENDPOINT, auth=(OXY_USER, OXY_PASS), json=payload_parsed, timeout=60)
    if r.status_code >= 400:
        raise HTTPException(status_code=502, detail=f"Oxylabs parsed error {r.status_code}: {r.text[:200]}")

    data = r.json()
    content = (data.get("results") or [{}])[0].get("content") or {}
    price_val = None
    if isinstance(content, dict):
        # chemins fréquents
        price_val = (content.get("buybox_winner") or {}).get("price") or content.get("price")
        if price_val is None and "buybox" in content:
            price_val = (content["buybox"] or {}).get("price")
        if isinstance(price_val, str):
            price_val = _normalize_price_str(price_val)
        if isinstance(price_val, (int, float)):
            return {"price": float(price_val), "currency": "EUR"}

    # ---- 2) FALLBACK HTML ----
    url = f"https://{domain}/dp/{asin}"
    payload_html = {"source": "amazon", "url": url, "geo_location": geo}
    r2 = requests.post(OXY_ENDPOINT, auth=(OXY_USER, OXY_PASS), json=payload_html, timeout=60)
    if r2.status_code >= 400:
        raise HTTPException(status_code=502, detail=f"Oxylabs html error {r2.status_code}: {r2.text[:200]}")

    html = (r2.json().get("results") or [{}])[0].get("content") or ""
    if not html:
        raise HTTPException(status_code=502, detail="Empty content from Oxylabs")

    # A) JSON-LD (schema.org)
    try:
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(html, "html.parser")
        for script in soup.find_all("script", type="application/ld+json"):
            try:
                data_ld = json.loads(script.string or "")
                candidates = data_ld if isinstance(data_ld, list) else [data_ld]
                for d in candidates:
                    offer = d.get("offers") if isinstance(d, dict) else None
                    if isinstance(offer, dict) and "price" in offer:
                        val = offer.get("price")
                        if isinstance(val, (int, float)):
                            return {"price": float(val), "currency": "EUR"}
                        if isinstance(val, str):
                            num = _normalize_price_str(val)
                            if num is not None:
                                return {"price": num, "currency": "EUR"}
            except Exception:
                continue
    except Exception:
        pass

    # B) Texte affiché (a-offscreen et variantes)
    try:
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(html, "html.parser")

        def pick_price_text():
            for sel in [
                ".a-price .a-offscreen",
                "#apex_desktop .a-offscreen",
                "#corePrice_feature_div .a-offscreen",
                "#priceblock_ourprice",
                "#priceblock_dealprice",
                "#priceblock_saleprice",
            ]:
                n = soup.select_one(sel)
                if n and n.get_text(strip=True):
                    return n.get_text(strip=True)
            n = soup.find("span", {"class": "a-offscreen"})
            if n and n.get_text(strip=True):
                return n.get_text(strip=True)
            return None

        txt = pick_price_text()
        if txt:
            num = _normalize_price_str(txt)
            if num is not None:
                return {"price": num, "currency": "EUR"}
    except Exception:
        pass

    # C) Fallback ultime: regex brute
    m = re.search(r"([0-9][0-9\.,\s\u00A0\u202F]+)\s?(€|EUR)", html)
    if m:
        num = _normalize_price_str(m.group(1))
        if num is not None:
            return {"price": num, "currency": "EUR"}

    raise HTTPException(status_code=404, detail="Price not found")

# -------------------- Routes --------------------

@app.get("/")
def root():
    return {"ok": True, "service": "AmaHunter API (lite)"}

@app.post("/compare", response_model=CompareResponse)
def compare(req: CompareRequest):
    asin = extract_asin(req.input)
    if not asin:
        raise HTTPException(status_code=400, detail="ASIN or valid Amazon URL required")

    items: List[CompareItem] = []
    for country in ALLOWED_COUNTRIES:
        try:
            p = oxylabs_amazon_price(asin, country)
            items.append(CompareItem(country=country, price=p["price"], currency=p["currency"], affiliate_link=direct_link(asin, country)))
        except HTTPException:
            continue

    items.sort(key=lambda x: x.price)
    if not items:
        raise HTTPException(status_code=404, detail="No prices found for this ASIN")
    return CompareResponse(asin=asin, items=items)

@app.get("/history/{asin}")
def history(asin: str):
    # Pas d'historique pour la version lite (frontend attend un format similaire)
    return {"asin": asin, "rows": []}

@app.get("/debug")
def debug(asin: str, country: str = "FR"):
    domain = COUNTRY_TO_DOMAIN.get(country, "amazon.fr")
    geo = COUNTRY_TO_GEO.get(country, "France")
    payload = {"source": "amazon_product", "query": asin, "domain": domain, "geo_location": geo, "parse": True}
    r = requests.post(OXY_ENDPOINT, auth=(OXY_USER, OXY_PASS), json=payload, timeout=60)
    ok = r.ok
    return {
        "status_code": r.status_code,
        "ok": ok,
        "short": (r.text[:400] if not ok else None)
    }
