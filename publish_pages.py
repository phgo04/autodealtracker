#!/usr/bin/env python3
"""
Publish the latest mobile reports to docs/ for GitHub Pages.
Called from the GitHub Actions workflow after run_tracker.py.

For each car in CARS:
  - Copies output/report_{car}_{date}_mobile.html  ->  docs/{car}.html
  - Generates docs/index.html linking to all published car pages

GitHub Pages must be enabled on the repo:
  Settings -> Pages -> Source: Deploy from a branch -> Branch: main, Folder: /docs
"""

from datetime import date
from pathlib import Path

from config import CARS, OUTPUT_DIR

DOCS_DIR = Path(__file__).parent / "docs"
TODAY    = date.today().isoformat()


def main() -> None:
    DOCS_DIR.mkdir(exist_ok=True)

    published: list[tuple[str, str]] = []

    for car_key, car_config in CARS.items():
        label = car_config["label"]
        # Find the most-recent mobile report for this car (sorted by filename = by date)
        candidates = sorted(OUTPUT_DIR.glob(f"report_{car_key}_*_mobile.html"))
        if not candidates:
            print(f"  [{car_key}] No mobile report found — skipping.")
            continue
        latest = candidates[-1]
        dest = DOCS_DIR / f"{car_key}.html"
        dest.write_bytes(latest.read_bytes())
        print(f"  [{car_key}] {latest.name} -> docs/{car_key}.html")
        published.append((car_key, label))

    if not published:
        print("No reports published — docs/index.html not updated.")
        return

    _write_index(published)
    print(f"docs/index.html generated with {len(published)} car(s).")


def _write_index(published: list[tuple[str, str]]) -> None:
    links_html = "\n    ".join(
        f'<li><a href="{key}.html">{label}</a></li>'
        for key, label in published
    )

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>AutoDealTracker</title>
  <style>
    * {{ box-sizing: border-box; margin: 0; padding: 0; }}
    body {{
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
      background: #f1f5f9;
      min-height: 100vh;
      display: flex;
      align-items: center;
      justify-content: center;
      padding: 24px;
    }}
    .card {{
      background: #fff;
      border-radius: 18px;
      padding: 32px 28px 28px;
      box-shadow: 0 4px 24px rgba(0,0,0,.09);
      width: 100%;
      max-width: 420px;
    }}
    h1 {{
      font-size: 1.45rem;
      font-weight: 700;
      color: #0f172a;
      margin-bottom: 4px;
    }}
    .sub {{
      color: #64748b;
      font-size: 0.88rem;
      margin-bottom: 28px;
    }}
    ul {{
      list-style: none;
      display: flex;
      flex-direction: column;
      gap: 12px;
    }}
    a {{
      display: block;
      padding: 16px 20px;
      background: #2563eb;
      color: #fff;
      border-radius: 11px;
      text-decoration: none;
      font-size: 1.05rem;
      font-weight: 600;
      letter-spacing: 0.01em;
    }}
    a:active {{ opacity: 0.88; }}
    .footer {{
      color: #94a3b8;
      font-size: 0.78rem;
      margin-top: 24px;
      text-align: center;
    }}
  </style>
</head>
<body>
  <div class="card">
    <h1>AutoDealTracker</h1>
    <p class="sub">Ontario dealer inventory &mdash; updated every 3 days</p>
    <ul>
    {links_html}
    </ul>
    <p class="footer">Last updated: {TODAY}</p>
  </div>
</body>
</html>"""

    (DOCS_DIR / "index.html").write_text(html, encoding="utf-8")


if __name__ == "__main__":
    main()
