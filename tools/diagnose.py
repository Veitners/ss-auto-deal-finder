"""Calibration report for the fair-value model, read straight from the live database.

    docker exec deal-radar python tools/diagnose.py [car|flat|house]

A healthy model has residuals centred on zero in every segment (age, price tier, source,
make). A segment whose median residual is far from zero would produce fake deals or hide
real ones, so this is the first thing to look at after changing the model.
"""
import json
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime, timezone

import numpy as np

vertical = sys.argv[1] if len(sys.argv) > 1 else "car"
conn = sqlite3.connect("/data/radar.db", timeout=60)  # read-only use; WAL needs a normal connection
conn.row_factory = sqlite3.Row
rows = conn.execute(
    """SELECT l.id, l.source, l.price, l.attrs, l.region, s.fair, s.sigma, s.conf, s.discount, s.group_n, s.method, s.flags
       FROM listings l JOIN scores s ON s.listing_id = l.id
       WHERE l.vertical=? AND l.deal_type='sell' AND l.active=1""", (vertical,)).fetchall()
print(f"{vertical}: {len(rows)} scored listings")

resid = np.array([np.log(r["price"] / r["fair"]) for r in rows])
disc = np.array([r["discount"] for r in rows])
sigma = np.array([r["sigma"] for r in rows])
conf = np.array([r["conf"] for r in rows])
q = lambda a, ps: " ".join(f"p{p}={np.percentile(a, p):+.3f}" for p in ps)
print("\nlog(price/fair) quantiles :", q(resid, [1, 5, 25, 50, 75, 95, 99]))
print(f"model sigma (median)      : {np.median(sigma):.3f}   confidence median {np.median(conf):.2f}")
print(f"share discount >20% / >30% / >45% : {np.mean(disc > .2):.1%} / {np.mean(disc > .3):.1%} / {np.mean(disc > .45):.1%}")
print(f"share price ABOVE fair by >20%    : {np.mean(disc < -.2):.1%}   (a symmetric model has about as many)")
print(f"MAPE of fair vs price             : {np.mean(np.abs(np.exp(-resid) - 1)):.1%}")

def segment(title, key, minimum=150):
    groups = defaultdict(list)
    for r, x in zip(rows, resid):
        groups[key(r)].append(x)
    print(f"\n-- {title} (median log residual; 0 is perfect) --")
    for name, vals in sorted(groups.items(), key=lambda kv: str(kv[0])):
        if len(vals) >= minimum:
            print(f"  {str(name):<18} n={len(vals):>6}  median={np.median(vals):+.3f}  p10={np.percentile(vals, 10):+.3f}  p90={np.percentile(vals, 90):+.3f}")

if vertical == "car":
    year_now = datetime.now(timezone.utc).year
    attrs = lambda r: json.loads(r["attrs"])
    segment("age", lambda r: "0-3y" if year_now - attrs(r).get("year", 0) <= 3 else "4-6y" if year_now - attrs(r)["year"] <= 6 else "7-10y" if year_now - attrs(r)["year"] <= 10 else "11-15y" if year_now - attrs(r)["year"] <= 15 else "16y+")
    segment("price tier", lambda r: "<3k" if r["price"] < 3000 else "3-6k" if r["price"] < 6000 else "6-10k" if r["price"] < 10000 else "10-20k" if r["price"] < 20000 else "20-40k" if r["price"] < 40000 else "40k+")
    segment("source", lambda r: r["source"])
    segment("fuel", lambda r: attrs(r).get("fuel"))
    segment("mileage", lambda r: "unknown" if attrs(r).get("mileage_km") is None else "<50k" if attrs(r)["mileage_km"] < 50000 else "50-150k" if attrs(r)["mileage_km"] < 150000 else "150-250k" if attrs(r)["mileage_km"] < 250000 else "250k+")
    segment("method", lambda r: r["method"])
    segment("make", lambda r: attrs(r).get("make"), minimum=400)
else:
    attrs = lambda r: json.loads(r["attrs"])
    segment("source", lambda r: r["source"])
    segment("price tier", lambda r: "<30k" if r["price"] < 30000 else "30-60k" if r["price"] < 60000 else "60-120k" if r["price"] < 120000 else "120-250k" if r["price"] < 250000 else "250k+", minimum=50)
    segment("area", lambda r: "<40" if (attrs(r).get("area_m2") or 0) < 40 else "40-60" if attrs(r)["area_m2"] < 60 else "60-90" if attrs(r)["area_m2"] < 90 else "90-150" if attrs(r)["area_m2"] < 150 else "150+", minimum=50)
    segment("region", lambda r: r["region"], minimum=100)
