# CX-5 Tracker — Architecture Design
**Date:** 2026-04-25

## Context

The scraper (Step 1) is complete and working: 326 listings from 74 Ontario dealers via AutoTrader.ca, using Selenium + Chrome headless. The pipeline now needs to become autonomous and scalable.

## Goals

1. Run automatically without the user's PC being on
2. Eliminate the manual Claude Code session for analysis
3. Support cross-shopping across multiple car makes/models
4. Minimize token cost and manual effort per run

## Key Decisions

**AutoTrader-only scraping** — Individual dealer sites are not worth the effort (~85-90% coverage from AutoTrader alone). Confirmed and closed.

**GitHub Actions for scheduling** — Free, cloud-hosted, supports Chrome headless, allows manual trigger from the GitHub mobile app. Replaces Windows Task Scheduler.

**Claude API for analysis** — Direct programmatic call to Claude replaces the interactive Claude Code session. Tighter prompt, structured output, lower token overhead, fully automated.

**Multi-car via config array** — Each car (CX-5, CR-V, RAV4, etc.) is a config entry with its own search URL, filters, and thresholds. Scraper runs once per config entry per workflow run.

**Per-car state directories** — `state/cx5/`, `state/crv/`, etc. Each car tracks price history independently. State files commit back to the repo after each run.

**GitHub Pages as long-term web front-end** — Reports published to a Pages URL replaces email delivery. Deferred to Step 9 — not a priority now.

## Architecture

```
GitHub Actions (scheduled: every 3 days, 8am)
  ├── scraper.py --car cx5  →  state/cx5/raw_listings.json
  ├── scraper.py --car crv  →  state/crv/raw_listings.json
  ├── run_tracker.py        →  output/report_YYYY-MM-DD.html
  │                             output/report_YYYY-MM-DD_mobile.html
  │                             (emails mobile report)
  ├── alerts.py             →  email if BUY NOW threshold crossed
  └── git commit state/     →  price history persists between runs
```

## Step Order (revised)

| Step | What | Replaces |
|---|---|---|
| 1 ✓ | AutoTrader scraper | — |
| 2 | GitHub repo + Actions | Windows Task Scheduler (old Step 4) |
| 3 | Claude API + email | Manual Claude Code session |
| 4 | BUY NOW alerts | Old Step 2 |
| 5 | Multi-car support | New |
| 6 | Price history sparklines | Old Step 3 |
| 7 | Dealer reputation layer | Old Step 5 |
| 8 | Depreciation benchmark | Old Step 6 |
| 9 | GitHub Pages dashboard | New (long-term) |
