import json

import pytest
from fastapi.testclient import TestClient

from app import config, db, server
from app.scoring import market
from tests.test_scoring import make_cars


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "t.db")
    server._cache.clear()
    conn = db.connect()
    db.init(conn)
    rows, deal_ids = make_cars(n=260, n_deals=4)
    for r in rows:
        db.upsert_listing(conn, {
            "source": r["source"], "ext_id": str(r["id"]), "vertical": "car", "url": f"https://x/{r['id']}",
            "title": "Audi ad", "price": r["price"], "region": "riga", "attrs": r["attrs"]}, "2026-11-01T00:00:00Z")
    # a damaged one, to prove it is hidden by default
    db.upsert_listing(conn, {"source": "ss", "ext_id": "dmg", "vertical": "car", "url": "https://x/dmg",
                             "title": "Pēc avārijas, uz daļām", "price": rows[3]["price"] * 0.7, "attrs": rows[3]["attrs"]})
    conn.commit()
    market.compute_market(conn, ("car",))
    conn.close()
    return TestClient(server.app)


def test_health_and_index(client):
    assert client.get("/api/health").json() == {"ok": True}
    page = client.get("/")
    assert page.status_code == 200 and "Deal Radar" in page.text
    assert client.get("/static/app.js").status_code == 200
    assert client.get("/static/app.css").status_code == 200


def test_deals_are_ranked_and_shaped(client):
    body = client.get("/api/deals", params={"vertical": "car", "limit": 20}).json()
    assert body["total"] > 100 and len(body["items"]) == 20
    scores = [d["score"] for d in body["items"]]
    assert scores == sorted(scores, reverse=True)
    first = body["items"][0]
    for key in ("id", "url", "price", "fair", "discount", "conf", "score", "factors", "flags", "attrs", "sigma", "vertical"):
        assert key in first
    assert first["fair"] > first["price"] and first["vertical"] == "car"


def test_damaged_ads_are_hidden_unless_asked(client):
    ids = lambda **kw: {d["title"] for d in client.get("/api/deals", params={"vertical": "car", "limit": 300, **kw}).json()["items"]}
    assert "Pēc avārijas, uz daļām" not in ids()
    assert "Pēc avārijas, uz daļām" in ids(include_flagged="true", limit=300)


def test_filters(client):
    only_pp = client.get("/api/deals", params={"vertical": "car", "source": "pp", "limit": 300}).json()
    assert only_pp["total"] > 0 and all(d["source"] == "pp" for d in only_pp["items"])
    recent = client.get("/api/deals", params={"vertical": "car", "year_min": 2018, "limit": 300}).json()
    assert recent["total"] > 0 and all(d["attrs"]["year"] >= 2018 for d in recent["items"])
    diesel = client.get("/api/deals", params={"vertical": "car", "fuel": "diesel", "limit": 300}).json()
    assert diesel["total"] > 0 and all(d["attrs"]["fuel"] == "diesel" for d in diesel["items"])
    cheap = client.get("/api/deals", params={"vertical": "car", "price_max": 8000, "limit": 300}).json()
    assert all(d["price"] <= 8000 for d in cheap["items"])
    model = client.get("/api/deals", params={"vertical": "car", "q": "q5", "limit": 300}).json()
    assert model["total"] > 0 and all(d["attrs"]["model"] == "q5" for d in model["items"])
    assert client.get("/api/deals", params={"vertical": "car", "region": "nowhere"}).json()["total"] == 0


def test_sorting_and_paging(client):
    asc = client.get("/api/deals", params={"vertical": "car", "sort": "price", "limit": 15}).json()["items"]
    assert [d["price"] for d in asc] == sorted(d["price"] for d in asc)
    p1 = client.get("/api/deals", params={"vertical": "car", "limit": 10, "offset": 0}).json()["items"]
    p2 = client.get("/api/deals", params={"vertical": "car", "limit": 10, "offset": 10}).json()["items"]
    assert not {d["id"] for d in p1} & {d["id"] for d in p2}


def test_preferences_change_the_ranking(client):
    base = client.get("/api/deals", params={"vertical": "car", "limit": 300}).json()["items"]
    prefs = json.dumps({"car": {"fuel": {"diesel": 0.0}}})
    mine = client.get("/api/deals", params={"vertical": "car", "limit": 300, "prefs": prefs}).json()["items"]
    assert any(d["attrs"]["fuel"] == "diesel" for d in base[:30])
    assert all(d["score"] == 0 for d in mine if d["attrs"]["fuel"] == "diesel")


def test_bad_preferences_are_rejected_or_sanitised(client):
    assert client.get("/api/deals", params={"vertical": "car", "prefs": "{not json"}).status_code == 400
    wild = json.dumps({"car": {"fuel": {"diesel": 1e12, "rocket": 5}, "mileage": {"rate": "x"}}, "evil": 1})
    ok = client.get("/api/deals", params={"vertical": "car", "prefs": wild})
    assert ok.status_code == 200


def test_invalid_query_params(client):
    assert client.get("/api/deals", params={"vertical": "boat"}).status_code == 422
    assert client.get("/api/deals", params={"vertical": "car", "limit": 5000}).status_code == 422
    assert client.get("/api/deals", params={"vertical": "car", "source": "x"}).status_code == 422


def test_listing_detail(client):
    top = client.get("/api/deals", params={"vertical": "car", "limit": 1}).json()["items"][0]
    d = client.get(f"/api/listing/{top['id']}").json()
    assert d["id"] == top["id"] and d["history"] and "comps" in d and d["sigma"] > 0
    assert client.get("/api/listing/999999").status_code == 404


def test_status_and_regions(client):
    s = client.get("/api/status").json()
    assert any(c["vertical"] == "car" and c["scored"] > 100 for c in s["counts"])
    assert s["defaults"]["car"]["fuel"]["diesel"] == 0.9 and s["can_trigger"] is False
    regions = client.get("/api/regions", params={"vertical": "car"}).json()
    assert regions and regions[0]["key"] == "riga"


def test_triggering_crawls_requires_a_token(client, monkeypatch):
    assert client.post("/api/run/ss_cars").status_code == 403          # no token configured
    monkeypatch.setattr(config, "ADMIN_TOKEN", "s3cret")
    assert client.post("/api/run/ss_cars").status_code == 401           # missing / wrong token
    assert client.post("/api/run/ss_cars", headers={"X-Admin-Token": "nope"}).status_code == 401
    assert client.post("/api/run/nonsense", headers={"X-Admin-Token": "s3cret"}).status_code == 404
    assert client.post("/api/run/ss_cars", headers={"X-Admin-Token": "s3cret"}).json() == {"queued": "ss_cars"}
    server.scheduler.requested.discard("ss_cars")
