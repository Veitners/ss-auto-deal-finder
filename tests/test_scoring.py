"""Scoring is tested on synthetic markets with a known price law and planted bargains."""
import json

import numpy as np
import pytest

from app import db
from app.scoring import fairvalue as fv
from app.scoring import market, models

DEAL_FACTOR = 0.70  # planted bargains are priced at 70% of fair value: the most credible kind (~30% off)


def make_cars(seed=7, n=420, n_deals=6, extra_outlier=False):
    rng = np.random.default_rng(seed)
    base = {"a4": 26_000, "a6": 38_000, "q5": 45_000, "tt": 30_000}
    ref = models._now_year_fraction()
    rows = []
    for i in range(n):
        model = rng.choice(list(base), p=[0.4, 0.3, 0.2, 0.1])
        year = int(rng.integers(2008, 2024))
        age = max(0.0, ref - year - 0.5)
        km = int(15_000 * max(age, 0.5) * rng.lognormal(0, 0.3))
        fuel = rng.choice(["diesel", "petrol"], p=[0.6, 0.4])
        gear = rng.choice(["auto", "manual"])
        fair = (base[model] * np.exp(-0.115 * age) * np.exp(-0.0035 * km / 1000)
                * (1.05 if fuel == "diesel" else 1.0) * (1.06 if gear == "auto" else 1.0))
        rows.append({"id": i + 1, "source": "pp" if i % 4 == 0 else "ss", "price": float(fair * np.exp(rng.normal(0, 0.07))),
                     "region": None, "district": None, "title": "t", "description": None,
                     "attrs": {"make": "audi", "model": model, "year": year, "mileage_km": km,
                               "fuel": fuel, "gearbox": gear, "engine_l": 2.0}})
    deals = set(rng.choice(n, n_deals, replace=False).tolist())
    for d in deals:
        rows[d]["price"] *= DEAL_FACTOR
    if extra_outlier:
        rows.append({**rows[0], "id": n + 1, "price": rows[0]["price"] * 10})
    return rows, {rows[d]["id"] for d in deals}


def make_flats(seed=3, n=360, n_deals=5):
    rng = np.random.default_rng(seed)
    districts = {"centrs": 0.25, "purvciems": 0.0, "bolderaja": -0.30}
    rows = []
    for i in range(n):
        d = rng.choice(list(districts))
        area = float(rng.integers(28, 110))
        rooms = int(np.clip(round(area / 28), 1, 5))
        floors = int(rng.integers(3, 10))
        floor = int(rng.integers(1, floors + 1))
        ppm2 = 1800 * np.exp(districts[d]) * (area / 50) ** -0.12 * (0.93 if floor == 1 else 1.0)
        rows.append({"id": i + 1, "source": "pp" if i % 3 == 0 else "ss",
                     "price": float(ppm2 * area * np.exp(rng.normal(0, 0.06))), "region": "riga",
                     "district": d, "title": "t", "description": None,
                     "attrs": {"area_m2": area, "rooms": rooms, "floor": floor, "floors": floors,
                               "series": "119" if i % 2 else "602"}})
    deals = set(rng.choice(n, n_deals, replace=False).tolist())
    for d in deals:
        rows[d]["price"] *= DEAL_FACTOR
    return rows, {rows[d]["id"] for d in deals}


def discounts(rows, results):
    return np.array([market.derive(r["price"], res)["discount"] if res else np.nan
                     for r, res in zip(rows, results)])


def top_ids(rows, results, k):
    scored = [(market.derive(r["price"], res)["base"], r["id"]) for r, res in zip(rows, results) if res]
    return {i for _, i in sorted(scored, reverse=True)[:k]}


# --------------------------------------------------------------------- the maths

def test_loo_regression_recovers_a_line():
    rng = np.random.default_rng(0)
    x = rng.uniform(0, 10, 200)
    y = 5 + 0.3 * x + rng.normal(0, 0.05, 200)
    X = np.column_stack([np.ones(200), x])
    fit = fv.robust_ridge_loo(X, y, np.array([0.0, 0.1]))
    assert fit["beta"][1] == pytest.approx(0.3, abs=0.02)
    assert fit["sigma"] == pytest.approx(0.05, abs=0.03)
    assert np.abs(fit["loo_pred"] - y).mean() < 0.12


def test_robust_fit_shrugs_off_outliers():
    rng = np.random.default_rng(1)
    x = rng.uniform(0, 10, 200)
    y = 5 + 0.3 * x + rng.normal(0, 0.05, 200)
    y[:10] -= 2.0  # ten crazy cheap listings
    X = np.column_stack([np.ones(200), x])
    fit = fv.robust_ridge_loo(X, y, np.array([0.0, 0.1]))
    assert fit["beta"][1] == pytest.approx(0.3, abs=0.04)


def test_knn_follows_the_data():
    rng = np.random.default_rng(2)
    x = np.sort(rng.uniform(0, 10, 150))
    logp = 3 + 0.2 * x
    est, idx = fv.knn_loo(x[:, None], logp, k=6)
    assert np.nanmax(np.abs(est - logp)) < 0.1
    assert not np.any(idx == np.arange(150)[:, None])  # nobody is their own neighbour


def test_knn_respects_groups_and_needs_neighbours():
    num = np.random.default_rng(3).normal(size=(10, 2))
    groups = np.array([0] * 8 + [1] * 2)
    est, _ = fv.knn_loo(num, np.zeros(10), k=5, groups=groups)
    assert np.isnan(est[8]) and np.isnan(est[9]) and not np.isnan(est[0])


def test_confidence_behaviour():
    assert fv.confidence(200, 0.07, 1.0) > fv.confidence(10, 0.07, 1.0)
    assert fv.confidence(200, 0.07, 1.0) > fv.confidence(200, 0.4, 1.0)
    assert fv.confidence(200, 0.07, 1.0) > fv.confidence(200, 0.07, 0.0)
    assert 0.05 <= fv.confidence(1, 5, 0) <= 1


# ------------------------------------------------------------------------ cars

def test_cars_market_is_unbiased_and_finds_planted_deals():
    rows, deal_ids = make_cars()
    results = models.score_cars(rows)
    assert sum(r is not None for r in results) >= 0.95 * len(rows)
    disc = discounts(rows, results)

    normal = np.array([r["id"] not in deal_ids for r in rows])
    assert abs(np.nanmedian(disc[normal])) < 0.02          # no systematic bias
    assert np.nanmean(np.abs(disc[normal]) > 0.25) < 0.03  # honest listings are not flagged

    deal_disc = disc[~normal]
    assert np.nanmin(deal_disc) > 0.15                     # every planted deal is clearly cheap
    # They rank in the top 10% of the whole market. (Not all in the top 10: a bargain in a thin
    # model group rightly earns less credit than one in a deep market, that is the confidence weight.)
    top_tenth = top_ids(rows, results, int(0.10 * len(rows)))
    assert len(deal_ids & top_tenth) >= 5


def test_cars_sigma_matches_true_noise():
    rows, _ = make_cars()
    results = models.score_cars(rows)
    sigmas = [r["sigma"] for r in results if r]
    assert 0.05 < np.median(sigmas) < 0.12  # true noise is 0.07


def test_cars_mileage_is_priced_in():
    """Two identical cars, one with double the mileage: the model must not call it a bargain."""
    rows, _ = make_cars(n_deals=0)
    ref = models._now_year_fraction()
    year = int(ref - 6.5)
    probe_hi = {**rows[0], "id": 9001, "price": 0.0, "attrs": {**rows[0]["attrs"], "model": "a4", "year": year,
                "mileage_km": 200_000, "fuel": "diesel", "gearbox": "auto"}}
    probe_lo = {**probe_hi, "id": 9002, "attrs": {**probe_hi["attrs"], "mileage_km": 70_000}}
    # price each at its own market value (same law as the generator)
    def law(a):
        age = max(0.0, ref - a["year"] - 0.5)
        return 26_000 * np.exp(-0.115 * age) * np.exp(-0.0035 * a["mileage_km"] / 1000) * 1.05 * 1.06
    probe_hi["price"], probe_lo["price"] = float(law(probe_hi["attrs"])), float(law(probe_lo["attrs"]))
    results = models.score_cars(rows + [probe_hi, probe_lo])
    d_hi = market.derive(probe_hi["price"], results[-2])["discount"]
    d_lo = market.derive(probe_lo["price"], results[-1])["discount"]
    assert abs(d_hi) < 0.12 and abs(d_lo) < 0.12


def test_plausible_discount_curve_peaks_then_falls():
    f = market.plausible_discount
    assert f(-0.2) == 0.0 and f(0.0) == 0.0
    assert f(0.10) == 0.10 and f(0.30) == 0.30                  # counts in full up to the peak
    assert f(0.30) > f(0.40) > f(0.45) > f(0.55)                # then more is *less* credible
    assert f(0.95) == 0.05                                      # never quite zero
    assert market.plausible_discount(0.44) < market.plausible_discount(0.28)


def test_a_suspiciously_deep_discount_does_not_outrank_a_credible_one():
    res = {"fair": 10_000.0, "sigma": 0.15, "conf": 0.6}
    credible = market.derive(7_200, res)["base"]     # 28% below fair
    suspicious = market.derive(5_600, res)["base"]   # 44% below fair
    assert credible > 1.5 * suspicious


def test_flat_floor_above_building_height_is_repaired():
    rows, _ = make_flats(n=40, n_deals=0)
    rows[0]["attrs"].update({"floor": 24, "floors": 4})
    _design, ctx = models._flat_design(rows, "riga")
    assert ctx["floors"][0] == 24
    assert all(r is None or r["fair"] > 0 for r in models.score_flats(rows))


def test_old_cars_are_noisier_so_the_same_discount_earns_less_credit():
    rng = np.random.default_rng(21)
    ref = models._now_year_fraction()
    rows = []
    for i in range(600):
        year = int(rng.integers(2005, 2025))
        age = max(0.0, ref - year - 0.5)
        noise = 0.05 if age < 4 else 0.30 if age > 12 else 0.15  # condition matters more as cars age
        km = int(15_000 * max(age, 0.5))
        fair = 30_000 * np.exp(-0.11 * age) * np.exp(-0.003 * km / 1000)
        rows.append({"id": i + 1, "source": "ss", "price": float(fair * np.exp(rng.normal(0, noise))),
                     "region": None, "district": None, "title": "", "description": None,
                     "attrs": {"make": "audi", "model": "a4", "year": year, "mileage_km": km,
                               "fuel": "diesel", "gearbox": "auto", "engine_l": 2.0}})
    new = next(r for r in rows if ref - r["attrs"]["year"] - 0.5 < 3)
    old = next(r for r in rows if ref - r["attrs"]["year"] - 0.5 > 15)
    new["price"] *= 0.65
    old["price"] *= 0.65
    results = models.score_cars(rows)
    r_new, r_old = results[rows.index(new)], results[rows.index(old)]
    assert r_old["sigma"] > 2 * r_new["sigma"]
    d_new, d_old = market.derive(new["price"], r_new), market.derive(old["price"], r_old)
    assert d_new["z"] > d_old["z"] and d_new["base"] > 1.5 * d_old["base"]


def test_an_outlier_does_not_move_other_fair_values():
    clean, _ = make_cars(n_deals=0)
    dirty, _ = make_cars(n_deals=0, extra_outlier=True)
    fair_clean = np.array([r["fair"] for r in models.score_cars(clean) if r])
    results_dirty = models.score_cars(dirty)
    fair_dirty = np.array([r["fair"] for r in results_dirty[:len(clean)] if r])
    assert np.median(np.abs(fair_dirty / fair_clean - 1)) < 0.01


def test_invalid_and_tiny_groups_are_skipped():
    rows = [{"id": 1, "source": "ss", "price": 100.0, "region": None, "district": None, "title": "",
             "description": None, "attrs": {"make": "lada", "model": "x", "year": 1999}}]
    assert models.score_cars(rows) == [None]
    assert models.score_cars([]) == []


# ------------------------------------------------------------------------ flats

def test_flats_find_planted_deals_and_respect_districts():
    rows, deal_ids = make_flats()
    results = models.score_flats(rows)
    disc = discounts(rows, results)
    normal = np.array([r["id"] not in deal_ids for r in rows])
    assert abs(np.nanmedian(disc[normal])) < 0.02
    assert np.nanmin(disc[~normal]) > 0.12
    assert len(deal_ids & top_ids(rows, results, 10)) >= 4


def test_cheap_district_is_not_a_bargain():
    """A flat priced like its (cheap) district must not look like a deal against the city."""
    rows, _ = make_flats(n_deals=0)
    disc = discounts(rows, models.score_flats(rows))
    for district in ("centrs", "bolderaja"):
        d = np.array([x for x, r in zip(disc, rows) if r["district"] == district])
        assert abs(np.nanmedian(d)) < 0.03


def test_flat_junk_prices_are_excluded():
    base = make_flats(n_deals=0)[0]
    junk = {**base[0], "id": 777, "price": 1500.0}  # a EUR 1,500 "flat": a typo, not a deal
    results = models.score_flats(base + [junk])
    assert results[-1] is None


# ----------------------------------------------------------------------- houses

def test_houses_score_with_plot_size():
    rng = np.random.default_rng(5)
    rows = []
    for i in range(200):
        area, plot = float(rng.integers(70, 260)), float(rng.integers(400, 3000))
        price = 900 * area * np.exp(rng.normal(0, 0.1)) + 12 * plot
        rows.append({"id": i + 1, "source": "ss", "price": float(price), "region": "riga-region",
                     "district": "olaine" if i % 2 else "salaspils", "title": "", "description": None,
                     "attrs": {"area_m2": area, "plot_m2": plot, "floors": 2}})
    rows[10]["price"] *= DEAL_FACTOR
    results = models.score_houses(rows)
    assert sum(r is not None for r in results) >= 190
    disc = discounts(rows, results)
    assert disc[10] > 0.12 and abs(np.nanmedian(disc)) < 0.03


# ------------------------------------------------------------- end-to-end with DB

def test_compute_market_and_ranking_end_to_end(conn):
    rows, deal_ids = make_cars(n=300, n_deals=5)
    for r in rows:
        db.upsert_listing(conn, {
            "source": r["source"], "ext_id": str(r["id"]), "vertical": "car", "url": f"https://x/{r['id']}",
            "title": "plain ad", "price": r["price"], "attrs": r["attrs"]}, "2026-11-01T00:00:00Z")
    conn.commit()
    counts = market.compute_market(conn, ("car",))
    assert counts["car"] >= 280

    ranked = market.ranked(conn, "car", market.P.merge_prefs(None))
    assert ranked == sorted(ranked, key=lambda d: d["score"], reverse=True)
    top = {int(d["ext_id"]) for d in ranked[:12]}
    assert len(top & deal_ids) >= 4
    best = ranked[0]
    assert best["discount"] > 0.25 and 0 < best["conf"] <= 1 and best["fair"] > best["price"]
    assert [n for n, _ in best["factors"]][0] == "fuel"

    # changing the preferences re-ranks without touching the market model
    petrol_only = market.P.merge_prefs({"car": {"fuel": {"diesel": 0.0}}})
    rr = market.ranked(conn, "car", petrol_only)
    assert all(d["score"] == 0 for d in rr if d["attrs"]["fuel"] == "diesel")
    assert rr[0]["attrs"]["fuel"] == "petrol"


def test_too_good_to_be_true_is_flagged_not_celebrated(conn):
    rows, _ = make_cars(n=200, n_deals=0)
    for r in rows:
        db.upsert_listing(conn, {"source": "ss", "ext_id": str(r["id"]), "vertical": "car",
                                 "url": "https://x", "title": "ad", "price": r["price"], "attrs": r["attrs"]})
    victim = rows[5]
    db.upsert_listing(conn, {"source": "ss", "ext_id": str(victim["id"]), "vertical": "car", "url": "https://x",
                             "title": "ad", "price": victim["price"] * 0.3, "attrs": victim["attrs"]})
    conn.commit()
    market.compute_market(conn, ("car",))
    d = next(x for x in market.ranked(conn, "car", market.P.merge_prefs(None)) if x["ext_id"] == str(victim["id"]))
    assert "too_good" in d["flags"] and d["discount"] > 0.45


def test_damaged_wording_sinks_a_cheap_car(conn):
    rows, _ = make_cars(n=200, n_deals=0)
    for r in rows:
        db.upsert_listing(conn, {"source": "ss", "ext_id": str(r["id"]), "vertical": "car",
                                 "url": "https://x", "title": "ad", "price": r["price"], "attrs": r["attrs"]})
    cheap = rows[7]
    for ext, text in (("a", "Pēc avārijas, uz daļām"), ("b", "Bez avārijām, servisa grāmatiņa")):
        db.upsert_listing(conn, {"source": "ss", "ext_id": ext, "vertical": "car", "url": "https://x/" + ext,
                                 "title": text, "price": cheap["price"] * 0.7, "attrs": cheap["attrs"]})
    conn.commit()
    market.compute_market(conn, ("car",))
    ranked = {d["ext_id"]: d for d in market.ranked(conn, "car", market.P.merge_prefs(None))}
    assert ranked["b"]["score"] > 3 * ranked["a"]["score"]
    assert "damaged" in ranked["a"]["flags"] and "damaged" not in ranked["b"]["flags"]
