"""Tells summer cottages (vasarnīca) from ordinary houses (māja).

ss.lv and pp.lv file both under one "houses and cottages" category and neither stores
a type, so the only evidence is the seller's own wording. Cottages (also "garden
houses", dachas in Russian) are priced very differently from year-round houses, so the
distinction matters for fair value as well as for filtering.

Rules, in order:
  1. Only cottage words           -> cottage
  2. Only year-round-house words  -> house
  3. Both ("zemes gabals ar dzīvojamo māju ... vasarnīca"): whichever comes first wins
  4. Neither                      -> house (the category's default)
"""
import re

_COTTAGE = re.compile(
    r"vasarn[īi]c|vasarnic|dārza\s+māj|darza\s+maj|dārzu\s+māj|dārziņ|darzin|dārzkopīb|darzkopib|"
    r"sezonas\s+māj|sezonāl|sodu\s+māj|"
    r"дач|садов(?:ый|ая|ое|ого)\s+(?:дом|домик)|садоводч|"
    r"summer\s+(?:house|cottage|home)|garden\s+(?:house|cottage)|dacha",
    re.IGNORECASE,
)
_HOUSE = re.compile(
    r"privātmāj|privatmaj|dzīvojam[āoa]\s+māj|dzivojam[aoa]\s+maj|savrupmāj|savrupmaj|kotedž|kotedz|"
    r"dupleks|rindu\s*māj|rindu\s*maj|vilu\b|villa\b|"
    r"коттедж|жилой\s+дом|частный\s+дом|"
    r"townhouse|detached\s+house|family\s+house|"
    r"year[-\s]round",
    re.IGNORECASE,
)


def house_kind(title: str | None, description: str | None = None) -> str:
    """'cottage' or 'house', from the ad's title and the start of its description."""
    text = f"{title or ''} . {(description or '')[:1500]}"
    cottage, house = _COTTAGE.search(text), _HOUSE.search(text)
    if cottage and house:
        return "cottage" if cottage.start() < house.start() else "house"
    return "cottage" if cottage else "house"
