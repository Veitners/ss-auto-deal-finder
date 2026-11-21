"""ss.lv real estate: flats and houses for sale, flats for rent (used for rental-yield comps)."""
import re

from .. import config, db
from ..http import HttpError
from ..norm import digits_to_int, parse_float, parse_int
from . import ss_common as c

REGIONS = [
    "riga", "jurmala", "riga-region", "aizkraukle-and-reg", "aluksne-and-reg", "balvi-and-reg",
    "bauska-and-reg", "cesis-and-reg", "daugavpils-and-reg", "dobele-and-reg", "gulbene-and-reg",
    "jekabpils-and-reg", "jelgava-and-reg", "kraslava-and-reg", "kuldiga-and-reg", "liepaja-and-reg",
    "limbazi-and-reg", "ludza-and-reg", "madona-and-reg", "ogre-and-reg", "preili-and-reg",
    "rezekne-and-reg", "saldus-and-reg", "talsi-and-reg", "tukums-and-reg", "valka-and-reg",
    "valmiera-and-reg", "ventspils-and-reg",
]
_NOT_REGIONS = {"new", "search", "sell", "hand_over", "today", "today-2", "today-5", "photo"}

# (vertical, deal_type) -> (url path, ss.lv deal slug)
SECTIONS = {
    ("flat", "sell"): ("flats", "sell"),
    ("flat", "rent"): ("flats", "hand_over"),
    ("house", "sell"): ("homes-summer-residences", "sell"),
}

_HREF = re.compile(r"/msg/lv/real-estate/(flats|homes-summer-residences)/([a-z0-9\-]+)/")


def first_cell_parts(row) -> list[str]:
    cells = row.xpath("./td[contains(@class, 'msga2-')]")
    if not cells:
        return []
    return [c.clean(t) for t in cells[0].xpath(".//text()") if c.clean(t)]


def _split_floor(text: str) -> tuple[int | None, int | None]:
    m = re.match(r"^\s*(\d+)\s*/\s*(\d+)", text or "")
    return (int(m.group(1)), int(m.group(2))) if m else (parse_int(text), None)


def _plot_m2(text: str | None) -> float | None:
    if not text:
        return None
    value = parse_float(text.replace(" ", ""))
    if value is None:
        return None
    return value * 10000 if re.search(r"\bha\b", text, re.IGNORECASE) else value


def parse_list(html: str, vertical: str, deal_type: str, region: str) -> list[dict]:
    doc = c.parse_html(html)
    records = []
    for row in c.list_rows(doc):
        link = row.xpath('.//a[@class="am"]')
        if not link or not _HREF.search(link[0].get("href", "")):
            continue
        cells = c.data_cells(row)
        parts = first_cell_parts(row)
        if not cells or not parts:
            continue
        attrs: dict = {}
        if vertical == "flat":
            if len(cells) < 7:
                continue
            _place, rooms, area, floor_txt, series, _price_m2, price_txt = cells[:7]
            floor, floors = _split_floor(floor_txt)
            attrs = {
                "rooms": parse_int(rooms), "area_m2": parse_float(area), "floor": floor,
                "floors": floors, "series": series if series not in ("", "-") else None,
            }
        else:  # house
            if len(cells) < 5:
                continue
            _place, area, floors, plot, price_txt = cells[:5]
            attrs = {"area_m2": parse_float(area), "floors": parse_int(floors),
                     "plot_m2": _plot_m2(plot)}
        price = digits_to_int(price_txt)
        if not price:
            continue
        records.append({
            "source": "ss",
            "ext_id": row.get("id").removeprefix("tr_"),
            "vertical": vertical,
            "deal_type": deal_type,
            "url": config.SS_BASE + link[0].get("href"),
            "title": c.clean(link[0].text_content()),
            "price": float(price),
            "region": region,
            "district": parts[0],
            "attrs": {**attrs, "street": parts[1] if len(parts) > 1 else None},
        })
    return records


def parse_detail(html: str) -> dict:
    doc = c.parse_html(html)
    opts = c.parse_options(doc)
    attrs = {
        "house_type": opts.get("Mājas tips"),
        "series": opts.get("Sērija"),
        "amenities": opts.get("Ērtības"),
        "rooms": parse_int(opts.get("Istabas")),
        "area_m2": parse_float(opts.get("Platība")),
        "plot_m2": _plot_m2(opts.get("Zemes platība")),
    }
    return {
        "description": c.parse_description(doc),
        "posted_at": c.parse_posted(html),
        "views": c.parse_views(html),
        "attrs": {k: v for k, v in attrs.items() if v is not None},
    }


def section_url(vertical: str, deal_type: str, region: str, page: int = 1) -> str:
    path, deal = SECTIONS[(vertical, deal_type)]
    base = f"{config.SS_BASE}/lv/real-estate/{path}/{region}/{deal}/"
    return base if page == 1 else f"{base}page{page}.html"


def crawl(client, conn, stats: dict, vertical: str, deal_type: str, regions=None) -> bool:
    """Crawl one (vertical, deal type) across all regions. True only if nothing was skipped."""
    path, deal = SECTIONS[(vertical, deal_type)]
    complete = True
    for region in regions or REGIONS:
        try:
            html = client.get_text(section_url(vertical, deal_type, region))
        except HttpError as exc:
            if exc.status == 404:  # region has no such section; not an error
                continue
            stats["errors"] += 1
            complete = False
            stats["note"] = f"{region}: {exc}"
            continue
        except Exception as exc:
            stats["errors"] += 1
            complete = False
            stats["note"] = f"{region}: {exc}"
            continue
        fragment = f"/lv/real-estate/{path}/{region}/{deal}/"
        last = min(c.last_page(html, fragment), config.MAX_PAGES_PER_SECTION)
        for page in range(1, last + 1):
            if page > 1:
                try:
                    html = client.get_text(section_url(vertical, deal_type, region, page))
                except Exception as exc:
                    stats["errors"] += 1
                    complete = False
                    stats["note"] = f"{region} p{page}: {exc}"
                    continue
            stats["pages"] += 1
            now = db.utcnow()
            for rec in parse_list(html, vertical, deal_type, region):
                res = db.upsert_listing(conn, rec, now)
                stats["seen"] += 1
                stats["new"] += res.is_new
                stats["changed"] += res.price_changed
            conn.commit()
    return complete
