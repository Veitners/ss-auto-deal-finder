# Deal Radar

A self-hosted service that watches [ss.lv](https://www.ss.lv) and [pp.lv](https://pp.lv) for **cars, flats and houses**, estimates what each listing *should* cost from comparable listings, and ranks the ones that look underpriced. It crawls on a schedule, keeps price history, and serves a dashboard that explains every score.

```
docker compose up -d --build        # dashboard on http://localhost:8082
```

The first crawl takes roughly an hour (about 28 000 cars, 10 000 estate listings); after that it refreshes every 12 hours.

## How a deal is found

1. **Fair value.** For every listing a robust ridge regression predicts log(price) from what drives price in that market, and predicts it *leave-one-out*, so a listing never helps to price itself.
   - **Cars:** age, mileage, fuel, gearbox, engine size, model. Fit per make, and per model where there are at least 40 listings and it fits better.
   - **Flats:** area, rooms, floor (first / last), building series and type, district. Separate fits for Riga, Jūrmala and the rest of Latvia.
   - **Houses:** floor area, plot size, region, district, and **house vs summer cottage** (`vasarnīca`), read from the ad's wording because neither site labels it.
   - Huber re-weighting keeps odd listings (typos, wrecks) from bending the market line; the ridge penalty keeps rare categories honest.
2. **Second opinion.** The median price of the nearest comparable listings. The two estimates are blended; when they disagree, confidence drops.
3. **Noise per listing.** Asking prices scatter far more for old cars than new ones, so every listing gets a noise level from its own age band. A 30% discount on a 2-year-old car counts for more than on a 20-year-old one.
4. **Confidence** combines sample size, scatter and agreement of the two estimates. The market score is `discount × confidence`.
5. **Too good to be true.** Discounts above 45% are flagged and hidden by default: they are far more often a trap than a bargain.
6. **Your preferences multiply on top** (all adjustable in the dashboard, each with an "importance" that blends from ignored to fully applied):
   fuel-type trust, mileage wear past a threshold, rust-prone models (Mazda before 2014, VW Passat B6), technical-inspection (TA) validity for cars over 10 years, damage / defect wording in Latvian, Russian and English (negations like *bez avārijām* are handled), freshness, and price drops.
7. **Rental yield for flats.** Estimated rent comes from the median rent per m² of comparable rentals (district + room count first, wider sets as fallback), giving a gross yield.

Every number is shown in the listing's detail panel: fair value, scatter, each multiplier, the nearest comparables, and the price history.

## Data sources and etiquette

| Source | How | Delay |
|---|---|---|
| ss.lv | HTML list pages; ad pages for TA date, gearbox, description (best candidates first) | 0.8 s |
| pp.lv | The site's own public JSON API (`apipub.pp.lv`), structured fields + price history | 5 s (its `robots.txt` asks for this; enforced as a floor) |

The HTTP client enforces the per-host delay no matter what calls it, backs off on 429/5xx and honours `Retry-After`. Disallowed paths are never fetched. This is a personal price tracker; keep the delays.

## Configuration

Environment variables (see `app/config.py`): `CRAWL_INTERVAL_HOURS` (12), `SS_DELAY`, `PP_DELAY` (min 5), `MAX_PAGES_PER_SECTION`, `MAX_ENRICH_PER_RUN`, `AUTOSTART_CRAWLS` (set `0` to only crawl on request), `ADMIN_TOKEN` (enables `POST /api/run/<job>` with header `X-Admin-Token`), `NTFY_URL` (push alerts for fresh, high-confidence deals through an [ntfy](https://ntfy.sh) topic).

Data lives in `./data/radar.db` (SQLite, WAL).

## API

`GET /api/deals?vertical=car|flat|house` with filters (`source`, `region`, `q`, `price_min/max`, `year_min/max`, `km_max`, `fuel`, `gear`, `area_min/max`, `rooms_min/max`, `kind=house|cottage`, `yield_min`, `min_conf`, `include_too_good`, `include_flagged`, `sort`, `limit`, `offset`) and `prefs` (JSON of your weights) ·
`GET /api/listing/{id}` · `GET /api/regions` · `GET /api/status` · `GET /api/health`

## Development

```
python -m pytest                       # 100+ tests: parsers pinned to real saved pages, scoring on synthetic markets with planted bargains
python tools/smoke.py                  # tiny live crawl of both sites through the whole pipeline
python tools/diagnose.py car|flat|house   # calibration report from the live database
```

`tools/diagnose.py` is the one to run after touching the model: residuals should be centred on zero in every age, source, fuel and make segment.

## Limits worth knowing

- Asking prices, not sale prices. Condition, trim, service history and photos are invisible to the model, so a listing far below fair value is *a reason to look*, not a verdict.
- Typical error is about 20% for cars and flats and far more for houses (condition and land dominate), which the confidence score reflects.
- ss.lv cars carry no gearbox until the ad page is fetched; the crawler fills that in over a few days, best candidates first.
- Whether the sites tolerate automated access is their call; if either objects, stop.

`legacy/` holds the original one-off static page and PowerShell scrapers this replaced.
