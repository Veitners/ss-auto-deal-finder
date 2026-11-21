import numpy as np
import pytest
from fastapi.testclient import TestClient

from app import config, db, server
from app.classify import house_kind
from app.scoring import market, models


@pytest.mark.parametrize("title", [
    "Mājīga vasarnīca Saulkrastos – vasaras atpūtai netālu no jūras.",
    "Pārdod vasarnīcu dārzkopības sabiedrībā",
    "Продается добротный 3-комнатный дачный дом с печным отоплением",
    "Garden house near the lake, 40 m2",
    "Dārza māja ar zemi, 6 ass.",
    "Продаётся садовый домик в садоводческом товариществе",
    "Summer cottage with sauna",
])
def test_cottages(title):
    assert house_kind(title) == "cottage"


@pytest.mark.parametrize("title", [
    "Pārdod divstāvu māju Rāmavā, piemērota divām ģimenēm",
    "Privātmāja ar skaistu dārzu",          # a garden is not a cottage
    "Zemes gabals ar dzīvojamo māju",
    "Продается бревенчатый дом ручной работы",
    "Elites ciematā tiek pārdota māja pirmajā līnijā",
    "Kotedža Mārupē", "Detached house with a garage", "",
])
def test_houses(title):
    assert house_kind(title) == "house"


def test_mixed_wording_follows_whichever_comes_first():
    assert house_kind("Vasarnīca ar dzīvojamo māju") == "cottage"
    assert house_kind("Dzīvojamā māja un vasarnīca") == "house"


def test_description_is_used_too():
    assert house_kind("Īpašums Saulkrastos", "Ērta vasarnīca pie jūras") == "cottage"
    assert house_kind(None, None) == "house"


def _houses(n=120, seed=11):
    rng = np.random.default_rng(seed)
    rows = []
    for i in range(n):
        cottage = i % 3 == 0
        area = float(rng.integers(25, 60) if cottage else rng.integers(90, 240))
        plot = float(rng.integers(500, 1200))
        value = (350 if cottage else 1000) * area + 15 * plot       # cottages are far cheaper per m2
        rows.append({"id": i + 1, "source": "ss", "price": float(value * np.exp(rng.normal(0, 0.08))),
                     "region": "riga-region", "district": "olaine" if i % 2 else "salaspils",
                     "title": "Pārdod vasarnīcu" if cottage else "Pārdod privātmāju", "description": None,
                     "attrs": {"area_m2": area, "plot_m2": plot, "floors": 1 if cottage else 2,
                               "kind": "cottage" if cottage else "house"}})
    return rows


def test_cottage_flag_removes_the_false_bargains():
    """Without the type, small cheap cottages look like bargains next to houses of the same plot."""
    rows = _houses()
    with_kind = models.score_houses(rows)
    stripped = [{**r, "attrs": {k: v for k, v in r["attrs"].items() if k != "kind"}} for r in rows]
    without = models.score_houses(stripped)

    def spread(results):
        d = [market.derive(r["price"], res)["discount"] for r, res in zip(rows, results) if res]
        return float(np.mean(np.abs(d)))
    assert spread(with_kind) < spread(without)


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "h.db")
    server._cache.clear()
    conn = db.connect()
    db.init(conn)
    for r in _houses():
        attrs = {k: v for k, v in r["attrs"].items() if k != "kind"}   # the crawler never sets it
        db.upsert_listing(conn, {"source": "ss", "ext_id": str(r["id"]), "vertical": "house",
                                 "url": f"https://x/{r['id']}", "title": r["title"], "price": r["price"],
                                 "region": r["region"], "district": r["district"], "attrs": attrs})
    conn.commit()
    market.compute_market(conn, ("house",))
    conn.close()
    return TestClient(server.app)


def test_kind_is_detected_stored_and_filterable(client):
    q = lambda **kw: client.get("/api/deals", params={"vertical": "house", "limit": 300, "min_conf": 0,
                                                       "include_too_good": "true", **kw}).json()
    everything, cottages, houses = q(), q(kind="cottage"), q(kind="house")
    assert cottages["total"] > 0 and houses["total"] > 0
    assert cottages["total"] + houses["total"] == everything["total"]
    assert all(d["attrs"]["kind"] == "cottage" and d["attrs"]["area_m2"] < 60 for d in cottages["items"])
    assert all(d["attrs"]["kind"] == "house" and d["attrs"]["area_m2"] >= 90 for d in houses["items"])
    assert client.get("/api/deals", params={"vertical": "house", "kind": "castle"}).status_code == 422
