"""Pieces shared by the ss.lv list and ad-page parsers."""
import re
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import lxml.html

RIGA = ZoneInfo("Europe/Riga")


def clean(text: str | None) -> str:
    return re.sub(r"\s+", " ", (text or "").replace("\xa0", " ")).strip()


def parse_html(html: str):
    return lxml.html.fromstring(html)


def list_rows(doc):
    """Real listing rows only (skips banner rows such as id="tr_bnr_...")."""
    return [tr for tr in doc.xpath('//tr[starts-with(@id, "tr_")]')
            if re.fullmatch(r"tr_\d+", tr.get("id", ""))]


def data_cells(row) -> list[str]:
    """Text of the data columns of a list row (everything after photo + title)."""
    return [clean(td.text_content()) for td in row.xpath("./td[contains(@class, 'msga2-')]")]


def last_page(html: str, path_fragment: str) -> int:
    """Highest page number linked from a list page (the 'previous' link on page 1 points at the last)."""
    pattern = re.escape(path_fragment) + r"page(\d+)\.html"
    pages = [int(n) for n in re.findall(pattern, html)]
    return max(pages) if pages else 1


def parse_options(doc) -> dict[str, str]:
    """The label/value table on an ad page ('Platība: 45 m²', 'Tehniskā apskate: 10.2027', ...)."""
    out: dict[str, str] = {}
    for name_td in doc.xpath('//td[contains(@class, "ads_opt_name")]'):
        label = clean(name_td.text_content()).rstrip(":").strip()
        val = name_td.getnext()
        if val is not None and "ads_opt" in (val.get("class") or ""):
            out[label] = clean(val.text_content())
    return out


def parse_description(doc) -> str | None:
    div = doc.get_element_by_id("msg_div_msg", None)
    if div is None:
        return None
    for table in div.xpath(".//table"):
        table.drop_tree()
    text = clean(div.text_content())
    return text or None


def parse_posted(html: str) -> str | None:
    """'Datums: 14.11.2026 16:25' (Riga time) -> UTC ISO string."""
    m = re.search(r"Datums:\s*(\d{2})\.(\d{2})\.(\d{4})\s+(\d{2}):(\d{2})", html)
    if not m:
        return None
    d, mo, y, hh, mm = (int(x) for x in m.groups())
    local = datetime(y, mo, d, hh, mm, tzinfo=RIGA)
    return local.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_views(html: str) -> int | None:
    m = re.search(r"Apskatīts:\s*(?:</?[^>]+>\s*)*(\d[\d\s]*)", html)
    return int(re.sub(r"\s", "", m.group(1))) if m else None
