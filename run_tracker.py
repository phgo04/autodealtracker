#!/usr/bin/env python3
"""
AutoDealTracker — automated analysis pipeline.
Loops over all cars in CARS config, calls Claude API per car, saves reports, sends email.
"""

import json
import os
import smtplib
import sys
from collections import defaultdict
from datetime import date, datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

import anthropic
from dotenv import load_dotenv

from alerts import check_alerts
from config import CARS, OUTPUT_DIR, car_paths, depreciation_delta, expected_value

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


# ── Claude API calls ──────────────────────────────────────────────────────────
# One call per car — desktop report only (mobile disabled; see Step 12 comment above).
# Step 12 (build_state_summary) drops prior_state from ~49K to ~6–8K tokens,
# bringing per-call input from ~88K to ~45K — safely under the 50K TPM limit.

def _base_user_content(
    listings_data: str,
    prior_state_text: str,
    car_label: str,
    run_number: int,
    dealer_stats_text: str = "",
) -> str:
    content = (
        f"Today's date: {TODAY}\n"
        f"Run number: {run_number}\n"
        f"Vehicle: {car_label}\n\n"
        f"Current listings (from scraper):\n{listings_data}\n\n"
        f"Prior state summary (status/prev_price/first_seen per active listing):\n{prior_state_text}\n\n"
    )
    if dealer_stats_text:
        content += f"Dealer statistics (pre-computed across all runs):\n{dealer_stats_text}\n\n"
    return content


def _call_claude(system_prompt: str, user_content: str) -> str:
    client = anthropic.Anthropic()
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=8192,
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
    return response.content[0].text


def call_claude_desktop(
    system_prompt: str,
    listings_data: str,
    prior_state_text: str,
    car_label: str,
    run_number: int,
    dealer_stats_text: str = "",
) -> str:
    user_content = (
        _base_user_content(listings_data, prior_state_text, car_label, run_number, dealer_stats_text)
        + "Generate ONLY the full desktop HTML report as defined in Section 10 of the master prompt "
        + "(File 1: Full desktop report). Output raw HTML starting with <!DOCTYPE html>. "
        + "Do not include the mobile report or any delimiter."
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

    listings_data = raw_listings_file.read_text(encoding="utf-8")
    prior_state   = load_json(paths["state_file"], {"listings": {}})

    raw_listings = json.loads(listings_data)
    if isinstance(raw_listings, dict):
        raw_listings = raw_listings.get("listings", [])

    print(f"  Loaded {len(raw_listings)} listings from scraper.")

    # Annotate each listing with depreciation benchmark before sending to Claude
    annotate_depreciation(raw_listings)
    n_benchmarked = sum(1 for l in raw_listings if l.get("vs_expected_pct") is not None)
    print(f"  Depreciation benchmark: {n_benchmarked}/{len(raw_listings)} listings matched a curve.")

    # Build compact state summary (Step 12 — replaces full json.dumps of prior_state)
    run_number       = _estimate_run_number(prior_state)
    prior_state_text = build_state_summary(raw_listings, prior_state)
    print(f"  State summary: {len(prior_state_text):,} chars "
          f"({len(prior_state.get('listings', {})):,} prior / {len(raw_listings)} current listings).")

    # BUY NOW alerts
    check_alerts(
        raw_listings,
        car_config.get("buy_now", {}),
        paths["alerts_file"],
        car_label=label,
    )

    # Merge into persistent state and compute dealer reputation stats
    updated_state = update_state(raw_listings, prior_state)
    dealer_stats  = update_dealer_stats(updated_state["listings"], TODAY)
    updated_state["dealer_stats"] = dealer_stats
    dealer_stats_text = json.dumps(dealer_stats, ensure_ascii=False)
    print(f"  Dealer stats computed for {len(dealer_stats)} dealers.")

    print("  Calling Claude API — desktop report...")
    desktop_html = call_claude_desktop(
        system_prompt, listings_data, prior_state_text, label, run_number, dealer_stats_text
    )

    desktop_path = save_report(desktop_html, car_key)

    # Persist updated state
    paths["state_dir"].mkdir(parents=True, exist_ok=True)
    paths["state_file"].write_text(
        json.dumps(updated_state, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"  State updated: {paths['state_file']}")

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
