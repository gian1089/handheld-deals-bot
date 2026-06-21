#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Bot offerte console handheld -> notifiche su Telegram.

Fonti:
  - Vinted   (API interna, gratis) con filtro feedback venditore
  - eBay     (API ufficiale, gratis - si attiva solo con le chiavi)
  - Amazon   (nuovo + usato) tramite il feed RSS di CamelCamelCamel

Invia su Telegram SOLO gli annunci/alert nuovi (mai visti prima).
Non risponde a comandi: lo lancia GitHub Actions a intervalli.
"""

import os
import re
import json
import time
import base64
import requests
import xml.etree.ElementTree as ET

# ---------------------------------------------------------------------------
# 1) COSA CERCARE su Vinted/eBay  -- modifica liberamente
# ---------------------------------------------------------------------------
SEARCHES = [
    {"query": "ROG Xbox Ally X",      "max_price": 850, "match": ["ally x"]},
    {"query": "ROG Ally Z1 Extreme",  "max_price": 520, "match": ["ally"]},
    {"query": "MSI Claw 8 AI",        "max_price": 900, "match": ["claw"]},
    {"query": "Steam Deck OLED",      "max_price": 480, "match": ["steam deck"]},
    {"query": "Lenovo Legion Go",     "max_price": 480, "match": ["legion go"]},
]

# Se il titolo contiene una di queste parole -> scartato (accessori, ricambi...)
EXCLUDE = [
    "cover", "custodia", "case", "pellicola", "vetro", "grip", "guscio",
    "stand", "dock", "caricabatterie", "ricambi", "ricambio", "solo scatola",
    "vuota", "scatola vuota", "adesivi", "skin", "pad ", "memory", "scheda",
    # accessori in altre lingue (Vinted e' pieno di annunci FR/DE/ES)
    "coque", "carcasa", "funda", "etui", "étui", "housse", "pochette",
    "hülle", "hulle", "tragetasche", "joystick", "controllers",
    "chargeur", "caricatore", "sticker",
]

# Vinted: feedback minimo del venditore. 1 = scarta chi ha 0 feedback. 0 = disabilita.
VINTED_MIN_FEEDBACK = 1

# Fonti attive per le ricerche per-keyword (eBay si disattiva da solo se mancano le chiavi)
SOURCES = {"ebay": True, "vinted": True}

SEEN_FILE = "seen.json"
MAX_SEEN = 3000
HTTP_TIMEOUT = 20
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

# ---------------------------------------------------------------------------
# Segreti (GitHub Actions Secrets, NON nel codice)
# ---------------------------------------------------------------------------
TG_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
TG_CHAT = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
EBAY_ID = os.environ.get("EBAY_CLIENT_ID", "").strip()
EBAY_SECRET = os.environ.get("EBAY_CLIENT_SECRET", "").strip()
CAMEL_RSS_URL = os.environ.get("CAMEL_RSS_URL", "").strip()


# ---------------------------------------------------------------------------
# Utility
# ---------------------------------------------------------------------------
def parse_decimal(s):
    """Per eBay/Vinted: il punto e' separatore decimale ('450.00' -> 450)."""
    try:
        return int(float(s))
    except (TypeError, ValueError):
        return None


def title_ok(title, search):
    """True se il titolo passa i filtri match/EXCLUDE."""
    t = (title or "").lower()
    if any(bad in t for bad in EXCLUDE):
        return False
    match = search.get("match")
    if match and not any(m in t for m in match):
        return False
    return True


def load_seen():
    try:
        with open(SEEN_FILE, "r", encoding="utf-8") as f:
            return set(json.load(f))
    except (FileNotFoundError, json.JSONDecodeError):
        return set()


def save_seen(seen):
    data = list(seen)[-MAX_SEEN:]
    with open(SEEN_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=0)


def telegram_send(deal):
    lines = [f"🎮 <b>{deal['title']}</b>"]
    price = deal.get("price")
    if price not in (None, "", 0):
        lines.append(f"💶 {price} €")
    lines.append(f"🔎 {deal['source']} · <i>{deal['query']}</i>")
    lines.append(deal["url"])
    text = "\n".join(lines)

    url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
    payload = {
        "chat_id": TG_CHAT,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": False,
    }
    try:
        r = requests.post(url, json=payload, timeout=HTTP_TIMEOUT)
        if r.status_code != 200:
            print(f"  ! Telegram {r.status_code}: {r.text[:200]}")
    except requests.RequestException as e:
        print(f"  ! Telegram errore: {e}")


# ---------------------------------------------------------------------------
# Fonte: EBAY (API ufficiale Browse)
# ---------------------------------------------------------------------------
_ebay_token = {"value": None}


def ebay_token():
    if _ebay_token["value"]:
        return _ebay_token["value"]
    creds = base64.b64encode(f"{EBAY_ID}:{EBAY_SECRET}".encode()).decode()
    r = requests.post(
        "https://api.ebay.com/identity/v1/oauth2/token",
        headers={
            "Authorization": f"Basic {creds}",
            "Content-Type": "application/x-www-form-urlencoded",
        },
        data={
            "grant_type": "client_credentials",
            "scope": "https://api.ebay.com/oauth/api_scope",
        },
        timeout=HTTP_TIMEOUT,
    )
    r.raise_for_status()
    _ebay_token["value"] = r.json()["access_token"]
    return _ebay_token["value"]


def search_ebay(search):
    out = []
    token = ebay_token()
    headers = {
        "Authorization": f"Bearer {token}",
        "X-EBAY-C-MARKETPLACE-ID": "EBAY_IT",
        "Content-Type": "application/json",
    }
    params = {
        "q": search["query"],
        "limit": "30",
        "sort": "newlyListed",
        "filter": f"price:[..{search['max_price']}],priceCurrency:EUR",
    }
    r = requests.get(
        "https://api.ebay.com/buy/browse/v1/item_summary/search",
        params=params, headers=headers, timeout=HTTP_TIMEOUT,
    )
    r.raise_for_status()
    for it in r.json().get("itemSummaries", []) or []:
        try:
            uid = "ebay:" + str(it.get("itemId"))
            title = it.get("title", "")
            url = it.get("itemWebUrl", "")
            price = parse_decimal((it.get("price") or {}).get("value"))
            out.append({"id": uid, "title": title, "url": url, "price": price})
        except Exception as e:  # noqa: BLE001
            print(f"  ! eBay parse: {e}")
    return out


# ---------------------------------------------------------------------------
# Fonte: VINTED (API interna) + feedback venditore
# ---------------------------------------------------------------------------
_vinted_session = {"s": None}
_vinted_feedback_cache = {}


def get_vinted_session():
    """Sessione condivisa con cookie, riusata per annunci e profili venditore."""
    if _vinted_session["s"] is None:
        s = requests.Session()
        s.headers.update({"User-Agent": UA, "Accept": "application/json"})
        try:
            s.get("https://www.vinted.it/", timeout=HTTP_TIMEOUT)
        except requests.RequestException:
            pass
        _vinted_session["s"] = s
    return _vinted_session["s"]


def vinted_user_feedback(user_id):
    """Numero di feedback del venditore. None se non determinabile (fail-open)."""
    if not user_id:
        return None
    if user_id in _vinted_feedback_cache:
        return _vinted_feedback_cache[user_id]
    fb = None
    try:
        s = get_vinted_session()
        r = s.get(f"https://www.vinted.it/api/v2/users/{user_id}",
                  timeout=HTTP_TIMEOUT)
        if r.status_code == 200:
            fb = (r.json().get("user") or {}).get("feedback_count")
    except (requests.RequestException, ValueError):
        fb = None
    _vinted_feedback_cache[user_id] = fb
    return fb


def search_vinted(search):
    out = []
    s = get_vinted_session()
    r = s.get(
        "https://www.vinted.it/api/v2/catalog/items",
        params={
            "search_text": search["query"],
            "order": "newest_first",
            "per_page": "20",
        },
        timeout=HTTP_TIMEOUT,
    )
    r.raise_for_status()
    for it in r.json().get("items", []) or []:
        try:
            uid = "vinted:" + str(it.get("id"))
            title = it.get("title", "")
            url = it.get("url", "")
            price = it.get("price")
            if isinstance(price, dict):
                price = parse_decimal(price.get("amount"))
            else:
                price = parse_decimal(price)
            user = it.get("user") or {}
            out.append({
                "id": uid, "title": title, "url": url, "price": price,
                "seller_id": user.get("id"),
                # a volte il feedback e' gia' nell'oggetto user
                "feedback": user.get("feedback_count"),
            })
        except Exception as e:  # noqa: BLE001
            print(f"  ! Vinted parse: {e}")
    return out


# ---------------------------------------------------------------------------
# Fonte: CAMELCAMELCAMEL (Amazon nuovo+usato via feed RSS)
# ---------------------------------------------------------------------------
def fetch_camel():
    """Legge il feed RSS degli alert CamelCamelCamel.
    Gli alert scattano gia' sotto la soglia impostata su CCC, quindi
    qui non serve filtrare per prezzo o keyword: si rigira tutto il nuovo."""
    if not CAMEL_RSS_URL:
        return []
    out = []
    r = requests.get(CAMEL_RSS_URL, headers={"User-Agent": UA},
                     timeout=HTTP_TIMEOUT)
    r.raise_for_status()
    root = ET.fromstring(r.content)
    for item in root.iter("item"):
        title = (item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip()
        guid = (item.findtext("guid") or link or title).strip()
        out.append({"id": "camel:" + guid, "title": title, "url": link})
    return out


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
SOURCE_FUNCS = {
    "ebay": search_ebay,
    "vinted": search_vinted,
}


def main():
    if not TG_TOKEN or not TG_CHAT:
        raise SystemExit("Mancano TELEGRAM_BOT_TOKEN o TELEGRAM_CHAT_ID.")

    if not (EBAY_ID and EBAY_SECRET):
        SOURCES["ebay"] = False
        print("eBay disattivato (chiavi mancanti).")
    if not CAMEL_RSS_URL:
        print("CamelCamelCamel disattivato (CAMEL_RSS_URL mancante).")

    seen = load_seen()
    first_run = len(seen) == 0
    new_count = 0
    skipped_feedback = 0

    # --- Ricerche per keyword su Vinted/eBay ---
    for search in SEARCHES:
        for name, enabled in SOURCES.items():
            if not enabled:
                continue
            try:
                results = SOURCE_FUNCS[name](search)
            except Exception as e:  # noqa: BLE001
                print(f"[{name}] '{search['query']}' errore: {e}")
                continue

            for d in results:
                if d["id"] in seen:
                    continue
                seen.add(d["id"])
                if not title_ok(d["title"], search):
                    continue
                if d["price"] is None or d["price"] > search["max_price"]:
                    continue

                # Filtro feedback venditore (solo Vinted)
                if (name == "vinted" and VINTED_MIN_FEEDBACK and not first_run):
                    fb = d.get("feedback")
                    if fb is None:
                        fb = vinted_user_feedback(d.get("seller_id"))
                    if fb is not None and fb < VINTED_MIN_FEEDBACK:
                        skipped_feedback += 1
                        continue

                deal = {
                    "title": d["title"],
                    "price": d["price"],
                    "url": d["url"],
                    "source": name.capitalize(),
                    "query": search["query"],
                }
                if not first_run:
                    telegram_send(deal)
                    time.sleep(1)
                new_count += 1
                print(f"[{name}] NUOVO: {d['price']}€ - {d['title'][:60]}")

    # --- Amazon nuovo+usato via CamelCamelCamel (feed unico) ---
    try:
        for d in fetch_camel():
            if d["id"] in seen:
                continue
            seen.add(d["id"])
            deal = {
                "title": d["title"],
                "price": "",  # il prezzo e' gia' nel titolo dell'alert CCC
                "url": d["url"],
                "source": "Amazon (Camel)",
                "query": "price watch",
            }
            if not first_run:
                telegram_send(deal)
                time.sleep(1)
            new_count += 1
            print(f"[camel] NUOVO: {d['title'][:70]}")
    except Exception as e:  # noqa: BLE001
        print(f"[camel] errore: {e}")

    save_seen(seen)

    if skipped_feedback:
        print(f"Scartati per feedback venditore 0: {skipped_feedback}.")
    if first_run:
        print(f"Primo avvio: registrati {new_count} elementi senza notifiche.")
    else:
        print(f"Fatto. Notifiche inviate: {new_count}.")


if __name__ == "__main__":
    main()
