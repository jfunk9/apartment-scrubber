"""
Apartment Radar — Downtown Minneapolis 1BR / Loft / Den scraper.

Same shape as Job Radar / RFP Scrubber:
  buildings.csv  -->  apartment_scraper.py  -->  listings.json  -->  index.html

Usage:
  python apartment_scraper.py                  # scrape every building, write listings.json
  python apartment_scraper.py --building "Encore"   # single building (debug)
  python apartment_scraper.py --dry-run        # parse but don't write listings.json

Note: building-listing pages are JS-heavy and change layouts often. The generic
scraper extracts unit cards by regex over rendered HTML. When a building returns
0 listings, inspect the saved debug HTML, then either:
  - update listings_url in buildings.csv to the actual /availability or /floorplans page
  - add a platform-specific parser (rentcafe, entrata, etc.)
"""

import argparse
import csv
import json
import os
import re
import sys
import shutil
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

# Optional deps; degrade gracefully so this file always py_compiles
try:
    import requests
    from bs4 import BeautifulSoup
except Exception:
    requests = None
    BeautifulSoup = None

try:
    from playwright.sync_api import sync_playwright
except Exception:
    sync_playwright = None


# --------------------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------------------

HERE = Path(__file__).parent
BUILDINGS_CSV = HERE / "buildings.csv"
OUTPUT_JSON = HERE / "listings.json"
DEBUG_DIR = HERE / "_debug"

# Optional: copy outputs into a GitHub Pages repo at the end of run()
# (leave empty until the repo exists)
GITHUB_PAGES_REPO = Path(r"W:\AI\GitHub\apartment-scrubber")

# Total-cost ceiling Jason set
ALL_IN_CEILING = 2500

# Unit type targets (what we accept)
ACCEPTED_LAYOUTS = {"studio_loft", "1br", "1br_den"}

# Geographic whitelist (neighborhood from CSV must be one of these)
GEO_WHITELIST = {"North Loop", "Mill District", "Loring Park", "Elliot Park", "Core Downtown"}


# --------------------------------------------------------------------------------------
# FIT matrix — Jason's weighted apartment scoring
# --------------------------------------------------------------------------------------
#
# Picked by Jason on 2026-05-31:
#   - Amenities (Lakehaus-style)         <- top driver
#   - Light / Views / Character          <- top driver
#
# The two top categories total 54%, mirroring the "62% top-3" logic of the job scraper.
# Cost, neighborhood, layout, vintage, parking fill the remainder.

FIT_MATRIX = {
    "amenities": {
        "weight": 26,
        "source": "building",   # buildings.csv amenity_tier (1-5)
    },
    "character": {
        "weight": 20,
        "source": "building",   # buildings.csv character_tier (1-5) — architectural character only
    },
    "cost_fit": {
        "weight": 15,
        "source": "computed",   # how far under $2500 all-in
    },
    "west_view_potential": {
        "weight": 12,
        "source": "building",   # buildings.csv west_view_potential (1-5) — west-facing high-floor open sightline
    },
    "neighborhood": {
        "weight": 10,
        "source": "computed",   # ranked Jason preference
    },
    "layout_fit": {
        "weight": 10,
        "source": "computed",   # studio_loft / 1br / 1br_den preferred
    },
    "vintage": {
        "weight": 4,
        "source": "computed",   # bonus for historic OR signature-new
    },
    "parking_included": {
        "weight": 3,
        "source": "building",   # bonus if parking is bundled
    },
}

# Neighborhood preference (Jason picked all 4 Downtown areas; these are softer prefs)
NEIGHBORHOOD_RANK = {
    "North Loop": 1.0,
    "Mill District": 0.95,
    "Loring Park": 0.85,
    "Elliot Park": 0.80,
    "Core Downtown": 0.75,
}


# --------------------------------------------------------------------------------------
# Layout detection
# --------------------------------------------------------------------------------------

LAYOUT_PATTERNS = [
    (re.compile(r"\b(?:1\s*bed(?:room)?\s*\+\s*den|1\s*br\s*\+\s*den|jr\.?\s*2\s*br)\b", re.I), "1br_den"),
    (re.compile(r"\b(?:1\s*bed(?:room)?|1\s*br|one\s*bed)\b", re.I), "1br"),
    (re.compile(r"\b(?:loft|studio)\b", re.I), "studio_loft"),
    (re.compile(r"\b(?:2\s*bed(?:room)?|2\s*br|two\s*bed)\b", re.I), "2br_plus"),
    (re.compile(r"\b(?:3\s*bed(?:room)?|3\s*br)\b", re.I), "2br_plus"),
]

EXCLUDE_LAYOUT = {"2br_plus"}


def classify_layout(text: str) -> str:
    """Return one of: studio_loft, 1br, 1br_den, 2br_plus, unknown."""
    if not text:
        return "unknown"
    for pat, label in LAYOUT_PATTERNS:
        if pat.search(text):
            return label
    return "unknown"


# --------------------------------------------------------------------------------------
# Rent / sqft / bed extraction
# --------------------------------------------------------------------------------------

# Matches "$1,950" / "$1950" / "from $2,100" / "starting at $2,250"
RENT_PATTERNS = [
    re.compile(r"\$\s*([0-9]{1,2}[,.]?[0-9]{3})\b"),
]
SQFT_PATTERN = re.compile(r"([0-9]{3,5})\s*(?:sq\.?\s*ft|sqft|square\s*f(?:ee|oo)?t)\b", re.I)
BED_PATTERN = re.compile(r"(\d+(?:\.\d+)?)\s*(?:bed|br|bd)\b", re.I)
BATH_PATTERN = re.compile(r"(\d+(?:\.\d+)?)\s*(?:bath|ba)\b", re.I)


def extract_rent(text: str):
    if not text:
        return None
    best = None
    for pat in RENT_PATTERNS:
        for m in pat.finditer(text):
            try:
                val = int(m.group(1).replace(",", "").replace(".", ""))
                if 500 <= val <= 15000:
                    if best is None or val < best:
                        best = val
            except Exception:
                continue
    return best


def extract_sqft(text: str):
    if not text:
        return None
    m = SQFT_PATTERN.search(text)
    return int(m.group(1)) if m else None


def extract_beds(text: str):
    if not text:
        return None
    m = BED_PATTERN.search(text)
    if not m:
        if re.search(r"\bstudio\b", text, re.I):
            return 0
        return None
    try:
        return float(m.group(1))
    except Exception:
        return None


# --------------------------------------------------------------------------------------
# Fetchers
# --------------------------------------------------------------------------------------

def fetch_static(url: str, timeout: int = 20):
    if requests is None:
        return None
    try:
        r = requests.get(
            url,
            timeout=timeout,
            headers={
                "User-Agent": "Mozilla/5.0 (compatible; ApartmentRadar/1.0; +https://github.com/jfunk9)",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            },
        )
        if r.status_code == 200:
            return r.text
    except Exception as e:
        print(f"  static fetch failed: {e}")
    return None


def fetch_js(url: str, wait_ms: int = 4500, scroll: bool = True):
    if sync_playwright is None:
        return None
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            ctx = browser.new_context(user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ))
            page = ctx.new_page()
            page.goto(url, wait_until="networkidle", timeout=30000)
            page.wait_for_timeout(wait_ms)
            if scroll:
                # Scroll to load lazy unit cards
                for _ in range(4):
                    page.mouse.wheel(0, 2000)
                    page.wait_for_timeout(600)
            html = page.content()
            browser.close()
            return html
    except Exception as e:
        print(f"  playwright fetch failed: {e}")
        return None


# --------------------------------------------------------------------------------------
# Generic unit-card extraction
# --------------------------------------------------------------------------------------
#
# Strategy: split the rendered HTML into "card-like" chunks (li, div with class
# containing floorplan/unit/card/result). For each chunk, try to pull a name +
# layout + rent + sqft. Anything that fails minimum requirements is dropped.

CARD_CLASS_HINTS = re.compile(
    r"(?:floor[-_ ]?plan|floorplan|unit|listing|apartment|model|residence|card|result|tile)",
    re.I,
)


def find_unit_chunks(html: str):
    """Return raw text chunks likely to describe a unit. Best-effort."""
    if not html or BeautifulSoup is None:
        return []
    soup = BeautifulSoup(html, "html.parser")
    chunks = []

    candidates = soup.find_all(
        ["div", "li", "article", "section"],
        class_=CARD_CLASS_HINTS,
    )
    for el in candidates:
        text = el.get_text(" ", strip=True)
        if len(text) < 20 or len(text) > 2000:
            continue
        chunks.append(text)

    # Dedup near-duplicates
    seen = set()
    out = []
    for t in chunks:
        key = re.sub(r"\s+", " ", t)[:140].lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(t)
    return out


def parse_unit_chunk(chunk: str):
    layout = classify_layout(chunk)
    if layout in EXCLUDE_LAYOUT:
        return None
    rent = extract_rent(chunk)
    sqft = extract_sqft(chunk)
    beds = extract_beds(chunk)
    if rent is None and sqft is None:
        return None  # not actually a unit card
    # Pull a plausible name = first short phrase that isn't pure numbers
    name = None
    for token in re.split(r"\.|\n|\|", chunk):
        token = token.strip()
        if 3 <= len(token) <= 60 and not token.replace(",", "").replace("$", "").isdigit():
            name = token
            break
    return {
        "unit_name": name,
        "layout": layout,
        "rent": rent,
        "sqft": sqft,
        "beds": beds,
    }


# --------------------------------------------------------------------------------------
# FIT scoring
# --------------------------------------------------------------------------------------

def compute_fit(building: dict, unit: dict) -> dict:
    """Return {score: int, breakdown: {category: int}}"""
    breakdown = {}

    # Amenities (building-driven, 1-5)
    amen = int(building.get("amenity_tier") or 3)
    breakdown["amenities"] = round(FIT_MATRIX["amenities"]["weight"] * (amen / 5.0), 1)

    # Character (building-driven, 1-5)
    char = int(building.get("character_tier") or 3)
    breakdown["character"] = round(FIT_MATRIX["character"]["weight"] * (char / 5.0), 1)

    # West view potential (building-driven, 1-5)
    try:
        wv = int(building.get("west_view_potential") or 3)
    except Exception:
        wv = 3
    breakdown["west_view_potential"] = round(FIT_MATRIX["west_view_potential"]["weight"] * (wv / 5.0), 1)

    # Cost fit: 1.0 if all-in <= $2200, decays to 0 at $2800
    all_in = unit.get("all_in_cost") or 9999
    if all_in <= 2200:
        cost_pct = 1.0
    elif all_in >= 2800:
        cost_pct = 0.0
    else:
        cost_pct = 1.0 - (all_in - 2200) / 600.0
    breakdown["cost_fit"] = round(FIT_MATRIX["cost_fit"]["weight"] * cost_pct, 1)

    # Neighborhood
    nb = building.get("neighborhood", "")
    nb_pct = NEIGHBORHOOD_RANK.get(nb, 0.6)
    breakdown["neighborhood"] = round(FIT_MATRIX["neighborhood"]["weight"] * nb_pct, 1)

    # Layout
    layout = unit.get("layout", "unknown")
    if layout == "1br_den":
        layout_pct = 1.0
    elif layout in ("1br", "studio_loft"):
        layout_pct = 0.85
    else:
        layout_pct = 0.4
    breakdown["layout_fit"] = round(FIT_MATRIX["layout_fit"]["weight"] * layout_pct, 1)

    # Vintage: historic conversion (<1950) or signature-new (>=2020) get full credit
    vintage = building.get("vintage_year")
    try:
        v = int(vintage) if vintage else None
    except Exception:
        v = None
    if v is None:
        v_pct = 0.5
    elif v < 1950 or v >= 2020:
        v_pct = 1.0
    elif 2010 <= v < 2020:
        v_pct = 0.75
    else:
        v_pct = 0.5
    breakdown["vintage"] = round(FIT_MATRIX["vintage"]["weight"] * v_pct, 1)

    # Parking
    parking = (building.get("parking_included") or "").lower()
    if parking in ("yes", "included", "incl"):
        p_pct = 1.0
    elif parking == "no":
        p_pct = 0.0
    else:
        p_pct = 0.3
    breakdown["parking_included"] = round(FIT_MATRIX["parking_included"]["weight"] * p_pct, 1)

    score = int(round(sum(breakdown.values())))
    return {"score": min(100, score), "breakdown": breakdown}


# --------------------------------------------------------------------------------------
# All-in cost estimation
# --------------------------------------------------------------------------------------

def estimate_all_in(building: dict, rent: int) -> int:
    """Rent + assumed parking + assumed utilities + assumed internet."""
    if rent is None:
        return None
    total = rent

    parking = (building.get("parking_included") or "").lower()
    if parking == "no":
        total += 0  # building has no parking (rare downtown — usually street/garage extra)
    elif parking in ("yes", "included", "incl"):
        total += 0
    else:
        # Assume ~$175/mo for downtown secured parking
        total += 175

    utils = (building.get("utilities_note") or "").lower()
    if "all" in utils:
        electric = 0
    elif "water" in utils or "trash" in utils:
        electric = 70   # just electric/gas
    else:
        electric = 100  # everything separate

    internet = 80

    return total + electric + internet


# --------------------------------------------------------------------------------------
# Per-building scrape
# --------------------------------------------------------------------------------------

def scrape_building(row: dict, debug: bool = False):
    name = row["name"]
    url = row.get("listings_url") or row.get("website")
    if not url:
        print(f"  [skip] {name}: no URL")
        return []

    print(f"[{row.get('priority','??')}] {name} — {url}")

    html = fetch_js(url) if sync_playwright else fetch_static(url)
    if not html:
        print(f"  [warn] no HTML returned for {name}")
        return []

    if debug:
        DEBUG_DIR.mkdir(exist_ok=True)
        slug = re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")
        (DEBUG_DIR / f"{slug}.html").write_text(html, encoding="utf-8", errors="ignore")

    chunks = find_unit_chunks(html)
    units = []
    for ch in chunks:
        u = parse_unit_chunk(ch)
        if not u:
            continue
        units.append(u)

    # Apply filters
    kept = []
    for u in units:
        if u["layout"] in EXCLUDE_LAYOUT:
            continue
        if u["layout"] == "unknown" and (u["beds"] is None or u["beds"] > 1):
            # be conservative on unknowns — must look small
            continue
        if u["rent"] is None:
            continue

        all_in = estimate_all_in(row, u["rent"])
        if all_in and all_in > ALL_IN_CEILING + 200:  # 8% grace
            continue

        u["all_in_cost"] = all_in
        fit = compute_fit(row, u)
        u["fit"] = fit["score"]
        u["fit_breakdown"] = fit["breakdown"]
        u["building"] = name
        u["neighborhood"] = row.get("neighborhood")
        u["address"] = row.get("address")
        u["url"] = url
        u["amenity_tier"] = row.get("amenity_tier")
        u["character_tier"] = row.get("character_tier")
        u["parking_included"] = row.get("parking_included")
        u["utilities_note"] = row.get("utilities_note")
        u["vintage_year"] = row.get("vintage_year")
        u["west_view_potential"] = row.get("west_view_potential")
        u["building_notes"] = row.get("notes")
        u["skyway"] = row.get("skyway")
        u["jason_favorite"] = (row.get("jason_favorite") or "").strip().lower() == "yes"
        u["unit_filter"] = (row.get("unit_filter") or "").strip()
        kept.append(u)

    print(f"  found {len(units)} candidate cards, kept {len(kept)} after filters")
    return kept


# --------------------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------------------

def load_buildings():
    if not BUILDINGS_CSV.exists():
        print(f"ERROR: {BUILDINGS_CSV} not found")
        sys.exit(1)
    with open(BUILDINGS_CSV, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def run(building_filter: str = None, dry_run: bool = False, debug: bool = False):
    buildings = load_buildings()
    if building_filter:
        bf = building_filter.lower()
        buildings = [b for b in buildings if bf in b["name"].lower()]
        if not buildings:
            print(f"No building matched '{building_filter}'")
            return

    all_listings = []
    for row in buildings:
        if row.get("neighborhood") not in GEO_WHITELIST:
            continue
        try:
            listings = scrape_building(row, debug=debug)
            all_listings.extend(listings)
        except Exception as e:
            print(f"  [error] {row.get('name')}: {e}")
            continue
        time.sleep(1.0)  # be polite

    # Sort by FIT desc
    all_listings.sort(key=lambda u: u.get("fit", 0), reverse=True)

    output = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "ceiling_all_in": ALL_IN_CEILING,
        "fit_matrix": {k: v["weight"] for k, v in FIT_MATRIX.items()},
        "neighborhood_rank": NEIGHBORHOOD_RANK,
        "buildings_scanned": len(buildings),
        "listings": all_listings,
    }

    if dry_run:
        print(json.dumps(output, indent=2)[:2000])
        return

    OUTPUT_JSON.write_text(json.dumps(output, indent=2), encoding="utf-8")
    print(f"\nWrote {len(all_listings)} listings -> {OUTPUT_JSON}")

    # Mirror to GitHub Pages repo if it exists
    if GITHUB_PAGES_REPO.exists():
        try:
            shutil.copy2(OUTPUT_JSON, GITHUB_PAGES_REPO / "listings.json")
            index = HERE / "index.html"
            if index.exists():
                shutil.copy2(index, GITHUB_PAGES_REPO / "index.html")
            print(f"Mirrored to {GITHUB_PAGES_REPO}")
        except Exception as e:
            print(f"  [warn] failed to mirror to GitHub repo: {e}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--building", help="Substring match a single building name")
    ap.add_argument("--dry-run", action="store_true", help="Print JSON, don't write")
    ap.add_argument("--debug", action="store_true", help="Save rendered HTML to _debug/")
    args = ap.parse_args()
    run(building_filter=args.building, dry_run=args.dry_run, debug=args.debug)


if __name__ == "__main__":
    main()
e:
            print(f"  [warn] failed to mirror to GitHub repo: {e}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--building", help="Substring match a single building name")
    ap.add_argument("--dry-run", action="store_true", help="Print JSON, don't write")
    ap.add_argument("--debug", action="store_true", help="Save rendered HTML to _debug/")
    args = ap.parse_args()
    run(building_filter=args.building, dry_run=args.dry_run, debug=args.debug)


if __name__ == "__main__":
    main()
