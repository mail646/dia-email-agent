#!/usr/bin/env python3
"""
Reads dia_events.db and generates a single self-contained HTML board
with tabs (Deadlines/Events/Calendar/All Items), search, topic
filters, urgency-colored deadlines, relative date labels, and a
collapsible "Passed" section.
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
    updated = datetime.now().strftime('%d %b %Y, %H:%M')

    return f"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>DIA Emirates Hills — Parent Board</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Source+Serif+4:wght@600;700&family=Inter:wght@400;500;600;700&display=swap" rel="stylesheet">
<style>
  :root {{
    --ink:#1c1c1e; --muted:#8a8a8e; --line:#e8e6e1; --bg:#faf9f6; --card:#ffffff;
    --urgent:#c0392b; --soon:#c98a1c; --later:#1a7a6a; --accent:#1c1c1e;
    --shadow: 0 1px 2px rgba(0,0,0,0.04), 0 4px 12px rgba(0,0,0,0.03);
  }}
  * {{ box-sizing: border-box; }}
  body {{
    font-family: 'Inter', -apple-system, Helvetica, Arial, sans-serif;
    max-width: 820px; margin: 0 auto; padding: 32px 18px 70px;
    background: var(--bg); color: var(--ink); line-height: 1.45;
  }}
  h1 {{
    font-family: 'Source Serif 4', Georgia, serif; font-weight: 700;
    font-size: 2em; margin: 0 0 4px; letter-spacing: -0.01em;
  }}
  .sub {{ color: var(--muted); font-size: 0.82em; margin-bottom: 22px; }}
  .disclaimer {{
    background: var(--card); border: 1px solid var(--line);
    padding: 16px 18px; border-radius: 10px; font-size: 0.86em; color: #555;
    margin-bottom: 22px; box-shadow: var(--shadow);
  }}
  .toolbar {{ display: flex; flex-wrap: wrap; gap: 8px; margin-bottom: 22px; }}
  input[type=text] {{
    flex: 1 1 100%; padding: 12px 14px; border: 1px solid var(--line);
    border-radius: 10px; font-size: 0.95em; background: var(--card);
    font-family: inherit; box-shadow: var(--shadow);
  }}
  input[type=text]:focus {{ outline: none; border-color: #c4c0b6; }}
  .chip {{
    padding: 7px 14px; border-radius: 999px; border: 1px solid var(--line);
    background: var(--card); font-size: 0.8em; font-weight: 500; cursor: pointer;
    color: var(--muted); transition: all 0.15s;
  }}
  .chip.active {{ background: var(--accent); color: #fff; border-color: var(--accent); }}
  .tabs {{
    display: flex; gap: 2px; margin-bottom: 24px; border-bottom: 1px solid var(--line);
    overflow-x: auto;
  }}
  .tab {{
    padding: 11px 16px; font-size: 0.88em; font-weight: 500; color: var(--muted);
    cursor: pointer; white-space: nowrap; border-bottom: 2px solid transparent;
    margin-bottom: -1px; transition: color 0.15s;
  }}
  .tab.active {{ color: var(--ink); font-weight: 700; border-bottom-color: var(--ink); }}
  .view {{ display: none; }}
  .view.active {{ display: block; }}
  h2.section-title {{
    font-family: 'Source Serif 4', Georgia, serif; font-weight: 700;
    font-size: 1.5em; margin: 0 0 14px;
  }}
  .item {{
    display: flex; gap: 0; background: var(--card); border-radius: 10px;
    margin-bottom: 8px; box-shadow: var(--shadow); overflow: hidden;
  }}
  .item.past {{ opacity: 0.5; }}
  .urgency-bar {{ width: 4px; flex: 0 0 4px; }}
  .item-body {{ display: flex; gap: 12px; padding: 14px 16px; flex: 1; }}
  .rel {{
    flex: 0 0 78px; font-size: 0.7em; font-weight: 700; letter-spacing: 0.02em;
    padding-top: 2px; text-transform: uppercase;
  }}
  .rel.urgent {{ color: var(--urgent); }}
  .rel.soon {{ color: var(--soon); }}
  .rel.later {{ color: var(--later); }}
  .rel.muted {{ color: var(--muted); }}
  .item-title {{ font-weight: 600; font-size: 0.98em; margin-bottom: 2px; }}
  .item-summary {{ color: #555; font-size: 0.9em; margin-bottom: 5px; }}
  .item-conflict {{
    color: #9a5b00; background: #fdf3e3; border-radius: 6px; padding: 4px 8px;
    font-size: 0.8em; margin-bottom: 5px; display: inline-block;
  }}
  .item-meta {{ color: #a3a3a3; font-size: 0.74em; }}
  .tag {{
    display: inline-block; font-size: 0.68em; font-weight: 600; padding: 2px 8px;
    border-radius: 5px; background: #f1efe9; color: #6b6a63; margin-right: 5px;
    letter-spacing: 0.01em;
  }}
  .empty {{ color: var(--muted); font-style: italic; padding: 28px 0; text-align: center; }}
  .toggle-passed {{
    margin: 12px 0; font-size: 0.82em; color: var(--muted); cursor: pointer;
    text-align: center; padding: 10px; border: 1px dashed var(--line); border-radius: 10px;
    background: var(--card);
  }}
  .passed-section {{ display: none; margin-top: 8px; }}
  .passed-section.show {{ display: block; }}
  .cal-header {{ display: flex; justify-content: space-between; align-items: center; margin-bottom: 14px; }}
  .cal-header strong {{ font-family: 'Source Serif 4', Georgia, serif; font-size: 1.2em; }}
  .cal-btn {{
    background: var(--card); border: 1px solid var(--line); border-radius: 8px;
    padding: 7px 14px; cursor: pointer; box-shadow: var(--shadow); font-size: 0.9em;
  }}
  .cal-grid {{ display: grid; grid-template-columns: repeat(7, 1fr); gap: 4px; font-size: 0.72em; }}
  .cal-day-head {{ text-align: center; color: var(--muted); font-weight: 600; padding: 6px 0; font-size: 0.85em; }}
  .cal-cell {{
    min-height: 64px; border-radius: 8px; padding: 5px; background: var(--card);
    box-shadow: var(--shadow);
  }}
  .cal-cell.other-month {{ opacity: 0.25; box-shadow: none; }}
  .cal-daynum {{ font-weight: 600; font-size: 0.9em; }}
  .cal-event {{
    font-size: 0.64em; border-radius: 4px; padding: 2px 4px; margin-top: 3px; color: #fff;
    overflow: hidden; white-space: nowrap; text-overflow: ellipsis;
  }}
</style>
</head>
<body>
  <h1>DIA Emirates Hills</h1>
  <div class="sub">Parent Board · Last updated {updated}</div>
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

  <h2 class="section-title" id="sectionTitle">Deadlines</h2>
  <div class="view active" id="view-deadlines"></div>
  <div class="view" id="view-events"></div>
  <div class="view" id="view-calendar"></div>
  <div class="view" id="view-summaries"></div>

<script>
const DATA = {data_json};
const CATEGORY_COLORS = {{deadline:'#c0392b', event:'#1a7a6a', task:'#c98a1c', announcement:'#8a8a8e'}};
const TITLES = {{deadlines:'Deadlines', events:'Events', calendar:'Calendar', summaries:'All Items'}};

let activeTopic = 'All';
let currentTab = 'deadlines';
let calMonth = new Date();
let showPassedDeadlines = false;
let showPassedEvents = false;

function todayStr() {{ return new Date().toISOString().slice(0,10); }}

function daysUntil(dateStr) {{
  if (!dateStr) return null;
  const today = new Date(todayStr());
  const d = new Date(dateStr);
  return Math.round((d - today) / 86400000);
}}

function relativeLabel(dateStr) {{
  const diffDays = daysUntil(dateStr);
  if (diffDays === null) return '';
  if (diffDays === 0) return 'TODAY';
  if (diffDays === 1) return 'TOMORROW';
  if (diffDays === -1) return 'YESTERDAY';
  if (diffDays > 1 && diffDays <= 60) return `IN ${{diffDays}} DAYS`;
  if (diffDays < -1 && diffDays >= -60) return `${{Math.abs(diffDays)}} DAYS AGO`;
  const d = new Date(dateStr);
  return d.toLocaleDateString('en-GB', {{day:'numeric', month:'short', year:'numeric'}});
}}

function urgencyClass(dateStr) {{
  const diffDays = daysUntil(dateStr);
  if (diffDays === null) return 'muted';
  if (diffDays < 0) return 'muted';
  if (diffDays <= 3) return 'urgent';
  if (diffDays <= 14) return 'soon';
  return 'later';
}}

function urgencyColor(dateStr) {{
  const cls = urgencyClass(dateStr);
  if (cls === 'urgent') return 'var(--urgent)';
  if (cls === 'soon') return 'var(--soon)';
  if (cls === 'later') return 'var(--later)';
  return 'var(--line)';
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
  const rel = relativeLabel(item.date);
  const relCls = past ? 'muted' : urgencyClass(item.date);
  const barColor = past ? 'var(--line)' : urgencyColor(item.date);
  const conflict = item.conflict_note ? `<div class="item-conflict">⚠ ${{item.conflict_note}}</div>` : '';
  return `<div class="item ${{past ? 'past' : ''}}">
    <div class="urgency-bar" style="background:${{barColor}}"></div>
    <div class="item-body">
      <div class="rel ${{relCls}}">${{rel}}</div>
      <div>
        <div class="item-title">${{item.title || '(untitled)'}}</div>
        <div class="item-summary">${{item.summary || ''}}</div>
        ${{conflict}}
        <div class="item-meta">
          <span class="tag">${{item.year_group || 'All'}}</span>
          ${{item.topic ? `<span class="tag">${{item.topic}}</span>` : ''}}
          from "${{item.email_subject || ''}}"
        </div>
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
  const startOffset = (firstOfMonth.getDay() + 6) % 7;
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
        dayItems.slice(0,3).map(it => `<div class="cal-event" style="background:${{CATEGORY_COLORS[it.category]||'#8a8a8e'}}" title="${{it.title}}">${{it.title}}</div>`).join('') +
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
  document.getElementById('sectionTitle').textContent = TITLES[view];
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
