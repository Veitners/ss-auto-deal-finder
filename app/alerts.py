"""Optional push notifications through an ntfy topic (set NTFY_URL to enable)."""
from __future__ import annotations

import logging

import requests

from . import config, db
from .scoring import market
from .scoring.prefs import merge_prefs

log = logging.getLogger("radar.alerts")

MAX_PER_RUN = 5
LABEL = {"car": "Car", "flat": "Flat", "house": "House"}


def _headline(d: dict) -> str:
    a = d["attrs"]
    if d["vertical"] == "car":
        make = (a.get("make") or "").replace("-", " ").title()
        return f"{make} {a.get('model_label') or a.get('model') or ''} {a.get('year') or ''}".strip()
    area = f"{a.get('area_m2'):g} m²" if a.get("area_m2") else ""
    rooms = f"{a.get('rooms')}-room " if a.get("rooms") else ""
    return f"{rooms}{LABEL[d['vertical']].lower()} {area} {d.get('district') or d.get('region') or ''}".strip()


def pick_candidates(conn, prefs: dict, threshold: float) -> list[dict]:
    already = {r["listing_id"] for r in conn.execute("SELECT listing_id FROM alerts_sent")}
    found = []
    for vertical in ("car", "flat", "house"):
        for d in market.ranked(conn, vertical, prefs):
            if d["score"] < threshold:
                break  # ranked() is sorted, nothing further can qualify
            fresh = d["age_days"] is not None and d["age_days"] < 2
            if (fresh and d["conf"] >= 0.4 and "too_good" not in d["flags"] and d["id"] not in already):
                found.append(d)
    found.sort(key=lambda x: x["score"], reverse=True)
    return found[:MAX_PER_RUN]


def send_new_deals(conn) -> int:
    if not config.NTFY_URL:
        return 0
    prefs = merge_prefs(db.get_setting(conn, "prefs"))
    threshold = float(db.get_setting(conn, "alert_threshold", 15))
    base, _, topic = config.NTFY_URL.rstrip("/").rpartition("/")
    sent = 0
    for d in pick_candidates(conn, prefs, threshold):
        body = (f"EUR {d['price']:,.0f} vs fair {d['fair']:,.0f} ({d['discount'] * 100:.0f}% below). "
                f"Score {d['score']:.0f}, confidence {d['conf'] * 100:.0f}%.")
        try:
            r = requests.post(base, json={
                "topic": topic, "title": _headline(d), "message": body,
                "click": d["url"], "tags": ["moneybag"],
            }, timeout=10)
            r.raise_for_status()
        except requests.RequestException as exc:
            log.warning("ntfy delivery failed: %s", exc)
            break
        conn.execute("INSERT OR REPLACE INTO alerts_sent VALUES (?, ?)", (d["id"], db.utcnow()))
        sent += 1
    conn.commit()
    return sent
