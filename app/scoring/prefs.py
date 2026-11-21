"""Buyer preferences and market signals layered on top of the statistical fair value.

The fair-value model answers "is it cheap for what it is?". These multipliers answer
"is it a *good buy* for me?": fuel type you trust, mileage wear beyond what the price
already reflects, models with known rust problems, an expiring technical inspection
(TA), how fresh the ad is, and whether the seller has already cut the price.

Every factor is 1.0 when it has nothing to say, and each "importance" setting blends
between ignoring the factor (0) and applying it fully (1).
"""
from __future__ import annotations

import copy
from datetime import datetime, timezone

from ..norm import norm_key

DEFAULT_PREFS: dict = {
    "car": {
        "fuel": {"diesel": 0.9, "petrol": 0.85, "gas": 0.75, "hybrid": 0.7, "electric": 0.6},
        "mileage": {"diesel_threshold": 150_000, "other_threshold": 100_000, "rate": 0.1, "importance": 1.0},
        "corrosion_importance": 1.0,
        "ta": {"importance": 1.0, "unknown_default": 0.8, "min_age_years": 10},
    },
    "common": {"text_importance": 1.0, "freshness": 1.0, "price_drop": 1.0},
}

# Models with a known rust problem: (make, model or None, first year, last year, multiplier).
CORROSION_RULES = [
    ("mazda", None, 0, 2013, 0.5),
    ("volkswagen", "passat", 2005, 2010, 0.5),
]
# Remaining TA validity: (months, weight). Linear between points, 1.0 above the first,
# extrapolated and floored below the last (expired / none on record).
TA_ANCHORS = [(36, 1.0), (24, 0.95), (12, 0.9), (1, 0.4)]
TA_FLOOR = 0.2


def merge_prefs(overrides: dict | None) -> dict:
    prefs = copy.deepcopy(DEFAULT_PREFS)

    def merge(dst, src):
        for k, v in (src or {}).items():
            if isinstance(v, dict) and isinstance(dst.get(k), dict):
                merge(dst[k], v)
            elif k in dst:
                dst[k] = v

    merge(prefs, overrides)
    return prefs


def _blend(raw: float, importance: float) -> float:
    return 1.0 - importance * (1.0 - raw)


def fuel_weight(prefs: dict, fuel: str | None) -> float:
    return float(prefs["car"]["fuel"].get(fuel or "petrol", 0.8))


def mileage_weight(prefs: dict, attrs: dict) -> float:
    m = prefs["car"]["mileage"]
    km = attrs.get("mileage_km")
    if km is None:
        return 1.0
    threshold = m["diesel_threshold"] if attrs.get("fuel") == "diesel" else m["other_threshold"]
    raw = 1.0
    if km > threshold:
        raw = max(0.0, 1.0 - m["rate"] * (km - threshold) / 100_000.0)
    return _blend(raw, m["importance"])


def corrosion_multiplier(prefs: dict, attrs: dict) -> float:
    make, model, year = norm_key(attrs.get("make")), norm_key(attrs.get("model")), attrs.get("year") or 0
    mult = 1.0
    for r_make, r_model, y0, y1, factor in CORROSION_RULES:
        if make == r_make and (r_model is None or model == r_model) and y0 <= year <= y1:
            mult *= factor
    return _blend(mult, prefs["car"]["corrosion_importance"])


def ta_months_remaining(ta_date: str, now: datetime) -> int:
    y, m = (int(x) for x in ta_date.split("-"))
    return (y - now.year) * 12 + (m - now.month)


def ta_weight_raw(months: float) -> float:
    if months >= TA_ANCHORS[0][0]:
        return 1.0
    for (m1, w1), (m2, w2) in zip(TA_ANCHORS, TA_ANCHORS[1:]):
        if m2 <= months <= m1:
            return w2 + (months - m2) / (m1 - m2) * (w1 - w2)
    (m1, w1), (m2, w2) = TA_ANCHORS[-2], TA_ANCHORS[-1]
    slope = (w1 - w2) / (m1 - m2)
    return max(TA_FLOOR, w2 + (months - m2) * slope)


def ta_weight(prefs: dict, attrs: dict, now: datetime) -> float:
    cfg = prefs["car"]["ta"]
    if now.year - (attrs.get("year") or now.year) < cfg["min_age_years"]:
        return 1.0
    ta = attrs.get("ta_date")
    if ta == "none":
        raw = ta_weight_raw(0)
    elif not ta:
        raw = cfg["unknown_default"]
    else:
        raw = ta_weight_raw(ta_months_remaining(ta, now))
    return _blend(raw, cfg["importance"])


def _days_since(ts: str | None, now: datetime) -> float | None:
    if not ts:
        return None
    return (now - datetime.fromisoformat(ts.replace("Z", "+00:00"))).total_seconds() / 86400.0


def freshness_multiplier(prefs: dict, age_days: float | None) -> float:
    if age_days is None:
        return 1.0
    if age_days < 1:
        raw = 1.15
    elif age_days < 3:
        raw = 1.08
    elif age_days < 14:
        raw = 1.0
    elif age_days < 45:
        raw = 0.97
    else:
        raw = 0.93
    return 1.0 + prefs["common"]["freshness"] * (raw - 1.0)


def price_drop_multiplier(prefs: dict, first_price: float | None, price: float, last_change_days: float | None):
    if not first_price or price >= first_price:
        return 1.0, 0.0
    drop = (first_price - price) / first_price
    bonus = min(0.25, 1.5 * drop)
    if last_change_days is not None and last_change_days <= 7:
        bonus += 0.05
    return 1.0 + prefs["common"]["price_drop"] * bonus, drop


def final_score(listing: dict, score: dict, prefs: dict, now: datetime | None = None) -> dict:
    """Combine the market score with buyer preferences and signals; returns score + explanation."""
    now = now or datetime.now(timezone.utc)
    factors: list[tuple[str, float]] = []
    value = score["base"]

    if listing["vertical"] == "car":
        attrs = listing["attrs"]
        for name, f in (
            ("fuel", fuel_weight(prefs, attrs.get("fuel"))),
            ("mileage", mileage_weight(prefs, attrs)),
            ("corrosion", corrosion_multiplier(prefs, attrs)),
            ("inspection", ta_weight(prefs, attrs, now)),
        ):
            factors.append((name, round(f, 3)))
            value *= f

    text = _blend(score.get("text_mult") or 1.0, prefs["common"]["text_importance"])
    factors.append(("wording", round(text, 3)))
    value *= text

    fresh = freshness_multiplier(prefs, listing.get("age_days"))
    factors.append(("freshness", round(fresh, 3)))
    value *= fresh

    drop_mult, drop = price_drop_multiplier(prefs, listing.get("first_price"), listing["price"],
                                            listing.get("last_change_days"))
    factors.append(("price_drop", round(drop_mult, 3)))
    value *= drop_mult

    return {"score": round(value, 2), "factors": factors, "drop_pct": round(drop * 100, 1)}
