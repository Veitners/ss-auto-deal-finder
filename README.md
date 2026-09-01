# SS Auto Deal Finder

A static dashboard for browsing average asking prices on [ss.lv](https://www.ss.lv/lv/transport/cars/)'s car listings, broken down by make, model, year, and engine type — plus a finder for listings priced below their group's average.

**[Live demo](https://claude.ai/code/artifact/84818aa0-40a3-47e9-aedb-056bb6d2c9fa)**

## How it works

`index.html` is a single self-contained static page with the dataset embedded directly as JSON — there's no backend and no live scraping from the page itself (ss.lv doesn't allow cross-origin requests from a hosted page).

The dataset is a point-in-time snapshot gathered by two PowerShell scrapers:

- `scrape-models.ps1` — walks every make on ss.lv and records its full model taxonomy (which models exist, regardless of whether any are currently listed).
- `scrape-listings.ps1` — walks every make's listing pages (page depth scaled to the make's size — up to 6 pages for high-volume makes like BMW/Audi/VW, down to 1 for niche ones) and parses each row into `{ brand, model, year, engineVolume, fuel, mileageKm, priceEur, url }`.

## Regenerating the data

```powershell
./scrape-models.ps1     # writes models.json
./scrape-listings.ps1   # writes listings.json
```

Then minify both and splice them into `index.html` in place of the `__MODELS_JSON__` / `__LISTINGS_JSON__` placeholders in a fresh copy of the template.

## Methodology & limitations

- Prices are seller-listed asking prices, not confirmed sale prices — condition, damage, and negotiation aren't accounted for.
- Averages are computed per (make, model, year, fuel type) group; groups under 3 sampled listings are excluded from the Deal Finder as too thin to trust.
- This is a sample, not a full-site crawl — coverage is deepest for high-volume makes and thinner for niche ones.
