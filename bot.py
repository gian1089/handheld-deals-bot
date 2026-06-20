#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Bot offerte console handheld -> notifiche su Telegram.

Controlla Subito, eBay e Vinted per le ricerche configurate sotto,
e invia su Telegram SOLO gli annunci nuovi (mai visti prima).
Non risponde a comandi: e' un "notificatore", quindi non deve girare
sempre. Lo lancia GitHub Actions a intervalli (vedi .github/workflows).
"""

import os
import re
import json
import time
import base64
import requests

# ---------------------------------------------------------------------------
# 1) COSA CERCARE  -- modifica liberamente questa lista
# ---------------------------------------------------------------------------
# query     = testo cercato sui siti
# max_price = prezzo massimo in euro (annunci sopra vengono ignorati)
# match     = il titolo deve contenere almeno una di queste stringhe
#             (in minuscolo) -> serve a tagliare accessori e annunci sbagliati
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
]

# Fonti attive (eBay si disattiva da solo se mancano le chiavi)
SOURCES = {"subito": True, "ebay": True, "vinted": True}

SEEN_FILE = "seen.json"
MAX_SEEN = 3000          # quanti ID "gia' visti" tenere in memoria
HTTP_TIMEOUT = 20
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

# ---------------------------------------------------------------------------
# Segreti (impostati come GitHub Actions Secrets, NON nel codice)
# ---------------------------------------------------------------------------
TG_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
TG_CHAT = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
EBAY_ID = os.environ.get("EBAY_CLIENT_ID", "").strip()
EBAY_SECRET = os.environ.get("EBAY_CLIENT_SECRET", "").strip()


# ---------------------------------------------------------------------------
# Utility
# ---------------------------------------------------------------------------
def parse_decimal(s):
    """Per eBay/Vinted: il punto e' separatore decimale ('450.00' -> 450)."""
    try:
        return int(float(s))
    except (TypeError, ValueError):
        return None


def parse_it_price(s):
    """Per Subito: formato italiano ('1.299,00' -> 1299)."""
    if s is None:
        return None
    s = re.sub(r"[^\d.,]", "", str(s))
    if not s:
        return None
    s = s.replace(".", "").replace(",", ".")
    try:
        return int(float(s))
    except ValueError:
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
    text = (
        f"🎮 <b>{deal['title']}</b>\n"
        f"💶 {deal['price']} €\n"
        f"🔎 {deal['source']} · <i>{deal['query']}</i>\n"
        f"{deal['url']}"
    )
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
# Fonte: SUBITO
# ---------------------------------------------------------------------------
def search_subito(search):
    out = []
    params = {
        "q": search["query"],
        "qso": "true",       # cerca nel titolo
        "shp": "true",
        "sort": "datedesc",  # piu' recenti prima
        "lim": "30",
        "start": "0",
        "t": "s",            # in vendita
    }
    headers = {"User-Agent": UA, "Accept": "application/json"}
    r = requests.get("https://hades.subito.it/v1/search/items",
                     params=params, headers=headers, timeout=HTTP_TIMEOUT)
    r.raise_for_status()
    for ad in r.json().get("ads", []):
        try:
            uid = "subito:" + str(ad.get("urn") or ad.get("id"))
            title = ad.get("subject", "")
            url = (ad.get("urls", {}) or {}).get("default", "")
            price = None
            for feat in ad.get("features", []):
                if feat.get("uri") == "/price":
                    vals = feat.get("values", [])
                    if vals:
                        price = parse_it_price(vals[0].get("value"))
                    break
            out.append({"id": uid, "title": title, "url": url, "price": price})
        except Exception as e:  # noqa: BLE001
            print(f"  ! Subito parse: {e}")
    return out


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
# Fonte: VINTED (API interna, puo' rompersi - isolata cosi' non blocca il resto)
# ---------------------------------------------------------------------------
def search_vinted(search):
    out = []
    s = requests.Session()
    s.headers.update({"User-Agent": UA, "Accept": "application/json"})
    # prima visita per ottenere i cookie di sessione
    s.get("https://www.vinted.it/", timeout=HTTP_TIMEOUT)
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
            out.append({"id": uid, "title": title, "url": url, "price": price})
        except Exception as e:  # noqa: BLE001
            print(f"  ! Vinted parse: {e}")
    return out


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
SOURCE_FUNCS = {
    "subito": search_subito,
    "ebay": search_ebay,
    "vinted": search_vinted,
}


def main():
    if not TG_TOKEN or not TG_CHAT:
        raise SystemExit("Mancano TELEGRAM_BOT_TOKEN o TELEGRAM_CHAT_ID.")

    if not (EBAY_ID and EBAY_SECRET):
        SOURCES["ebay"] = False
        print("eBay disattivato (chiavi mancanti).")

    seen = load_seen()
    first_run = len(seen) == 0
    new_count = 0

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
                seen.add(d["id"])  # marca come visto comunque (anche se scartato)

                if not title_ok(d["title"], search):
                    continue
                if d["price"] is None or d["price"] > search["max_price"]:
                    continue

                deal = {
                    "title": d["title"],
                    "price": d["price"],
                    "url": d["url"],
                    "source": name.capitalize(),
                    "query": search["query"],
                }
                # al primissimo avvio non spammare: registra e basta
                if not first_run:
                    telegram_send(deal)
                    time.sleep(1)  # rispetta i limiti di Telegram
                new_count += 1
                print(f"[{name}] NUOVO: {d['price']}€ - {d['title'][:60]}")

    save_seen(seen)

    if first_run:
        print(f"Primo avvio: registrati {new_count} annunci senza notifiche.")
    else:
        print(f"Fatto. Notifiche inviate: {new_count}.")


if __name__ == "__main__":
    main()
