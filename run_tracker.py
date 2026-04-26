#!/usr/bin/env python3
"""
AutoDealTracker — automated analysis pipeline.
Loops over all cars in CARS config, calls Claude API per car, saves reports, sends email.
"""

import json
import os
import smtplib
import sys
from datetime import date
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

import anthropic
from dotenv import load_dotenv

from alerts import check_alerts
from config import CARS, OUTPUT_DIR, car_paths

load_dotenv()

TODAY = date.today().isoformat()
DELIMITER = "===MOBILE_REPORT==="


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


# ── Claude API call ───────────────────────────────────────────────────────────

def call_claude(system_prompt: str, listings_data: str, prior_state_text: str, car_label: str) -> str:
    client = anthropic.Anthropic()

    run_number = _estimate_run_number(prior_state_text)

    user_content = (
        f"Today's date: {TODAY}\n"
        f"Run number: {run_number}\n"
        f"Vehicle: {car_label}\n\n"
        f"Current listings (from scraper):\n{listings_data}\n\n"
        f"Prior state (for price drop detection):\n{prior_state_text}\n\n"
        "Generate both HTML reports exactly as defined in the master prompt (Sections 9-10). "
        f"Separate the two reports with this exact delimiter on its own line: {DELIMITER}"
    )

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

    return response.content[0].text


def _estimate_run_number(prior_state_text: str) -> int:
    try:
        state = json.loads(prior_state_text)
        listings = state.get("listings", {})
        if not listings:
            return 1
        dates = [e.get("first_seen") for e in listings.values() if e.get("first_seen")]
        return max(1, len(set(dates)))
    except Exception:
        return 1


# ── HTML output ───────────────────────────────────────────────────────────────

def save_reports(full_text: str, car_key: str) -> tuple[Path, Path]:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    if DELIMITER in full_text:
        desktop_html, mobile_html = full_text.split(DELIMITER, 1)
    else:
        print(f"WARNING: delimiter not found in Claude response for {car_key} — saving full text as desktop only.")
        desktop_html = full_text
        mobile_html = "<html><body><p>Mobile report not generated — delimiter missing from API response.</p></body></html>"

    desktop_path = OUTPUT_DIR / f"report_{car_key}_{TODAY}.html"
    mobile_path  = OUTPUT_DIR / f"report_{car_key}_{TODAY}_mobile.html"

    desktop_path.write_text(desktop_html.strip(), encoding="utf-8")
    mobile_path.write_text(mobile_html.strip(), encoding="utf-8")

    print(f"  Saved: {desktop_path}")
    print(f"  Saved: {mobile_path}")
    return desktop_path, mobile_path


# ── Email delivery ────────────────────────────────────────────────────────────

def send_email(mobile_path: Path, car_label: str) -> None:
    gmail_address = os.getenv("GMAIL_ADDRESS", "")
    app_password  = os.getenv("GMAIL_APP_PASSWORD", "")
    recipient     = os.getenv("ALERT_RECIPIENT", "")

    if not gmail_address or not app_password or not recipient:
        print(f"  Email skipped — GMAIL_ADDRESS, GMAIL_APP_PASSWORD, or ALERT_RECIPIENT not set.")
        return

    if "abc123" in app_password or app_password == "xxxx-xxxx-xxxx-xxxx":
        print("  Email skipped — placeholder credentials detected.")
        return

    html_body = mobile_path.read_text(encoding="utf-8")

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
    prior_state_text = json.dumps(prior_state, ensure_ascii=False)

    raw_listings = json.loads(listings_data)
    if isinstance(raw_listings, dict):
        raw_listings = raw_listings.get("listings", [])

    print(f"  Loaded {len(raw_listings)} listings from scraper.")

    # BUY NOW alerts
    check_alerts(
        raw_listings,
        car_config.get("buy_now", {}),
        paths["alerts_file"],
        car_label=label,
    )

    # Merge into persistent state
    updated_state = update_state(raw_listings, prior_state)

    print("  Calling Claude API...")
    response_text = call_claude(system_prompt, listings_data, prior_state_text, label)

    desktop_path, mobile_path = save_reports(response_text, car_key)

    # Persist updated state
    paths["state_dir"].mkdir(parents=True, exist_ok=True)
    paths["state_file"].write_text(
        json.dumps(updated_state, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"  State updated: {paths['state_file']}")

    send_email(mobile_path, label)


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    print(f"run_tracker.py starting — {TODAY}")
    print(f"Cars configured: {', '.join(CARS.keys())}")

    for car_key, car_config in CARS.items():
        run_car(car_key, car_config)

    print("\nDone.")


if __name__ == "__main__":
    main()
