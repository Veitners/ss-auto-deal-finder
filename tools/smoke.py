"""Live smoke test: a tiny, polite crawl of both sites through the whole pipeline.

    docker run --rm -e MAX_PAGES_PER_SECTION=3 deal-radar python tools/smoke.py

Uses a throw-away database, so it never touches real data.
"""
import json
import os
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("MAX_PAGES_PER_SECTION", "3")
os.environ["DATA_DIR"] = tempfile.mkdtemp()
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import db  # noqa: E402
from app.http import default_client  # noqa: E402
from app.jobs import apply_detail, new_stats  # noqa: E402
from app.scoring import market  # noqa: E402
from app.scoring.prefs import merge_prefs  # noqa: E402
from app.sources import pp, ss_cars, ss_estate  # noqa: E402

conn = db.connect()
db.init(conn)
client = default_client()


def step(label, fn):
    stats = new_stats()
    before = conn.execute("SELECT COUNT(*) FROM listings").fetchone()[0]
    ok = fn(stats)
    after = conn.execute("SELECT COUNT(*) FROM listings").fetchone()[0]
    print(f"{label:<28} complete={ok} pages={stats['pages']:>2} seen={stats['seen']:>3} "
          f"+{after - before} errors={stats['errors']} {stats['note'] or ''}", flush=True)


step("ss cars (audi, bmw, vw)", lambda s: ss_cars.crawl(client, conn, s, brands=["audi", "bmw", "volkswagen"]))
step("pp cars", lambda s: pp.crawl(client, conn, s, "car"))
step("ss flats riga", lambda s: ss_estate.crawl(client, conn, s, "flat", "sell", regions=["riga"]))
step("ss houses riga-region", lambda s: ss_estate.crawl(client, conn, s, "house", "sell", regions=["riga-region"]))
step("ss rent riga", lambda s: ss_estate.crawl(client, conn, s, "flat", "rent", regions=["riga"]))

# enrich one ss ad per vertical like the real job would
for vertical, parser in (("car", ss_cars.parse_detail), ("flat", ss_estate.parse_detail)):
    row = conn.execute("SELECT id, url FROM listings WHERE source='ss' AND vertical=? LIMIT 1", (vertical,)).fetchone()
    detail = parser(client.get_text(row["url"]))
    apply_detail(conn, row["id"], detail)
    print(f"enriched {vertical}: {json.dumps(detail['attrs'], ensure_ascii=False)[:150]}", flush=True)
conn.commit()

print("\n-- listings by source/vertical/deal --")
for r in conn.execute("SELECT source, vertical, deal_type, COUNT(*) n FROM listings GROUP BY 1,2,3"):
    print(f"  {r['source']} {r['vertical']:<6} {r['deal_type']:<5} {r['n']}")

print("\n-- scoring --")
print(market.compute_market(conn))
prefs = merge_prefs(None)
for vertical in ("car", "flat", "house"):
    ranked = market.ranked(conn, vertical, prefs)
    print(f"\n{vertical}: {len(ranked)} scored; top 3")
    for d in ranked[:3]:
        a = d["attrs"]
        label = (f"{a.get('make')} {a.get('model')} {a.get('year')} {a.get('mileage_km')}km {a.get('fuel')}"
                 if vertical == "car" else f"{a.get('area_m2')}m2 {a.get('rooms')}r {d.get('district')}")
        print(f"  {d['source']} EUR {d['price']:>9,.0f} fair {d['fair']:>9,.0f} disc {d['discount']*100:5.1f}% "
              f"conf {d['conf']:.2f} score {d['score']:5.1f} n={d['group_n']:<4} {label} {d['flags']}")
print("\nrequests made:", client.request_count)
