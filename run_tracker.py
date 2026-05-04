#!/usr/bin/env python3
"""
AutoDealTracker — automated analysis pipeline.
Loops over all cars in CARS config, calls Claude API per car, saves reports, sends email.
"""

import json
import os
import re
import smtplib
import sys
import time
from collections import defaultdict
from datetime import date, datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from typing import Optional

import anthropic
from dotenv import load_dotenv

from alerts import check_alerts
from config import (
    CARS,
    OUTPUT_DIR,
    USED_FILTERS,
    car_paths,
    depreciation_delta,
    expected_value,
    is_excluded_trim,
)

load_dotenv()

TODAY = date.today().isoformat()


# ── State management ──────────────────────────────────────────────────────────

def load_json(path: Path, default):
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return default


def update_state(raw_listings: list, prior_state: dict) -> dict:
    """
    Merge today's scraped listings into the persistent state.
    Tracks price_history, first_seen, last_seen per listing ID.
    """
    listings_by_id = prior_state.get("listings", {})

    current_ids = set()
    for listing in raw_listings:
        lid = listing.get("id") or listing.get("listing_id")
        if not lid:
            continue
        current_ids.add(lid)

        existing = listings_by_id.get(lid, {})
        current_price = listing.get("price")

        price_history = existing.get("price_history", [])
        if not price_history or (current_price and price_history[-1]["price"] != current_price):
            price_history.append({"date": TODAY, "price": current_price})

        listings_by_id[lid] = {
            **existing,
            **listing,
            "first_seen":    existing.get("first_seen", TODAY),
            "last_seen":     TODAY,
            "price_history": price_history,
        }

    return {"listings": listings_by_id}


# ── State summary (Step 12) ──────────────────────────────────────────────────

def build_state_summary(raw_listings: list, prior_state: dict) -> str:
    """
    Build a minimal prior-state summary for the Claude API call.

    Replaces json.dumps(prior_state) (~49K tokens) with a compact per-listing
    dict containing only what Claude uses: status, prev_price, first_seen.
    Inactive listings, full price_history arrays, dealer_stats, and sparkline
    data are all dropped — they are not needed by the report renderer.

    Estimated output: ~6–8K tokens regardless of how long the tracker has run.
    """
    prior_listings = prior_state.get("listings", {})
    summary: dict = {}

    for listing in raw_listings:
        lid = listing.get("id") or listing.get("listing_id")
        if not lid:
            continue

        prev = prior_listings.get(lid)
        if not prev:
            summary[lid] = {"status": "new"}
            continue

        ph         = prev.get("price_history", [])
        prev_price = ph[-1].get("price") if ph else prev.get("price")
        curr_price = listing.get("price")

        if prev_price and curr_price:
            if curr_price < prev_price:
                status = "price_drop"
            elif curr_price > prev_price:
                status = "price_increase"
            else:
                status = "same"
        else:
            status = "same"

        summary[lid] = {
            "status":     status,
            "prev_price": prev_price,
            "first_seen": prev.get("first_seen"),
        }

    return json.dumps(summary, ensure_ascii=False)


# ── Depreciation benchmark ───────────────────────────────────────────────────

def annotate_depreciation(raw_listings: list) -> list:
    """
    Enrich each listing with two fields before it is sent to the Claude API:
      expected_value   — fair-market price for the year/trim/km band (CAD int or None)
      vs_expected_pct  — % deviation from expected (positive = overpriced, None if no curve)

    Listings with no matching depreciation curve entry (e.g. new cars, unknown trim)
    receive None for both fields — Claude is instructed to show '—' in that case.
    """
    for listing in raw_listings:
        exp   = expected_value(listing.get("year"), listing.get("trim"), listing.get("km"))
        delta = depreciation_delta(listing.get("price"), exp)
        listing["expected_value"]  = exp
        listing["vs_expected_pct"] = delta
    return raw_listings


# ── Dealer reputation layer ───────────────────────────────────────────────────

def _trim_similar(t1, t2) -> bool:
    """True if two trim strings are the same (case-insensitive) or either is unknown."""
    if not t1 or not t2 or t1 == "Not listed" or t2 == "Not listed":
        return True   # can't distinguish — treat as potentially the same
    return t1.strip().lower() == t2.strip().lower()


def _km_similar(km1, km2, tolerance: int = 2_000) -> bool:
    """True if two mileage values are within tolerance km of each other."""
    if km1 is None or km2 is None:
        return True
    return abs(km1 - km2) <= tolerance


def update_dealer_stats(listings_by_id: dict, today: str) -> dict:
    """
    Compute per-dealer aggregate stats from the full listings state.
    Returns a dealer_stats dict ready to be stored under state['dealer_stats'].

    Fields per dealer:
      listings_seen      — total unique listings ever tracked
      avg_days_on_lot    — average (last_seen - first_seen) in days
      price_drop_rate    — fraction of listings that had at least one price drop
      avg_price_drop_pct — average % drop among listings that did drop
      relists_detected   — listings that vanished then reappeared with a new ID
    """
    # Accumulate raw data per dealer
    by_dealer: dict = defaultdict(lambda: {
        "listing_ids":    set(),
        "days_on_lot":    [],
        "had_drop":       [],   # bool per listing with 2+ price points
        "drop_pcts":      [],   # % drop for listings that did drop
    })

    for lid, listing in listings_by_id.items():
        dealer = (listing.get("dealer") or "").strip()
        if not dealer or dealer == "Not listed":
            continue

        d = by_dealer[dealer]
        d["listing_ids"].add(lid)

        # Days on lot
        first_str = listing.get("first_seen")
        last_str  = listing.get("last_seen")
        if first_str and last_str:
            try:
                days = (datetime.fromisoformat(last_str) - datetime.fromisoformat(first_str)).days
                d["days_on_lot"].append(days)
            except ValueError:
                pass

        # Price drop analysis
        ph = listing.get("price_history", [])
        if len(ph) >= 2:
            p0 = ph[0].get("price")
            p1 = ph[-1].get("price")
            if p0 and p1:
                if p1 < p0:
                    d["had_drop"].append(True)
                    d["drop_pcts"].append(round((p0 - p1) / p0 * 100, 1))
                else:
                    d["had_drop"].append(False)

    # Relist detection
    # A relist = a listing that was NOT seen this run (last_seen < today) that
    # matches a currently-active listing (last_seen == today) at the same dealer
    # with the same year, similar trim, and mileage within ±2,000 km.
    current_by_dealer: dict = defaultdict(list)
    old_by_dealer:     dict = defaultdict(list)

    for lid, listing in listings_by_id.items():
        dealer = (listing.get("dealer") or "").strip()
        if not dealer or dealer == "Not listed":
            continue
        if listing.get("last_seen") == today:
            current_by_dealer[dealer].append(listing)
        elif listing.get("last_seen") and listing.get("last_seen") < today:
            old_by_dealer[dealer].append(listing)

    relist_counts: dict = defaultdict(int)
    for dealer, old_listings in old_by_dealer.items():
        current_listings = current_by_dealer.get(dealer, [])
        for old in old_listings:
            for curr in current_listings:
                if (old.get("year") == curr.get("year")
                        and _trim_similar(old.get("trim"), curr.get("trim"))
                        and _km_similar(old.get("km"), curr.get("km"))
                        and old.get("listing_id") != curr.get("listing_id")):
                    relist_counts[dealer] += 1
                    break   # count each old listing once

    # Build final stats dict
    dealer_stats: dict = {}
    for dealer, d in by_dealer.items():
        n_seen    = len(d["listing_ids"])
        avg_days  = (round(sum(d["days_on_lot"]) / len(d["days_on_lot"]), 1)
                     if d["days_on_lot"] else None)
        n_eligible  = len(d["had_drop"])
        drop_rate   = round(sum(d["had_drop"]) / n_eligible, 2) if n_eligible else 0.0
        avg_drop    = (round(sum(d["drop_pcts"]) / len(d["drop_pcts"]), 1)
                       if d["drop_pcts"] else 0.0)

        dealer_stats[dealer] = {
            "listings_seen":      n_seen,
            "avg_days_on_lot":    avg_days,
            "price_drop_rate":    drop_rate,
            "avg_price_drop_pct": avg_drop,
            "relists_detected":   relist_counts.get(dealer, 0),
        }

    return dealer_stats


# ── Prompt-input compression (RCA §12.1 — Path A) ─────────────────────────────
# The 5-2 dispatch run logged 88,882 input tokens against a Tier-1 50K ITPM
# ceiling. Step 12 already shrank prior_state; the remaining bloat is the
# verbatim raw_listings.json. Three coordinated functions below cut input from
# ~88K → ~22K (56% headroom under Tier 1):
#
#   select_candidates  — keep ~50 best-value listings, hard-filter the rest
#   thin_for_claude    — explicit allowlist of fields per listing
#   market_digest      — pre-compute Section 9G stats from the FULL set
#
# Section H (Dealer Intelligence) and per-listing sparklines are no longer
# emitted by Claude. They are rendered post-call by render_dealer_table /
# render_sparklines and substituted into the HTML by post_process_html.
# Reason: Tier-1 OTPM = 10K tokens/min; bumping max_tokens alone exposes a
# structural OTPM 429. See engineer_response.md §9 for the full Option-4
# walk-through.

def _classify_tier(listing: dict) -> Optional[str]:
    """Return one of used_2024 / used_2025 / new_2026 / new_2025 / None."""
    cond = (listing.get("condition") or "").lower()
    is_new = cond in ("new", "demo") or listing.get("is_new", False)
    y = listing.get("year")
    if not is_new and y == 2024: return "used_2024"
    if not is_new and y == 2025: return "used_2025"
    if is_new and y == 2026:     return "new_2026"
    if is_new and y == 2025:     return "new_2025"
    # 2024 demos, 2026 used, anything else: out of spec — caller drops it.
    return None


# Slot allocation for select_candidates. Sums to 50. Tuned to CX-5 inventory mix
# (177 new_2026 / 88 used_2025 / 49 used_2024 / 5 new_2025 in a typical run).
# new_2026 gets the most slots because it's both the largest population and the
# primary new target; new_2025 gets a small dedicated allocation so clearance
# units cannot be crowded out. Unfilled slots in any tier (e.g. only 5 new_2025
# exist) are redistributed to the other tiers in priority order.
_SLOT_ALLOCATION = {
    "used_2024": 15,
    "used_2025": 10,
    "new_2026":  20,
    "new_2025":   5,
}
_TIER_PRIORITY = ["used_2024", "new_2026", "used_2025", "new_2025"]


def _hard_filter(listing: dict, tier: str) -> bool:
    """True if listing passes published hard limits (CLAUDE.md §2)."""
    if is_excluded_trim(listing):
        return False
    price = listing.get("price")
    km = listing.get("km")
    if price is None:
        return False
    if tier in ("used_2024", "used_2025"):
        year = 2024 if tier == "used_2024" else 2025
        limits = USED_FILTERS.get(year, {})
        if km is not None and km > limits.get("km_hard_limit", 9_999_999):
            return False
        # No price hard-limit on used — Claude flags overpriced via vs_expected_pct.
        return True
    if tier in ("new_2026", "new_2025"):
        # No price hard-filter on new tiers. The CLAUDE.md `price_ceiling` is
        # buyer-guidance ("I won't pay more than this"), not a spec violation.
        # Filtering kills the buyer's view of market context — verified in V2
        # smoke test: hard-filtering new_2026 at $42K dropped 174/177 listings,
        # leaving 3 candidates. _value_score (cohort-median-relative) ranks them
        # so the cheapest naturally rise to the top. Trim filter still applies.
        return True
    return False


def _value_score(listing: dict, cohort_median_price: Optional[float]) -> float:
    """
    Return a comparable value score across curve-matched and non-curve listings.
    Lower = better deal (sort ascending). Curve-matched listings use their
    vs_expected_pct directly (e.g. -8.0 = 8% underpriced); non-curve listings
    use % deviation from their (year, condition) cohort median price.
    """
    vs = listing.get("vs_expected_pct")
    if vs is not None:
        return float(vs)
    price = listing.get("price")
    if price is None or not cohort_median_price:
        return 0.0
    return ((price - cohort_median_price) / cohort_median_price) * 100.0


def select_candidates(raw_listings: list, max_n: int = 50) -> list:
    """
    Pre-screen raw listings down to ~max_n best-value candidates for Claude.

    Pipeline:
      1. Group listings by tier; drop anything outside the four CX-5 tiers.
      2. Apply hard filters per tier (excluded trim, km hard-limit on used,
         price ceiling on new). Per RCA §11.2 missing trim is NOT auto-excluded.
      3. Compute (year, condition) cohort medians for non-curve listings.
      4. Score each listing via _value_score (negative = underpriced).
      5. Take top-K per tier per _SLOT_ALLOCATION; redistribute unfilled slots
         in _TIER_PRIORITY order so the cap is reached when supply allows.

    The pre-screen narrows what Claude sees — but Section 9G (Market Snapshot)
    is computed from the full raw_listings via market_digest(), so headline
    market stats remain accurate. Section 9D (Avoid) operates on candidates
    only (see CLAUDE.md §9D note). Section H is rendered post-call from full
    dealer_stats.
    """
    # 1+2: group + hard-filter
    by_tier: dict = defaultdict(list)
    for l in raw_listings:
        t = _classify_tier(l)
        if t and _hard_filter(l, t):
            by_tier[t].append(l)

    # 3: cohort medians, keyed by tier
    cohort_median: dict = {}
    for tier, group in by_tier.items():
        prices = sorted(l["price"] for l in group if l.get("price") is not None)
        if prices:
            cohort_median[tier] = prices[len(prices) // 2]

    # 4: rank within each tier
    for tier, group in by_tier.items():
        group.sort(key=lambda l: _value_score(l, cohort_median.get(tier)))

    # 5: allocate slots, redistribute unfilled
    selected: list = []
    remaining_slots = dict(_SLOT_ALLOCATION)
    # First pass: take min(slot, available) from each tier
    for tier in _TIER_PRIORITY:
        slot = remaining_slots[tier]
        taken = by_tier.get(tier, [])[:slot]
        selected.extend(taken)
        remaining_slots[tier] -= len(taken)
        # Update what's left for this tier (for redistribution pass)
        by_tier[tier] = by_tier.get(tier, [])[slot:]
    # Redistribute unfilled slots (sum of remaining_slots) in priority order
    leftover = sum(remaining_slots.values())
    if leftover > 0:
        for tier in _TIER_PRIORITY:
            if leftover <= 0:
                break
            extras = by_tier.get(tier, [])[:leftover]
            selected.extend(extras)
            leftover -= len(extras)

    # Final cap (defensive — should already be ≤ max_n)
    return selected[:max_n]


# Fields Claude reads when producing the report. Anything not in this allowlist
# is stripped to keep prompt size bounded if the scraper later starts emitting
# `description`, `photos`, etc. (the original RCA §12.1 estimate assumed these
# existed; current scraper emits only the 11 fields below).
_CLAUDE_FIELDS = frozenset({
    "listing_id", "url", "year", "trim", "condition",
    "price", "km", "dealer", "location", "is_cpo",
    "expected_value", "vs_expected_pct",
})


def thin_for_claude(listing: dict) -> dict:
    """Return a copy of the listing with only the fields Claude needs."""
    return {k: v for k, v in listing.items() if k in _CLAUDE_FIELDS}


def market_digest(raw_listings: list) -> str:
    """
    Pre-computed §9G Market Snapshot stats from the FULL listing set.

    Returns a small JSON-formatted block (~0.5K tokens). Replaces having Claude
    derive averages from raw rows — which only works if Claude sees all rows.
    select_candidates trims the rows Claude sees to ~50, so this digest is the
    only authoritative source for market-wide stats post-§12.1.
    """
    by_cohort: dict = defaultdict(list)
    for l in raw_listings:
        cond = (l.get("condition") or "").lower()
        is_new = cond in ("new", "demo") or l.get("is_new", False)
        key = f"{'new' if is_new else 'used'}_{l.get('year')}"
        if l.get("price") is not None:
            by_cohort[key].append((l["price"], l.get("km")))

    digest: dict = {}
    for cohort, rows in by_cohort.items():
        prices = sorted(p for p, _ in rows)
        kms    = [k for _, k in rows if k is not None]
        digest[cohort] = {
            "count":        len(rows),
            "price_min":    prices[0],
            "price_max":    prices[-1],
            "price_mean":   round(sum(prices) / len(prices)),
            "price_median": prices[len(prices) // 2],
            "km_mean":      round(sum(kms) / len(kms)) if kms else None,
        }
    digest["_total_listings_in_market"] = len(raw_listings)
    return json.dumps(digest, ensure_ascii=False)


# ── Post-call HTML rendering (RCA §12 — Option 4) ─────────────────────────────
# Section H Dealer Intelligence and per-listing sparklines are rendered here in
# Python rather than emitted by Claude. Reason: Tier-1 OTPM = 10K tokens/min;
# the §H table at 88+ dealers and the sparkline scaffolding are the two largest
# variable contributors to natural output size and would push us past OTPM
# regardless of max_tokens. Claude emits placeholders and we substitute.

_DEALER_TABLE_PLACEHOLDER = "<!-- DEALER_TABLE -->"
_SPARKLINE_PLACEHOLDER_FMT = "<!-- SPARKLINE:{listing_id} -->"
_CHART_JS_CDN = (
    '<script src="https://cdn.jsdelivr.net/npm/chart.js@4/'
    'dist/chart.umd.min.js"></script>'
)


def _drop_label(rate: float) -> tuple[str, str]:
    """Return (text, css_color) for the price_drop_rate column in §H."""
    if rate > 0.50:
        return "Yes", "#22c55e"   # green
    if rate >= 0.20:
        return "Sometimes", "#f97316"   # orange
    return "Rarely", "#9ca3af"   # gray


def render_dealer_table(dealer_stats: dict) -> str:
    """
    Build the Section H HTML table from precomputed dealer_stats.
    Returns empty string if fewer than 2 dealers (per CLAUDE.md §9H rule —
    "only include if dealer_stats present with 2+ dealers").
    """
    if not dealer_stats or len(dealer_stats) < 2:
        return ""

    # Sort by listings_seen descending — most active dealers first.
    rows_sorted = sorted(
        dealer_stats.items(),
        key=lambda kv: kv[1].get("listings_seen", 0),
        reverse=True,
    )

    rows_html = []
    for dealer, stats in rows_sorted:
        avg_days = stats.get("avg_days_on_lot")
        days_cell = f"{avg_days:.1f}" if avg_days is not None else "—"
        drop_text, drop_color = _drop_label(stats.get("price_drop_rate", 0.0))
        avg_drop = stats.get("avg_price_drop_pct", 0.0)
        avg_drop_cell = f"{avg_drop:.1f}%" if avg_drop else "—"
        relists = stats.get("relists_detected", 0)
        relist_cell = str(relists) if relists else "—"
        rows_html.append(
            "<tr>"
            f"<td>{dealer}</td>"
            f"<td>{stats.get('listings_seen', 0)}</td>"
            f"<td>{days_cell}</td>"
            f'<td style="color:{drop_color};font-weight:600">{drop_text}</td>'
            f"<td>{avg_drop_cell}</td>"
            f"<td>{relist_cell}</td>"
            "</tr>"
        )

    return (
        '<section id="dealer-intelligence">'
        "<h2>📊 Dealer Intelligence</h2>"
        '<table class="dealer-table">'
        "<thead><tr>"
        "<th>Dealer</th><th>Listings Seen</th><th>Avg Days on Lot</th>"
        "<th>Drops Price?</th><th>Avg Drop %</th><th>Relists</th>"
        "</tr></thead>"
        f"<tbody>{''.join(rows_html)}</tbody>"
        "</table>"
        '<p style="font-size:0.85em;color:#666;margin-top:10px;">'
        "Data accumulates across runs — dealer patterns become meaningful after 5+ runs."
        "</p>"
        "</section>"
    )


def _sparkline_color(prices: list) -> str:
    """Green if trending down, red if up, orange if flat."""
    if not prices or len(prices) < 2:
        return "#f97316"
    if prices[-1] < prices[0]:
        return "#22c55e"
    if prices[-1] > prices[0]:
        return "#ef4444"
    return "#f97316"


def render_sparklines(listings_state: dict) -> tuple[dict, str]:
    """
    Build a {listing_id: <canvas> HTML} map plus a single <script> block
    initializing every Chart.js sparkline. Only listings with 2+ price_history
    points get a sparkline — single-point listings render nothing (per §10).
    """
    canvases: dict = {}
    inits: list = []
    for lid, listing in listings_state.items():
        ph = listing.get("price_history", [])
        if len(ph) < 2:
            continue
        prices = [p["price"] for p in ph if p.get("price") is not None]
        labels = [p["date"] for p in ph if p.get("price") is not None]
        if len(prices) < 2:
            continue
        color = _sparkline_color(prices)
        canvas_id = f"spark-{lid}"
        canvases[lid] = (
            f'<canvas id="{canvas_id}" class="sparkline-container" '
            'width="120" height="40"></canvas>'
        )
        inits.append(
            f"new Chart(document.getElementById({canvas_id!r}).getContext('2d'),"
            f"{{type:'line',data:{{labels:{json.dumps(labels)},"
            f"datasets:[{{data:{json.dumps(prices)},"
            f"borderColor:'{color}',backgroundColor:'{color}26',"
            f"fill:true,pointRadius:2,borderWidth:1.5}}]}},"
            "options:{plugins:{legend:{display:false},tooltip:{enabled:false}},"
            "scales:{x:{display:false},y:{display:false}},"
            "responsive:false,maintainAspectRatio:false}});"
        )
    if not inits:
        return canvases, ""
    script = "<script>" + "".join(inits) + "</script>"
    return canvases, script


def post_process_html(
    html: str,
    listings_state: dict,
    dealer_stats: dict,
) -> str:
    """
    Substitute placeholders Claude emits with Python-rendered HTML.

    Failure modes (logged loudly — silent failure here would silently regress
    the report, exactly the class of bug §12 was about):

    * DEALER_TABLE placeholder missing while dealer_stats has ≥2 dealers
      → Claude didn't follow CLAUDE.md §9H. Log ERROR and append the rendered
      table before </body> as a fallback so the section is not lost.
    * SPARKLINE placeholders count > 0 but Chart.js block not yet injected
      → Inject CDN tag + init script before </body>.
    * SPARKLINE placeholder for a listing with <2 price points → strip silently
      (Claude emitted a placeholder for a single-point listing; render_sparklines
      already filtered, so the substitute is empty string).
    """
    # Strip leading/trailing markdown code fences Claude sometimes adds.
    h = html.strip()
    if h.startswith("```"):
        # remove opening fence (```html or ```)
        first_nl = h.find("\n")
        if first_nl != -1:
            h = h[first_nl + 1:]
    if h.endswith("```"):
        h = h[:h.rfind("```")].rstrip()

    # Dealer table substitution
    dealer_html = render_dealer_table(dealer_stats)
    if _DEALER_TABLE_PLACEHOLDER in h:
        h = h.replace(_DEALER_TABLE_PLACEHOLDER, dealer_html)
    elif dealer_html:
        print(
            f"  WARNING: <!-- DEALER_TABLE --> placeholder missing from Claude "
            f"output — appending rendered table before </body> as fallback.",
            file=sys.stderr,
        )
        h = h.replace("</body>", dealer_html + "</body>", 1)

    # Sparkline substitution
    canvases, script_block = render_sparklines(listings_state)
    needs_chartjs = False
    for lid in list(canvases.keys()):
        ph = _SPARKLINE_PLACEHOLDER_FMT.format(listing_id=lid)
        if ph in h:
            h = h.replace(ph, canvases[lid])
            needs_chartjs = True
    # Strip any sparkline placeholders that didn't have data (no canvas for that id).
    # Listing IDs are UUIDs (contain hyphens), so the inner pattern must allow them.
    # Pattern matches "<!-- SPARKLINE:" + any chars + " -->" non-greedily.
    leftover_sparks = re.findall(r"<!-- SPARKLINE:[^>]*?-->", h)
    if leftover_sparks:
        h = re.sub(r"<!-- SPARKLINE:[^>]*?-->", "", h)
    # Inject Chart.js CDN + init block before </body> only if needed
    if needs_chartjs and script_block:
        if _CHART_JS_CDN not in h:
            h = h.replace("</head>", _CHART_JS_CDN + "</head>", 1)
        h = h.replace("</body>", script_block + "</body>", 1)

    return h


# ── Claude API calls ──────────────────────────────────────────────────────────
# One call per car — desktop report only. Section 9H + sparklines are rendered
# by post_process_html, NOT emitted by Claude. See engineer_response.md §9.
# Per RCA §12: Tier-1 ITPM 50K, OTPM 10K. Target: input ≤25K, output ≤9.5K.

def _base_user_content(
    listings_data: str,
    prior_state_text: str,
    digest_text: str,
    car_label: str,
    run_number: int,
) -> str:
    """
    Assemble the user-content prompt body. Per RCA §12 / Option 4:
    - listings_data is the THINNED, PRE-SCREENED candidate set (~50 listings)
    - digest_text is market-wide stats from the FULL set (for §9G accuracy)
    - dealer_stats is NO LONGER sent — Section H is rendered post-call from
      the in-memory dealer_stats dict by post_process_html.
    """
    return (
        f"Today's date: {TODAY}\n"
        f"Run number: {run_number}\n"
        f"Vehicle: {car_label}\n\n"
        f"Candidate listings (top {listings_data.count('listing_id')} after pre-screen — see CLAUDE.md §9D):\n{listings_data}\n\n"
        f"Prior state summary (status/prev_price/first_seen per active listing):\n{prior_state_text}\n\n"
        f"Market digest (pre-computed stats across the FULL inventory — use for Section 9G):\n{digest_text}\n\n"
    )


def _call_claude(system_prompt: str, user_content: str) -> str:
    # max_retries=5 — load-bearing. SDK applies exponential backoff on 429s.
    # Removing this previously (commit dfddbf0) is what made transient throttling fatal.
    client = anthropic.Anthropic(max_retries=5)
    # §11.4 rollback hatch: set PRE_CALL_DELAY_SECONDS=120 (or any non-zero) on a
    # workflow_dispatch run to throttle calls if a future cron starts 429ing again.
    wait = int(os.getenv("PRE_CALL_DELAY_SECONDS", "0") or "0")
    if wait > 0:
        print(f"  PRE_CALL_DELAY_SECONDS={wait}s — sleeping before API call...")
        time.sleep(wait)
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        # max_tokens=12000 — well below Haiku 4.5's 64K model ceiling (verified
        # 2026-05-03 via Anthropic docs). Sized to allow self-termination of the
        # post-Option-4 report (estimated natural output ~8-10K with §H + sparklines
        # rendered by Python). Setting higher invites OTPM 429 (Tier-1 OTPM = 10K
        # tokens/min); setting lower risks truncation. Owner is on Tier 1 confirmed.
        max_tokens=12000,
        system=[
            {
                "type": "text",
                "text": system_prompt,
                "cache_control": {"type": "ephemeral"},
            }
        ],
        messages=[{"role": "user", "content": user_content}],
    )
    usage = response.usage
    print(f"  tokens: in={usage.input_tokens}  out={usage.output_tokens}")
    # OTPM telemetry. Tier-1 Haiku 4.5 OTPM = 10,000 tokens/min. SDK retry does
    # NOT save us from a structural OTPM 429 — every retry produces the same
    # long output and 429s the same way. If this warning fires consistently,
    # CLAUDE.md §10 needs to demand a more compact HTML.
    if usage.output_tokens > 9500:
        print(
            f"  ⚠ OTPM RISK: output_tokens={usage.output_tokens} approaching "
            f"Tier-1 OTPM ceiling 10,000 tokens/min. Investigate before next run.",
            file=sys.stderr,
        )
    return response.content[0].text


def call_claude_desktop(
    system_prompt: str,
    listings_data: str,
    prior_state_text: str,
    digest_text: str,
    car_label: str,
    run_number: int,
) -> str:
    user_content = (
        _base_user_content(listings_data, prior_state_text, digest_text, car_label, run_number)
        + "Generate ONLY the full desktop HTML report as defined in Section 10 of the master prompt "
        + "(File 1: Full desktop report). Output raw HTML starting with <!DOCTYPE html>. "
        + "Do not include the mobile report or any delimiter.\n\n"
        + "CRITICAL — do NOT generate the following inline; emit placeholders verbatim "
        + "(Python substitutes them post-call):\n"
        + "  • Section H Dealer Intelligence: emit `<!-- DEALER_TABLE -->` once at the position where the section belongs.\n"
        + "  • Per-listing sparklines: emit `<!-- SPARKLINE:{listing_id} -->` (replacing {listing_id} with the actual id) "
        + "in the price cell of any Top 10 row or Best Picks card whose listing has 2+ price-history points."
    )
    return _call_claude(system_prompt, user_content)


def _estimate_run_number(prior_state: dict) -> int:
    """Estimate run number from the full prior-state dict (not the summary)."""
    try:
        listings = prior_state.get("listings", {})
        if not listings:
            return 1
        dates = [e.get("first_seen") for e in listings.values() if e.get("first_seen")]
        return max(1, len(set(dates)))
    except Exception:
        return 1


# ── HTML output ───────────────────────────────────────────────────────────────

def save_report(desktop_html: str, car_key: str) -> Path:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    desktop_path = OUTPUT_DIR / f"report_{car_key}_{TODAY}.html"
    desktop_path.write_text(desktop_html.strip(), encoding="utf-8")
    print(f"  Saved: {desktop_path}")
    return desktop_path


# ── Email delivery ────────────────────────────────────────────────────────────

def send_email(report_path: Path, car_label: str) -> None:
    gmail_address = os.getenv("GMAIL_ADDRESS", "")
    app_password  = os.getenv("GMAIL_APP_PASSWORD", "")
    recipient     = os.getenv("ALERT_RECIPIENT", "")

    if not gmail_address or not app_password or not recipient:
        print(f"  Email skipped — GMAIL_ADDRESS, GMAIL_APP_PASSWORD, or ALERT_RECIPIENT not set.")
        return

    if "abc123" in app_password or app_password == "xxxx-xxxx-xxxx-xxxx":
        print("  Email skipped — placeholder credentials detected.")
        return

    html_body = report_path.read_text(encoding="utf-8")

    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"{car_label} — {TODAY}"
    msg["From"]    = gmail_address
    msg["To"]      = recipient
    msg.attach(MIMEText(html_body, "html", "utf-8"))

    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
            server.login(gmail_address, app_password)
            server.sendmail(gmail_address, recipient, msg.as_string())
        print(f"  Email sent to {recipient}")
    except Exception as exc:
        print(f"  Email failed: {exc}", file=sys.stderr)


# ── Per-car pipeline ──────────────────────────────────────────────────────────

def run_car(car_key: str, car_config: dict) -> None:
    label = car_config["label"]
    paths = car_paths(car_key)

    print(f"\n{'='*60}")
    print(f"  {label} ({car_key})")
    print(f"{'='*60}")

    raw_listings_file = paths["raw_listings"]
    if not raw_listings_file.exists():
        print(f"  SKIP: {raw_listings_file} not found. Run: python scraper.py --car {car_key}")
        return

    # Resolve system prompt
    prompt_file = car_config.get("prompt_file")
    if prompt_file and Path(prompt_file).exists():
        system_prompt = Path(prompt_file).read_text(encoding="utf-8")
    else:
        if prompt_file:
            print(f"  WARNING: prompt_file '{prompt_file}' not found — using CLAUDE.md fallback.")
        fallback = Path("CLAUDE.md")
        if not fallback.exists():
            print(f"  SKIP: No system prompt available for {car_key}.")
            return
        system_prompt = fallback.read_text(encoding="utf-8")

    raw_listings_text = raw_listings_file.read_text(encoding="utf-8")
    prior_state       = load_json(paths["state_file"], {"listings": {}})

    raw_listings = json.loads(raw_listings_text)
    if isinstance(raw_listings, dict):
        raw_listings = raw_listings.get("listings", [])

    print(f"  Loaded {len(raw_listings)} listings from scraper.")

    # Annotate each listing with depreciation benchmark before pre-screen / Claude.
    annotate_depreciation(raw_listings)
    n_benchmarked = sum(1 for l in raw_listings if l.get("vs_expected_pct") is not None)
    print(f"  Depreciation benchmark: {n_benchmarked}/{len(raw_listings)} listings matched a curve.")

    # Build compact state summary (Step 12 — replaces full json.dumps of prior_state)
    run_number       = _estimate_run_number(prior_state)
    prior_state_text = build_state_summary(raw_listings, prior_state)
    print(f"  State summary: {len(prior_state_text):,} chars "
          f"({len(prior_state.get('listings', {})):,} prior / {len(raw_listings)} current listings).")

    # Option-4: pre-screen the prompt input (RCA §12.1)
    candidates       = select_candidates(raw_listings, max_n=50)
    candidates_thin  = [thin_for_claude(l) for l in candidates]
    listings_data    = json.dumps(candidates_thin, ensure_ascii=False)
    digest_text      = market_digest(raw_listings)
    print(f"  Candidates: {len(candidates)}/{len(raw_listings)} after pre-screen "
          f"({len(listings_data):,} chars).")
    print(f"  Market digest: {len(digest_text):,} chars (full inventory stats for §9G).")

    # BUY NOW alerts (run on FULL raw_listings, not candidates — independent of pre-screen)
    check_alerts(
        raw_listings,
        car_config.get("buy_now", {}),
        paths["alerts_file"],
        car_label=label,
    )

    # Merge into persistent state + compute dealer reputation stats.
    # dealer_stats is no longer sent to Claude (§H rendered post-call by post_process_html).
    updated_state = update_state(raw_listings, prior_state)
    dealer_stats  = update_dealer_stats(updated_state["listings"], TODAY)
    updated_state["dealer_stats"] = dealer_stats
    print(f"  Dealer stats computed for {len(dealer_stats)} dealers.")

    print("  Calling Claude API — desktop report...")
    desktop_html = call_claude_desktop(
        system_prompt, listings_data, prior_state_text, digest_text, label, run_number
    )

    # Option-4: substitute Section H + sparkline placeholders with Python-rendered HTML.
    desktop_html = post_process_html(
        desktop_html,
        listings_state=updated_state["listings"],
        dealer_stats=dealer_stats,
    )

    desktop_path = save_report(desktop_html, car_key)

    # Persist updated state — atomic write (tmp + os.replace) so a crash mid-write
    # cannot corrupt listings.json. A partial write would otherwise lose all history
    # on the next load (json.loads on truncated file).
    paths["state_dir"].mkdir(parents=True, exist_ok=True)
    state_final = paths["state_file"]
    state_tmp   = state_final.with_suffix(state_final.suffix + ".tmp")
    state_tmp.write_text(
        json.dumps(updated_state, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    os.replace(state_tmp, state_final)
    print(f"  State updated: {state_final}")

    send_email(desktop_path, label)


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    print(f"run_tracker.py starting — {TODAY}")
    print(f"Cars configured: {', '.join(CARS.keys())}")

    for car_key, car_config in CARS.items():
        run_car(car_key, car_config)

    print("\nDone.")


if __name__ == "__main__":
    main()
