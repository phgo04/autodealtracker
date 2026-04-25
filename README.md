# CX-5 Tracker

A personal car deal tracker that scrapes AutoTrader.ca for Mazda CX-5 listings in Ontario, runs Claude AI analysis, and emails a mobile-friendly report every 3 days — automatically, with no manual steps.

## What it does

- Scrapes ~300+ 2024–2026 Mazda CX-5 listings from 70+ Ontario dealers via AutoTrader.ca
- Detects price drops between runs
- Ranks listings by true value (price, mileage, trim, condition)
- Fires an immediate email alert when a listing crosses a BUY NOW threshold
- Generates a desktop HTML report and a mobile-optimized HTML report
- Runs automatically via GitHub Actions — no PC required

## How to run manually

See [RUNME.md](RUNME.md) for step-by-step instructions.

## Tech stack

- Python 3.11
- Selenium + Chrome headless (scraper)
- BeautifulSoup / lxml (HTML parsing fallback)
- Anthropic Claude API (analysis and report generation)
- GitHub Actions (scheduling and automation)
- Gmail SMTP (email delivery)

## Project structure

```
scraper.py          — AutoTrader scraper
run_tracker.py      — Automated pipeline (API call + email)
alerts.py           — BUY NOW email alerts
config.py           — Central configuration (thresholds, URLs, filters)
CLAUDE.md           — Master prompt and analysis rules for Claude
RUNME.md            — How to run manually
state/              — Persistent listing data and price history
output/             — Generated HTML reports
.github/workflows/  — GitHub Actions automation
```

## Setup

1. Clone this repo (private)
2. Copy `.env.example` to `.env` and fill in your credentials
3. Run `pip install -r requirements.txt`
4. Run `python scraper.py --test` to verify the scraper works
5. See [nextsteps.md](nextsteps.md) for the full implementation roadmap

## Configuration

All filters, thresholds, and search parameters are in `config.py`. Update values there — do not hardcode them in other scripts.

The analysis rules and vehicle preferences are in `CLAUDE.md`.
