"""Rental yield: what would this flat rent for, and what does that make of the asking price?

Rent comparables are the active for-rent flats on both sites. The estimate walks from the
most specific comparable set to the most general, taking the first one with enough
listings: same district and room count, then district, then city and room count, then city.
Rent per m² is used (small flats cost more per m², hence the room-count bands), so a
60 m² flat is priced from comparable 60 m²-ish flats, not from the average rent.

Gross yield = estimated annual rent / asking price. It ignores vacancy, tax and upkeep,
so it is for comparing flats with each other, not a promise of income.
"""
from __future__ import annotations

from collections import defaultdict
from statistics import median

from ..norm import norm_key

MIN_N = {"dist_band": 6, "dist": 10, "reg_band": 10, "reg": 15}


def _band(rooms) -> int | None:
    return None if rooms is None else min(max(int(rooms), 1), 4)


def rent_ok(row: dict) -> bool:
    a = row["attrs"]
    area = a.get("area_m2")
    if not area or not (10 <= area <= 300) or not (80 <= row["price"] <= 8000):
        return False
    return 3.0 <= row["price"] / area <= 45.0


def build_index(rent_rows: list[dict]) -> dict:
    idx: dict[str, dict] = {k: defaultdict(list) for k in MIN_N}
    for r in rent_rows:
        if not rent_ok(r):
            continue
        rpm2 = r["price"] / r["attrs"]["area_m2"]
        region, district, band = norm_key(r.get("region")), norm_key(r.get("district")), _band(r["attrs"].get("rooms"))
        idx["dist_band"][(region, district, band)].append(rpm2)
        idx["dist"][(region, district)].append(rpm2)
        idx["reg_band"][(region, band)].append(rpm2)
        idx["reg"][region].append(rpm2)
    return idx


def estimate_rent(idx: dict, row: dict) -> dict | None:
    """Estimated monthly rent for a for-sale flat, or None when there are too few comparables."""
    area = row["attrs"].get("area_m2")
    if not area:
        return None
    region, district, band = norm_key(row.get("region")), norm_key(row.get("district")), _band(row["attrs"].get("rooms"))
    for level, key in (("dist_band", (region, district, band)), ("dist", (region, district)),
                       ("reg_band", (region, band)), ("reg", region)):
        sample = idx[level].get(key)
        if sample and len(sample) >= MIN_N[level]:
            rent = median(sample) * area
            return {"rent": round(rent), "rent_n": len(sample), "rent_level": level,
                    "yield": rent * 12.0 / row["price"]}
    return None
