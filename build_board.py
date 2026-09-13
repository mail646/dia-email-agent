#!/usr/bin/env python3
"""
Reads dia_events.db and generates a single self-contained HTML board
with a child selector (by year group), tabs (Deadlines/Events/Calendar/
All Items), fuzzy search, topic filters, urgency-colored deadlines,
relative date labels, and a collapsible "Passed" section.
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


def fetch_email_log():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute("SELECT * FROM email_log ORDER BY email_date DESC").fetchall()
    except sqlite3.OperationalError:
        rows = []  # table may not exist yet on an old DB that hasn't run the new ingest.py
    conn.close()
    return [dict(r) for r in rows]


def build_html(rows, email_log_rows=None):
    email_log_rows = email_log_rows or []
    data_json = json.dumps(rows, default=str)
    email_log_json = json.dumps(email_log_rows, default=str)
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
  .child-chip {{
    padding: 9px 18px; border-radius: 999px; border: 1px solid var(--line);
    background: var(--card); font-size: 0.88em; font-weight: 600; cursor: pointer;
    color: var(--ink); box-shadow: var(--shadow);
  }}
  .child-chip.active {{ background: var(--accent); color: #fff; border-color: var(--accent); }}
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
  .tag-todo {{ background: #fdeee8; color: var(--urgent); }}
  .stats-grid {{
    display: grid; grid-template-columns: repeat(auto-fit, minmax(120px, 1fr)); gap: 10px;
    margin-bottom: 22px;
  }}
  .stat-card {{
    background: var(--card); border-radius: 10px; padding: 16px 14px; text-align: center;
    box-shadow: var(--shadow);
  }}
  .stat-value {{
    font-family: 'Source Serif 4', Georgia, serif; font-weight: 700; font-size: 1.7em; color: var(--ink);
  }}
  .stat-label {{ font-size: 0.76em; color: var(--muted); margin-top: 4px; }}
  .stats-subhead {{
    font-family: 'Source Serif 4', Georgia, serif; font-weight: 700; font-size: 1.05em;
    margin: 22px 0 12px;
  }}
  .stats-daterange {{ font-size: 0.85em; color: var(--muted); margin-top: 6px; }}
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
  .item {{ cursor: pointer; }}
  .source-overlay {{
    position: fixed; inset: 0; background: rgba(0,0,0,0.45); display: none;
    align-items: center; justify-content: center; padding: 20px; z-index: 50;
  }}
  .source-overlay.show {{ display: flex; }}
  .source-card {{
    background: var(--card); border-radius: 14px; padding: 20px 22px; max-width: 420px;
    width: 100%; box-shadow: var(--shadow);
  }}
  .source-title {{
    font-family: 'Source Serif 4', Georgia, serif; font-weight: 700; font-size: 1.1em; margin-bottom: 12px;
  }}
  .source-row {{ font-size: 0.87em; margin-bottom: 7px; color: #444; word-break: break-word; }}
  .source-row strong {{ color: var(--ink); }}
  .source-hint {{ font-size: 0.8em; color: var(--muted); margin: 12px 0 16px; line-height: 1.4; }}
  .source-btn {{
    width: 100%; padding: 11px; border-radius: 9px; border: none; background: var(--accent);
    color: #fff; font-size: 0.87em; font-weight: 600; margin-bottom: 8px; cursor: pointer;
    font-family: inherit;
  }}
  .source-btn-close {{ background: var(--card); color: var(--ink); border: 1px solid var(--line); }}
</style>
</head>
<body>
  <h1>DIA Emirates Hills</h1>
  <div class="sub">Parent Board · Last updated {updated}</div>

  <div class="toolbar" id="childChips"></div>
  <input type="text" id="search" placeholder="Search titles, summaries..." oninput="render()">
  <div class="toolbar" id="topicChips"></div>

  <div class="tabs">
    <div class="tab active" data-view="deadlines" onclick="switchTab('deadlines')">Deadlines</div>
    <div class="tab" data-view="events" onclick="switchTab('events')">Events</div>
    <div class="tab" data-view="calendar" onclick="switchTab('calendar')">Calendar</div>
    <div class="tab" data-view="summaries" onclick="switchTab('summaries')">All Items</div>
    <div class="tab" data-view="emaillog" onclick="switchTab('emaillog')">Email Summaries</div>
    <div class="tab" data-view="stats" onclick="switchTab('stats')">Stats</div>
  </div>

  <h2 class="section-title" id="sectionTitle">Deadlines</h2>
  <div class="view active" id="view-deadlines"></div>
  <div class="view" id="view-events"></div>
  <div class="view" id="view-calendar"></div>
  <div class="view" id="view-summaries"></div>
  <div class="view" id="view-emaillog"></div>
  <div class="view" id="view-stats"></div>

  <div class="source-overlay" id="sourceOverlay" onclick="if(event.target===this) closeSource()">
    <div class="source-card">
      <div class="source-title" id="srcTitle">Source</div>
      <div class="source-row"><strong>From:</strong> <span id="srcSender"></span></div>
      <div class="source-row"><strong>Subject:</strong> <span id="srcSubject"></span></div>
      <div class="source-row"><strong>Date:</strong> <span id="srcDate"></span></div>
      <div class="source-hint" id="srcHint"></div>
      <button class="source-btn" onclick="copySourceSubject()">Copy subject to search Mail</button>
      <button class="source-btn source-btn-close" onclick="closeSource()">Close</button>
    </div>
  </div>

<script>
const DATA = {data_json};
const EMAIL_LOG = {email_log_json};
const CATEGORY_COLORS = {{deadline:'#c0392b', event:'#1a7a6a', task:'#c98a1c', announcement:'#8a8a8e'}};
const TITLES = {{deadlines:'Deadlines', events:'Events', calendar:'Calendar', summaries:'All Items', emaillog:'Email Summaries', stats:'Stats'}};

// Maps the old 7-value topic taxonomy onto the current 3-category system, so
// the board only ever shows Academics/School Info/Activities chips even if
// some rows in the underlying data still carry an old tag (e.g. from before
// a migration ran, or a stray extraction). This keeps the UI simple
// regardless of what's sitting in the data.
const TOPIC_ALIASES = {{
  'Academic': 'Academics',
  'Admin': 'School Info',
  'Events': 'School Info',
  'Clinic': 'School Info',
  'Payments': 'School Info',
  'CCAs': 'Activities',
  'PE': 'Activities',
}};
const CANONICAL_TOPICS = ['Academics', 'School Info', 'Activities'];

function normalizeTopic(topic) {{
  if (!topic) return 'School Info';
  return TOPIC_ALIASES[topic] || topic;
}}

// `stage` records which school stage each child is in (Primary = Years 1-6,
// Secondary = Years 7-13 at DIA Emirates Hills). This lets the filter logic
// correctly match stage-wide items (year_group "Primary"/"Secondary") to the
// right child, instead of only matching exact year strings.
const CHILDREN = [
  {{ id: 'All', label: 'All' }},
  {{ id: 'Evelyn', label: 'Evelyn · Year 5', years: ['Year 5'], stage: 'Primary' }},
  {{ id: 'Milo', label: 'Milo · Year 8', years: ['Year 8'], stage: 'Secondary' }},
];

let activeTopic = 'All';
let activeChild = 'All';
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

function levenshtein(a, b) {{
  const m = a.length, n = b.length;
  if (m === 0) return n;
  if (n === 0) return m;
  const dp = Array.from({{length: m+1}}, () => new Array(n+1).fill(0));
  for (let i = 0; i <= m; i++) dp[i][0] = i;
  for (let j = 0; j <= n; j++) dp[0][j] = j;
  for (let i = 1; i <= m; i++) {{
    for (let j = 1; j <= n; j++) {{
      dp[i][j] = a[i-1] === b[j-1] ? dp[i-1][j-1] : 1 + Math.min(dp[i-1][j], dp[i][j-1], dp[i-1][j-1]);
    }}
  }}
  return dp[m][n];
}}

function fuzzyIncludes(haystack, needle) {{
  if (!needle) return true;
  if (haystack.includes(needle)) return true;
  const words = haystack.split(/\\s+/);
  for (const w of words) {{
    if (w.length < 3) continue;
    const maxDist = w.length <= 5 ? 1 : 2;
    if (levenshtein(w.slice(0, needle.length + 2), needle) <= maxDist) return true;
  }}
  return false;
}}

function matchesFilters(item) {{
  const q = document.getElementById('search').value.toLowerCase().trim();
  if (q) {{
    const haystack = `${{item.title}} ${{item.summary}} ${{item.year_group}}`.toLowerCase();
    if (!fuzzyIncludes(haystack, q)) return false;
  }}
  if (activeTopic !== 'All' && normalizeTopic(item.topic) !== activeTopic) return false;
  if (activeChild !== 'All') {{
    const child = CHILDREN.find(c => c.id === activeChild);
    const yg = item.year_group || '';
    if (yg === 'All') {{
      // genuinely whole-school items always match
    }} else if (yg === child.stage) {{
      // stage-wide item (e.g. "Secondary") matches any child in that stage
    }} else if (child.years.includes(yg)) {{
      // exact year match (e.g. "Year 8")
    }} else {{
      return false;
    }}
  }}
  return true;
}}

function itemHtml(item) {{
  const past = isPast(item.date);
  const rel = relativeLabel(item.date);
  const relCls = past ? 'muted' : urgencyClass(item.date);
  const barColor = past ? 'var(--line)' : urgencyColor(item.date);
  const conflict = item.conflict_note ? `<div class="item-conflict">⚠ ${{item.conflict_note}}</div>` : '';
  const attachments = item.attachment_files ? ` · [${{item.attachment_files}}]` : '';
  return `<div class="item ${{past ? 'past' : ''}}" onclick="showSource(${{item.id}})">
    <div class="urgency-bar" style="background:${{barColor}}"></div>
    <div class="item-body">
      <div class="rel ${{relCls}}">${{rel}}</div>
      <div>
        <div class="item-title">${{item.title || '(untitled)'}}</div>
        <div class="item-summary">${{item.summary || ''}}</div>
        ${{conflict}}
        <div class="item-meta">
          <span class="tag">${{item.year_group || 'All'}}</span>
          ${{item.topic ? `<span class="tag">${{normalizeTopic(item.topic)}}</span>` : ''}}
          from "${{item.email_subject || ''}}"${{attachments}}
        </div>
      </div>
    </div>
  </div>`;
}}

function renderList(containerId, items, passedFlag, setPassedFlag) {{
  const upcoming = items.filter(i => !isPast(i.date));
  const passed = items.filter(i => isPast(i.date));
  upcoming.sort((a, b) => (a.date || '9999').localeCompare(b.date || '9999'));
  passed.sort((a, b) => (b.date || '').localeCompare(a.date || ''));
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

function showSource(id) {{
  const item = DATA.find(d => Number(d.id) === Number(id));
  if (!item) return;
  const sender = item.email_sender || '(unknown sender)';
  const subject = item.email_subject || '(no subject)';
  const dateStr = item.email_date || '';

  document.getElementById('srcTitle').textContent = item.title || 'Source';
  document.getElementById('srcSender').textContent = sender;
  document.getElementById('srcSubject').textContent = subject;
  document.getElementById('srcDate').textContent = dateStr;

  const domainMatch = sender.match(/@([\\w.-]+)/);
  const domain = domainMatch ? domainMatch[1].toLowerCase() : '';
  let hint;
  if (domain && domain !== 'diadubai.com') {{
    hint = `This came from ${{domain}}, not a direct school email — check that app/platform for the original message.`;
  }} else {{
    hint = `iCloud Mail doesn't support jumping straight to one email from here. Tap "Copy subject" below, then paste it into Mail's search bar to find the original.`;
  }}
  document.getElementById('srcHint').textContent = hint;
  window.__lastSourceSubject = subject;
  document.getElementById('sourceOverlay').classList.add('show');
}}

function closeSource() {{
  document.getElementById('sourceOverlay').classList.remove('show');
}}

function copySourceSubject() {{
  const text = window.__lastSourceSubject || '';
  if (navigator.clipboard && text) {{
    navigator.clipboard.writeText(text).then(() => {{
      alert('Copied: ' + text);
    }}).catch(() => {{
      alert('Subject: ' + text);
    }});
  }} else {{
    alert('Subject: ' + text);
  }}
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

function renderEmailLog() {{
  const q = document.getElementById('search').value.toLowerCase().trim();
  let entries = EMAIL_LOG.filter(e => {{
    if (activeTopic !== 'All' && normalizeTopic(e.topic) !== activeTopic) return false;
    if (q) {{
      const haystack = `${{e.subject || ''}} ${{e.summary || ''}}`.toLowerCase();
      if (!fuzzyIncludes(haystack, q)) return false;
    }}
    return true;
  }});
  entries.sort((a, b) => (new Date(b.email_date) - new Date(a.email_date)) || 0);

  if (entries.length === 0) {{
    document.getElementById('view-emaillog').innerHTML = '<div class="empty">No emails logged yet.</div>';
    return;
  }}

  const html = entries.map(e => {{
    const todoBadge = e.has_actionable ? '<span class="tag tag-todo">TO DO</span>' : '';
    const attachments = e.attachment_files ? ` · [${{e.attachment_files}}]` : '';
    let dateStr = '';
    try {{ dateStr = new Date(e.email_date).toLocaleDateString('en-GB', {{day:'numeric', month:'short'}}); }} catch (err) {{}}
    return `<div class="item">
      <div class="urgency-bar" style="background:var(--line)"></div>
      <div class="item-body">
        <div class="rel muted">${{dateStr}}</div>
        <div>
          <div class="item-title">${{e.subject || '(no subject)'}}</div>
          <div class="item-summary">${{e.summary || ''}}</div>
          <div class="item-meta">
            <span class="tag">${{normalizeTopic(e.topic)}}</span>${{todoBadge}}${{attachments}}
          </div>
        </div>
      </div>
    </div>`;
  }}).join('');

  document.getElementById('view-emaillog').innerHTML = html;
}}

function renderStats() {{
  const totalEmails = EMAIL_LOG.length;
  const withActionable = EMAIL_LOG.filter(e => e.has_actionable).length;
  const noActionable = totalEmails - withActionable;
  const totalItems = DATA.length;

  const topicCounts = {{}};
  CANONICAL_TOPICS.forEach(t => topicCounts[t] = 0);
  DATA.forEach(i => {{
    const t = normalizeTopic(i.topic);
    topicCounts[t] = (topicCounts[t] || 0) + 1;
  }});

  const categoryCounts = {{}};
  DATA.forEach(i => {{
    const c = i.category || 'other';
    categoryCounts[c] = (categoryCounts[c] || 0) + 1;
  }});

  const childCounts = {{}};
  CHILDREN.filter(c => c.id !== 'All').forEach(child => {{
    childCounts[child.label] = DATA.filter(i => {{
      const yg = i.year_group || '';
      return yg === 'All' || yg === child.stage || child.years.includes(yg);
    }}).length;
  }});

  let dateRangeStr = '—';
  const validDates = EMAIL_LOG.map(e => new Date(e.email_date)).filter(d => !isNaN(d));
  if (validDates.length > 0) {{
    const minD = new Date(Math.min(...validDates));
    const maxD = new Date(Math.max(...validDates));
    const fmt = d => d.toLocaleDateString('en-GB', {{day:'numeric', month:'short', year:'numeric'}});
    dateRangeStr = `${{fmt(minD)}} – ${{fmt(maxD)}}`;
  }}

  const statCard = (label, value) => `<div class="stat-card"><div class="stat-value">${{value}}</div><div class="stat-label">${{label}}</div></div>`;

  let html = '<div class="stats-grid">';
  html += statCard('Emails processed', totalEmails);
  html += statCard('With actionable items', withActionable);
  html += statCard('No action needed', noActionable);
  html += statCard('Total items extracted', totalItems);
  html += '</div>';

  html += '<h3 class="stats-subhead">By topic</h3><div class="stats-grid">';
  CANONICAL_TOPICS.forEach(t => {{ html += statCard(t, topicCounts[t] || 0); }});
  html += '</div>';

  html += '<h3 class="stats-subhead">By category</h3><div class="stats-grid">';
  Object.keys(categoryCounts).forEach(c => {{ html += statCard(c.charAt(0).toUpperCase() + c.slice(1) + 's', categoryCounts[c]); }});
  html += '</div>';

  html += '<h3 class="stats-subhead">By child</h3><div class="stats-grid">';
  Object.keys(childCounts).forEach(label => {{ html += statCard(label, childCounts[label]); }});
  html += '</div>';

  html += `<div class="stats-daterange">Emails span: <strong>${{dateRangeStr}}</strong></div>`;

  document.getElementById('view-stats').innerHTML = html;
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
  if (currentTab === 'emaillog') renderEmailLog();
  if (currentTab === 'stats') renderStats();
}}

function buildTopicChips() {{
  const topics = ['All', ...CANONICAL_TOPICS];
  document.getElementById('topicChips').innerHTML = topics.map(t =>
    `<div class="chip ${{t === activeTopic ? 'active' : ''}}" onclick="setTopic('${{t}}')">${{t}}</div>`
  ).join('');
}}

function setTopic(t) {{
  activeTopic = t;
  buildTopicChips();
  render();
}}

function buildChildChips() {{
  document.getElementById('childChips').innerHTML = CHILDREN.map(c =>
    `<div class="child-chip ${{c.id === activeChild ? 'active' : ''}}" onclick="setChild('${{c.id}}')">${{c.label}}</div>`
  ).join('');
}}

function setChild(id) {{
  activeChild = id;
  buildChildChips();
  render();
}}

buildChildChips();
buildTopicChips();
render();
</script>
</body>
</html>"""


def main():
    rows = fetch_events()
    email_log_rows = fetch_email_log()
    html = build_html(rows, email_log_rows)
    with open(OUTPUT_PATH, "w") as f:
        f.write(html)
    print(f"Wrote {OUTPUT_PATH} ({len(rows)} events, {len(email_log_rows)} logged emails)")


if __name__ == "__main__":
    main()
