"""
Central configuration for AutoDealTracker.
Add a new car to CARS to extend tracking without touching any other file.
"""

from pathlib import Path
from typing import Optional

# ── Paths ─────────────────────────────────────────────────────────────────────
PROJECT_ROOT = Path(__file__).parent
STATE_DIR    = PROJECT_ROOT / "state"
OUTPUT_DIR   = PROJECT_ROOT / "output"

MAX_PAGES = 30   # Safety cap per-car (30 pages × 20 listings = 600 raw)


def car_paths(car_key: str) -> dict:
    """Return resolved Path objects for a car's state files."""
    car_dir = STATE_DIR / car_key
    return {
        "state_dir":    car_dir,
        "raw_listings": car_dir / "raw_listings.json",
        "state_file":   car_dir / "listings.json",
        "alerts_file":  car_dir / "alerts_sent.json",
    }


# ── Car watchlist ─────────────────────────────────────────────────────────────
# Each entry drives scraper.py --car <key> and a full run_tracker.py loop.
# prompt_file: path to the Claude system prompt for this car (relative to project root).
#   Defaults to CLAUDE.md if not set. Create a car-specific prompt to get
#   properly tuned analysis (see CLAUDE.md for the CX-5 template).
CARS = {
    "cx5": {
        "label":       "Mazda CX-5",
        "search_url":  "https://www.autotrader.ca/cars/mazda/cx-5/reg_on/cit_toronto/",
        "year_min":    2024,
        "year_max":    2026,
        "prompt_file": "CLAUDE.md",
        "buy_now": {
            "used_2024":      {"max_price": 31_500, "max_km": 25_000},
            "used_2025":      {"max_price": 35_000, "max_km": 15_000},
            "new_2026":       {"max_price": 39_000, "max_km": None},
            "new_2025_clear": {"max_price": 34_500, "max_km": None},
        },
    },
    "crv": {
        "label":       "Honda CR-V",
        "search_url":  "https://www.autotrader.ca/cars/honda/cr-v/reg_on/cit_toronto/",
        "year_min":    2024,
        "year_max":    2026,
        "prompt_file": None,   # create prompts/crv.md to enable Claude analysis
        "buy_now": {
            "used_2024": {"max_price": 33_000, "max_km": 30_000},
        },
    },
}

# ── CX-5 vehicle filters (referenced by CLAUDE.md / Claude analysis) ──────────
EXCLUDED_TRIMS = ["GX"]

USED_FILTERS = {
    2024: {"km_preferred": 35_000, "km_hard_limit": 40_000, "price_target": 34_000},
    2025: {"km_preferred": 20_000, "km_hard_limit": 30_000, "price_target": 36_500},
}

NEW_FILTERS = {
    2026: {"price_ceiling": 42_000, "strong_buy": 39_000},
    2025: {"price_ceiling": 36_500, "strong_buy": 34_500},
}

# ── Financing assumptions (from CLAUDE.md Section 8) ─────────────────────────
FINANCING = {
    "down_payment":   2_500,
    "term_months":    72,
    "rate_new_low":   0.0299,
    "rate_new_high":  0.0599,
    "rate_used_low":  0.0599,
    "rate_used_high": 0.0849,
}

# ── Depreciation curve ────────────────────────────────────────────────────────
# Based on Ontario market data. Update every 6 months or when market shifts.
# Keys follow the pattern "{year}_{base_trim}" — e.g. "2024_GS", "2025_GS".
# expected_value() tries the exact trim first, then falls back to the first
# word of the trim string so "GS Kuro" and "GS AWD" resolve to "GS".
DEPRECIATION_CURVE = {
    "2024_GS": {
        "msrp": 36_500,
        "bands": [
            {"km_max": 20_000, "retain_pct": 0.85},   # ~$31,025
            {"km_max": 40_000, "retain_pct": 0.75},   # ~$27,375
            {"km_max": 60_000, "retain_pct": 0.65},   # ~$23,725
            {"km_max": 80_000, "retain_pct": 0.57},   # ~$20,805
        ],
    },
    "2024_GS Kuro": {
        "msrp": 37_800,
        "bands": [
            {"km_max": 20_000, "retain_pct": 0.85},
            {"km_max": 40_000, "retain_pct": 0.75},
            {"km_max": 60_000, "retain_pct": 0.65},
            {"km_max": 80_000, "retain_pct": 0.57},
        ],
    },
    "2025_GS": {
        "msrp": 38_200,
        "bands": [
            {"km_max": 10_000, "retain_pct": 0.93},   # near-new
            {"km_max": 20_000, "retain_pct": 0.88},
            {"km_max": 30_000, "retain_pct": 0.82},
        ],
    },
}


def expected_value(year, trim: str, km) -> Optional[int]:
    """
    Return the expected fair-market value (CAD) for a listing based on the
    depreciation curve, or None if no matching curve entry exists.

    Lookup order:
      1. f"{year}_{trim}"           exact match  (e.g. "2024_GS")
      2. f"{year}_{trim.split()[0]}" base-trim   (e.g. "2024_GS" from "GS Kuro")
    """
    if year is None or km is None or trim is None:
        return None

    trim_str = str(trim).strip()
    keys_to_try = [f"{year}_{trim_str}"]
    base = trim_str.split()[0] if trim_str else ""
    if base and base != trim_str:
        keys_to_try.append(f"{year}_{base}")

    curve = None
    for key in keys_to_try:
        curve = DEPRECIATION_CURVE.get(key)
        if curve:
            break

    if curve is None:
        return None

    for band in curve["bands"]:
        if km <= band["km_max"]:
            return round(curve["msrp"] * band["retain_pct"])

    return None   # km exceeds all bands — no reliable estimate


def depreciation_delta(listing_price, expected) -> Optional[float]:
    """
    Return how far the listing price deviates from expected value as a
    percentage (positive = overpriced, negative = underpriced), or None
    if either value is unavailable.
    """
    if expected is None or listing_price is None:
        return None
    return round(((listing_price - expected) / expected) * 100, 1)
