"""Fair-value models for cars, flats and houses.

Every function takes plain dict rows ({"id", "source", "price", "region", "district", "attrs"})
and returns one result dict per input row (or None where no estimate is possible):

    fair, sigma, conf, method, group_key, group_n, knn_fair, comps (neighbour row indices)

Prices are compared in log space, so "20% below fair" means the same thing for a
EUR 3,000 car and a EUR 30,000 one.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime, timezone

import numpy as np

from ..norm import norm_key
from . import fairvalue as fv

MIN_GROUP = 8  # fewer comparable listings than this: no estimate at all


class Design:
    """Builds a regression design matrix together with a per-column ridge penalty."""

    def __init__(self, n: int):
        self.n = n
        self.cols = [np.ones(n)]
        self.pen = [0.0]

    def numeric(self, values, pen: float = 1.0):
        v = np.asarray(values, dtype=float)
        std = v.std()
        if std < 1e-9:
            return
        self.cols.append((v - v.mean()) / std)
        self.pen.append(pen)

    def dummy(self, mask, pen: float = 2.0):
        m = np.asarray(mask, dtype=float)
        if m.sum() == 0 or m.sum() == self.n:
            return
        self.cols.append(m)
        self.pen.append(pen)

    def categorical(self, values, pen: float = 4.0, min_count: int = 3):
        values = np.asarray(values, dtype=object)
        counts = Counter(values.tolist())
        labelled = np.array([v if counts[v] >= min_count else "~other" for v in values], dtype=object)
        for level in sorted(set(labelled.tolist())):
            self.dummy(labelled == level, pen)

    def matrix(self):
        return np.column_stack(self.cols), np.array(self.pen)


def _codes(values) -> np.ndarray:
    lookup: dict = {}
    return np.array([lookup.setdefault(v, len(lookup)) for v in values], dtype=int)


def _zscore(v: np.ndarray) -> np.ndarray:
    v = np.asarray(v, dtype=float)
    s = v.std()
    return (v - v.mean()) / s if s > 1e-9 else np.zeros_like(v)


def _median_fill(values: list, fallback: float):
    """Replace None with the median of the known values; also return the missing mask."""
    known = [v for v in values if v is not None]
    med = float(np.median(known)) if known else fallback
    missing = np.array([v is None for v in values])
    return np.array([med if v is None else float(v) for v in values], dtype=float), missing


def _finish(log_price, reg_pred, sigma, knn_est, knn_idx, n_group, method, group_key, idx_map):
    """Blend the two estimates and package per-row results."""
    log_fair = fv.blend(reg_pred, knn_est)
    out = []
    for i in range(len(log_price)):
        agree = fv.agreement(reg_pred[i], knn_est[i])
        comps = [int(idx_map[j]) for j in knn_idx[i] if j >= 0][:6]
        out.append({
            "fair": float(np.exp(log_fair[i])),
            "sigma": sigma,
            "conf": fv.confidence(n_group, sigma, agree),
            "method": method,
            "group_key": group_key,
            "group_n": n_group,
            "knn_fair": None if np.isnan(knn_est[i]) else float(np.exp(knn_est[i])),
            "comps": comps,
        })
    return out


# --------------------------------------------------------------------------- cars

def _now_year_fraction() -> float:
    now = datetime.now(timezone.utc)
    return now.year + (now.month - 1) / 12.0


def car_valid(row: dict) -> bool:
    a = row["attrs"]
    return bool(a.get("make") and a.get("year") and 300 <= row["price"] <= 400_000
                and 1950 <= a["year"] <= datetime.now(timezone.utc).year + 1)


def _car_arrays(rows: list[dict]) -> dict:
    ref = _now_year_fraction()
    attrs = [r["attrs"] for r in rows]
    age = np.array([max(0.0, ref - a["year"] - 0.5) for a in attrs])
    mil, mil_missing = _median_fill([a.get("mileage_km") for a in attrs], 0.0)
    # Missing mileage: assume the typical 14,000 km a year rather than a group median.
    mil = np.where(mil_missing, 14_000 * np.maximum(age, 0.5), mil)
    eng, eng_missing = _median_fill([a.get("engine_l") for a in attrs], 1.8)
    fuel = np.array([a.get("fuel") or "petrol" for a in attrs])
    gear = np.array([a.get("gearbox") or "?" for a in attrs])
    return {
        "age": age, "mil": mil, "mil_missing": mil_missing, "eng": eng, "eng_missing": eng_missing,
        "fuel": fuel, "gear": gear,
        "src_pp": np.array([r["source"] == "pp" for r in rows]),
        "log_price": np.log(np.array([r["price"] for r in rows], dtype=float)),
        "model": np.array([norm_key(a.get("model")) or "?" for a in attrs], dtype=object),
    }


def _car_design(ar: dict, with_model: bool) -> Design:
    d = Design(len(ar["age"]))
    d.numeric(ar["age"])
    d.numeric(ar["age"] ** 2)
    d.numeric(np.log1p(ar["mil"] / 1000.0))
    d.dummy(ar["mil_missing"])
    for f in ("diesel", "hybrid", "electric", "gas"):
        d.dummy(ar["fuel"] == f)
    d.dummy(ar["gear"] == "auto")
    d.dummy(ar["gear"] == "?")
    d.numeric(ar["eng"])
    d.dummy(ar["eng_missing"])
    d.dummy(ar["src_pp"])
    if with_model:
        d.categorical(ar["model"], pen=4.0, min_count=3)
    return d


AGE_EDGES = (3, 6, 10, 15)  # new / young / middle-aged / old / very old


def local_sigma(age: np.ndarray, resid: np.ndarray, base_sigma: float, shrink: float = 12.0) -> np.ndarray:
    """Per-listing noise level: asking prices of old cars scatter far more than nearly-new ones.

    A 30% discount on a 2-year-old car is a strong signal; the same discount on a 20-year-old
    one is mostly condition scatter. Each age band gets its own robust spread, shrunk toward the
    group-wide value when the band is thin.
    """
    band = np.digitize(age, AGE_EDGES)
    out = np.full(len(age), base_sigma)
    for b in np.unique(band):
        m = band == b
        r = resid[m]
        s = max(fv.MAD_TO_SIGMA * np.median(np.abs(r - np.median(r))), 0.05)
        out[m] = np.sqrt((m.sum() * s * s + shrink * base_sigma ** 2) / (m.sum() + shrink))
    return out


def score_cars(rows: list[dict]) -> list[dict | None]:
    results: list[dict | None] = [None] * len(rows)
    by_make: dict[str, list[int]] = defaultdict(list)
    for i, r in enumerate(rows):
        if car_valid(r):
            by_make[norm_key(r["attrs"]["make"])].append(i)

    for make, idxs in by_make.items():
        if len(idxs) < MIN_GROUP:
            continue
        sub = [rows[i] for i in idxs]
        ar = _car_arrays(sub)
        X, pen = _car_design(ar, with_model=True).matrix()
        make_fit = fv.robust_ridge_loo(X, ar["log_price"], pen)

        # Nearest comparables within the same model.
        num = np.column_stack([_zscore(ar["age"]), _zscore(np.log1p(ar["mil"] / 1000)), _zscore(ar["eng"])])
        cats = [(_codes(ar["fuel"]), 1.5), (_codes(ar["gear"]), 0.4)]
        knn_est, knn_idx = fv.knn_loo(num, ar["log_price"], k=8, cats=cats, groups=_codes(ar["model"]))

        reg_pred = make_fit["loo_pred"].copy()
        sigma = local_sigma(ar["age"], ar["log_price"] - make_fit["loo_pred"], make_fit["sigma"])
        method = np.array(["make"] * len(sub), dtype=object)
        n_group = np.full(len(sub), len(sub))
        group_key = np.array([f"{make}"] * len(sub), dtype=object)

        # Models with plenty of listings get their own slopes if that fits them better.
        by_model: dict[str, list[int]] = defaultdict(list)
        for j, m in enumerate(ar["model"]):
            by_model[m].append(j)
        for model, js in by_model.items():
            if len(js) < 40 or model == "?":
                continue
            js = np.array(js)
            sub_ar = {k: v[js] for k, v in ar.items()}
            Xm, penm = _car_design(sub_ar, with_model=False).matrix()
            fit = fv.robust_ridge_loo(Xm, sub_ar["log_price"], penm)
            resid_make = ar["log_price"][js] - make_fit["loo_pred"][js]
            sigma_make_here = max(fv.MAD_TO_SIGMA * np.median(np.abs(resid_make - np.median(resid_make))), 0.04)
            if fit["sigma"] < 0.95 * sigma_make_here:
                reg_pred[js] = fit["loo_pred"]
                sigma[js] = local_sigma(sub_ar["age"], sub_ar["log_price"] - fit["loo_pred"], fit["sigma"])
                method[js] = "model"
            else:
                sigma[js] = local_sigma(sub_ar["age"], resid_make, sigma_make_here)
            n_group[js] = len(js)
            group_key[js] = f"{make}/{model}"

        log_fair = fv.blend(reg_pred, knn_est)
        for j, i in enumerate(idxs):
            agree = fv.agreement(reg_pred[j], knn_est[j])
            results[i] = {
                "fair": float(np.exp(log_fair[j])),
                "sigma": float(sigma[j]),
                "conf": fv.confidence(int(n_group[j]), float(sigma[j]), agree),
                "method": str(method[j]),
                "group_key": str(group_key[j]),
                "group_n": int(n_group[j]),
                "knn_fair": None if np.isnan(knn_est[j]) else float(np.exp(knn_est[j])),
                "comps": [idxs[c] for c in knn_idx[j] if c >= 0][:6],
            }
    return results


# ------------------------------------------------------------------------- estate

def _segment(region: str | None) -> str:
    key = norm_key(region)
    return key if key in ("riga", "jurmala") else "other"


def flat_valid(row: dict) -> bool:
    a = row["attrs"]
    area = a.get("area_m2")
    if not area or not (10 <= area <= 400) or not (1_000 <= row["price"] <= 3_000_000):
        return False
    return 100 <= row["price"] / area <= 12_000


def house_valid(row: dict) -> bool:
    a = row["attrs"]
    area = a.get("area_m2")
    if not area or not (15 <= area <= 1500) or not (3_000 <= row["price"] <= 5_000_000):
        return False
    return 30 <= row["price"] / area <= 15_000


def _flat_design(rows: list[dict], segment: str) -> tuple[Design, dict]:
    attrs = [r["attrs"] for r in rows]
    n = len(rows)
    area = np.array([a["area_m2"] for a in attrs], dtype=float)
    rooms, rooms_missing = _median_fill([a.get("rooms") for a in attrs], 2.0)
    floors, floors_missing = _median_fill([a.get("floors") for a in attrs], 5.0)
    floor = np.array([a.get("floor") if a.get("floor") is not None else -1 for a in attrs], dtype=float)
    floors = np.maximum(floors, floor)  # sellers do write "floor 24 of 4"; a building has at least as many floors
    last = (floor >= 0) & (floor == floors) & (floors > 1)
    first = floor == 1
    series = np.array([norm_key(a.get("series")) or "?" for a in attrs], dtype=object)
    house_type = np.array([norm_key(a.get("house_type")) or "?" for a in attrs], dtype=object)
    district = np.array([norm_key(r.get("district")) or "?" for r in rows], dtype=object)
    region = np.array([norm_key(r.get("region")) or "?" for r in rows], dtype=object)

    d = Design(n)
    d.numeric(np.log(area))
    d.numeric(np.log(area) ** 2)
    d.numeric(rooms)
    d.dummy(rooms_missing)
    d.numeric(floors)
    d.dummy(floors_missing)
    d.dummy(first)
    d.dummy(last)
    d.categorical(series, pen=4.0, min_count=6)
    d.categorical(house_type, pen=4.0, min_count=6)
    d.categorical(district, pen=4.0, min_count=2)
    if segment == "other":
        d.categorical(region, pen=3.0, min_count=2)
    d.dummy(np.array([r["source"] == "pp" for r in rows]))
    ctx = {"area": area, "rooms": rooms, "floor": floor, "floors": floors, "district": district,
           "series": series, "region": region}
    return d, ctx


def score_flats(rows: list[dict]) -> list[dict | None]:
    results: list[dict | None] = [None] * len(rows)
    by_seg: dict[str, list[int]] = defaultdict(list)
    for i, r in enumerate(rows):
        if flat_valid(r):
            by_seg[_segment(r.get("region"))].append(i)

    for seg, idxs in by_seg.items():
        if len(idxs) < MIN_GROUP:
            continue
        sub = [rows[i] for i in idxs]
        design, ctx = _flat_design(sub, seg)
        X, pen = design.matrix()
        logp = np.log(np.array([r["price"] for r in sub], dtype=float))
        fit = fv.robust_ridge_loo(X, logp, pen, sigma_floor=0.06)

        num = np.column_stack([_zscore(np.log(ctx["area"])), _zscore(ctx["rooms"]),
                               _zscore(np.where(ctx["floor"] < 0, 0.5, ctx["floor"] / np.maximum(ctx["floors"], 1)))])
        cats = [(_codes(ctx["district"]), 1.6), (_codes(ctx["series"]), 0.5)]
        knn_est, knn_idx = fv.knn_loo(num, logp, k=10, cats=cats, groups=_codes(ctx["region"]))

        for j, res in enumerate(_finish(logp, fit["loo_pred"], fit["sigma"], knn_est, knn_idx,
                                        len(sub), "flat-" + seg, "flat/" + seg, np.array(idxs))):
            results[idxs[j]] = res
    return results


def score_houses(rows: list[dict]) -> list[dict | None]:
    results: list[dict | None] = [None] * len(rows)
    idxs = [i for i, r in enumerate(rows) if house_valid(r)]
    if len(idxs) < MIN_GROUP:
        return results
    sub = [rows[i] for i in idxs]
    attrs = [r["attrs"] for r in sub]
    n = len(sub)
    area = np.array([a["area_m2"] for a in attrs], dtype=float)
    plot, plot_missing = _median_fill([a.get("plot_m2") for a in attrs], 800.0)
    floors, floors_missing = _median_fill([a.get("floors") for a in attrs], 2.0)
    region = np.array([norm_key(r.get("region")) or "?" for r in sub], dtype=object)
    district = np.array([norm_key(r.get("district")) or "?" for r in sub], dtype=object)

    d = Design(n)
    d.numeric(np.log(area))
    d.numeric(np.log(area) ** 2)
    d.numeric(np.log(plot + 100.0))
    d.dummy(plot_missing)
    d.numeric(floors)
    d.dummy(floors_missing)
    d.categorical(region, pen=3.0, min_count=2)
    d.categorical(district, pen=4.0, min_count=2)
    cottage = np.array([a.get("kind") == "cottage" for a in attrs])
    d.dummy(cottage)  # a summer cottage and a year-round house of the same size are not comparable
    d.dummy(np.array([r["source"] == "pp" for r in sub]))
    X, pen = d.matrix()
    logp = np.log(np.array([r["price"] for r in sub], dtype=float))
    fit = fv.robust_ridge_loo(X, logp, pen, sigma_floor=0.10)

    num = np.column_stack([_zscore(np.log(area)), _zscore(np.log(plot + 100.0))])
    cats = [(_codes(district), 1.2), (_codes(cottage), 2.0)]
    knn_est, knn_idx = fv.knn_loo(num, logp, k=10, cats=cats, groups=_codes(region))
    for j, res in enumerate(_finish(logp, fit["loo_pred"], fit["sigma"], knn_est, knn_idx,
                                    n, "house", "house/all", np.array(idxs))):
        results[idxs[j]] = res
    return results


SCORERS = {"car": score_cars, "flat": score_flats, "house": score_houses}
