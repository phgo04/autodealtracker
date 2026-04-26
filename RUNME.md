# How to Run AutoDealTracker

The tracker runs fully automatically on GitHub Actions — no PC needs to be on, no Claude Code session needed. Everything below is for reference or manual intervention only.

---

## Normal operation — fully automated

The workflow runs every **3 days at 8am EDT** (noon UTC). It:

1. Scrapes AutoTrader.ca for all configured cars
2. Calls the Claude API to generate desktop + mobile HTML reports
3. Sends the mobile report by email to the configured recipient
4. Publishes the mobile report to GitHub Pages
5. Commits updated state files back to the repo

**Nothing to do.** Reports arrive in your inbox and the GitHub Pages URL updates automatically.

---

## Manual trigger (run now without waiting for the schedule)

1. Go to your repo on GitHub
2. Click the **Actions** tab
3. Select **AutoDealTracker** in the left sidebar
4. Click **Run workflow** → **Run workflow**

Takes about 10–12 minutes total (scraper ~8 min, Claude API ~2 min, email + pages ~1 min).

---

## Where to find your reports

**Email** — mobile report arrives at your configured `ALERT_RECIPIENT` after every run.

**GitHub Pages** — bookmark this on your phone:
```
https://{your-github-username}.github.io/{your-repo-name}/
```
Links to each car's latest mobile report. Updates automatically after every run.

**Local output** — if you run manually on your PC, reports are saved to the `output/` folder:
- `report_cx5_2026-04-28.html` — full desktop report (open in laptop browser)
- `report_cx5_2026-04-28_mobile.html` — mobile version

---

## Running locally (optional, for development)

Requires Python 3.9+, Chrome, and a `.env` file with credentials.

**Step 1 — Create `.env`** (use PowerShell to avoid encoding issues):
```powershell
@"
ANTHROPIC_API_KEY=sk-ant-...
GMAIL_ADDRESS=your-email@gmail.com
GMAIL_APP_PASSWORD=xxxx-xxxx-xxxx-xxxx
ALERT_RECIPIENT=your-email@gmail.com
"@ | Out-File -FilePath .env -Encoding utf8
```

**Step 2 — Install dependencies:**
```
pip install -r requirements.txt
```

**Step 3 — Scrape listings:**
```
python scraper.py --car cx5
```
Takes 3–8 minutes. Saves to `state/cx5/raw_listings.json`. Use `--test` for a quick single-page check.

**Step 4 — Generate reports and send email:**
```
python run_tracker.py
```

**Test email credentials only** (without running the full pipeline):
```
python test_email.py
```

---

## GitHub Secrets (required for Actions)

Go to repo **Settings → Secrets and variables → Actions** and ensure these are set:

| Secret | Value |
|---|---|
| `ANTHROPIC_API_KEY` | From console.anthropic.com |
| `GMAIL_ADDRESS` | Your Gmail address |
| `GMAIL_APP_PASSWORD` | 16-char App Password from Google Account → Security → App passwords |
| `ALERT_RECIPIENT` | Email address to receive reports |

> **App Password note:** Requires 2-Step Verification enabled on your Google account. Generate at myaccount.google.com/security → 2-Step Verification → App passwords.

---

## GitHub Pages — one-time setup

Do this once before the first run:

1. Go to your repo on GitHub
2. Click **Settings** → **Pages** (left sidebar)
3. Under **Source**, select **Deploy from a branch**
4. Set **Branch** to `main` and **Folder** to `/docs`
5. Click **Save**

GitHub Pages will be live within a minute. The tracker updates it automatically after every run.

---

## How price tracking works

Each run reads `state/{car}/listings.json` to detect price drops since the last run. Do not delete or edit those files manually. Old reports in `output/` are safe to delete.

Features that improve with more runs:
- **Price sparklines** — appear on desktop reports after 2+ runs with the same listing at different prices
- **Dealer reputation stats** — become meaningful after 5+ runs
- **Price drop detection** — active from run #2 onward

---

## Adding a new car to track

1. Add an entry to `CARS` in `config.py`
2. Create a prompt file for the car (e.g. `prompts/crv.md`) — copy `CLAUDE.md` and adapt the vehicle preferences
3. Add `python scraper.py --car {key}` to the scraper step in `.github/workflows/tracker.yml`

The pipeline picks it up automatically on the next run.
