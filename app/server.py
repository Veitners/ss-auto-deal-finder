"""HTTP API + static dashboard. The crawl scheduler lives inside this process."""
from __future__ import annotations

import json
import logging
import threading
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, Header, HTTPException, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from . import config, db
from .jobs import JOBS, Scheduler, rescore
from .norm import norm_key
from .scoring import market
from .scoring.prefs import DEFAULT_PREFS, merge_prefs

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("radar.server")

STATIC = Path(__file__).parent / "static"
scheduler = Scheduler()

# Ranking every listing is the expensive step, so results are cached per (vertical, prefs).
_cache: dict[tuple, tuple[float, list[dict]]] = {}
_cache_lock = threading.Lock()
CACHE_TTL = 90.0
FLAGGED_BAD = {"damaged", "for_parts", "not_running", "dilapidated", "scam_wording", "encumbered",
               "no_papers", "share_only"}


@asynccontextmanager
async def lifespan(app: FastAPI):
    conn = db.connect()
    db.init(conn)
    conn.close()
    # Scores are stored, so after an upgrade they would stay stale until a crawl finishes:
    # refresh them once in the background (the dashboard keeps serving the old ones meanwhile).
    threading.Thread(target=_startup_rescore, name="startup-rescore", daemon=True).start()
    scheduler.start()
    yield
    scheduler.stop()


def _startup_rescore():
    try:
        conn = db.connect()
        if conn.execute("SELECT 1 FROM listings LIMIT 1").fetchone():
            counts = rescore(conn)
            log.info("startup rescore done: %s", counts)
        conn.close()
    except Exception:
        log.exception("startup rescore failed")


app = FastAPI(title="Deal Radar", lifespan=lifespan)


def _conn():
    return db.connect()


def sanitize_prefs(raw: str | None) -> dict:
    """Accept user-supplied preference JSON, keeping only known numeric keys in sane ranges."""
    if not raw:
        return merge_prefs(None)
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        raise HTTPException(400, "prefs must be valid JSON")

    def clean(node, template):
        out = {}
        for key, default in template.items():
            if key not in (node or {}):
                continue
            val = node[key]
            if isinstance(default, dict):
                out[key] = clean(val, default) if isinstance(val, dict) else {}
            elif isinstance(val, (int, float)) and not isinstance(val, bool):
                out[key] = max(0.0, min(float(val), 1_000_000.0))
        return out

    return merge_prefs(clean(data, DEFAULT_PREFS))


def _data_version(conn) -> str:
    row = conn.execute("SELECT MAX(computed_at) AS t, COUNT(*) AS n FROM scores").fetchone()
    return f"{row['t']}:{row['n']}"


def cached_ranked(conn, vertical: str, prefs: dict) -> list[dict]:
    key = (vertical, json.dumps(prefs, sort_keys=True), _data_version(conn))
    now = time.monotonic()
    with _cache_lock:
        hit = _cache.get(key)
        if hit and now - hit[0] < CACHE_TTL:
            return hit[1]
    rows = market.ranked(conn, vertical, prefs)
    with _cache_lock:
        for k in [k for k, v in _cache.items() if now - v[0] > CACHE_TTL or k[0] == vertical and k != key]:
            _cache.pop(k, None)
        _cache[key] = (now, rows)
    return rows


def _public(d: dict) -> dict:
    keys = ("id", "source", "vertical", "sigma", "url", "title", "price", "fair", "discount", "z", "conf", "score",
            "base", "factors", "flags", "region", "district", "attrs", "first_seen", "posted_at",
            "age_days", "drop_pct", "views", "seller", "method", "group_key", "group_n",
            "first_price", "knn_fair", "extra")
    out = {k: d.get(k) for k in keys}
    out["description"] = (d.get("description") or "")[:600] or None
    return out


def _matches(d: dict, f: dict) -> bool:
    a = d["attrs"]
    if f["source"] and d["source"] != f["source"]:
        return False
    if f["region"] and norm_key(d.get("region")) != f["region"]:
        return False
    if f["price_min"] is not None and d["price"] < f["price_min"]:
        return False
    if f["price_max"] is not None and d["price"] > f["price_max"]:
        return False
    if d["conf"] < f["min_conf"]:
        return False
    flags = set(d["flags"])
    if "too_good" in flags and not f["include_too_good"]:
        return False
    if flags & FLAGGED_BAD and not f["include_flagged"]:
        return False
    if f["q"]:
        hay = " ".join(str(x or "") for x in (d["title"], a.get("make"), a.get("model_label"), a.get("model"),
                                              d.get("district"), d.get("region"), a.get("street"))).lower()
        if not all(tok in hay for tok in f["q"].lower().split()):
            return False
    if d["vertical"] == "car":
        if f["year_min"] and (a.get("year") or 0) < f["year_min"]:
            return False
        if f["year_max"] and (a.get("year") or 9999) > f["year_max"]:
            return False
        if f["km_max"] and (a.get("mileage_km") or 0) > f["km_max"]:
            return False
        if f["fuel"] and (a.get("fuel") or "petrol") not in f["fuel"]:
            return False
        if f["gear"] and (a.get("gearbox") or "?") != f["gear"]:
            return False
    else:
        if f["kind"] and d["vertical"] == "house" and (a.get("kind") or "house") != f["kind"]:
            return False
        if f["yield_min"] and (d["extra"].get("yield") or 0.0) * 100 < f["yield_min"]:
            return False
        if f["area_min"] and (a.get("area_m2") or 0) < f["area_min"]:
            return False
        if f["area_max"] and (a.get("area_m2") or 1e9) > f["area_max"]:
            return False
        if f["rooms_min"] and (a.get("rooms") or 0) < f["rooms_min"]:
            return False
        if f["rooms_max"] and (a.get("rooms") or 99) > f["rooms_max"]:
            return False
    return True


SORTS = {
    "score": lambda d: -d["score"],
    "discount": lambda d: -d["discount"],
    "price": lambda d: d["price"],
    "newest": lambda d: d["first_seen"] or "",
    "confidence": lambda d: -d["conf"],
    # Confidence-weighted and capped: 25% "yields" are data errors, not opportunities.
    "yield": lambda d: -(min(d["extra"].get("yield") or 0.0, 0.15) * d["conf"]),
}


@app.get("/api/health")
def health():
    return {"ok": True}


@app.get("/api/deals")
def deals(
    vertical: str = Query("car", pattern="^(car|flat|house)$"),
    prefs: str | None = None,
    source: str | None = Query(None, pattern="^(ss|pp)$"),
    region: str | None = None,
    q: str | None = Query(None, max_length=80),
    price_min: float | None = None, price_max: float | None = None,
    year_min: int | None = None, year_max: int | None = None,
    km_max: int | None = None, fuel: str | None = None, gear: str | None = Query(None, pattern="^(auto|manual)$"),
    area_min: float | None = None, area_max: float | None = None,
    rooms_min: int | None = None, rooms_max: int | None = None,
    kind: str | None = Query(None, pattern="^(house|cottage)$"),
    yield_min: float | None = Query(None, ge=0, le=100),
    min_conf: float = Query(0.0, ge=0, le=1),
    include_too_good: bool = False, include_flagged: bool = False,
    sort: str = Query("score", pattern="^(score|discount|price|newest|confidence|yield)$"),
    limit: int = Query(60, ge=1, le=300), offset: int = Query(0, ge=0),
):
    conn = _conn()
    try:
        rows = cached_ranked(conn, vertical, sanitize_prefs(prefs))
    finally:
        conn.close()
    f = {
        "source": source, "region": norm_key(region) if region else None, "q": q,
        "price_min": price_min, "price_max": price_max, "year_min": year_min, "year_max": year_max,
        "km_max": km_max, "fuel": set(fuel.split(",")) if fuel else None, "gear": gear,
        "area_min": area_min, "area_max": area_max, "rooms_min": rooms_min, "rooms_max": rooms_max,
        "kind": kind, "yield_min": yield_min, "min_conf": min_conf, "include_too_good": include_too_good, "include_flagged": include_flagged,
    }
    matched = [d for d in rows if _matches(d, f)]
    if sort != "score":
        matched.sort(key=SORTS[sort])
    # Highlight tiers are relative to the whole category (top 1% / top 5% of scores), because
    # house scores are inherently lower than car scores: low confidence, not worse deals.
    ordered = sorted((d["score"] for d in rows if d["score"] > 0), reverse=True)
    pick = lambda frac: ordered[min(len(ordered) - 1, int(len(ordered) * frac))] if ordered else 0.0
    return {"total": len(matched), "scored": len(rows), "offset": offset,
            "tiers": {"hot": pick(0.01), "good": pick(0.05)},
            "items": [_public(d) for d in matched[offset: offset + limit]]}


@app.get("/api/regions")
def regions(vertical: str = Query("car", pattern="^(car|flat|house)$")):
    conn = _conn()
    try:
        rows = conn.execute(
            """SELECT l.region AS region, COUNT(*) AS n FROM listings l JOIN scores s ON s.listing_id = l.id
               WHERE l.vertical=? AND l.active=1 AND l.region IS NOT NULL GROUP BY l.region
               ORDER BY n DESC LIMIT 60""", (vertical,)).fetchall()
    finally:
        conn.close()
    return [{"region": r["region"], "key": norm_key(r["region"]), "n": r["n"]} for r in rows if norm_key(r["region"])]


@app.get("/api/listing/{listing_id}")
def listing(listing_id: int, prefs: str | None = None):
    conn = _conn()
    try:
        row = conn.execute("SELECT vertical FROM listings WHERE id=?", (listing_id,)).fetchone()
        if not row:
            raise HTTPException(404, "unknown listing")
        ranked = cached_ranked(conn, row["vertical"], sanitize_prefs(prefs))
        item = next((d for d in ranked if d["id"] == listing_id), None)
        if not item:
            raise HTTPException(404, "listing is not scored")
        history = [dict(r) for r in conn.execute(
            "SELECT ts, price FROM price_history WHERE listing_id=? ORDER BY ts", (listing_id,))]
        comps = [{k: c[k] for k in ("id", "source", "title", "url", "price", "attrs")}
                 for c in market.comparables(conn, listing_id)]
    finally:
        conn.close()
    out = _public(item)
    out["description"] = (item.get("description") or "")[:1500] or None
    out["history"] = history
    out["comps"] = comps
    out["sigma"] = item.get("sigma")
    return out


@app.get("/api/status")
def status():
    conn = _conn()
    try:
        by = [dict(r) for r in conn.execute(
            """SELECT source, vertical, COUNT(*) AS n,
                      SUM(CASE WHEN EXISTS (SELECT 1 FROM scores s WHERE s.listing_id = l.id) THEN 1 ELSE 0 END) AS scored
               FROM listings l WHERE deal_type='sell' AND active=1 GROUP BY source, vertical""")]
        rent = conn.execute("SELECT COUNT(*) AS n FROM listings WHERE deal_type='rent' AND active=1").fetchone()["n"]
        last = conn.execute("SELECT MAX(computed_at) AS t FROM scores").fetchone()["t"]
        info = scheduler.status(conn)
    finally:
        conn.close()
    return {"counts": by, "rent_comps": rent, "scored_at": last, "now": db.utcnow(),
            "scheduler": info, "defaults": DEFAULT_PREFS, "alerts_enabled": bool(config.NTFY_URL),
            "can_trigger": bool(config.ADMIN_TOKEN)}


def _require_admin(token: str | None):
    if not config.ADMIN_TOKEN:
        raise HTTPException(403, "ADMIN_TOKEN is not set on the server")
    if token != config.ADMIN_TOKEN:
        raise HTTPException(401, "bad token")


@app.post("/api/run/{job}")
def run_job(job: str, x_admin_token: str | None = Header(None)):
    _require_admin(x_admin_token)
    if not scheduler.request(job):
        raise HTTPException(404, f"unknown job; choose one of {sorted(JOBS)}")
    return {"queued": job}


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")


app.mount("/static", StaticFiles(directory=STATIC), name="static")
