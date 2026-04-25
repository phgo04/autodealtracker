"""
Central configuration for the CX-5 Tracker.
Update values here — do not hardcode them in other scripts.
"""

from pathlib import Path

# ── Paths ─────────────────────────────────────────────────────────────────────
PROJECT_ROOT      = Path(__file__).parent
STATE_DIR         = PROJECT_ROOT / "state"
OUTPUT_DIR        = PROJECT_ROOT / "output"
RAW_LISTINGS_FILE = STATE_DIR / "raw_listings.json"
STATE_FILE        = STATE_DIR / "listings.json"
ALERTS_FILE       = STATE_DIR / "alerts_sent.json"

# ── AutoTrader search ─────────────────────────────────────────────────────────
# AutoTrader uses a Next.js SPA. The canonical search URL for Ontario CX-5s is:
#   /cars/mazda/cx-5/reg_on/cit_toronto/
# Pagination is via ?page=N (20 results per page, 1-indexed).
# Year/km/price filters are NOT respected server-side on this URL —
# filtering is applied client-side in scraper.py after fetching each page.
SEARCH_BASE_URL = "https://www.autotrader.ca/cars/mazda/cx-5/reg_on/cit_toronto/"

# Client-side year filter applied after scraping (inclusive)
YEAR_MIN = 2024
YEAR_MAX = 2026

MAX_PAGES = 30              # Safety cap — 30 pages × 20 listings = 600 raw, ~300 after year filter

# ── Vehicle filters (from README Section 2) ───────────────────────────────────
EXCLUDED_TRIMS = ["GX"]     # Never recommend, even if priced well

USED_FILTERS = {
    2024: {"km_preferred": 35_000, "km_hard_limit": 40_000, "price_target": 34_000},
    2025: {"km_preferred": 20_000, "km_hard_limit": 30_000, "price_target": 36_500},
}

NEW_FILTERS = {
    2026: {"price_ceiling": 42_000, "strong_buy": 39_000},   # Current redesign
    2025: {"price_ceiling": 36_500, "strong_buy": 34_500},   # Clearance
}

# ── BUY NOW thresholds (from README Section 5) ────────────────────────────────
BUY_NOW = {
    "used_2024":        {"max_price": 31_500, "max_km": 25_000},
    "used_2025":        {"max_price": 35_000, "max_km": 15_000},
    "new_2026":         {"max_price": 39_000, "max_km": None},
    "new_2025_clear":   {"max_price": 34_500, "max_km": None},
}

# ── Financing assumptions (from README Section 8) ─────────────────────────────
FINANCING = {
    "down_payment":  2_500,
    "term_months":   72,
    "rate_new_low":  0.0299,    # Best Mazda promo rate
    "rate_new_high": 0.0599,    # Standard bank rate
    "rate_used_low": 0.0599,
    "rate_used_high": 0.0849,
}

# ── Depreciation curve (used in Step 6) ──────────────────────────────────────
# Based on 2024 CX-5 GS original MSRP ~$36,500 CAD.
# Update every 6 months or when market shifts significantly.
DEPRECIATION_CURVE = {
    "2024_GS": {
        "msrp": 36_500,
        "bands": [
            {"km_max": 20_000, "retain_pct": 0.85},    # Expected: ~$31,025
            {"km_max": 40_000, "retain_pct": 0.75},    # Expected: ~$27,375
            {"km_max": 60_000, "retain_pct": 0.65},    # Expected: ~$23,725
            {"km_max": 80_000, "retain_pct": 0.57},    # Expected: ~$20,805
        ],
    },
}
