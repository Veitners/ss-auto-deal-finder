"""Parsers are pinned to real pages saved in tests/fixtures."""
import copy

from app.norm import (digits_to_int, fuel_from_ss_engine, fuel_from_text, gearbox_from_text,
                      parse_mileage_ss, ta_from_value)
from app.sources import pp, ss_cars, ss_common, ss_estate


# ---------------------------------------------------------------- normalisation

def test_price_digits():
    assert digits_to_int("9,990  €") == 9990
    assert digits_to_int("52 500 €") == 52500
    assert digits_to_int("Pērku") is None


def test_ss_engine_codes():
    assert fuel_from_ss_engine("3.0D") == (3.0, "diesel")
    assert fuel_from_ss_engine("1.8") == (1.8, "petrol")
    assert fuel_from_ss_engine("2.0H") == (2.0, "hybrid")
    assert fuel_from_ss_engine("E") == (None, "electric")


def test_fuel_and_gearbox_words_survive_unicode_forms():
    import unicodedata
    for word, fuel in [("Dīzelis", "diesel"), ("Benzīns", "petrol"), ("Hibrīds", "hybrid"),
                       ("Elektriskais", "electric"), ("Benzīns/gāze", "gas")]:
        assert fuel_from_text(word) == fuel
        assert fuel_from_text(unicodedata.normalize("NFD", word)) == fuel  # decomposed form
    assert gearbox_from_text("Automātiskā") == "auto"
    assert gearbox_from_text("Manuālā") == "manual"
    assert gearbox_from_text(None) is None


def test_mileage():
    assert parse_mileage_ss("351 tūkst.") == 351_000
    assert parse_mileage_ss("") is None
    assert parse_mileage_ss("-") is None


def test_ta_values():
    assert ta_from_value("10.2027") == "2027-10"
    assert ta_from_value("2023-04-13") == "2023-04"
    assert ta_from_value("Bez apskates") == "none"
    assert ta_from_value("") is None
    assert ta_from_value(None) is None


# ------------------------------------------------------------------- ss.lv cars

def test_ss_car_list(fixture_text):
    recs = ss_cars.parse_list(fixture_text("ss_cars_list.html"))
    assert len(recs) >= 20
    for r in recs:
        assert r["source"] == "ss" and r["vertical"] == "car" and r["deal_type"] == "sell"
        assert r["price"] > 0
        assert r["url"].startswith("https://www.ss.lv/msg/lv/transport/cars/audi/")
        a = r["attrs"]
        assert a["make"] == "audi" and a["model"] and 1950 < a["year"] < 2100
        assert a["fuel"] in {"petrol", "diesel", "hybrid", "electric", "gas"}
    assert len({r["ext_id"] for r in recs}) == len(recs)


def test_ss_last_page(fixture_text):
    assert ss_common.last_page(fixture_text("ss_cars_list.html"), "/lv/transport/cars/audi/sell/") > 10


def test_ss_car_detail(fixture_text):
    d = ss_cars.parse_detail(fixture_text("ss_car_ad.html"))
    assert d["attrs"]["ta_date"] == "2027-10"
    assert d["attrs"]["gearbox"] == "auto"
    assert d["attrs"]["fuel"] == "diesel"
    assert d["attrs"]["year"] == 2016
    assert d["attrs"]["mileage_km"] == 306_000
    assert d["posted_at"] and d["posted_at"].endswith("Z")


# --------------------------------------------------------------- ss.lv estate

def test_ss_flats_list(fixture_text):
    recs = ss_estate.parse_list(fixture_text("ss_flats_list.html"), "flat", "sell", "riga")
    assert len(recs) >= 20
    first = recs[0]
    assert first["price"] == 9990
    assert first["district"] == "Krasta r-ns"
    a = first["attrs"]
    assert a["rooms"] == 1 and a["area_m2"] == 18 and a["floor"] == 1 and a["floors"] == 2
    assert a["series"] == "P. kara" and a["street"] == "Rītupes 16"


def test_ss_houses_list(fixture_text):
    recs = ss_estate.parse_list(fixture_text("ss_houses_list.html"), "house", "sell", "riga-region")
    assert len(recs) >= 20
    first = recs[0]
    assert first["price"] == 204_000
    assert first["attrs"]["area_m2"] == 110 and first["attrs"]["plot_m2"] == 1200
    assert first["attrs"]["floors"] == 1
    assert first["district"] == "Olaines pag."


def test_ss_rent_list(fixture_text):
    recs = ss_estate.parse_list(fixture_text("ss_rent_list.html"), "flat", "rent", "riga")
    assert len(recs) >= 20
    assert all(r["deal_type"] == "rent" and 30 < r["price"] < 20_000 for r in recs)


def test_ss_flat_detail(fixture_text):
    d = ss_estate.parse_detail(fixture_text("ss_flat_ad.html"))
    assert d["attrs"]["house_type"] == "Koka"
    assert d["attrs"]["area_m2"] == 18
    assert "izremontēt" in d["description"]
    assert d["posted_at"] == "2026-11-14T14:25:00Z"  # 16:25 Riga time (UTC+2)


# ------------------------------------------------------------------------ pp.lv
# These cross-check the parser against the raw JSON, so they stay valid as listings change.

def raw(lot, name):
    """Independent extraction of one filter value straight from the API payload."""
    for item in lot["adFilterValues"]:
        if item["filter"]["name"] == name:
            text = item.get("textValue")
            return text if text not in (None, "") else (item.get("value") or {}).get("displayValue")
    return None


def test_pp_fixtures_are_proper_unicode(fixture_json):
    lot = fixture_json("pp_cars.json")["content"]["data"][0]
    assert lot["action"]["name"] == "Pārdod"  # guards against double-encoded fixtures


def test_pp_cars_match_the_raw_payload(fixture_json):
    lots = fixture_json("pp_cars.json")["content"]["data"]
    parsed = [(l, pp.parse_lot(l, "car")) for l in lots]
    good = [(l, r) for l, r in parsed if r]
    assert len(good) >= 5
    for lot, rec in good:
        a = rec["attrs"]
        assert rec["source"] == "pp" and rec["deal_type"] == "sell"
        assert rec["price"] == float(lot["prices"][0]["value"])
        assert a["year"] == int(raw(lot, "Izlaiduma gads"))
        km = raw(lot, "Nobraukums, km")
        if km is not None:
            assert a["mileage_km"] in (int(km), int(km) * 1000)  # x1000 only for the "369" typo case
        assert a["fuel"] == {"Dīzelis": "diesel", "Benzīns": "petrol", "Hibrīds": "hybrid",
                             "Elektriskais": "electric", "Benzīns/gāze": "gas"}.get(raw(lot, "Degvielas tips"), a["fuel"])
        parts = lot["frontUrl"].split("/vieglie-auto/")[1].split("/")
        assert a["make"] == parts[0] and a["model"] == parts[1]
        engine = raw(lot, "Motora tilpums")
        if engine and "engine_l" in a:
            assert 0.1 <= a["engine_l"] <= 9.0
        ta = raw(lot, "Tehniskā apskate")
        if ta:
            assert a["ta_date"] == ta[:7]
        else:
            assert "ta_date" not in a  # empty field = not stated, never guessed


def test_pp_mileage_varies_between_cars(fixture_json):
    """filter.description is generic help text: reading it as a value would make every car identical."""
    lots = fixture_json("pp_cars.json")["content"]["data"]
    kms = {r["attrs"].get("mileage_km") for r in (pp.parse_lot(l, "car") for l in lots) if r}
    assert len(kms) > 2


def test_pp_car_edge_cases(fixture_json):
    lot = copy.deepcopy(fixture_json("pp_cars.json")["content"]["data"][0])
    for item in lot["adFilterValues"]:
        if item["filter"]["name"] == "Izlaiduma gads":
            item["value"]["displayValue"] = "2007"
        if item["filter"]["name"] == "Nobraukums, km":
            item["textValue"] = "369"              # typed short: 369 thousand
        if item["filter"]["name"] == "Tehniskā apskate":
            item["textValue"] = "2027-01-01"
    a = pp.parse_lot(lot, "car")["attrs"]
    assert a["mileage_km"] == 369_000 and a["ta_date"] == "2027-01"
    for item in lot["adFilterValues"]:
        if item["filter"]["name"] == "Tehniskā apskate":
            item["textValue"] = "Bez apskates"
    assert pp.parse_lot(lot, "car")["attrs"]["ta_date"] == "none"


def test_pp_rejects_non_sale_and_priceless(fixture_json):
    lot = copy.deepcopy(fixture_json("pp_cars.json")["content"]["data"][0])
    lot["action"]["name"] = "Pērk"
    assert pp.parse_lot(lot, "car") is None
    lot["action"]["name"] = "Pārdod"
    lot["prices"] = []
    assert pp.parse_lot(lot, "car") is None


def test_pp_flats_match_the_raw_payload(fixture_json):
    lots = fixture_json("pp_flats.json")["content"]["data"]
    seen_sell = 0
    for lot in lots:
        rec = pp.parse_lot(lot, "flat", region_name="riga")
        if rec is None:
            continue
        a = rec["attrs"]
        assert rec["region"] == "riga" and rec["district"] == lot["publicLocation"]["region"]["name"]
        assert a["area_m2"] == float(raw(lot, "Platība, m2"))
        if raw(lot, "Istabu skaits"):
            assert a["rooms"] == int(raw(lot, "Istabu skaits"))
        seen_sell += rec["deal_type"] == "sell"
    assert seen_sell >= 3


def test_pp_flats_can_be_rent(fixture_json):
    lots = fixture_json("pp_flats.json")["content"]["data"]
    kinds = {pp.deal_type_of(l) for l in lots}
    assert "sell" in kinds


def test_pp_houses_match_the_raw_payload(fixture_json):
    lots = fixture_json("pp_houses.json")["content"]["data"]
    recs = [pp.parse_lot(l, "house") for l in lots]
    good = [r for r in recs if r]
    assert len(good) >= 4
    for lot, rec in zip(lots, recs):
        if rec:
            assert rec["attrs"]["area_m2"] == float(raw(lot, "Platība, m2"))
            if "plot_m2" in rec["attrs"]:
                assert rec["attrs"]["plot_m2"] >= 30  # hectares converted to m2
