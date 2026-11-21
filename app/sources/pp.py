"""pp.lv listings through the site's own public JSON API.

pp.lv asks crawlers for a 5 second delay (robots.txt); the shared HTTP client
enforces that for this host no matter what. The API is the same one the pp.lv
web app calls, returns 20 lots per page, and carries structured fields (year,
mileage, TA date, area, floor ...) plus the full description and price history.
"""
import json
import math
import re
from datetime import datetime, timezone

from .. import config, db
from ..norm import (fuel_from_text, gearbox_from_text, norm_key, parse_float, parse_int,
                    ta_from_value)

CARS = 2
FLATS = 3811
HOUSES = 3813

CATEGORY = {"car": CARS, "flat": FLATS, "house": HOUSES}
CATEGORY_SLUG = {
    "car": "transports-un-tehnika/vieglie-auto",
    "flat": "nekustamais-ipasums/dzivokli",
    "house": "nekustamais-ipasums/majas-vasarnicas",
}
PAGE_SIZE = 20
_SELL = {"pardod", "pardot"}
_RENT = {"izire", "izirat", "iznomā", "iznoma"}


def _iso_utc(value: str | None) -> str | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        return None


def filter_values(lot: dict) -> dict[str, list[dict]]:
    """adFilterValues keyed by diacritic-free filter name.

    Each entry keeps `text` (the listing's own typed value) and `disp` (the chosen option).
    `filter.description` is deliberately ignored: it is the filter's generic help text and
    is identical on every listing, so it must never be read as a value.
    """
    out: dict[str, list[dict]] = {}
    for item in lot.get("adFilterValues") or []:
        name = norm_key((item.get("filter") or {}).get("name"))
        value = item.get("value") or {}
        out.setdefault(name, []).append({
            "text": item.get("textValue"),
            "disp": value.get("displayValue"),
        })
    return out


def _first(fv: dict, name: str) -> dict | None:
    vals = fv.get(norm_key(name))
    return vals[0] if vals else None


def _txt(fv: dict, name: str) -> str | None:
    v = _first(fv, name)
    if not v:
        return None
    for key in ("text", "disp"):
        if v.get(key) not in (None, ""):
            return str(v[key])
    return None


def _engine_l(fv: dict) -> float | None:
    """textValue holds the size in the chosen unit (litres, or cm3 when disp says so)."""
    v = _first(fv, "Motora tilpums")
    if not v:
        return None
    value = parse_float(v.get("text"))
    if value is None:
        return None
    if (v.get("disp") or "").lower().startswith("cm") or value > 20:
        value /= 1000.0
    return value if 0.1 <= value <= 9.0 else None


def deal_type_of(lot: dict) -> str | None:
    name = norm_key(((lot.get("action") or {}).get("name") or ""))
    if name in _SELL:
        return "sell"
    if name in {norm_key(x) for x in _RENT}:
        return "rent"
    return None


def _price(lot: dict, price_type_id: int = 1) -> float | None:
    prices = lot.get("prices") or []
    for p in prices:
        if (p.get("priceType") or {}).get("id") == price_type_id and p.get("value"):
            return float(p["value"])
    if price_type_id == 1 and prices and prices[0].get("value"):
        return float(prices[0]["value"])
    return None


def _price_history(lot: dict) -> list[tuple[str, float]]:
    for p in lot.get("prices") or []:
        if (p.get("priceType") or {}).get("id") == 1:
            hist = [(_iso_utc(h.get("timestamp")), float(h["value"]))
                    for h in p.get("priceHistory") or [] if h.get("value")]
            return [(ts, v) for ts, v in sorted(hist, key=lambda x: x[0] or "") if ts]
    return []


def parse_lot(lot: dict, vertical: str, region_name: str | None = None) -> dict | None:
    """One pp.lv lot -> normalised listing record (None if unusable)."""
    deal = deal_type_of(lot)
    price = _price(lot)
    if deal is None or not price or not lot.get("frontUrl"):
        return None
    fv = filter_values(lot)
    texts = lot.get("adTexts") or []
    description = texts[0].get("text") if texts else None
    leaf = ((lot.get("publicLocation") or {}).get("region") or {}).get("name")
    owner = lot.get("owner") or {}

    rec = {
        "source": "pp",
        "ext_id": str(lot["id"]),
        "vertical": vertical,
        "deal_type": deal,
        "url": lot["frontUrl"],
        "title": (lot.get("title") or "").strip(),
        "description": description,
        "price": price,
        "region": region_name or leaf,
        "district": leaf,
        "posted_at": _iso_utc(lot.get("publishDate") or lot.get("createDate")),
        "views": lot.get("views"),
        "seller": "company" if owner.get("companyClassifiedsUserInfo") else "private",
        "history": _price_history(lot),
    }

    if vertical == "car":
        m = re.search(r"/vieglie-auto/([^/]+)/([^/!]+)", lot["frontUrl"])
        make, model = (m.group(1), m.group(2)) if m else (None, None)
        mileage = _txt(fv, "Nobraukums, km")
        year = parse_int(_txt(fv, "Izlaiduma gads"))
        km = parse_int(re.sub(r"\s", "", mileage)) if mileage else None
        if km is not None and km < 1000 and year and year <= datetime.now(timezone.utc).year - 3:
            km *= 1000  # "369" on a 2007 car is 369 thousand typed short
        rec["attrs"] = {
            "make": make,
            "model": model,
            "year": year,
            "mileage_km": km,
            "fuel": fuel_from_text(_txt(fv, "Degvielas tips")),
            "engine_l": _engine_l(fv),
            "gearbox": gearbox_from_text(_txt(fv, "Ātrumkārba")),
            "body": _txt(fv, "Virsbūves tips"),
            "drive": _txt(fv, "Piedziņa"),
            "color": _txt(fv, "Krāsa"),
            "ta_date": ta_from_value(_txt(fv, "Tehniskā apskate")) if _first(fv, "Tehniskā apskate") else None,
            "condition": _txt(fv, "Stāvoklis"),
        }
        if not rec["attrs"]["make"] or not rec["attrs"]["year"]:
            return None
    else:
        plot = _first(fv, "Zemes platība")
        plot_m2 = None
        if plot:
            value = parse_float(plot.get("text"))
            # The unit label is ambiguous ("m2/ha"); no plot is under 30 m2, so small numbers are hectares.
            plot_m2 = value * 10000 if value is not None and value < 30 else value
        rec["attrs"] = {
            "area_m2": parse_float(_txt(fv, "Platība, m2")),
            "rooms": parse_int(_txt(fv, "Istabu skaits")),
            "floor": parse_int(_txt(fv, "Stāvs")),
            "floors": parse_int(_txt(fv, "Stāvu skaits")),
            "series": _txt(fv, "Mājas sērija"),
            "house_type": _txt(fv, "Mājas tips"),
            "plot_m2": plot_m2,
            "condition": _txt(fv, "Īpašuma stāvoklis"),
        }
        if not rec["attrs"]["area_m2"]:
            return None
    rec["attrs"] = {k: v for k, v in rec["attrs"].items() if v is not None}
    return rec


def lots_url(category_id: int) -> str:
    return f"{config.PP_API}/categories/{category_id}/lots"


def fetch_page(client, category_id: int, page: int, region_id: int | None = None) -> dict:
    params = {"orderDirection": "DESC", "orderColumn": "orderDate"}
    if page > 1:
        params["currentPage"] = page
    if region_id:
        params["region"] = region_id
    return client.get_json(lots_url(category_id), params=params)["content"]


def fetch_regions(client, vertical: str) -> list[dict]:
    slug = CATEGORY_SLUG[vertical].replace("/", "%2F")
    data = client.get_json(f"{config.PP_API}/geo/regions?categorySlug={slug}")
    return [r for r in data["content"]["data"] if r.get("adCount", 0) > 0]


def store_lots(conn, lots: list[dict], vertical: str, stats: dict, region_name: str | None = None):
    now = db.utcnow()
    for lot in lots:
        rec = parse_lot(lot, vertical, region_name)
        if rec is None:
            continue
        history = rec.pop("history", [])
        res = db.upsert_listing(conn, rec, now)
        stats["seen"] += 1
        stats["new"] += res.is_new
        stats["changed"] += res.price_changed
        if res.is_new and len(history) > 1:  # pp.lv already knows earlier prices: keep them
            conn.execute("DELETE FROM price_history WHERE listing_id=?", (res.id,))
            for ts, value in history:
                conn.execute("INSERT INTO price_history VALUES (?,?,?)", (res.id, ts, value))
            conn.execute("UPDATE listings SET last_price_change=? WHERE id=?", (history[-1][0], res.id))


def _crawl_category(client, conn, stats, vertical, category_id, region_id=None, region_name=None) -> bool:
    complete = True
    first = fetch_page(client, category_id, 1, region_id)
    total = first.get("count") or 0
    pages = min(math.ceil(total / PAGE_SIZE), config.MAX_PAGES_PER_SECTION)
    stats["pages"] += 1
    store_lots(conn, first.get("data") or [], vertical, stats, region_name)
    conn.commit()
    for page in range(2, pages + 1):
        try:
            content = fetch_page(client, category_id, page, region_id)
        except Exception as exc:
            stats["errors"] += 1
            complete = False
            stats["note"] = f"cat {category_id} p{page}: {exc}"
            continue
        stats["pages"] += 1
        store_lots(conn, content.get("data") or [], vertical, stats, region_name)
        conn.commit()
    return complete


def crawl(client, conn, stats: dict, vertical: str) -> bool:
    """Crawl one vertical. Estate is split by top-level region so every lot knows its city."""
    if vertical == "car":
        return _crawl_category(client, conn, stats, vertical, CARS)
    complete = True
    for region in fetch_regions(client, vertical):
        ok = _crawl_category(client, conn, stats, vertical, CATEGORY[vertical],
                             region_id=region["id"], region_name=region["slug"])
        complete = complete and ok
    return complete
