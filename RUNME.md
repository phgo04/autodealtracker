# How to Run AutoDealTracker

## Step 1 — Scrape fresh listings (run this first)

Open a terminal in this folder and run:

```
python scraper.py
```

This takes about 3–4 minutes. It scrapes AutoTrader.ca for all 2024–2026 Mazda CX-5 listings in Ontario and saves them to `state/raw_listings.json` (~300 listings). You'll see a live page-by-page count as it runs.

To do a quick test (first page only, no save):
```
python scraper.py --test
```

---

## Step 2 — Generate the reports

After the scraper finishes, open Claude Code in this folder and paste this exact message:

---

Run the Mazda CX-5 tracker. Your master prompt and analysis rules are in CLAUDE.md — read that first. Load the previous state from state/listings.json. Load the current listings from state/raw_listings.json — do not search the web, all data is already in that file. Generate the full report as defined in the CLAUDE.md output structure. Save the updated state to state/listings.json. Save the full desktop report to output/report_YYYY-MM-DD.html and the mobile report to output/report_YYYY-MM-DD_mobile.html (use today's actual date in both filenames). All dealer names must be hyperlinks to the actual listing or dealer inventory page.

---

## Where to find your reports:

Two files are created in the `output/` folder after each run:

- `report_2026-04-28.html` — full report, open this on your laptop
- `report_2026-04-28_mobile.html` — mobile version, email this to yourself to read on your phone

## How price tracking works:

Each run reads `state/listings.json` to detect price drops since the last run. Do not delete or edit that file manually. Old reports in `output/` are safe to delete whenever you want.

## How often to run:

Every 3–4 days is ideal. The market moves slowly enough that daily runs add little value.

---

## GitHub Pages — bookmark your reports on your phone

After the first workflow run, your mobile reports are available at a permanent URL:

```
https://{your-github-username}.github.io/{your-repo-name}/
```

That index page links to:
- `/cx5.html` — Mazda CX-5 latest mobile report
- `/crv.html` — Honda CR-V latest mobile report (once a CR-V prompt is added)

**One-time setup (do this once, before the first run):**

1. Go to your repo on GitHub
2. Click **Settings** → **Pages** (left sidebar)
3. Under **Source**, select **Deploy from a branch**
4. Set **Branch** to `main` and **Folder** to `/docs`
5. Click **Save**

GitHub Pages will be live within a minute. Bookmark the URL on your phone — it updates automatically after every workflow run. No app, no login, no attachment to open.
