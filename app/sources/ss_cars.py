"""ss.lv passenger cars: list pages (every brand) and per-ad detail pages."""
import re

from .. import config, db
from ..norm import (digits_to_int, fuel_from_ss_engine, fuel_from_text, gearbox_from_text,
                    parse_float, parse_int, parse_mileage_ss, ta_from_value)
from . import ss_common as c

BRANDS = [
    "alfa-romeo", "audi", "bmw", "chevrolet", "chrysler", "citroen", "cupra", "dacia", "dodge",
    "fiat", "ford", "honda", "hyundai", "jaguar", "jeep", "kia", "lancia", "land-rover", "lexus",
    "mazda", "mercedes", "mini", "mitsubishi", "nissan", "opel", "peugeot", "porsche", "renault",
    "saab", "seat", "skoda", "smart", "subaru", "suzuki", "tesla", "toyota", "volkswagen", "volvo",
    "gaz", "moskvich", "vaz", "others",
]

_HREF = re.compile(r"/msg/lv/transport/cars/([a-z0-9\-]+)/([a-z0-9\-]+)/([A-Za-z0-9]+)\.html")


def parse_list(html: str) -> list[dict]:
    """One normalised record per priced for-sale row on a brand list page."""
    doc = c.parse_html(html)
    records = []
    for row in c.list_rows(doc):
        link = row.xpath('.//a[@class="am"]')
        if not link:
            continue
        m = _HREF.search(link[0].get("href", ""))
        cells = c.data_cells(row)
        if not m or len(cells) < 5:
            continue
        brand, model_slug, _key = m.groups()
        model_label, year_txt, engine_txt, mileage_txt, price_txt = cells[:5]
        price = digits_to_int(price_txt)
        year = parse_int(year_txt)
        if not price or not year:
            continue
        litres, fuel = fuel_from_ss_engine(engine_txt)
        records.append({
            "source": "ss",
            "ext_id": row.get("id").removeprefix("tr_"),
            "vertical": "car",
            "deal_type": "sell",
            "url": config.SS_BASE + link[0].get("href"),
            "title": c.clean(link[0].text_content()),
            "price": float(price),
            "attrs": {
                "make": brand,
                "model": model_slug,
                "model_label": model_label,
                "year": year,
                "mileage_km": parse_mileage_ss(mileage_txt),
                "fuel": fuel,
                "engine_l": litres,
            },
        })
    return records


def parse_detail(html: str) -> dict:
    """Extra facts from an ad page: TA date, gearbox, body, description, posted time."""
    doc = c.parse_html(html)
    opts = c.parse_options(doc)
    attrs: dict = {}

    if "Tehniskā apskate" in opts:
        attrs["ta_date"] = ta_from_value(opts["Tehniskā apskate"])
    attrs["gearbox"] = gearbox_from_text(opts.get("Ātrumkārba"))
    attrs["body"] = opts.get("Virsbūves tips")
    attrs["color"] = re.sub(r"\s*&nbsp;\s*", "", opts.get("Krāsa", "")) or None

    engine = opts.get("Motors")
    if engine:
        attrs["engine_l"] = parse_float(engine)
        fuel = fuel_from_text(engine)
        if fuel:
            attrs["fuel"] = fuel
    year_txt = opts.get("Izlaiduma gads")
    if year_txt and parse_int(year_txt):
        attrs["year"] = parse_int(year_txt)
    mileage = opts.get("Nobraukums, km")
    if mileage and digits_to_int(mileage):
        attrs["mileage_km"] = digits_to_int(mileage)

    return {
        "description": c.parse_description(doc),
        "posted_at": c.parse_posted(html),
        "views": c.parse_views(html),
        "attrs": {k: v for k, v in attrs.items() if v is not None},
    }


def section_url(brand: str, page: int = 1) -> str:
    base = f"{config.SS_BASE}/lv/transport/cars/{brand}/sell/"
    return base if page == 1 else f"{base}page{page}.html"


def crawl(client, conn, stats: dict, brands=None) -> bool:
    """Crawl every brand's for-sale pages. Returns True only if the pass was complete."""
    complete = True
    for brand in brands or BRANDS:
        try:
            html = client.get_text(section_url(brand))
        except Exception as exc:  # one broken brand must not abort the others
            stats["errors"] += 1
            complete = False
            stats["note"] = f"{brand}: {exc}"
            continue
        last = min(c.last_page(html, f"/lv/transport/cars/{brand}/sell/"), config.MAX_PAGES_PER_SECTION)
        pages = [html] + [None] * (last - 1)
        for idx in range(last):
            if pages[idx] is None:
                try:
                    pages[idx] = client.get_text(section_url(brand, idx + 1))
                except Exception as exc:
                    stats["errors"] += 1
                    complete = False
                    stats["note"] = f"{brand} p{idx + 1}: {exc}"
                    continue
            stats["pages"] += 1
            now = db.utcnow()
            for rec in parse_list(pages[idx]):
                res = db.upsert_listing(conn, rec, now)
                stats["seen"] += 1
                stats["new"] += res.is_new
                stats["changed"] += res.price_changed
            pages[idx] = None
            conn.commit()
    return complete
