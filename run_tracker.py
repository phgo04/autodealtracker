#!/usr/bin/env python3
"""
AutoDealTracker — automated analysis pipeline.
Reads scraped listings, calls Claude API, saves HTML reports, sends email.
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
from config import (
    OUTPUT_DIR,
    RAW_LISTINGS_FILE,
    STATE_FILE,
)

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
    Returns the updated state dict.
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
            "first_seen": existing.get("first_seen", TODAY),
            "last_seen": TODAY,
            "price_history": price_history,
        }

    # Mark listings not seen this run
    for lid, entry in listings_by_id.items():
        if lid not in current_ids and entry.get("last_seen") == TODAY:
            pass  # already last_seen from prior runs

    return {"listings": listings_by_id}


# ── Claude API call ───────────────────────────────────────────────────────────

def call_claude(master_prompt: str, listings_data: str, prior_state_text: str) -> str:
    client = anthropic.Anthropic()

    run_number = _estimate_run_number(prior_state_text)

    user_content = (
        f"Today's date: {TODAY}\n"
        f"Run number: {run_number}\n\n"
        f"Current listings (from scraper, state/raw_listings.json):\n{listings_data}\n\n"
        f"Prior state (state/listings.json — use for price drop detection):\n{prior_state_text}\n\n"
        "Generate both HTML reports exactly as defined in the master prompt (Sections 9-10). "
        f"Separate the two reports with this exact delimiter on its own line: {DELIMITER}"
    )

    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=8192,
        system=[
            {
                "type": "text",
                "text": master_prompt,
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

def save_reports(full_text: str) -> tuple[Path, Path]:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    if DELIMITER in full_text:
        desktop_html, mobile_html = full_text.split(DELIMITER, 1)
    else:
        print("WARNING: delimiter not found in Claude response — saving full text as desktop report only.")
        desktop_html = full_text
        mobile_html = "<html><body><p>Mobile report not generated — delimiter missing from API response.</p></body></html>"

    desktop_path = OUTPUT_DIR / f"report_{TODAY}.html"
    mobile_path = OUTPUT_DIR / f"report_{TODAY}_mobile.html"

    desktop_path.write_text(desktop_html.strip(), encoding="utf-8")
    mobile_path.write_text(mobile_html.strip(), encoding="utf-8")

    print(f"Saved: {desktop_path}")
    print(f"Saved: {mobile_path}")
    return desktop_path, mobile_path


# ── Email delivery ────────────────────────────────────────────────────────────

def send_email(mobile_path: Path) -> None:
    gmail_address = os.getenv("GMAIL_ADDRESS", "")
    app_password = os.getenv("GMAIL_APP_PASSWORD", "")
    recipient = os.getenv("ALERT_RECIPIENT", "")

    if not gmail_address or not app_password or not recipient:
        print("Email skipped — GMAIL_ADDRESS, GMAIL_APP_PASSWORD, or ALERT_RECIPIENT not set.")
        return

    # Skip placeholder values set during initial setup
    if "abc123" in app_password or app_password == "xxxx-xxxx-xxxx-xxxx":
        print("Email skipped — placeholder credentials detected.")
        return

    html_body = mobile_path.read_text(encoding="utf-8")

    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"CX-5 Tracker — {TODAY}"
    msg["From"] = gmail_address
    msg["To"] = recipient
    msg.attach(MIMEText(html_body, "html", "utf-8"))

    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
            server.login(gmail_address, app_password)
            server.sendmail(gmail_address, recipient, msg.as_string())
        print(f"Email sent to {recipient}")
    except Exception as exc:
        print(f"Email failed: {exc}", file=sys.stderr)


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    print(f"run_tracker.py starting — {TODAY}")

    if not RAW_LISTINGS_FILE.exists():
        print(f"ERROR: {RAW_LISTINGS_FILE} not found. Run scraper.py first.", file=sys.stderr)
        sys.exit(1)

    master_prompt = Path("CLAUDE.md").read_text(encoding="utf-8")
    listings_data = RAW_LISTINGS_FILE.read_text(encoding="utf-8")
    prior_state = load_json(STATE_FILE, {"listings": {}})
    prior_state_text = json.dumps(prior_state, ensure_ascii=False)

    raw_listings = json.loads(listings_data)
    if isinstance(raw_listings, dict):
        raw_listings = raw_listings.get("listings", [])

    print(f"Loaded {len(raw_listings)} listings from scraper.")

    # Check BUY NOW thresholds and fire alerts before full report
    check_alerts(raw_listings)

    # Merge into persistent state
    updated_state = update_state(raw_listings, prior_state)

    print("Calling Claude API...")
    response_text = call_claude(master_prompt, listings_data, prior_state_text)

    desktop_path, mobile_path = save_reports(response_text)

    # Persist updated state
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(
        json.dumps(updated_state, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"State updated: {STATE_FILE}")

    send_email(mobile_path)

    print("Done.")


if __name__ == "__main__":
    main()
