"""Reads listing titles and descriptions for wording that changes how much to trust a price.

A suspiciously cheap car "after an accident" or "for parts" is not a deal, and an
ad that says "no accidents, service history" deserves slightly more trust. Wording
is matched in Latvian, Russian and English. Negated phrases ("bez avārijām",
"без аварий") are removed first so they are never mistaken for the problem itself.
"""
import re

# (flag, multiplier, regex). Multipliers apply to the final deal score.
_NEGATIONS = re.compile(
    r"\b(bez|nav\s+bijusi|nav\s+bijis|neesam\s+bijuš\w*|без|не\s+бит\w*|no|without)\s+"
    r"(avārij\w*|avarij\w*|dtp|bojājum\w*|rūsas|rusas|аварий\w*|дтп|повреждени\w*|"
    r"accident\w*|damage\w*|rust)\b",
    re.IGNORECASE,
)
_NEG_PHRASES = re.compile(
    r"\b(neavārij\w*|neavarij\w*|небит\w*|небитый|undamaged|not\s+damaged|accident[\s-]*free|"
    r"never\s+(?:had|been\s+in)\s+an?\s+accident)\b",
    re.IGNORECASE,
)

CAR_NEGATIVE = [
    ("damaged", 0.25, r"\bavārij|\bavarij|\bbojāt|\bbojat|pēc\s+dtp|после\s+дтп|\bаварийн|"
                      r"\bбитый|\bбитая|\bповреждён|\bповреждени|\bdamaged|\baccident|\bcrash"),
    ("for_parts", 0.2, r"uz\s+daļām|uz\s+dalam|на\s+запчасти|на\s+разбор|for\s+parts"),
    ("not_running", 0.25, r"nav\s+kustīb|nav\s+kustib|nebrauc|nedarbojas|nestrādā|nestrada|"
                          r"не\s+на\s+ходу|не\s+заводится|не\s+едет|not\s+running|doesn'?t\s+start|"
                          r"engine\s+(?:problem|fault|damage)|dzinēj\w*\s+bojāt"),
    ("needs_repair", 0.7, r"remontam|jāremontē|jaremonte|vajag\s+remont|nepieciešams\s+remont|"
                          r"требует\s+ремонт|под\s+восстановление|needs\s+repair|project\s+car"),
    ("no_papers", 0.4, r"bez\s+dokument|bez\s+dokumentiem|без\s+документов|no\s+documents"),
    ("scam_wording", 0.5, r"avanss|avansa\s+maks|pirms\s+apskates\s+nav|ārzemēs\s+atrodas|"
                          r"предоплат|аванс\b|advance\s+payment|owner\s+abroad"),
]
CAR_POSITIVE = [
    ("service_history", 1.03, r"servisa\s+grāmat|servisa\s+gramat|servisa\s+vēstur|"
                              r"сервисная\s+книжк|service\s+history|full\s+service"),
    ("first_owner", 1.03, r"pirmais\s+īpašnieks|pirmais\s+ipasnieks|1\.\s*īpašnieks|"
                          r"первый\s+владелец|first\s+owner|one\s+owner"),
    ("fresh_ta", 1.02, r"jauna\s+ta\b|svaiga\s+ta\b|tikko\s+izieta|jauna\s+tehnisk|"
                       r"свежий\s+то\b|new\s+mot"),
]

ESTATE_NEGATIVE = [
    ("dilapidated", 0.3, r"avārijas\s+stāvokl|avarijas\s+stavokl|grausts|sagruv|нужен\s+капитальный|"
                         r"аварийн|derelict|ruin"),
    # A fractional share of a flat or house is priced as a share, not as the whole property.
    ("share_only", 0.25, r"domājam\w*\s+daļ|domajam\w*\s+dal|\b\d/\d\s+(?:no\s+)?(?:dzīvokļa|dzivokla|mājas|majas)|"
                         r"\bдол[яи]\s+(?:в|квартир)|share\s+of\s+(?:a\s+)?(?:flat|house|apartment)"),
    ("encumbered", 0.5, r"apgrūtināj|apgrutinaj|ķīla|kila\b|parāds|parads|hipotēk|в\s+залоге|"
                        r"обременени|mortgaged|encumbr"),
    ("missing_basics", 0.8, r"wc\s+jāizbūvē|jāizbūvē\s+wc|bez\s+ērtīb|bez\s+ūdens|bez\s+kanalizācij|"
                            r"без\s+удобств|без\s+воды|no\s+(?:running\s+water|toilet)"),
    ("unfinished", 0.8, r"pelēk\w+\s+apdar|pelek\w+\s+apdar|bez\s+apdares|серая\s+отделк|shell\s+(?:and|&)\s+core"),
    ("needs_renovation", 0.9, r"jāremontē|jaremonte|remontējam|remontejam|bez\s+remonta|nepieciešams\s+remonts|"
                              r"ar\s+vecu\s+apdari|требует\s+ремонт|без\s+ремонта|needs\s+renovation"),
    ("scam_wording", 0.5, r"avanss|предоплат|advance\s+payment"),
]
ESTATE_POSITIVE = [
    ("renovated", 1.03, r"pēc\s+remonta|pec\s+remonta|renovēt|renovet|izremontēt\w*\s+dzīvokl|"
                        r"новый\s+ремонт|после\s+ремонта|renovated|refurbished"),
    ("ready_to_move", 1.02, r"gatavs\s+dzīvošanai|gatavs\s+iekārtošanai|можно\s+заезжать|move-in\s+ready"),
]


def _compile(rules):
    return [(flag, mult, re.compile(pattern, re.IGNORECASE)) for flag, mult, pattern in rules]


_CAR_NEG, _CAR_POS = _compile(CAR_NEGATIVE), _compile(CAR_POSITIVE)
_EST_NEG, _EST_POS = _compile(ESTATE_NEGATIVE), _compile(ESTATE_POSITIVE)


def scan(vertical: str, *texts: str | None) -> tuple[list[str], float]:
    """Return (flags, combined multiplier) for the given title/description text."""
    text = " ".join(t for t in texts if t)
    if not text:
        return [], 1.0
    cleaned = _NEGATIONS.sub(" ", text)
    cleaned = _NEG_PHRASES.sub(" ", cleaned)
    neg, pos = (_CAR_NEG, _CAR_POS) if vertical == "car" else (_EST_NEG, _EST_POS)

    flags: list[str] = []
    mult = 1.0
    worst = 1.0
    for flag, m, rx in neg:
        if rx.search(cleaned):
            flags.append(flag)
            worst = min(worst, m)  # the single worst problem sets the penalty
    mult *= worst
    for flag, m, rx in pos:
        if rx.search(text):
            flags.append(flag)
            mult *= m
    if vertical == "car" and _NEGATIONS.search(text) and re.search(
        r"avārij|avarij|аварий|accident|дтп", text, re.IGNORECASE
    ) and "damaged" not in flags:
        flags.append("no_accident_claimed")
        mult *= 1.02
    return flags, round(mult, 4)
