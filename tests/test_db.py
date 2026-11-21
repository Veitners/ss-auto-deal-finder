from app import db


def rec(ext_id="1", price=1000.0, **kw):
    base = {"source": "ss", "ext_id": ext_id, "vertical": "car", "url": "https://x/1",
            "title": "t", "price": price, "attrs": {"year": 2015, "ta_date": "2027-01"}}
    base.update(kw)
    return base


def test_new_then_unchanged_then_price_change(conn):
    r1 = db.upsert_listing(conn, rec(), "2026-01-01T00:00:00Z")
    assert r1.is_new and not r1.price_changed
    r2 = db.upsert_listing(conn, rec(), "2026-01-02T00:00:00Z")
    assert not r2.is_new and not r2.price_changed
    r3 = db.upsert_listing(conn, rec(price=900.0), "2026-01-03T00:00:00Z")
    assert r3.price_changed and r3.old_price == 1000.0
    history = conn.execute("SELECT price FROM price_history ORDER BY ts").fetchall()
    assert [h["price"] for h in history] == [1000.0, 900.0]
    row = conn.execute("SELECT * FROM listings").fetchone()
    assert row["price"] == 900.0 and row["first_seen"] == "2026-01-01T00:00:00Z"
    assert row["last_price_change"] == "2026-01-03T00:00:00Z"


def test_attrs_merge_keeps_enriched_facts(conn):
    db.upsert_listing(conn, rec(), "2026-01-01T00:00:00Z")
    # a later list-page refresh has no ta_date: the enriched value must survive
    db.upsert_listing(conn, rec(attrs={"year": 2015}), "2026-01-02T00:00:00Z")
    import json
    attrs = json.loads(conn.execute("SELECT attrs FROM listings").fetchone()["attrs"])
    assert attrs["ta_date"] == "2027-01"


def test_ageing_out_needs_two_complete_crawls(conn):
    db.upsert_listing(conn, rec("a"), "2026-01-01T00:00:00Z")
    db.upsert_listing(conn, rec("b"), "2026-01-01T00:00:00Z")
    # crawl 2 sees only "a"
    db.upsert_listing(conn, rec("a"), "2026-01-02T00:00:00Z")
    assert db.finish_scope(conn, "ss", "car", "sell", "2026-01-02T00:00:00Z", 2) == 0
    active = lambda: {r["ext_id"] for r in conn.execute("SELECT ext_id FROM listings WHERE active=1")}
    assert active() == {"a", "b"}
    # crawl 3 again sees only "a": now "b" has missed twice
    db.upsert_listing(conn, rec("a"), "2026-01-03T00:00:00Z")
    assert db.finish_scope(conn, "ss", "car", "sell", "2026-01-03T00:00:00Z", 2) == 1
    assert active() == {"a"}
    # and it comes back to life if it reappears
    db.upsert_listing(conn, rec("b"), "2026-01-04T00:00:00Z")
    assert active() == {"a", "b"}


def test_settings_roundtrip(conn):
    assert db.get_setting(conn, "x", 5) == 5
    db.set_setting(conn, "x", {"a": 1})
    assert db.get_setting(conn, "x") == {"a": 1}
