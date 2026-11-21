"""Small normalisation helpers shared by the crawlers and the scoring code."""
import re
import unicodedata


def norm_key(text: str | None) -> str:
    """Lower-case alphanumerics only, so 'Land Rover' / 'land-rover' / 'LandRover' match."""
    if not text:
        return ""
    text = unicodedata.normalize("NFKD", text)
    text = "".join(c for c in text if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9]", "", text.lower())


def digits_to_int(text: str | None) -> int | None:
    """'9,990  €' -> 9990, '52 500 €' -> 52500. Thousands separators only, no decimals."""
    if not text:
        return None
    digits = re.sub(r"[^\d]", "", text)
    return int(digits) if digits else None


def parse_float(text: str | None) -> float | None:
    if text is None:
        return None
    m = re.search(r"\d+(?:[.,]\d+)?", str(text))
    return float(m.group(0).replace(",", ".")) if m else None


def parse_int(text) -> int | None:
    if text is None:
        return None
    m = re.search(r"\d+", str(text))
    return int(m.group(0)) if m else None


def parse_mileage_ss(text: str | None) -> int | None:
    """ss.lv shows mileage in thousands: '351 tūkst.' -> 351000. A bare '-' means unknown."""
    if not text:
        return None
    m = re.search(r"(\d[\d\s]*)", text)
    if not m:
        return None
    value = int(re.sub(r"\s", "", m.group(1)))
    return value * 1000 if value < 2000 else value  # >=2000 is already plain km


def fuel_from_ss_engine(cell: str | None) -> tuple[float | None, str]:
    """ss.lv engine cell: '1.8' petrol, '3.0D' diesel, '2.0H' hybrid, 'E' electric."""
    cell = (cell or "").strip().upper()
    m = re.match(r"^(\d+(?:\.\d+)?)?\s*([A-Z/]*)$", cell)
    if not m:
        return None, "petrol"
    litres = float(m.group(1)) if m.group(1) else None
    code = m.group(2)
    if "E" in code and "H" not in code:
        fuel = "electric"
    elif "H" in code:
        fuel = "hybrid"
    elif "D" in code:
        fuel = "diesel"
    elif "G" in code or "L" in code:
        fuel = "gas"
    else:
        fuel = "petrol"
    return litres, fuel


def _fold(text: str) -> str:
    """Lower-case and strip diacritics so matching survives precomposed/decomposed forms."""
    text = unicodedata.normalize("NFKD", text)
    return "".join(c for c in text if not unicodedata.combining(c)).lower()


def fuel_from_text(text: str | None) -> str | None:
    """pp.lv / ad-page wording: 'Dīzelis', 'Benzīns', 'Hibrīds', 'Elektriskais', 'Benzīns/gāze'."""
    if not text:
        return None
    low = _fold(text)
    if "elektr" in low:
        return "electric"
    if "hibr" in low:
        return "hybrid"
    if "gaz" in low:  # 'Benzīns/gāze' and plain 'Gāze' are both LPG conversions
        return "gas"
    if "diz" in low:
        return "diesel"
    if "benz" in low:
        return "petrol"
    return None


def gearbox_from_text(text: str | None) -> str | None:
    if not text:
        return None
    low = _fold(text)
    if "autom" in low:
        return "auto"
    if "manu" in low:
        return "manual"
    return None


def ta_from_value(value: str | None) -> str | None:
    """Return 'YYYY-MM', 'none' (explicitly no valid TA) or None (not stated)."""
    if value is None:
        return None
    value = value.strip()
    if not value:
        return None
    m = re.match(r"^(\d{4})-(\d{2})(?:-\d{2})?", value)  # pp.lv: 2027-04-13
    if m:
        return f"{m.group(1)}-{m.group(2)}"
    m = re.match(r"^(\d{2})\.(\d{4})$", value)  # ss.lv: 04.2027
    if m:
        return f"{m.group(2)}-{m.group(1)}"
    return "none"  # e.g. 'Bez apskates'
