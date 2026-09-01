# SS Auto Deal Finder

A static dashboard for browsing average asking prices on [ss.lv](https://www.ss.lv/lv/transport/cars/)'s car listings, broken down by make, model, year, and engine type — plus a finder for listings priced below their group's average.

**[Live demo](https://claude.ai/code/artifact/84818aa0-40a3-47e9-aedb-056bb6d2c9fa)**

## How it works

`index.html` is a single self-contained static page with the dataset embedded directly as JSON — there's no backend and no live scraping from the page itself (ss.lv doesn't allow cross-origin requests from a hosted page).

The dataset is a point-in-time snapshot gathered by two PowerShell scrapers:

- `scrape-models.ps1` — walks every make on ss.lv and records its full model taxonomy (which models exist, regardless of whether any are currently listed).
- `scrape-listings.ps1` — walks every make's listing pages (page depth scaled to the make's size — up to 6 pages for high-volume makes like BMW/Audi/VW, down to 1 for niche ones) and parses each row into `{ brand, model, year, engineVolume, fuel, mileageKm, priceEur, url }`.
- `scrape-ta.ps1` — visits each sampled listing's own ad page (one request per listing, ~1,700 total) and pulls its technical inspection (TA) expiry date, merging a `taDate` field (`"YYYY-MM"` or `null`) back into `listings.json`.

## Regenerating the data

```powershell
./scrape-models.ps1     # writes models.json
./scrape-listings.ps1   # writes listings.json
./scrape-ta.ps1         # merges taDate into listings.json (writes ta.json along the way)
```

Then minify both and splice them into `index.html` in place of the `__MODELS_JSON__` / `__LISTINGS_JSON__` placeholders in a fresh copy of the template.

## Deal Score

The Deal Finder ranks listings by a composite score rather than raw discount percentage:

```
score = %-below-group-average × fuelWeight × mileageWeight × corrosionMultiplier × taWeight
```

- **Fuel weight** — flat desirability per fuel type (diesel 0.9, petrol 0.85, hybrid 0.7, electric 0.6, gas 0.75).
- **Mileage weight** — `1 - rate × (km over threshold) / 100,000`, threshold 150k km for diesel vs 100k for everything else (diesel engines tolerate more distance before it counts against them).
- **Corrosion multiplier** — flat penalties for known problem model/year combos (Mazda pre-2014, VW Passat B6 both ×0.5).
- **TA weight** — for cars older than 10 years only, based on remaining technical-inspection validity: piecewise-linear between (36mo→1.0, 24mo→0.95, 12mo→0.9, 1mo→0.4), extrapolated and floored below 1 month (including expired/no TA on record).

Mileage, corrosion, and TA each have an "importance" slider in the UI (0–100%) that blends between the full formula above and ignoring that factor entirely (100% km is treated the same as 0km when importance is 0%). All raw weight values are also live-adjustable sliders.

## Methodology & limitations

- Prices are seller-listed asking prices, not confirmed sale prices — condition, damage, and negotiation aren't accounted for.
- Averages are computed per (make, model, year, fuel type) group; groups under 3 sampled listings are excluded from the Deal Finder as too thin to trust.
- This is a sample, not a full-site crawl — coverage is deepest for high-volume makes and thinner for niche ones.
- TA dates reflect what each listing's ad page stated on 2026-08-31; sellers may update or omit this field.
