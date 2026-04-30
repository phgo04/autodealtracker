# Next Steps — AutoDealTracker Roadmap

This file is the implementation guide for Claude Code. Steps are ordered by dependency — do not skip ahead. Read `CLAUDE.md` for full context on vehicle preferences and value logic.

**Architecture decision (2026-04-25):** The system runs on GitHub Actions (cloud, no PC needed). The manual Claude Code session is replaced by a direct Claude API call. Multiple cars are supported via a config array. See `docs/superpowers/specs/2026-04-25-tracker-architecture-design.md` for the full design rationale.

---

## Step 1 — AutoTrader scraper DONE

`scraper.py` is complete and working. Scrapes AutoTrader.ca via Selenium + Chrome headless. Returns ~326 listings from 74 Ontario dealers per run. Paginates via `?page=N`, applies client-side year filter (2024-2026). Saves to `state/raw_listings.json`.

---

## Step 2 — GitHub repository + Actions automation DONE ✓

### Problem being solved
The tracker currently requires the user's PC to be on and Claude Code to be open. This step moves everything to the cloud so it runs automatically with no user action, and can be triggered from the GitHub mobile app.

### What to build

**2a. Initialize the GitHub repository**
- Create a private GitHub repo named `cx5-tracker` (or similar)
- Push all current project files
- Add a `.gitignore` that excludes `.env`, `__pycache__/`, and `output/` (reports are generated fresh each run, no need to version them)
- Do NOT exclude `state/` — those JSON files must persist between runs

**2b. Create the GitHub Actions workflow**

File: `.github/workflows/tracker.yml`

```yaml
name: CX-5 Tracker

on:
  schedule:
    - cron: '0 12 */3 * *'   # every 3 days at 8am EDT (noon UTC)
  workflow_dispatch:           # allows manual trigger from GitHub mobile app

jobs:
  run:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4

      - uses: actions/setup-python@v5
        with:
          python-version: '3.11'

      - name: Install Chrome
        uses: browser-actions/setup-chrome@latest

      - name: Install dependencies
        run: pip install -r requirements.txt

      - name: Run scraper
        run: python scraper.py

      - name: Run tracker
        env:
          ANTHROPIC_API_KEY: ${{ secrets.ANTHROPIC_API_KEY }}
          GMAIL_ADDRESS: ${{ secrets.GMAIL_ADDRESS }}
          GMAIL_APP_PASSWORD: ${{ secrets.GMAIL_APP_PASSWORD }}
          ALERT_RECIPIENT: ${{ secrets.ALERT_RECIPIENT }}
        run: python run_tracker.py

      - name: Commit updated state
        run: |
          git config user.name "cx5-tracker-bot"
          git config user.email "bot@users.noreply.github.com"
          git add state/
          git diff --staged --quiet || git commit -m "state: run $(date +%Y-%m-%d)"
          git push
```

**2c. Store secrets in GitHub**
Go to repo Settings -> Secrets -> Actions and add:
- `ANTHROPIC_API_KEY` — needed for Step 3
- `GMAIL_ADDRESS`, `GMAIL_APP_PASSWORD`, `ALERT_RECIPIENT` — needed for Steps 3 and 4

### Success criteria
The workflow appears in the Actions tab. Clicking "Run workflow" on GitHub mobile triggers a run. The scraper completes and state files update in the repo.

---

## Step 3 — Claude API integration + email delivery DONE ✓

### Problem being solved
The manual Claude Code session for analysis is slow, interactive, and token-inefficient. This step replaces it with a direct API call that runs headlessly inside the GitHub Actions workflow.

### What to build

A script `run_tracker.py` in the project root that:
1. Reads `CLAUDE.md` (master prompt), `state/raw_listings.json` (current listings), and `state/listings.json` (price history)
2. Calls the Claude API with all three as input
3. Receives back structured analysis text for both HTML reports
4. Writes `output/report_YYYY-MM-DD.html` and `output/report_YYYY-MM-DD_mobile.html`
5. Emails the mobile report as an HTML attachment
6. Updates `state/listings.json` with any price changes detected

### Claude API call

```python
import anthropic
from pathlib import Path
from datetime import date

client = anthropic.Anthropic()   # reads ANTHROPIC_API_KEY from env

master_prompt = Path("CLAUDE.md").read_text()
listings_data = Path("state/raw_listings.json").read_text()
prior_state   = Path("state/listings.json").read_text()

response = client.messages.create(
    model="claude-haiku-4-5-20251001",   # cheapest, sufficient for structured analysis
    max_tokens=8192,
    messages=[{
        "role": "user",
        "content": (
            f"{master_prompt}\n\n"
            f"Today's date: {date.today().isoformat()}\n\n"
            f"Current listings (from scraper):\n{listings_data}\n\n"
            f"Prior state (for price drop detection):\n{prior_state}\n\n"
            "Generate both HTML reports as defined in the README. "
            "Return them separated by the delimiter: ===MOBILE_REPORT==="
        )
    }]
)
```

Split the response on `===MOBILE_REPORT===` to extract the two reports.

### Email setup — Gmail SMTP

Use Gmail with an app password. Store credentials in `.env` locally and in GitHub Secrets for Actions.

`.env` format:
```
ANTHROPIC_API_KEY=sk-ant-...
GMAIL_ADDRESS=your-email@gmail.com
GMAIL_APP_PASSWORD=xxxx-xxxx-xxxx-xxxx
ALERT_RECIPIENT=your-email@gmail.com
```

Install: `pip install anthropic python-dotenv`

### Cost estimate
- Claude Haiku input: ~35,000 tokens per run x $0.25/M = ~$0.009
- Claude Haiku output: ~8,000 tokens x $1.25/M = ~$0.010
- Total per run: under $0.02. Monthly at 3-day cadence: under $0.20.

### Success criteria
Running `python run_tracker.py` locally produces both HTML files and sends the mobile report by email, with no Claude Code session open.

---

## Step 4 — BUY NOW email alerts DONE ✓

### Problem being solved
A great listing can appear and sell within 24 hours. This step sends an immediate email the moment a listing crosses a BUY NOW threshold, independently of the full report.

### What to build

A script `alerts.py` with a function `check_alerts(listings)` that:
1. Checks each listing against `BUY_NOW` thresholds in `config.py`
2. Skips listing IDs already in `state/alerts_sent.json` (deduplication)
3. Sends a plain-text email for each new qualifying listing
4. Appends triggered IDs to `state/alerts_sent.json`

No Claude needed — this is pure Python logic.

### BUY NOW thresholds (already in config.py)
```python
BUY_NOW = {
    "used_2024":      {"max_price": 31_500, "max_km": 25_000},
    "used_2025":      {"max_price": 35_000, "max_km": 15_000},
    "new_2026":       {"max_price": 39_000, "max_km": None},
    "new_2025_clear": {"max_price": 34_500, "max_km": None},
}
```

### Email format
```
Subject: CX-5 BUY NOW -- 2024 GS $30,900 / 22,400 km -- Dealer Name

A listing crossed your BUY NOW threshold.

Year:    2024
Trim:    GS
Price:   $30,900 CAD
Km:      22,400
Dealer:  Dealer Name (City)
Link:    https://www.autotrader.ca/...

Threshold: used 2024 under $31,500 and under 25,000 km.

---
CX-5 Tracker - automated alert
```

### Integration
Call `check_alerts(listings)` in `run_tracker.py` before generating the full HTML report.

### Success criteria
Add a fake listing with price $30,000 and km 10,000 to `state/raw_listings.json`. Run `python run_tracker.py`. Alert email arrives. Re-run immediately — no duplicate email.

---

## Step 5 — Multi-car support DONE ✓

### Problem being solved
The buyer is cross-shopping across makes (e.g. CX-5 vs Honda CR-V vs Toyota RAV4). The scraper and config are hardcoded for CX-5 only.

### What to build

**5a. Extend config.py with a car watchlist**

```python
CARS = {
    "cx5": {
        "label":      "Mazda CX-5",
        "search_url": "https://www.autotrader.ca/cars/mazda/cx-5/reg_on/cit_toronto/",
        "year_min":   2024,
        "year_max":   2026,
        "buy_now": {
            "used_2024": {"max_price": 31_500, "max_km": 25_000},
            "new_2026":  {"max_price": 39_000, "max_km": None},
        },
    },
    "crv": {
        "label":      "Honda CR-V",
        "search_url": "https://www.autotrader.ca/cars/honda/cr-v/reg_on/cit_toronto/",
        "year_min":   2024,
        "year_max":   2026,
        "buy_now": {
            "used_2024": {"max_price": 33_000, "max_km": 30_000},
        },
    },
    # Add more cars without touching any other file
}
```

**5b. Parameterize scraper.py**

Add a `--car` argument:
```
python scraper.py --car cx5   # saves to state/cx5/raw_listings.json
python scraper.py --car crv   # saves to state/crv/raw_listings.json
```

Defaults to the first entry in `CARS` if `--car` is omitted.

**5c. Move to per-car state directories**

- `state/cx5/raw_listings.json`
- `state/cx5/listings.json`
- `state/cx5/alerts_sent.json`
- `state/crv/raw_listings.json`
- (etc.)

**5d. Update GitHub Actions workflow**

```yaml
- name: Run scraper for all cars
  run: |
    python scraper.py --car cx5
    python scraper.py --car crv
```

**5e. Per-car reports and emails**

Each car gets its own report file: `output/report_cx5_YYYY-MM-DD.html`. Email subject includes the car label. Optionally add a one-page comparison summary across all cars.

### Success criteria
`python scraper.py --car crv` produces a valid `state/crv/raw_listings.json` with Honda CR-V listings. `python run_tracker.py` generates and emails reports for all configured cars.

---

## Step 6 — Price history sparklines DONE ✓

### Problem being solved
The state JSON tracks price over time per listing, but this data is invisible in the HTML output. A visual trend shows whether a car is dropping consistently, holding firm, or was re-priced upward.

### What to build

Add a small sparkline to each listing card in the **desktop report** using Chart.js via CDN. Do not add to the mobile report.

```html
<script src="https://cdn.jsdelivr.net/npm/chart.js@4/dist/chart.umd.min.js"></script>
```

State schema — append to each listing entry in `state/{car}/listings.json`:
```json
"price_history": [
    {"date": "2026-04-24", "price": 34998},
    {"date": "2026-04-28", "price": 33500}
]
```

Only append a new entry when the price changes. Update `last_seen` on every run.

Color: green if last price < first price, red if higher, orange if flat.

### Success criteria
After 2+ runs with the same listing at different prices, the desktop report shows a price trend line on that card.

---

## Step 7 — Dealer reputation layer DONE ✓

### Problem being solved
Accumulated run data answers useful questions: Does this dealer negotiate? Do they relist the same cars? Are their prices consistently above or below market?

### What to build

A `dealer_stats` section in `state/{car}/listings.json`, updated on every run:

```json
"dealer_stats": {
    "Airport Mazda of Toronto": {
        "listings_seen": 12,
        "avg_days_on_lot": 18,
        "price_drop_rate": 0.67,
        "avg_price_drop_pct": 3.2,
        "relists_detected": 2
    }
}
```

Add a **Section H: Dealer Intelligence** table to the desktop report. Columns: Dealer | Active Listings | Avg Days on Lot | Drops Price? | Avg Drop % | Relists.

Color-code "Drops Price?": green if drop rate > 50%, orange if 20-50%, gray if under 20%.

**Relist detection:** flag when a listing disappears then reappears with a different ID but matching dealer + year + trim + mileage within +/- 2,000 km.

### Success criteria
After 5+ runs, the dealer table shows meaningful differentiation. At least one dealer is flagged as a consistent price-dropper.

---

## Step 8 — Depreciation benchmark DONE ✓

### Problem being solved
Comparing a listing's price against the market average is a weak signal. A stronger signal is: how does this price compare to where the car should be given its age and mileage?

### What to build

The depreciation curve is already in `config.py`. Use it during analysis to compute expected fair value per listing:

```python
def expected_value(year, trim, km):
    curve = DEPRECIATION_CURVE.get(f"{year}_{trim}")
    if not curve:
        return None
    for band in curve["bands"]:
        if km <= band["km_max"]:
            return round(curve["msrp"] * band["retain_pct"])
    return None

def depreciation_delta(listing_price, expected):
    if expected is None:
        return None
    return round(((listing_price - expected) / expected) * 100, 1)
```

Add a `vs. expected` column to the desktop report: `+12%` (orange) or `-8%` (green).

Add one line to each mobile report card: "Priced 12% above expected for mileage" or "Priced 8% below — strong value signal."

Update `DEPRECIATION_CURVE` in `config.py` every 6 months as the market shifts.

### Success criteria
A used listing priced $35,000 at 38,000 km correctly shows `+28% above expected` and its value rating reflects that regardless of the raw market average.

---

## Step 9 — GitHub Pages dashboard DONE ✓

### Problem being solved
Emailed HTML attachments work but are clunky. A bookmarked URL on your phone is faster. This step makes the latest report always accessible at a stable URL — and is the foundation for a future web app.

### What to build

Enable GitHub Pages on the repo (Settings -> Pages -> deploy from `docs/` folder).

Add a step to the workflow that copies the latest mobile report to `docs/index.html`:

```yaml
- name: Publish to GitHub Pages
  run: |
    cp output/report_*_mobile.html docs/index.html
    git add docs/index.html
    git commit -m "pages: update $(date +%Y-%m-%d)"
    git push
```

The mobile report is now at `https://{username}.github.io/{repo}/` — bookmark it on your phone.

For multi-car: each car gets its own page (`docs/cx5.html`, `docs/crv.html`) with a simple index linking to all of them.

### Success criteria
Opening `https://{username}.github.io/{repo}/` on your phone shows the latest mobile report, updated automatically after each run.

---

## Implementation order summary

| Step | File(s) | Status |
|---|---|---|
| 1 | `scraper.py`, `config.py` | ✓ Done |
| 2 | `.github/workflows/tracker.yml` | ✓ Done |
| 3 | `run_tracker.py`, `.env` | ✓ Done |
| 4 | `alerts.py` | ✓ Done |
| 5 | `config.py`, `scraper.py --car` | ✓ Done |
| 6 | HTML report sparklines | ✓ Done |
| 7 | `state/{car}/listings.json` dealer stats | ✓ Done |
| 8 | `config.py` depreciation benchmark | ✓ Done |
| 9 | `.github/workflows/`, `docs/` GitHub Pages | ✓ Done |
| 10 | `tests/test_config_consistency.py` | ◯ Planned |
| 11 | `config.py` curve timestamp + report banner | ◯ Planned |
| 12 | trim `CLAUDE.md` to halve input tokens | ◯ Planned |

---

## Current system status (as of 2026-04-26)

All 9 steps are complete and verified working end-to-end:

- **Scraper** — Selenium + Chrome headless, scrapes AutoTrader.ca, handles timeouts gracefully (page load timeout 60s, saves partial results on crash)
- **Pipeline** — `run_tracker.py` loops all CARS, two separate Claude API calls per car (desktop + mobile), prevents token truncation
- **Email** — Gmail SMTP with App Password, confirmed delivering to the configured `ALERT_RECIPIENT`
- **GitHub Pages** — Live at `https://phgo04.github.io/autodealtracker/`, updates automatically after each run
- **Schedule** — Runs every 3 days at noon UTC (8am EDT) via GitHub Actions cron; also triggerable manually from the Actions tab

### Known issues / future ideas
- CR-V prompt file not yet written — scraper runs for CR-V but `run_tracker.py` skips it (no prompt file)
- Sparklines and dealer stats require 2+ runs to accumulate meaningful data
- Workflow dispatch could accept a `skip_scrape` boolean input for faster test runs (not yet implemented)

---

## Step 10 — Config-consistency CI lint (planned)

### Problem being solved
Numerical thresholds (price targets, mileage limits, BUY NOW criteria) currently live in **two** places that must stay in sync: `config.py` (machine-readable, drives alerts) and `CLAUDE.md` (prose, drives AI report logic). A drift between them is a silent bug — a listing could trigger a BUY NOW email but be rated only "Fair" in the report, or vice versa.

### What to build
A small test that fails CI when the two sources disagree.

- Add `tests/test_config_consistency.py`
- Parse `CLAUDE.md` and extract the numerical thresholds it cites (Sections 2 and 5)
- Compare against the corresponding values in `config.py` (`CARS[<key>]["buy_now"]`, `USED_FILTERS`, `NEW_FILTERS`)
- Assert each match — fail with a clear message naming the diverging value
- Add a "test" step to `.github/workflows/tracker.yml` **before** the scraper step so a broken commit can't burn an 8-minute scrape run

### Why this approach over a unified config
A single-source-of-truth refactor (YAML + templating, or stripping numbers from CLAUDE.md entirely) is cleaner but ~1 day of work and requires rewriting prose. The CI lint is ~1 hour and gets 80% of the benefit — drift becomes loud instead of silent. Promote to a unified config later if thresholds start changing more than 2–3 times a year.

### Success criteria
A deliberate mismatch (change `max_price` in `config.py` but not in `CLAUDE.md`) causes CI to fail with a message like `Mismatch: CARS["cx5"]["buy_now"]["used_2024"]["max_price"] = 31500 but CLAUDE.md cites $32,000 in Section 5`.

---

## Step 11 — Stale depreciation curve warning (planned)

### Problem being solved
The depreciation curve in `config.py` reflects the Ontario market at a point in time. Mazda raises CX-5 MSRPs ~2–3% annually, and used market dynamics shift faster. The comment says "Update every 6 months" but there is no enforcement — a 2-year-old curve will silently produce misleading "vs. expected" percentages, and the user might never notice.

### What to build
A timestamped curve with a runtime staleness check.

- Add a `last_updated: "YYYY-MM-DD"` field to each curve entry (or a single top-level field) in `config.py`
- In `run_tracker.py`, compute the age of the curve at the start of each run
- If age > 180 days, prepend a warning banner to the report:
  > ⚠️ Depreciation curve is N days old — the "vs. expected" column may be unreliable. Refresh `DEPRECIATION_CURVE` in `config.py`.
- Optionally: have GitHub Actions auto-open an issue on the 180-day anniversary tagged `maintenance`

### Why not auto-update the curve
- Self-fitting against scraped listings is a feedback loop — if the market is over- or under-pricing, the curve absorbs the bias and the "vs expected" signal disappears
- Authoritative third-party sources (Black Book, etc.) require paid API access
- A 5–10 entry curve is small enough that human judgment is appropriate; automation here would replace 30 minutes of refresh work every 6 months with much more code to maintain

### Eventual replacement (not part of this step)
Long-term, the depreciation curve could be replaced by a quartile signal: "this listing is in the bottom 25% of comparable listings (same year, similar mileage)." That's purely computed, self-updating, and generalizes to any car make. But it loses the absolute-MSRP anchor — defer this decision until adding a second car make exposes the curve's scaling problem.

### Success criteria
After 180 simulated days, the next run produces a report with a visible "curve is stale" banner. After updating `last_updated` to a recent date, the banner disappears.

---

## Step 12 — Trim CLAUDE.md to fit comfortably under input rate limit (planned)

### Problem being solved
On 2026-04-28, the workflow started failing with `RateLimitError: 429` on the second of the two Claude API calls per car. Anthropic's Tier 1 input rate limit on Haiku 4.5 is 50,000 tokens per minute. Each call sends ~35K input tokens (CLAUDE.md ~30K + listings ~5K). Two back-to-back calls = 70K within a rolling 60s window → over the limit.

A short-term fix has shipped: `time.sleep(65)` between the two calls plus `max_retries=5` on the SDK client. This works but adds 65 seconds of dead time per car. With CR-V eventually enabled (and 4 calls per run), the dead time grows linearly.

### What to build
Trim CLAUDE.md from ~30K tokens to ~15K tokens by collapsing redundant or low-value sections, so each call drops to ~20K input tokens. Two back-to-back calls = 40K, comfortably under 50K. The `time.sleep(65)` can then be removed.

Sections that are *guidance/philosophy* the model already absorbs from training and likely doesn't need spelled out:
- Section 1 (goal narrative) — collapse to 2 sentences
- Sections 14–18 (buyer guidance, inspection mindset, output tone, decision philosophy) — most of this is implicit in any analytical car-shopping prompt
- Section 19 (implementation notes) — pure dev metadata, the runtime model doesn't need it
- Section 20 (scope summary) — already covered by Section 2

Sections that should *not* be trimmed:
- Section 2 (user constraints/preferences) — drives every decision
- Section 5 (value classification thresholds) — concrete logic
- Section 9 (output structure) — drives report shape
- Section 10 (HTML output spec) — drives rendering
- Sections 6, 7, 8 (price drop, promo, financing) — concrete rules

### How to verify quality didn't regress
1. Save the current CLAUDE.md as `CLAUDE-v1.md` and the trimmed version as `CLAUDE.md`
2. Run both versions against an identical `state/cx5/raw_listings.json` snapshot
3. Diff the resulting reports. Acceptable: minor wording differences. Unacceptable: missing sections, different value ratings, missing dealer links.
4. If it regresses, identify which trimmed section the model was actually using and restore just that part.

### Bonus
Halves the monthly Claude API cost (input tokens dominate the bill). At current cadence: $0.20/mo → $0.12/mo. Adds headroom to enable CR-V analysis without rate-limit gymnastics.

### Success criteria
- A run completes without `time.sleep` between the two API calls
- Reports are visually and analytically equivalent to the v1 outputs
- Total per-run input tokens drops from ~70K to ~40K (verifiable in workflow logs)
