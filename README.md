# Apartment Radar — Downtown Minneapolis

Personal apartment-tracking tool. Same shape as Job Radar and the RFP scrubber.

```
buildings.csv  -->  apartment_scraper.py  -->  listings.json  -->  index.html
```

## Run locally

```powershell
cd "W:\AI\Anthropic\jason-core\Apartment move"
pip install requests beautifulsoup4 playwright
python -m playwright install chromium
python apartment_scraper.py
```

Single building, with debug HTML saved to `_debug/`:

```powershell
cd "W:\AI\Anthropic\jason-core\Apartment move"
python apartment_scraper.py --building "Encore" --debug
```

## buildings.csv schema

| Column | Notes |
|---|---|
| `priority` | `P1` luxury target / `P2` mid / `P3` character/historic |
| `name` | Display name |
| `neighborhood` | One of: North Loop, Mill District, Loring Park, Elliot Park, Core Downtown |
| `address` | Free text |
| `website` | Building landing page |
| `listings_url` | The actual availability/floorplans page (what the scraper hits) |
| `scraper_key` | `generic` for now; later: `rentcafe`, `entrata`, `appfolio` |
| `verified` | `yes` if URL confirmed to return real units, `no` otherwise |
| `amenity_tier` | 1–5 (5 = Lakehaus-tier: pool + gym + rooftop + concierge) |
| `character_tier` | 1–5 (5 = exposed brick/timber/floor-to-ceiling/loft) |
| `parking_included` | `yes` / `no` / `extra` |
| `utilities_note` | What's bundled — e.g. `water+trash`, `all incl`, `none` |
| `skyway` | `yes` / `no` / `partial` |
| `vintage_year` | Year built (used for the vintage FIT component) |
| `notes` | Free text |

## FIT matrix

Weighted toward what Jason picked on 2026-05-31:

| Category | Weight |
|---|---|
| Amenities | 28 |
| Character / Light / Views | 26 |
| Cost fit (vs $2,500 all-in) | 15 |
| Neighborhood | 12 |
| Layout (studio/1BR/1BR+den) | 10 |
| Vintage (historic OR signature-new) | 5 |
| Parking included | 4 |

Cost fit decays linearly: 1.0 at <=$2,200 all-in → 0.0 at $2,800 all-in.

## All-in cost estimate

`rent + parking(~$175 if extra) + electric(~$70–100) + internet($80)`

Tuned to Jason's current 2900 Lagoon setup: $2,100 rent + $150 parking + $150 electric + $80 internet = **$2,480**.

## Filters (drops)

A listing is dropped if any of these are true:
- Neighborhood not in `GEO_WHITELIST`
- Layout is 2BR+ (or unknown with >1 beds)
- No rent could be parsed
- All-in cost > $2,500 × 1.08 grace

## Adding a building

1. Add a row to `buildings.csv` with best-guess `listings_url` and `scraper_key=generic`
2. Run `python apartment_scraper.py --building "<name>" --debug`
3. If 0 listings, open `_debug/<name>.html` to see what rendered
4. Update `listings_url` to the actual floorplans/availability page if needed
5. If the building's platform looks like RENTCafe / Entrata / etc., add a platform-specific parser

## GitHub Pages mirror

Once the public repo exists at `W:\AI\GitHub\apartment-scrubber`, the scraper auto-copies `listings.json` and `index.html` into it after each run. The workflow in `.github/workflows/scrape.yml` runs weekly on Monday at 7am Central.

## Sister projects

- Job Radar — `W:\AI\Anthropic\projects\Job scrubber\` (jfunk9.github.io/job-scrubber)
- RFP scrubber — `W:\AI\Anthropic\projects\RFP scrubber\` (jfunk9.github.io/RFP-scrubber)
