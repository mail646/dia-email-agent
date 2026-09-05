#!/usr/bin/env python3
"""
Reads dia_events.db and generates a single self-contained HTML board
with tabs (Deadlines/Events/Email Summaries/Calendar), search, topic
filters, relative date labels, and a collapsible "Passed" section.
"""

import sqlite3
import os
import json
from datetime import datetime

DB_PATH = os.environ.get("DIA_DB_PATH", os.path.join(os.path.dirname(__file__), "dia_events.db"))
OUTPUT_PATH = os.path.join(os.path.dirname(__file__), "board.html")


def fetch_events():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT * FROM events ORDER BY (date IS NULL), date ASC").fetchall()
    conn.close()
    return [dict(r) for r in rows]


def build_html(rows):
    data_json = json.dumps(rows, default=str)
    updated = datetime.now().strftime('%Y-%m-%d %H:%M')

    return f"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>DIA Emirates Hills — Parent Board</title>
<style>
  :root {{ --ink:#1a1a1a; --muted:#767676; --line:#e3e3e3; --bg:#fafafa; --card:#fff;
    --deadline:#c0392b; --event:#1a7a6a; --accent:#1a1a1a; }}
  * {{ box-sizing: border-box; }}
  body {{ font-family: -apple-system, Helvetica, Arial, sans-serif; max-width: 780px; margin: 0 auto;
    padding: 20px 16px 60px; background: var(--bg); color: var(--ink); }}
  h1 {{ font-size: 1.5em; margin-bottom: 2px; }}
  .sub {{ color: var(--muted); font-size: 0.85em; margin-bottom: 16px; }}
  .disclaimer {{ background: var(--card); border: 1px solid var(--line); border-left: 4px solid var(--accent);
    padding: 14px 16px; border-radius: 6px; font-size: 0.88em; color: #444; margin-bottom: 18px; }}
  .toolbar {{ display: flex; flex-wrap: wrap; gap: 8px; margin-bottom: 16px; }}
  input[type=text] {{ flex: 1 1 100%; padding: 10px 12px; border: 1px solid var(--line); border-radius: 6px; font-size: 0.95em; }}
  .chip {{ padding: 6px 12px; border-radius: 999px; border: 1px solid var(--line); background: var(--card);
    font-size: 0.82em; cursor: pointer; color: var(--muted); }}
  .chip.active {{ background: var(--accent); color: #fff; border-color: var(--accent); }}
  .tabs {{ display: flex; gap: 4px; margin-bottom: 16px; border-bottom: 2px solid var(--line); overflow-x: auto; }}
  .tab {{ padding: 10px 14px; font-size: 0.88em; color: var(--muted); cursor: pointer; white-space: nowrap; }}
  .tab.active {{ color: var(--ink); font-weight: 600; border-bottom: 2px solid var(--ink); margin-bottom: -2px; }}
  .view {{ display: none; }}
  .view.active {{ display: block; }}
  .group-heading {{ font-weight: 700; margin: 18px 0 6px; font-size: 0.8em; text-transform: uppercase;
    letter-spacing: 0.03em; color: var(--muted); }}
  .item {{ display: flex; gap: 10px; padding: 10px 0; border-bottom: 1px solid var(--line); }}
  .item.past {{ opacity: 0.45; }}
  .rel {{ flex: 0 0 84px; font-size: 0.72em; font-weight: 700; color: var(--muted); padding-top: 2px; }}
  .rel.soon {{ color: var(--deadline); }}
  .dot {{ width: 9px; height: 9px; border-radius: 50%; margin-top: 6px; flex: 0 0 auto; }}
  .item-title {{ font-weight: 600; font-size: 0.97em; }}
  .item-summary {{ color: #444; font-size: 0.9em; margin: 3px 0; }}
  .item-conflict {{ color: #a15c00; font-size: 0.82em; margin: 3px 0; font-style: italic; }}
  .item-meta {{ color: #999; font-size: 0.76em; }}
  .tag {{ display: inline-block; font-size: 0.68em; padding: 1px 6px; border-radius: 4px; background: #eee;
    color: #555; margin-right: 4px; }}
  .empty {{ color: #999; font-style: italic; padding: 20px 0; }}
  .toggle-passed {{ margin-top: 10px; font-size: 0.82em; color: var(--muted); cursor: pointer; text-align: center;
    padding: 8px; border: 1px dashed var(--line); border-radius: 6px; }}
  .passed-section {{ display: none; }}
  .passed-section.show {{ display: block; }}
  .cal-header {{ display: flex; justify-content: space-between; align-items: center; margin-bottom: 10px; }}
  .cal-btn {{ background: var(--card); border: 1px solid var(--line); border-radius: 6px; padding: 6px 12px; cursor: pointer; }}
  .cal-grid {{ display: grid; grid-template-columns: repeat(7, 1fr); gap: 2px; font-size: 0.72em; }}
  .cal-day-head {{ text-align: center; color: var(--muted); font-weight: 600; padding: 4px 0; }}
  .cal-cell {{ min-height: 60px; border: 1px solid var(--line); border-radius: 4px; padding: 3px; background: var(--card); }}
  .cal-cell.other-month {{ opacity: 0.3; }}
  .cal-daynum {{ font-weight: 600; }}
  .cal-event {{ font-size: 0.65em; border-radius: 3px; padding: 1px 3px; margin-top: 2px; color: #fff; overflow: hidden;
    white-space: nowrap; text-overflow: ellipsis; }}
</style>
</head>
<body>
  <h1>DIA Emirates Hills — Parent Board</h1>
  <div class="sub">Last updated: {updated}</div>
  <div class="disclaimer">
    An unofficial digest kept by a parent. Every entry is summarised from an email the school sent; nothing here
    is written or endorsed by the school. Dates are copied as the school wrote them — check the original email
    before acting on anything.
  </div>

  <input type="text" id="search" placeholder="Search titles, summaries..." oninput="render()">
  <div class="toolbar" id="topicChips"></div>

  <div class="tabs">
    <div class="tab active" data-view="deadlines" onclick="switchTab('deadlines')">Deadlines</div>
    <div class="tab" data-view="events" onclick="switchTab('events')">Events</div>
    <div class="tab" data-view="calendar" onclick="switchTab('calendar')">Calendar</div>
    <div class="tab" data-view="summaries" onclick="switchTab('summaries')">All Items</div>
  </div>

  <div class="view active" id="view-deadlines"></div>
  <div class="view" id="view-events"></div>
  <div class="view" id="view-calendar"></div>
  <div class="view" id="view-summaries"></div>

<script>
const DATA = {data_json};
const TOPIC_COLORS = {{Academic:'#4a6fa5', Admin:'#8d99ae', CCAs:'#2a9d8f', Clinic:'#c0392b',
  Events:'#e9a13a', PE:'#6a4c93', Payments:'#c0392b'}};
const CATEGORY_COLORS = {{deadline:'#c0392b', event:'#1a7a6a', task:'#e9c46a', announcement:'#8d99ae'}};

let activeTopic = 'All';
let currentTab = 'deadlines';
let calMonth = new Date();
let showPassedDeadlines = false;
let showPassedEvents = false;

function todayStr() {{ return new Date().toISOString().slice(0,10); }}

function relativeLabel(dateStr) {{
  if (!dateStr) return '';
  const today = new Date(todayStr());
  const d = new Date(dateStr);
  const diffDays = Math.round((d - today) / 86400000);
  if (diffDays === 0) return 'TODAY';
  if (diffDays === 1) return 'TOMORROW';
  if (diffDays === -1) return 'YESTERDAY';
  if (diffDays > 1 && diffDays <= 60) return `IN ${{diffDays}} DAYS`;
  if (diffDays < -1 && diffDays >= -60) return `${{Math.abs(diffDays)}} DAYS AGO`;
  return d.toLocaleDateString('en-GB', {{day:'numeric', month:'short', year:'numeric'}});
}}

function isPast(dateStr) {{
  if (!dateStr) return false;
  return dateStr < todayStr();
}}

function matchesFilters(item) {{
  const q = document.getElementById('search').value.toLowerCase();
  if (q && !(`${{item.title}} ${{item.summary}} ${{item.year_group}}`.toLowerCase().includes(q))) return false;
  if (activeTopic !== 'All' && item.topic !== activeTopic) return false;
  return true;
}}

function itemHtml(item) {{
  const past = isPast(item.date);
  const color = CATEGORY_COLORS[item.category] || '#8d99ae';
  const rel = relativeLabel(item.date);
  const relClass = (item.date && !past) ? 'soon' : '';
  const conflict = item.conflict_note ? `<div class="item-conflict">⚠ ${{item.conflict_note}}</div>` : '';
  return `<div class="item ${{past ? 'past' : ''}}">
    <div class="rel ${{relClass}}">${{rel}}</div>
    <div class="dot" style="background:${{color}}"></div>
    <div>
      <div class="item-title">${{item.title || '(untitled)'}}</div>
      <div class="item-summary">${{item.summary || ''}}</div>
      ${{conflict}}
      <div class="item-meta">
        <span class="tag">${{item.year_group || 'All'}}</span>
        <span class="tag">${{item.topic || ''}}</span>
        from "${{item.email_subject || ''}}"
      </div>
    </div>
  </div>`;
}}

function renderList(containerId, items, passedFlag, setPassedFlag) {{
  const upcoming = items.filter(i => !isPast(i.date));
  const passed = items.filter(i => isPast(i.date));
  let html = '';
  if (upcoming.length === 0 && passed.length === 0) {{
    html = '<div class="empty">Nothing here yet.</div>';
  }} else {{
    html += upcoming.map(itemHtml).join('');
    if (passed.length > 0) {{
      html += `<div class="toggle-passed" onclick="${{setPassedFlag}}">
        ${{passedFlag ? 'Hide' : 'Show'}} ${{passed.length}} that have passed
      </div>`;
      html += `<div class="passed-section ${{passedFlag ? 'show' : ''}}">${{passed.map(itemHtml).join('')}}</div>`;
    }}
  }}
  document.getElementById(containerId).innerHTML = html;
}}

function toggleDeadlinesPassed() {{ showPassedDeadlines = !showPassedDeadlines; render(); }}
function toggleEventsPassed() {{ showPassedEvents = !showPassedEvents; render(); }}

function renderCalendar() {{
  const year = calMonth.getFullYear(), month = calMonth.getMonth();
  const monthName = calMonth.toLocaleDateString('en-GB', {{month:'long', year:'numeric'}});
  const firstOfMonth = new Date(year, month, 1);
  const startOffset = (firstOfMonth.getDay() + 6) % 7; // Monday-first
  const daysInMonth = new Date(year, month + 1, 0).getDate();

  const byDate = {{}};
  DATA.filter(matchesFilters).forEach(item => {{
    if (!item.date) return;
    byDate[item.date] = byDate[item.date] || [];
    byDate[item.date].push(item);
  }});

  let cells = '';
  const totalCells = Math.ceil((startOffset + daysInMonth) / 7) * 7;
  for (let i = 0; i < totalCells; i++) {{
    const dayNum = i - startOffset + 1;
    const inMonth = dayNum >= 1 && dayNum <= daysInMonth;
    let cellContent = '';
    if (inMonth) {{
      const dateStr = `${{year}}-${{String(month+1).padStart(2,'0')}}-${{String(dayNum).padStart(2,'0')}}`;
      const dayItems = byDate[dateStr] || [];
      cellContent = `<div class="cal-daynum">${{dayNum}}</div>` +
        dayItems.slice(0,3).map(it => `<div class="cal-event" style="background:${{CATEGORY_COLORS[it.category]||'#8d99ae'}}" title="${{it.title}}">${{it.title}}</div>`).join('') +
        (dayItems.length > 3 ? `<div style="font-size:0.65em;color:#999">+${{dayItems.length-3}} more</div>` : '');
    }}
    cells += `<div class="cal-cell ${{inMonth ? '' : 'other-month'}}">${{cellContent}}</div>`;
  }}

  const dayHeads = ['Mon','Tue','Wed','Thu','Fri','Sat','Sun'].map(d => `<div class="cal-day-head">${{d}}</div>`).join('');

  document.getElementById('view-calendar').innerHTML = `
    <div class="cal-header">
      <button class="cal-btn" onclick="changeMonth(-1)">&larr;</button>
      <strong>${{monthName}}</strong>
      <button class="cal-btn" onclick="changeMonth(1)">&rarr;</button>
    </div>
    <div class="cal-grid">${{dayHeads}}${{cells}}</div>
  `;
}}

function changeMonth(delta) {{
  calMonth.setMonth(calMonth.getMonth() + delta);
  renderCalendar();
}}

function switchTab(view) {{
  currentTab = view;
  document.querySelectorAll('.tab').forEach(t => t.classList.toggle('active', t.dataset.view === view));
  document.querySelectorAll('.view').forEach(v => v.classList.toggle('active', v.id === 'view-' + view));
  render();
}}

function render() {{
  const filtered = DATA.filter(matchesFilters);
  renderList('view-deadlines', filtered.filter(i => i.category === 'deadline'), showPassedDeadlines, 'toggleDeadlinesPassed()');
  renderList('view-events', filtered.filter(i => i.category === 'event'), showPassedEvents, 'toggleEventsPassed()');
  renderList('view-summaries', filtered, true, '');
  if (currentTab === 'calendar') renderCalendar();
}}

function buildTopicChips() {{
  const topics = ['All', ...new Set(DATA.map(i => i.topic).filter(Boolean))];
  document.getElementById('topicChips').innerHTML = topics.map(t =>
    `<div class="chip ${{t === activeTopic ? 'active' : ''}}" onclick="setTopic('${{t}}')">${{t}}</div>`
  ).join('');
}}

function setTopic(t) {{
  activeTopic = t;
  buildTopicChips();
  render();
}}

buildTopicChips();
render();
</script>
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
