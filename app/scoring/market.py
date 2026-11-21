"""Database glue: compute stored market scores, then rank listings for a given set of preferences."""
from __future__ import annotations

import json
import math
import sqlite3
from datetime import datetime, timezone

from .. import classify, db, textflags
from . import prefs as P
from . import rent
from .models import SCORERS

TOO_GOOD = 0.45   # beyond this a listing is flagged and hidden by default
PEAK = 0.30       # the most credible kind of bargain: about 30% below fair value


def plausible_discount(discount: float) -> float:
    """How much a given discount should count.

    Counts in full up to ~30% below fair value. Beyond that the evidence turns: a flat 55% below
    market is far more likely to be unfinished, encumbered, a fractional share or a typo than a
    55% bargain, so bigger gaps count for *less*, not more. (A plain cap would leave the top of
    every list full of listings sitting just under the cap, which are the most suspicious ones.)
    """
    if discount <= 0:
        return 0.0
    if discount <= PEAK:
        return discount
    return max(0.05, PEAK - (discount - PEAK) * 1.2)


def _load_rows(conn: sqlite3.Connection, vertical: str, deal_type: str = "sell") -> list[dict]:
    rows = []
    for r in conn.execute(
        "SELECT id, source, vertical, price, region, district, title, description, attrs "
        "FROM listings WHERE vertical=? AND deal_type=? AND active=1 AND price IS NOT NULL",
        (vertical, deal_type),
    ):
        d = dict(r)
        d["attrs"] = json.loads(d["attrs"] or "{}")
        d["kind_changed"] = False
        if vertical == "house":  # summer cottage vs house, read from the seller's wording
            kind = classify.house_kind(d["title"], d["description"])
            d["kind_changed"] = d["attrs"].get("kind") != kind
            d["attrs"]["kind"] = kind
        rows.append(d)
    return rows


def derive(price: float, res: dict) -> dict:
    """Turn a fair-value estimate into discount, z-score and the market-only score."""
    fair, sigma, conf = res["fair"], res["sigma"], res["conf"]
    discount = 1.0 - price / fair
    z = (math.log(fair) - math.log(price)) / sigma
    too_good = discount > TOO_GOOD
    base = 100.0 * plausible_discount(discount) * conf * (0.6 if too_good else 1.0)
    return {"discount": discount, "z": z, "base": base, "too_good": too_good}


def compute_market(conn: sqlite3.Connection, verticals=("car", "flat", "house")) -> dict[str, int]:
    """Recompute and store fair value for every active for-sale listing."""
    counts: dict[str, int] = {}
    now = db.utcnow()
    for vertical in verticals:
        # Everything slow (model fit, regex scans, JSON) happens BEFORE touching the write lock,
        # so a rescore never stalls the crawlers that share this database.
        rows = _load_rows(conn, vertical)
        results = SCORERS[vertical](rows) if rows else []
        rent_index = rent.build_index(_load_rows(conn, "flat", "rent")) if vertical == "flat" else None
        batch = []
        for row, res in zip(rows, results):
            if res is None:
                continue
            d = derive(row["price"], res)
            flags, text_mult = textflags.scan(vertical, row["title"], row["description"])
            if d["too_good"]:
                flags.append("too_good")
            extra = (rent.estimate_rent(rent_index, row) or {}) if rent_index is not None else {}
            batch.append((row["id"], res["fair"], res["sigma"], res["conf"], d["discount"], d["z"], d["base"],
                          res["method"], res["group_key"], res["group_n"], res["knn_fair"],
                          json.dumps([rows[i]["id"] for i in res["comps"]]), json.dumps(flags),
                          text_mult, json.dumps(extra), now))
        kinds = [(r["attrs"]["kind"], r["id"]) for r in rows if r["kind_changed"]]

        conn.commit()  # end any open read transaction, then write in one short burst
        conn.execute("DELETE FROM scores WHERE listing_id IN (SELECT id FROM listings WHERE vertical=?)",
                     (vertical,))
        conn.executemany(  # persist the detected house type so the API can filter on it cheaply
            "UPDATE listings SET attrs = json_set(attrs, '$.kind', ?) WHERE id = ?", kinds)
        conn.executemany(
            """INSERT INTO scores (listing_id, fair, sigma, conf, discount, z, base, method,
                   group_key, group_n, knn_fair, comps, flags, text_mult, extra, computed_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", batch)
        conn.commit()
        counts[vertical] = len(batch)
    return counts


def _parse_ts(ts: str | None) -> datetime | None:
    return datetime.fromisoformat(ts.replace("Z", "+00:00")) if ts else None


def ranked(conn: sqlite3.Connection, vertical: str, prefs: dict, now: datetime | None = None) -> list[dict]:
    """Every scored listing of a vertical with its final, preference-adjusted score (best first)."""
    now = now or datetime.now(timezone.utc)
    baselines = {k: v for k, v in
                 ((r["key"], json.loads(r["value"])) for r in
                  conn.execute("SELECT key, value FROM settings WHERE key LIKE 'baseline:%'"))}
    out = []
    query = """
        SELECT l.*, s.fair, s.sigma, s.conf, s.discount, s.z, s.base, s.method, s.group_key,
               s.group_n, s.knn_fair, s.flags, s.text_mult, s.extra,
               (SELECT price FROM price_history h WHERE h.listing_id = l.id ORDER BY ts ASC LIMIT 1)
                   AS first_price
        FROM listings l JOIN scores s ON s.listing_id = l.id
        WHERE l.vertical = ? AND l.deal_type = 'sell' AND l.active = 1"""
    for r in conn.execute(query, (vertical,)):
        d = dict(r)
        d["attrs"] = json.loads(d["attrs"] or "{}")
        d["flags"] = json.loads(d["flags"] or "[]")
        d["extra"] = json.loads(d["extra"] or "{}")

        posted = _parse_ts(d["posted_at"])
        first_seen = _parse_ts(d["first_seen"])
        baseline = baselines.get(f"baseline:{d['source']}:{vertical}")
        if posted:
            d["age_days"] = max(0.0, (now - posted).total_seconds() / 86400.0)
        elif baseline and d["first_seen"] > baseline:
            d["age_days"] = max(0.0, (now - first_seen).total_seconds() / 86400.0)
        else:
            d["age_days"] = None  # seen in the very first crawl: real age unknown, so no fresh bonus
        change = _parse_ts(d["last_price_change"])
        d["last_change_days"] = ((now - change).total_seconds() / 86400.0
                                 if change and d["first_price"] and d["first_price"] != d["price"] else None)

        fin = P.final_score(d, d, prefs, now)
        d.update(fin)
        out.append(d)
    out.sort(key=lambda x: x["score"], reverse=True)
    return out


def comparables(conn: sqlite3.Connection, listing_id: int) -> list[dict]:
    row = conn.execute("SELECT comps FROM scores WHERE listing_id=?", (listing_id,)).fetchone()
    ids = json.loads(row["comps"]) if row and row["comps"] else []
    out = []
    for cid in ids:
        r = conn.execute("SELECT id, source, title, url, price, attrs FROM listings WHERE id=?", (cid,)).fetchone()
        if r:
            d = dict(r)
            d["attrs"] = json.loads(d["attrs"] or "{}")
            out.append(d)
    return out
