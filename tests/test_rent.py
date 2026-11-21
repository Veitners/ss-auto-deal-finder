import numpy as np
import pytest
from fastapi.testclient import TestClient

from app import config, db, server
from app.scoring import market, rent
from tests.test_scoring import make_flats


def rental(i, district, rooms, area, rpm2, region="riga"):
    return {"id": i, "source": "ss", "price": rpm2 * area, "region": region, "district": district,
            "attrs": {"area_m2": float(area), "rooms": rooms}}


def sale(price, district, rooms, area, region="riga"):
    return {"id": 9999, "source": "ss", "price": float(price), "region": region, "district": district,
            "attrs": {"area_m2": float(area), "rooms": rooms}}


def test_estimate_uses_the_most_specific_comparables():
    rows = [rental(i, "centrs", 2, 50, 12.0) for i in range(8)] + \
           [rental(100 + i, "centrs", 3, 80, 8.0) for i in range(8)] + \
           [rental(200 + i, "purvciems", 2, 50, 9.0) for i in range(12)]
    idx = rent.build_index(rows)
    est = rent.estimate_rent(idx, sale(100_000, "centrs", 2, 55))
    assert est["rent"] == 660 and est["rent_level"] == "dist_band" and est["rent_n"] == 8
    assert est["yield"] == pytest.approx(660 * 12 / 100_000)


def test_estimate_falls_back_to_wider_sets_and_gives_up():
    idx = rent.build_index([rental(i, "centrs", 2, 50, 10.0) for i in range(12)])
    # unseen room count in a known district -> district level
    assert rent.estimate_rent(idx, sale(90_000, "centrs", 4, 90))["rent_level"] == "dist"
    # unknown district in the same city -> city + rooms level
    assert rent.estimate_rent(idx, sale(90_000, "mezaparks", 2, 50))["rent_level"] == "reg_band"
    # a different city entirely, and a thin sample: no estimate
    assert rent.estimate_rent(idx, sale(90_000, "x", 2, 50, region="valmiera")) is None
    thin = rent.build_index([rental(i, "centrs", 2, 50, 10.0) for i in range(3)])
    assert rent.estimate_rent(thin, sale(90_000, "centrs", 2, 50)) is None


def test_junk_rentals_are_ignored():
    junk = [rental(1, "centrs", 2, 50, 0.5), rental(2, "centrs", 2, 50, 200.0),   # EUR/m2 nonsense
            {**rental(3, "centrs", 2, 50, 10), "price": 20.0}, rental(4, "centrs", 2, 5, 10.0)]
    assert not any(rent.rent_ok(r) for r in junk)
    assert rent.rent_ok(rental(5, "centrs", 2, 50, 10.0))


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "r.db")
    server._cache.clear()
    conn = db.connect()
    db.init(conn)
    flats, _ = make_flats(n=300, n_deals=0)
    for r in flats:
        db.upsert_listing(conn, {"source": r["source"], "ext_id": f"s{r['id']}", "vertical": "flat",
                                 "url": "https://x", "title": "flat", "price": r["price"], "region": "riga",
                                 "district": r["district"], "attrs": r["attrs"]})
    rng = np.random.default_rng(4)
    for i in range(200):  # rentals at ~11 EUR/m2 in every district
        area = float(rng.integers(28, 100))
        db.upsert_listing(conn, {"source": "ss", "ext_id": f"r{i}", "vertical": "flat", "deal_type": "rent",
                                 "url": "https://x", "title": "rent", "price": float(area * rng.normal(11, 1)),
                                 "region": "riga", "district": ["centrs", "purvciems", "bolderaja"][i % 3],
                                 "attrs": {"area_m2": area, "rooms": int(np.clip(round(area / 28), 1, 5))}})
    conn.commit()
    market.compute_market(conn, ("flat",))
    conn.close()
    return TestClient(server.app)


def test_flats_get_a_rental_yield(client):
    body = client.get("/api/deals", params={"vertical": "flat", "limit": 300, "include_too_good": "true"}).json()
    with_yield = [d for d in body["items"] if d["extra"].get("yield")]
    assert len(with_yield) > 0.9 * len(body["items"])
    assert all(0.01 < d["extra"]["yield"] < 0.5 and d["extra"]["rent"] > 50 for d in with_yield)


def test_yield_filter_and_sort(client):
    q = lambda **kw: client.get("/api/deals", params={"vertical": "flat", "limit": 300, "include_too_good": "true", **kw}).json()
    everything = q()
    high = q(yield_min=7)
    assert 0 < high["total"] < everything["total"]
    assert all(d["extra"]["yield"] >= 0.07 for d in high["items"])
    key = lambda d: min(d["extra"].get("yield") or 0.0, 0.15) * d["conf"]   # capped and confidence-weighted
    ordered = [key(d) for d in q(sort="yield")["items"]]
    assert ordered == sorted(ordered, reverse=True)
    assert client.get("/api/deals", params={"vertical": "flat", "yield_min": 500}).status_code == 422


def test_tiers_are_relative_to_the_category(client):
    t = client.get("/api/deals", params={"vertical": "flat"}).json()["tiers"]
    assert t["hot"] >= t["good"] > 0
