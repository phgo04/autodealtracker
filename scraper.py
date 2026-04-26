#!/usr/bin/env python3
"""
AutoTrader.ca scraper for dealer inventory in Ontario/GTA.
Uses Selenium + Chrome (headless) to bypass Imperva bot protection.
Saves structured listing data to state/<car>/raw_listings.json.

Usage:
    python scraper.py                # scrape first car in CARS (cx5)
    python scraper.py --car cx5      # explicit car key
    python scraper.py --car crv      # Honda CR-V
    python scraper.py --test         # first page only, print summary, no save
    python scraper.py --car crv --test
"""

import argparse
import json
import re
import time
from datetime import date
from typing import Optional

from bs4 import BeautifulSoup
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.common.exceptions import WebDriverException
from webdriver_manager.chrome import ChromeDriverManager

from config import CARS, MAX_PAGES, car_paths

AUTOTRADER_BASE = "https://www.autotrader.ca"


# ── Browser setup ─────────────────────────────────────────────────────────────

def make_driver() -> webdriver.Chrome:
    """
    Create a headless Chrome driver that looks like a real browser.
    webdriver-manager handles downloading the right ChromeDriver automatically.
    """
    options = Options()
    options.add_argument("--headless=new")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-gpu")
    options.add_argument("--window-size=1920,1080")
    options.add_argument("--lang=en-CA")

    options.add_argument("--disable-blink-features=AutomationControlled")
    options.add_experimental_option("excludeSwitches", ["enable-automation"])
    options.add_experimental_option("useAutomationExtension", False)
    options.add_argument(
        "user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    )

    service = Service(ChromeDriverManager().install())
    driver = webdriver.Chrome(service=service, options=options)

    driver.execute_cdp_cmd(
        "Page.addScriptToEvaluateOnNewDocument",
        {"source": "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"},
    )
    return driver


# ── Helpers ───────────────────────────────────────────────────────────────────

def parse_price(raw) -> Optional[int]:
    if raw is None:
        return None
    cleaned = re.sub(r"[^\d]", "", str(raw))
    return int(cleaned) if cleaned else None


def parse_km(raw) -> Optional[int]:
    if raw is None:
        return None
    text = str(raw).strip().lower()
    if text in ("new", "0", "—", "-", "", "n/a"):
        return 0
    cleaned = re.sub(r"[^\d]", "", text)
    return int(cleaned) if cleaned else None


def full_url(path: str) -> str:
    if not path:
        return ""
    return path if path.startswith("http") else AUTOTRADER_BASE + path


# ── Strategy 1: Extract from Next.js __NEXT_DATA__ JSON ──────────────────────

def extract_from_next_data(html: str, today: str) -> Optional[list]:
    soup = BeautifulSoup(html, "lxml")
    tag = soup.find("script", {"id": "__NEXT_DATA__"})
    if not tag or not tag.string:
        return None

    try:
        data = json.loads(tag.string)
    except json.JSONDecodeError:
        return None

    page_props = data.get("props", {}).get("pageProps", {})

    raw_list = None
    for key in ("listings", "searchResults", "results", "vehicles", "ads"):
        if key in page_props:
            raw_list = page_props[key]
            break

    if raw_list is None:
        inner = page_props.get("data", {})
        for key in ("listings", "searchResults", "results"):
            if key in inner:
                raw_list = inner[key]
                break

    if raw_list is None:
        return None

    return [_normalise_next(r, today) for r in raw_list if isinstance(r, dict)]


def _normalise_next(raw: dict, today: str) -> dict:
    listing_id = str(raw.get("id") or raw.get("listingId") or raw.get("adId") or "")
    url = raw.get("url") or raw.get("link") or raw.get("href") or ""
    if url and not url.startswith("http"):
        url = AUTOTRADER_BASE + url
    if not listing_id and url:
        m = re.search(r"/([a-f0-9\-]{30,}|[\d]{6,})$", url)
        if m:
            listing_id = m.group(1)

    price_obj = raw.get("price") or {}
    if isinstance(price_obj, dict):
        raw_price = (
            price_obj.get("priceFormatted")
            or price_obj.get("suggestedRetailPrice")
            or price_obj.get("price")
        )
    else:
        raw_price = price_obj

    price = parse_price(raw_price)

    vehicle = raw.get("vehicle") or {}
    year  = vehicle.get("modelYear") or raw.get("year")
    trim  = vehicle.get("modelVersionInput") or vehicle.get("variant") or raw.get("trim") or "Not listed"
    offer = str(vehicle.get("offerType") or raw.get("condition") or "U").upper()
    condition = "new" if offer in ("N", "NEW") else "used"

    raw_km = (
        vehicle.get("mileageInKm")
        or raw.get("mileage")
        or raw.get("odometer")
        or raw.get("kilometers")
    )
    km = parse_km(raw_km)
    if km == 0:
        condition = "new"

    seller   = raw.get("seller") or {}
    location = raw.get("location") or {}
    dealer   = (
        seller.get("companyName")
        or seller.get("name")
        or raw.get("dealerName")
        or "Not listed"
    )
    city = (
        location.get("city")
        or raw.get("city")
        or "Not listed"
    ) if isinstance(location, dict) else str(location)

    special = raw.get("specialConditions") or []
    is_cpo = False
    if isinstance(special, list):
        is_cpo = any(
            "certif" in str(s).lower() or "cpo" in str(s).lower()
            for s in special
        )
    else:
        is_cpo = bool(raw.get("isCpo") or raw.get("cpo") or raw.get("certified"))

    return {
        "listing_id": listing_id,
        "url":        url,
        "year":       year,
        "trim":       trim,
        "condition":  condition,
        "price":      price,
        "km":         km,
        "dealer":     dealer,
        "location":   city,
        "is_cpo":     is_cpo,
        "scraped_at": today,
    }


# ── Strategy 2: HTML fallback ─────────────────────────────────────────────────

def extract_from_html(html: str, today: str) -> list:
    soup = BeautifulSoup(html, "lxml")
    listings = []

    cards = (
        soup.select("[data-listing-id]")
        or soup.select("[data-id][class*='result']")
        or soup.select("div[class*='result-list-item']")
        or soup.select("div[class*='listing-card']")
    )
    if not cards:
        cards = [
            a for a in soup.find_all("a", href=re.compile(r"/a/[^/]+/[^/]+/"))
            if a.find(string=re.compile(r"\$[\d,]+"))
        ]

    for card in cards:
        listing_id = card.get("data-listing-id") or card.get("data-id") or ""
        link_el = card if card.name == "a" else card.find("a", href=True)
        url = ""
        if link_el:
            url = full_url(link_el.get("href", ""))
            if not listing_id:
                m = re.search(r"/(\d{6,})", url)
                if m:
                    listing_id = m.group(1)

        if not listing_id and not url:
            continue

        price_el = card.find(class_=re.compile(r"price|Price", re.I))
        price = parse_price(price_el.get_text(strip=True) if price_el else None)

        km_el = card.find(class_=re.compile(r"odometer|mileage|kilometres|km", re.I))
        km = parse_km(km_el.get_text(strip=True) if km_el else None)

        title_el = (
            card.find(class_=re.compile(r"title|heading|name", re.I))
            or card.find(["h2", "h3", "h4"])
        )
        title = title_el.get_text(" ", strip=True) if title_el else ""
        year, trim = None, "Not listed"
        m = re.search(r"\b(202[0-9])\b", title)
        if m:
            year = int(m.group(1))
            after = re.sub(rf".*{year}.*?cx-?5\s*|.*{year}.*?cr-?v\s*", "", title, flags=re.I).strip()
            trim = after.split("\n")[0].strip() or "Not listed"

        condition = "used"
        cond_el = card.find(string=re.compile(r"\b(new|demo)\b", re.I))
        if cond_el:
            c = re.search(r"\b(new|demo|used)\b", str(cond_el), re.I)
            if c:
                condition = c.group(1).lower()
        if km == 0:
            condition = "new"

        dealer_el = card.find(class_=re.compile(r"dealer|seller|vendor", re.I))
        dealer = dealer_el.get_text(strip=True) if dealer_el else "Not listed"

        loc_el = card.find(class_=re.compile(r"location|city|address", re.I))
        location = loc_el.get_text(strip=True) if loc_el else "Not listed"

        listings.append({
            "listing_id": listing_id,
            "url":        url,
            "year":       year,
            "trim":       trim,
            "condition":  condition,
            "price":      price,
            "km":         km,
            "dealer":     dealer,
            "location":   location,
            "is_cpo":     bool(card.find(string=re.compile(r"certif|cpo", re.I))),
            "scraped_at": today,
        })

    return listings


# ── Build search URL ──────────────────────────────────────────────────────────

def build_url(search_base: str, page_num: int) -> str:
    if page_num <= 1:
        return search_base
    return f"{search_base}?page={page_num}"


# ── Core scrape loop ──────────────────────────────────────────────────────────

def scrape(car_config: dict, test_mode: bool = False) -> list:
    today = date.today().isoformat()
    all_listings: list = []
    seen_ids: set = set()

    label      = car_config["label"]
    search_url = car_config["search_url"]
    year_min   = car_config["year_min"]
    year_max   = car_config["year_max"]

    print(f"\nAutoDealTracker - {label} - AutoTrader Scrape - {today}")
    print(f"Search: {search_url}")
    print(f"Year filter (client-side): {year_min}-{year_max}")
    print()
    print("  Starting Chrome (headless)...")

    try:
        driver = make_driver()
    except WebDriverException as e:
        print(f"  Chrome failed to start: {e}")
        print("  Make sure Google Chrome is installed on this machine.")
        return []

    # Give each page up to 60 s to load before Selenium gives up.
    # Default is 300 s, which means a hung page blocks the whole run.
    driver.set_page_load_timeout(60)

    try:
        print("  Visiting homepage to pass bot check...", end=" ", flush=True)
        driver.get(AUTOTRADER_BASE)
        time.sleep(3)
        print("done")

        for page_num in range(1, MAX_PAGES + 1):
            url = build_url(search_url, page_num)

            print(f"  Page {page_num} ... ", end="", flush=True)
            try:
                driver.get(url)
                time.sleep(3)
                html = driver.page_source
            except WebDriverException as e:
                print(f"timeout/error — stopping with {len(all_listings)} listings. ({e.__class__.__name__})")
                break

            page_listings = extract_from_next_data(html, today)
            method = "JSON"
            if page_listings is None:
                page_listings = extract_from_html(html, today)
                method = "HTML"

            if not page_listings:
                print(f"0 listings ({method}) - end of results.")
                break

            page_listings = [
                l for l in page_listings
                if l.get("year") is None or year_min <= int(l["year"]) <= year_max
            ]

            added = 0
            for listing in page_listings:
                key = listing.get("listing_id") or listing.get("url")
                if key and key not in seen_ids:
                    seen_ids.add(key)
                    all_listings.append(listing)
                    added += 1

            print(f"{added} new ({method}, {len(all_listings)} total so far)")

            if test_mode:
                print("\n  [test mode] Stopping after page 1.")
                break

            if added == 0:
                break

            time.sleep(1.5)

    finally:
        driver.quit()

    return all_listings


# ── Summary ───────────────────────────────────────────────────────────────────

def print_summary(listings: list, car_config: dict) -> None:
    if not listings:
        print("\nNo listings found.")
        return

    label  = car_config["label"]
    prices = [l["price"] for l in listings if l.get("price")]
    kms    = [l["km"]    for l in listings if l.get("km") and l["km"] > 0]
    years  = [l["year"]  for l in listings if l.get("year")]
    by_cond: dict = {}
    for l in listings:
        c = l.get("condition", "unknown")
        by_cond[c] = by_cond.get(c, 0) + 1

    print(f"\n{'-'*52}")
    print(f"  {label.upper()} - SCRAPE RESULTS")
    print(f"{'-'*52}")
    print(f"  Total listings : {len(listings)}")
    print(f"  By condition   : {by_cond}")
    if years:
        print(f"  Years          : {min(years)}-{max(years)}")
    if prices:
        print(f"  Price range    : ${min(prices):,} - ${max(prices):,}")
        print(f"  Avg price      : ${int(sum(prices)/len(prices)):,}")
    if kms:
        print(f"  Km range       : {min(kms):,} - {max(kms):,}")
        print(f"  Avg km         : {int(sum(kms)/len(kms)):,}")

    buy_now = car_config.get("buy_now", {})
    for key, threshold in buy_now.items():
        is_used = key.startswith("used")
        year_part = None
        m = re.search(r"(\d{4})", key)
        if m:
            year_part = int(m.group(1))

        candidates = []
        for l in listings:
            if year_part and l.get("year") != year_part:
                continue
            cond = (l.get("condition") or "").lower()
            listing_is_used = cond not in ("new", "demo")
            if is_used != listing_is_used:
                continue
            if l.get("price") and l["price"] <= threshold["max_price"]:
                max_km = threshold.get("max_km")
                if max_km is None or (l.get("km") is not None and l["km"] <= max_km):
                    candidates.append(l)

        km_part = f" / <={threshold['max_km']:,} km" if threshold.get("max_km") else ""
        print(f"\n  BUY NOW [{key}] (<=${threshold['max_price']:,}{km_part}): {len(candidates)} found")
        for c in candidates[:3]:
            km_display = f"{c['km']:,}" if c.get("km") is not None else "N/A"
            print(f"    >> {c.get('year')} {c.get('trim')} - ${c['price']:,} / {km_display} km"
                  f" - {c.get('dealer')} ({c.get('location')})")
            if c.get("url"):
                print(f"       {c['url']}")

    print(f"{'-'*52}\n")


# ── Entry point ───────────────────────────────────────────────────────────────

def main():
    default_car = next(iter(CARS))

    parser = argparse.ArgumentParser(description="AutoTrader scraper")
    parser.add_argument(
        "--car", default=default_car,
        choices=list(CARS.keys()),
        help=f"Car to scrape (default: {default_car})"
    )
    parser.add_argument(
        "--test", action="store_true",
        help="First page only, print summary, do not save"
    )
    args = parser.parse_args()

    car_config = CARS[args.car]
    paths = car_paths(args.car)

    listings = scrape(car_config, test_mode=args.test)
    print_summary(listings, car_config)

    if args.test:
        print("Test mode — raw_listings.json not updated.\n")
        return

    if not listings:
        print("Nothing scraped — raw_listings.json unchanged.\n")
        return

    paths["state_dir"].mkdir(parents=True, exist_ok=True)
    with open(paths["raw_listings"], "w", encoding="utf-8") as f:
        json.dump(listings, f, indent=2, ensure_ascii=False)
    print(f"Saved {len(listings)} listings -> {paths['raw_listings']}\n")


if __name__ == "__main__":
    main()
