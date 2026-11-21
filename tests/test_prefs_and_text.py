from datetime import datetime, timezone

import pytest

from app import textflags
from app.scoring import prefs as P

NOW = datetime(2026, 11, 21, tzinfo=timezone.utc)
D = P.DEFAULT_PREFS


# ---------------------------------------------------------- the owner's own rules

def test_mileage_weight_matches_spec():
    assert P.mileage_weight(D, {"fuel": "petrol", "mileage_km": 500_000}) == pytest.approx(0.6)
    assert P.mileage_weight(D, {"fuel": "diesel", "mileage_km": 500_000}) == pytest.approx(0.65)
    assert P.mileage_weight(D, {"fuel": "petrol", "mileage_km": 99_000}) == 1.0
    assert P.mileage_weight(D, {"fuel": "diesel", "mileage_km": 149_000}) == 1.0
    assert P.mileage_weight(D, {"fuel": "petrol"}) == 1.0


def test_mileage_importance_zero_ignores_it():
    prefs = P.merge_prefs({"car": {"mileage": {"importance": 0}}})
    assert P.mileage_weight(prefs, {"fuel": "petrol", "mileage_km": 500_000}) == 1.0


@pytest.mark.parametrize("months,expected", [(36, 1.0), (60, 1.0), (24, 0.95), (12, 0.9), (1, 0.4)])
def test_ta_anchor_points(months, expected):
    assert P.ta_weight_raw(months) == pytest.approx(expected)


def test_ta_interpolates_and_floors():
    assert P.ta_weight_raw(30) == pytest.approx(0.975)
    assert P.ta_weight_raw(0) < 0.4
    assert P.ta_weight_raw(-24) == P.TA_FLOOR


def test_ta_only_applies_to_old_cars():
    young = {"year": 2022, "ta_date": "none"}
    old_none = {"year": 2010, "ta_date": "none"}
    old_unknown = {"year": 2010}
    old_valid = {"year": 2010, "ta_date": "2029-11"}
    assert P.ta_weight(D, young, NOW) == 1.0
    assert P.ta_weight(D, old_none, NOW) < 0.4
    assert P.ta_weight(D, old_unknown, NOW) == pytest.approx(0.8)
    assert P.ta_weight(D, old_valid, NOW) == 1.0


def test_corrosion_rules():
    assert P.corrosion_multiplier(D, {"make": "mazda", "model": "6", "year": 2012}) == 0.5
    assert P.corrosion_multiplier(D, {"make": "mazda", "model": "6", "year": 2015}) == 1.0
    assert P.corrosion_multiplier(D, {"make": "volkswagen", "model": "passat", "year": 2008}) == 0.5
    assert P.corrosion_multiplier(D, {"make": "volkswagen", "model": "golf", "year": 2008}) == 1.0
    off = P.merge_prefs({"car": {"corrosion_importance": 0}})
    assert P.corrosion_multiplier(off, {"make": "mazda", "model": "6", "year": 2012}) == 1.0


def test_fuel_weights():
    assert P.fuel_weight(D, "diesel") == 0.9 and P.fuel_weight(D, "hybrid") == 0.7


def test_merge_prefs_ignores_unknown_keys():
    merged = P.merge_prefs({"car": {"fuel": {"diesel": 0.5, "rocket": 9}}, "nonsense": {"a": 1}})
    assert merged["car"]["fuel"]["diesel"] == 0.5 and "rocket" not in merged["car"]["fuel"]
    assert "nonsense" not in merged


# ----------------------------------------------------------------- market signals

def test_freshness_and_price_drop():
    assert P.freshness_multiplier(D, 0.5) > 1.1
    assert P.freshness_multiplier(D, 100) < 1.0
    assert P.freshness_multiplier(D, None) == 1.0
    mult, drop = P.price_drop_multiplier(D, 10_000, 8_000, 2)
    assert drop == pytest.approx(0.2)
    assert mult == pytest.approx(1.30)  # bonus capped at 0.25, plus 0.05 for a cut within a week
    assert P.price_drop_multiplier(D, 8_000, 8_000, None) == (1.0, 0.0)


def test_final_score_applies_factors():
    listing = {"vertical": "car", "price": 5_000, "first_price": 5_000, "age_days": None,
               "last_change_days": None,
               "attrs": {"make": "audi", "model": "a4", "year": 2020, "fuel": "diesel", "mileage_km": 100_000}}
    out = P.final_score(listing, {"base": 20.0, "text_mult": 1.0}, D, NOW)
    assert out["score"] == pytest.approx(20.0 * 0.9)  # only the diesel weight applies
    names = [n for n, _ in out["factors"]]
    assert names == ["fuel", "mileage", "corrosion", "inspection", "wording", "freshness", "price_drop"]


# ------------------------------------------------------------------ text signals

@pytest.mark.parametrize("text", [
    "Auto pēc avārijas, jāremontē", "Pārdodu uz daļām", "машина после ДТП", "Не на ходу, требует ремонт",
    "Damaged front, engine problem", "dzinējs bojāts",
])
def test_car_negative_wording(text):
    flags, mult = textflags.scan("car", text)
    assert flags and mult < 0.8


@pytest.mark.parametrize("text", [
    "Bez avārijām, servisa grāmatiņa", "без аварий, первый владелец", "Nebojāts, rezerves daļas pieejamas",
    "Never had an accident, no accidents",
])
def test_car_negations_not_flagged(text):
    flags, mult = textflags.scan("car", text)
    assert not (set(flags) & {"damaged", "for_parts", "not_running"})
    assert mult >= 1.0


def test_car_positive_wording():
    flags, mult = textflags.scan("car", "Pirmais īpašnieks, servisa grāmatiņa, jauna TA")
    assert {"first_owner", "service_history", "fresh_ta"} <= set(flags)
    assert mult > 1.05


def test_estate_wording():
    flags, mult = textflags.scan("flat", "Dzīvoklis avārijas stāvoklī, ar apgrūtinājumiem")
    assert "dilapidated" in flags and mult == 0.3
    flags, mult = textflags.scan("flat", "Pēc remonta, gatavs dzīvošanai")
    assert "renovated" in flags and mult > 1.0


@pytest.mark.parametrize("text,flag", [
    ("Pārdod 1/2 domājamo daļu dzīvokļa", "share_only"),
    ("Pārdodas domājamās daļas no mājas", "share_only"),
    ("Remontējams dzīvoklis klusā vietā", "needs_renovation"),
    ("Cena ar pelēko apdari, jauns projekts", "unfinished"),
    ("Dzīvoklis, WC jāizbūvē, krāsns apkure", "missing_basics"),
    ("Доля в квартире, 1/3", "share_only"),
])
def test_estate_defects_are_recognised(text, flag):
    flags, mult = textflags.scan("flat", text)
    assert flag in flags and mult < 1.0


def test_a_share_of_a_flat_sinks_the_score_hardest():
    _, share = textflags.scan("flat", "Pārdod domājamo daļu dzīvokļa")
    _, reno = textflags.scan("flat", "Remontējams dzīvoklis")
    assert share < reno


def test_empty_text():
    assert textflags.scan("car", None, "") == ([], 1.0)
