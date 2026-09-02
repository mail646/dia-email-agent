#!/usr/bin/env python3
"""
Reads dia_events.db and generates a static HTML board (board.html)
grouped by year group and sorted by date.
"""

import sqlite3
import os
from datetime import datetime, date

DB_PATH = os.environ.get("DIA_DB_PATH", os.path.join(os.path.dirname(__file__), "dia_events.db"))
OUTPUT_PATH = os.path.join(os.path.dirname(__file__), "board.html")

CATEGORY_COLORS = {
    "deadline": "#e63946",
    "event": "#2a9d8f",
    "task": "#e9c46a",
    "announcement": "#8d99ae",
}


def fetch_events():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    rows = conn.execute("""
        SELECT * FROM events
        ORDER BY (date IS NULL), date ASC
    """).fetchall()
    conn.close()
    return rows


def build_html(rows):
    by_year = {}
    for r in rows:
        by_year.setdefault(r["year_group"] or "All", []).append(r)

    today = date.today().isoformat()

    sections = []
    for year, items in sorted(by_year.items()):
        rows_html = ""
        for r in items:
            is_past = r["date"] and r["date"] < today
            color = CATEGORY_COLORS.get(r["category"], "#8d99ae")
            rows_html += f"""
            <div class="item {'past' if is_past else ''}">
                <div class="dot" style="background:{color}"></div>
                <div class="item-content">
                    <div class="item-title">{r['title'] or '(untitled)'}</div>
                    <div class="item-summary">{r['summary'] or ''}</div>
                    <div class="item-meta">{r['date'] or 'no date'} · {r['category'] or ''} · from "{r['email_subject']}"</div>
                </div>
            </div>"""
        sections.append(f"""
        <section>
            <h2>{year}</h2>
            {rows_html or '<p class="empty">Nothing yet.</p>'}
        </section>""")

    return f"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<title>DIA Emirates Hills — Parent Board</title>
<style>
  body {{ font-family: -apple-system, Helvetica, Arial, sans-serif; max-width: 700px; margin: 40px auto; padding: 0 16px; background: #fafafa; color: #222; }}
  h1 {{ font-size: 1.4em; }}
  h2 {{ margin-top: 32px; border-bottom: 2px solid #ddd; padding-bottom: 6px; }}
  .item {{ display: flex; gap: 12px; padding: 10px 0; border-bottom: 1px solid #eee; }}
  .item.past {{ opacity: 0.4; }}
  .dot {{ width: 10px; height: 10px; border-radius: 50%; margin-top: 6px; flex-shrink: 0; }}
  .item-title {{ font-weight: 600; }}
  .item-summary {{ color: #444; font-size: 0.95em; margin: 2px 0; }}
  .item-meta {{ color: #888; font-size: 0.8em; }}
  .empty {{ color: #999; font-style: italic; }}
  .updated {{ color: #999; font-size: 0.85em; }}
</style>
</head>
<body>
  <h1>DIA Emirates Hills — Parent Board</h1>
  <p class="updated">Last updated: {datetime.now().strftime('%Y-%m-%d %H:%M')}</p>
  {''.join(sections)}
</body>
</html>"""


def main():
    rows = fetch_events()
    html = build_html(rows)
    with open(OUTPUT_PATH, "w") as f:
        f.write(html)
    print(f"Wrote {OUTPUT_PATH} ({len(rows)} events)")


if __name__ == "__main__":
    main()
