#!/usr/bin/env python3
"""
BUY NOW alert logic (Step 4).
Called from run_tracker.py before the full report is generated.
No Claude needed — pure threshold matching.
"""

import json
import os
import smtplib
import sys
from email.mime.text import MIMEText
from pathlib import Path

from config import ALERTS_FILE, BUY_NOW


def _classify(listing: dict):
    """
    Return (threshold_key, threshold) if the listing crosses a BUY NOW threshold,
    or None if it does not qualify.
    """
    year = listing.get("year")
    price = listing.get("price")
    km = listing.get("mileage_km") or listing.get("km") or listing.get("mileage")
    condition = (listing.get("condition") or "").lower()
    is_new = condition in ("new", "demo") or listing.get("is_new", False)

    if price is None:
        return None

    checks = []

    if not is_new and year == 2024:
        checks.append(("used_2024", BUY_NOW["used_2024"]))
    if not is_new and year == 2025:
        checks.append(("used_2025", BUY_NOW["used_2025"]))
    if is_new and year == 2026:
        checks.append(("new_2026", BUY_NOW["new_2026"]))
    if is_new and year == 2025:
        checks.append(("new_2025_clear", BUY_NOW["new_2025_clear"]))

    for key, threshold in checks:
        if price > threshold["max_price"]:
            continue
        if threshold["max_km"] is not None and km is not None and km > threshold["max_km"]:
            continue
        return key, threshold

    return None


def _format_email(listing: dict, threshold_key: str, threshold: dict) -> tuple[str, str]:
    year = listing.get("year", "?")
    trim = listing.get("trim", "Unknown")
    price = listing.get("price", 0)
    km = listing.get("mileage_km") or listing.get("km") or listing.get("mileage") or 0
    dealer = listing.get("dealer") or listing.get("seller") or "Unknown Dealer"
    city = listing.get("city") or listing.get("location") or ""
    url = listing.get("url") or listing.get("listing_url") or "URL not available"

    dealer_city = f"{dealer} ({city})" if city else dealer

    max_km = threshold.get("max_km")
    km_part = f" and under {max_km:,} km" if max_km else ""
    threshold_desc = f"{threshold_key.replace('_', ' ')} under ${threshold['max_price']:,}{km_part}"

    km_display = f"{km:,}" if km else "N/A"
    price_display = f"${price:,.0f}"

    subject = f"CX-5 BUY NOW -- {year} {trim} {price_display} / {km_display} km -- {dealer}"

    body = f"""A listing crossed your BUY NOW threshold.

Year:    {year}
Trim:    {trim}
Price:   {price_display} CAD
Km:      {km_display}
Dealer:  {dealer_city}
Link:    {url}

Threshold: {threshold_desc}.

---
CX-5 Tracker - automated alert"""

    return subject, body


def _send_alert(subject: str, body: str) -> bool:
    gmail_address = os.getenv("GMAIL_ADDRESS", "")
    app_password = os.getenv("GMAIL_APP_PASSWORD", "")
    recipient = os.getenv("ALERT_RECIPIENT", "")

    if not gmail_address or not app_password or not recipient:
        print("Alert email skipped — credentials not set.")
        return False

    if "abc123" in app_password or app_password == "xxxx-xxxx-xxxx-xxxx":
        print("Alert email skipped — placeholder credentials.")
        return False

    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = subject
    msg["From"] = gmail_address
    msg["To"] = recipient

    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
            server.login(gmail_address, app_password)
            server.sendmail(gmail_address, recipient, msg.as_string())
        print(f"BUY NOW alert sent: {subject}")
        return True
    except Exception as exc:
        print(f"Alert email failed: {exc}", file=sys.stderr)
        return False


def check_alerts(listings: list) -> None:
    """
    Check listings against BUY NOW thresholds.
    Sends one email per new qualifying listing.
    Deduplicates against state/alerts_sent.json.
    """
    alerts_sent: list = []
    if ALERTS_FILE.exists():
        try:
            alerts_sent = json.loads(ALERTS_FILE.read_text(encoding="utf-8"))
        except Exception:
            alerts_sent = []

    sent_ids = set(alerts_sent)
    newly_sent = []

    qualified = []
    for listing in listings:
        lid = listing.get("listing_id") or listing.get("id")
        if not lid or lid in sent_ids:
            continue

        result = _classify(listing)
        if result is not None:
            qualified.append((lid, listing, result))

    if not qualified:
        print("No new BUY NOW listings found.")
        return

    print(f"{len(qualified)} new BUY NOW listing(s) found:")
    for lid, listing, (threshold_key, threshold) in qualified:
        year = listing.get("year")
        price = listing.get("price")
        km = listing.get("mileage_km") or listing.get("km") or listing.get("mileage")
        km_str = f"{km:,}" if km is not None else "N/A"
        print(f"  [{threshold_key}] {year} ${price:,} / {km_str} km  id={lid}")

        subject, body = _format_email(listing, threshold_key, threshold)
        if _send_alert(subject, body):
            newly_sent.append(lid)
            sent_ids.add(lid)

    if newly_sent:
        ALERTS_FILE.parent.mkdir(parents=True, exist_ok=True)
        ALERTS_FILE.write_text(
            json.dumps(sorted(sent_ids), indent=2),
            encoding="utf-8",
        )
        print(f"Saved {len(sent_ids)} alert IDs to {ALERTS_FILE}")
