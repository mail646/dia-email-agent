#!/usr/bin/env python3
"""
DIA Emirates Hills email ingestion agent.
Connects to iCloud Mail via IMAP, pulls new emails from the school domain
(2025 onwards), extracts text from PDFs/DOCX/XLSX/PPTX/images (via OCR)
and HTML bodies, batches everything into a single Gemini call for
structured extraction, and stores results in SQLite.
"""

import imaplib
import email
import difflib
from email.header import decode_header
from email.utils import parsedate_to_datetime
import sqlite3
import json
import os
import sys
import time
import re
import hashlib
import html as html_module
from datetime import datetime, timezone
import io

import google.generativeai as genai
from PIL import Image
import pytesseract

try:
    import pillow_heif
    pillow_heif.register_heif_opener()  # lets PIL open .heic/.heif -- the default
    # photo format on iPhones, which parents very often forward attachments in
except ImportError:
    pass

ICLOUD_EMAIL = os.environ.get("ICLOUD_EMAIL", "your_icloud_email@icloud.com")
ICLOUD_APP_PASSWORD = os.environ.get("ICLOUD_APP_PASSWORD", "xxxx-xxxx-xxxx-xxxx")
IMAP_SERVER = "imap.mail.me.com"
IMAP_PORT = 993

# List of sender domains to search for. Add more here if other systems (e.g.
# Managebac, Toddle, Teams, iSAMS) send notification emails to your inbox from
# their own domain -- each domain in this list gets searched in every folder.
SCHOOL_DOMAINS = ["diadubai.com"]
DB_PATH = os.environ.get("DIA_DB_PATH", os.path.join(os.path.dirname(__file__), "dia_events.db"))

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
MODEL = "gemini-3.6-flash"

TOTAL_EMAIL_LIMIT = 30
GEMINI_CHUNK_SIZE = 1  # emails per Gemini call. Steady-state volume from the school
                        # is only a handful of emails a day, so there's no reason to
                        # economize here -- one email per call gives the model's full
                        # attention to each one (most thorough possible reading) while
                        # using only a few of the free tier's 20 daily requests. Use
                        # GEMINI_CHUNK_SIZE_OVERRIDE to temporarily raise this while
                        # still working through a large backlog, to conserve quota
                        # across many older emails at once.
SKIP_FOLDER_KEYWORDS = ["trash", "junk", "deleted", "sent", "draft", "notes"]
IMAGE_EXTENSIONS = (".jpg", ".jpeg", ".png", ".gif", ".bmp", ".tiff", ".webp", ".heic", ".heif")

# Specific years, plus the two broader stage-wide tags, plus "All" for
# genuinely whole-school items. Primary = Years 1-6, Secondary = Years 7-13
# at DIA Emirates Hills.
YEAR_GROUPS = [
    "Kindergarten", "Year 4", "Year 5", "Year 6", "Year 7", "Year 8", "Year 9",
    "Year 10", "Year 11", "Year 12", "Year 13",
    "Primary", "Secondary", "All",
]
TOPICS = ["Academics", "School Info", "Activities"]

EXTRACTION_SYSTEM_PROMPT = f"""You extract actionable information from school emails for busy parents.

You will receive several emails, each marked with a line like "=== EMAIL 0 ===", "=== EMAIL 1 ===", etc.
Each email's block includes its Date header -- use this to tell which of several emails is more recent.
Some emails include text extracted from PDF/Word/Excel/PowerPoint attachments or OCR'd from images (photos of
flyers, posters, forms) — treat this extracted text with the same importance as the email body itself, even if
it has OCR noise/typos.

BE EXHAUSTIVE. A single email or attachment very often bundles MANY distinct dated items together -- e.g. a
"Sports Trials" flyer with a different trial date for each sport and year group, a "Weekly Notices" email
covering five unrelated announcements, or a form with separate deadlines for different grades. Extract EVERY
one of these as its OWN separate item. Do not collapse several distinct dated things into one vague summary
item, and do not skip smaller or seemingly minor items just because the email is long or covers many topics --
a parent would rather see ten small items than miss one that mattered to their child. When in doubt about
whether something is "actionable enough" to include, include it.

For each item you extract, identify:
- Any deadlines, events, tasks, tests/assignments, or announcements with a date attached.
- Whole-school calendar items too: closures, holidays, early-dismissal days, etc. -- capture these as "event"
  category items. IMPORTANT: if the SAME closure/dismissal has different dates or times for different stages
  (e.g. Primary finishes at 11am but Secondary finishes at 12pm, or the stages break for a holiday on different
  days), create SEPARATE items -- one per stage/year group -- each with its own correct date and year_group.
  Do not collapse per-stage differences into a single item with just one date.
- Which year group(s) it applies to (choose from: {", ".join(YEAR_GROUPS)}, or a comma-separated combination of
  specific years -- see below).
  - Use "Kindergarten" when an email is specifically about KG1 and/or KG2 only, with no Year 1+ content.
    Do NOT tag Kindergarten-only content as "Primary" -- "Primary" specifically means Years 1-6, not KG.
  - Use a specific year like "Year 8" when the email clearly names one year group.
  - If an item names an explicit SUBSET of years that is NOT the whole stage -- e.g. a session specifically for
    "Year 1 and Year 2" only, or trials for "Year 3-4" only -- combine the specific years with commas, e.g.
    "Year 1, Year 2" or "Year 3, Year 4". Do NOT round this up to "Primary" just because several years are
    named; only use the broad stage tag when the item genuinely applies to the WHOLE stage, not a subset of it.
    This matters a lot: schools often bundle several separate sessions in one email, each for a different pair
    of years (e.g. one session for KG1-2, another for Year 1-2, another for Year 3-4, another for Year 5-6) --
    each of those is a SEPARATE item with its OWN specific year combination, not one "Primary" item.
  - Use "Primary" when it applies broadly across the WHOLE of Primary Years 1-6 (not KG, not the whole school) —
    e.g. an email addressed to "Year 7-13" or "Secondary" should be tagged "Secondary", NOT "All".
  - Use "Secondary" when it applies broadly across the WHOLE of Secondary (Years 7-13) but not the whole school.
  - Only use "All" when the email is genuinely whole-school, or you truly cannot tell which stage/year it targets.
  - Do not default to "All" or to a broad stage tag just because a range of years is mentioned — pick the exact
    years, or "Kindergarten"/"Primary"/"Secondary", based on what's actually stated.
- Whether it's addressed to ONE SPECIFIC HOMEROOM CLASS rather than a whole year group -- schools often send
  class-specific emails (e.g. "Dear parents of [Student] in 5A", "Welcome to 8C", "5B Parent Rep"). If the
  email clearly targets one specific class like this, set "class_group" to that class (e.g. "5A", "8C").
  Otherwise (it applies to the whole year group, several classes, or you can't tell), set "class_group" to
  null. Getting this right matters: a "Welcome to 5B" email should NOT show up for a parent whose child is in
  5A, even though both are Year 5 -- only actually class-specific emails should carry a class_group value.
- Which topic it belongs to (choose exactly one from: {", ".join(TOPICS)}):
  - "Academics": tests, exams, assignments/homework due, academic assessments (e.g. CAT4), and the release of
    report cards, exam results, or parent reports.
  - "School Info": trips, parent meetings and information sessions the school is inviting parents to (parent-
    teacher conferences, coffee mornings, orientation/welcome sessions), parent rep sign-ups, clinic/health
    notices, payments, whole-school closures/holidays/early dismissal, and general admin announcements.
  - "Activities": CCA trials, sports trials, matches/fixtures, auditions, rehearsals, club sign-ups and start
    dates.
- A short, clear summary a busy parent can scan in 5 seconds.
- CONFLICT RESOLUTION: if two or more emails in this batch describe what looks like the SAME deadline/event
  (e.g. two emails both about "CCA registration") but give different dates/times, do NOT produce two separate
  conflicting items. Instead produce ONE item using the date/details from whichever email has the MORE RECENT
  Date header (the newest information wins), and set "conflict_note" to briefly explain that an earlier email
  gave different information which this newer one supersedes.

Respond ONLY with a single JSON object (no markdown fences, no preamble) with exactly two keys: "items" and
"email_summaries".

"items" is an array combining actionable items from ALL emails. Each item:
{{
  "email_index": 0,
  "title": "short title, e.g. 'Year 8 Sports Day'",
  "date": "YYYY-MM-DD or null if no specific date",
  "category": "deadline | event | task | announcement",
  "year_group": "one of the allowed values above",
  "class_group": "a specific homeroom class like '5A' or '8C' if this email targets only that class, else null",
  "topic": "one of the allowed topic values above",
  "summary": "1-2 sentence summary of what the parent needs to know or do",
  "conflict_note": "explanation if this superseded an earlier conflicting mention, else null"
}}

"email_summaries" is an array with EXACTLY ONE entry per email in this batch, even for emails with no actionable
items -- this is a THOROUGH running log of every email processed, not a one-line gloss and not just the ones
with deadlines. For this entry, actually read the full email body AND every attachment/OCR'd
image/PDF/linked-page text in full -- do not skim. Each entry:
{{
  "email_index": 0,
  "topic": "one of the allowed topic values above (best guess even if there's no dated item)",
  "summary": "a thorough summary (a full paragraph, or several bullet-style sentences) covering EVERYTHING
              substantive in this email and its attachments/images/links -- names of staff or students
              mentioned, numbers, instructions, context, background, minor announcements, anything a parent
              would want on record -- not just deadlines. If the email is long or covers many topics, cover
              all of them; do not compress a multi-topic email down to one generic sentence.",
  "has_actionable": true or false
}}

The "email_index" field MUST match the number in that email's "=== EMAIL N ===" marker. If an item combines info
from a NEWER email that supersedes an OLDER one in this same batch, use the email_index of the NEWER email.
If an email has NO actionable dated information, simply produce no "items" entries for it, but STILL include it
in "email_summaries" with has_actionable set to false and a full, thorough summary as described above.
If an email covers multiple distinct items, produce multiple "items" objects with the same email_index.
"""


IMAP_TIMEOUT_SECONDS = 60  # hard cap on any single IMAP network operation


def imap_connect():
    if not ICLOUD_APP_PASSWORD or "xxxx" in ICLOUD_APP_PASSWORD:
        sys.exit("ERROR: set ICLOUD_APP_PASSWORD")
    # Without an explicit timeout, a dropped/stuck connection to iCloud's
    # server can make Python wait forever for a response that's never
    # coming -- this is what caused runs to hang for hours instead of
    # failing cleanly. Every other network call in this script (Gemini,
    # link-fetching) already has a timeout; this was the one gap.
    conn = imaplib.IMAP4_SSL(IMAP_SERVER, IMAP_PORT, timeout=IMAP_TIMEOUT_SECONDS)
    conn.login(ICLOUD_EMAIL, ICLOUD_APP_PASSWORD)
    return conn


def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_message_id TEXT,
            title TEXT,
            date TEXT,
            category TEXT,
            year_group TEXT,
            topic TEXT,
            summary TEXT,
            conflict_note TEXT,
            email_subject TEXT,
            email_sender TEXT,
            email_date TEXT,
            created_at TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS processed_messages (
            message_id TEXT PRIMARY KEY,
            processed_at TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS link_content_hashes (
            url TEXT PRIMARY KEY,
            content_hash TEXT,
            last_checked TEXT
        )
    """)
    existing_processed_cols = {row[1] for row in conn.execute("PRAGMA table_info(processed_messages)")}
    if "subject" not in existing_processed_cols:
        try:
            conn.execute("ALTER TABLE processed_messages ADD COLUMN subject TEXT")
        except sqlite3.OperationalError:
            pass
    existing_cols = {row[1] for row in conn.execute("PRAGMA table_info(events)")}
    for col in ["topic", "conflict_note", "attachment_files", "class_group"]:
        if col not in existing_cols:
            try:
                conn.execute(f"ALTER TABLE events ADD COLUMN {col} TEXT")
            except sqlite3.OperationalError:
                pass
    conn.execute("""
        CREATE TABLE IF NOT EXISTS email_log (
            message_id TEXT PRIMARY KEY,
            subject TEXT,
            sender TEXT,
            email_date TEXT,
            topic TEXT,
            summary TEXT,
            has_actionable INTEGER,
            attachment_files TEXT,
            created_at TEXT
        )
    """)
    conn.commit()
    return conn


def already_processed(conn, message_id):
    cur = conn.execute("SELECT 1 FROM processed_messages WHERE message_id = ?", (message_id,))
    return cur.fetchone() is not None


def mark_processed(conn, message_id, subject=None):
    conn.execute(
        "INSERT OR IGNORE INTO processed_messages (message_id, processed_at, subject) VALUES (?, ?, ?)",
        (message_id, datetime.now(timezone.utc).isoformat(), subject),
    )
    conn.commit()


def decode_mime_words(s):
    if not s:
        return ""
    parts = decode_header(s)
    return "".join(
        (p.decode(enc or "utf-8", errors="ignore") if isinstance(p, bytes) else p)
        for p, enc in parts
    )


def stage_from_year_mentions(text):
    """
    Scan free text for explicit "Year N" mentions (N = 4..13) and infer a stage:
    DIA's boundary is Year 7 and above = Secondary, Year 6 and below = Primary.
    Returns "Secondary" if every year number found is >= 7, "Primary" if every
    one found is <= 6, or None if no year numbers were found or they span both
    stages (in which case "All" may genuinely be correct, so we leave it alone).
    """
    nums = {int(n) for n in re.findall(r"year\s*([4-9]|1[0-3])\b", text, re.IGNORECASE)}
    if not nums:
        return None
    if all(n >= 7 for n in nums):
        return "Secondary"
    if all(n <= 6 for n in nums):
        return "Primary"
    return None


def retag_legacy_all_items(conn):
    """
    One-time-ish cleanup pass: events saved to the DB before the Primary/Secondary
    distinction existed may be tagged "All" when they were really stage-specific
    (e.g. a "Secondary CCA" notice). Rather than requiring manual DB edits, this
    reclassifies them automatically using two layers of heuristics, checked in
    order for each row still tagged "All":

      1. Subject-line conventions -- DIA prefixes Primary School emails with "PS"
         and Secondary School emails with "SS" / "EH-SS".
      2. Explicit "Year N" mentions anywhere in the subject/title/summary -- DIA's
         boundary is Year 7+ = Secondary, Year 6 and below = Primary (see
         stage_from_year_mentions). This catches cases that don't follow the
         PS/SS subject convention but still clearly name a stage-specific range
         like "Year 7-13" or "Year 4-6".

    Only rows that clearly match are changed; genuinely ambiguous or mixed-range
    items are left as "All". Safe to run on every pipeline run since already-
    corrected rows no longer match year_group = 'All'.
    """
    SECONDARY_PATTERNS = [
        r"\bss\b", r"eh-ss", r"year\s*7\s*-\s*13", r"year\s*7\s*-\s*9",
        r"year\s*10\s*-\s*11", r"year\s*12\s*-\s*13", r"\bsecondary\b",
    ]
    PRIMARY_PATTERNS = [
        r"^ps\b", r"^ps\s*-", r"eh-ps", r"kg1\s*-\s*y6", r"\bprimary\b",
        r"year\s*4\s*-\s*6", r"year\s*1\s*-\s*6",
    ]
    sec_re = re.compile("|".join(SECONDARY_PATTERNS), re.IGNORECASE)
    pri_re = re.compile("|".join(PRIMARY_PATTERNS), re.IGNORECASE)

    rows = conn.execute("SELECT id, title, email_subject, summary FROM events WHERE year_group = 'All'").fetchall()
    updated = 0
    for row_id, title, subject, summary in rows:
        text = f"{subject or ''} {title or ''} {summary or ''}".strip()
        new_tag = None
        if sec_re.search(text):
            new_tag = "Secondary"
        elif pri_re.search(text):
            new_tag = "Primary"
        else:
            new_tag = stage_from_year_mentions(text)
        if new_tag:
            conn.execute("UPDATE events SET year_group = ? WHERE id = ?", (new_tag, row_id))
            updated += 1
    conn.commit()
    if updated:
        print(f"DEBUG: retagged {updated} legacy 'All' item(s) to Primary/Secondary via heuristics")


def parse_email_date(date_str):
    """Parse an RFC 2822 email Date header into an aware datetime for sorting.
    Falls back to the minimum possible datetime (so unparseable dates sort last,
    not first) if parsing fails."""
    if not date_str:
        return datetime.min.replace(tzinfo=timezone.utc)
    try:
        parsed = parsedate_to_datetime(date_str)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed
    except Exception:
        return datetime.min.replace(tzinfo=timezone.utc)


def html_to_text(raw_html):
    if not raw_html:
        return ""
    text = re.sub(r"(?is)<(script|style).*?>.*?(</\1>)", "", raw_html)
    text = re.sub(r"(?i)<br\s*/?>", "\n", text)
    text = re.sub(r"(?i)</p>", "\n", text)
    text = re.sub(r"<[^>]+>", "", text)
    text = html_module.unescape(text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n\n", text)
    return text.strip()


def ocr_image_bytes(payload, filename=""):
    try:
        img = Image.open(io.BytesIO(payload))
        text = pytesseract.image_to_string(img)
        return text.strip()
    except Exception as e:
        return f"[Could not OCR {filename}: {e}]"


def extract_pdf_bytes_text(payload, label=""):
    """
    Renders every page of a PDF (given as raw bytes) to a full-page image and OCRs
    it, in addition to pulling any plain selectable text, regardless of whether the
    page also has some plain text elsewhere on it. Shared by both email attachments
    and PDFs found at links inside an email body.
    """
    import pymupdf as fitz  # PyMuPDF (non-deprecated import path)
    text_parts = []
    try:
        doc = fitz.open(stream=payload, filetype="pdf")
        for page_num, page in enumerate(doc):
            page_text = (page.get_text() or "").strip()
            if page_text:
                text_parts.append(page_text)
            try:
                pix = page.get_pixmap(matrix=fitz.Matrix(2, 2))  # 2x zoom for OCR quality
                ocr_text = ocr_image_bytes(pix.tobytes("png"), f"{label} page {page_num + 1}")
                if ocr_text and not ocr_text.startswith("[Could not"):
                    text_parts.append(f"[OCR from page {page_num + 1}]\n{ocr_text}")
            except Exception as e:
                text_parts.append(f"[Could not OCR page {page_num + 1}: {e}]")
        doc.close()
    except Exception as e:
        return f"[Could not extract PDF {label}: {e}]"
    return "\n".join(text_parts)


def extract_pdf_link_urls(payload, label=""):
    """
    Pulls clickable hyperlinks OUT of a PDF (as opposed to its visible text).
    Some school newsletters are a master PDF whose real per-year-group content
    sits behind a grid of clickable links -- e.g. a "Comm's Corner" page with
    one link each for KG1, KG2, Year 1, Year 2, ... Year 6 -- rather than in
    the master PDF's own body text. Those links are invisible to plain text
    extraction, so without this, a newsletter like that would only ever
    surface its generic front-page content and never the year-specific
    details (like a spelling homework list) living one click deeper.

    Two extraction methods are combined, because a PDF can make something
    clickable in more than one way:
    1. page.get_links() -- catches the common, standard "/URI" link type.
    2. Scanning every annotation's RAW underlying PDF object text for any
       http(s) URL -- catches links built other ways (a JavaScript action,
       a "launch" action, etc.) that get_links() alone does not surface.
    A link that is real to a human tapping it in a PDF viewer, but invisible
    to method 1, still has its destination URL sitting somewhere in the raw
    object bytes -- method 2 is a deliberately low-level, resilient catch-all
    for exactly that case.
    """
    import pymupdf as fitz
    urls = []
    seen = set()

    def add_if_new(u):
        u = u.strip().rstrip(')>"\']')
        if not u.startswith("http"):
            return
        if any(skip in u.lower() for skip in LINK_SKIP_PATTERNS):
            return
        if u not in seen:
            seen.add(u)
            urls.append(u)

    try:
        doc = fitz.open(stream=payload, filetype="pdf")
        for page in doc:
            for link in page.get_links():
                uri = link.get("uri")
                if uri:
                    add_if_new(uri)

            for annot in page.annots() or []:
                try:
                    raw = doc.xref_object(annot.xref, compressed=False)
                except Exception:
                    continue
                for match in re.findall(r'https?://[^\s()<>\[\]"\'\\]+', raw):
                    add_if_new(match)
        doc.close()
    except Exception as e:
        print(f"WARNING: could not extract embedded links from PDF {label}: {e}")
    return urls


LINK_SKIP_PATTERNS = [
    "unsubscribe", "mailto:", "facebook.com", "twitter.com", "x.com", "instagram.com",
    "linkedin.com", "youtube.com", "tiktok.com",
]
MAX_LINKS_PER_EMAIL = 12  # newsletters routinely have 7-10+ real content links
# (e.g. one per year group in a "Comm's Corner"-style grid); the old cap of 4
# was low enough that a genuinely relevant link could be silently dropped.
MAX_LINK_BYTES = 8 * 1024 * 1024  # 8 MB safety cap
MAX_IMAGES_PER_PAGE = 6  # cap how many embedded images per linked page get OCR'd


def extract_links_from_text(html_body, plain_body):
    """Pulls http(s) links out of an email's HTML/plain body, filtering out
    obvious non-content links (unsubscribe, social media, etc.), capped to a
    small number per email to bound fetch time/cost. Order is preserved as
    first-encountered rather than an unordered set, so if the cap is ever
    actually hit, it deterministically keeps the earliest (usually most
    prominent) links rather than an arbitrary subset."""
    urls = []
    seen = set()
    for text in (html_body or "", plain_body or ""):
        found = re.findall(r'href=["\']((?:https?:)?//[^"\']+)["\']', text, re.IGNORECASE)
        found += re.findall(r'(https?://[^\s<>"\')]+)', text)
        for match in found:
            u = match if match.startswith("http") else "https:" + match
            u = u.rstrip('.,);')
            if u not in seen:
                seen.add(u)
                urls.append(u)
    filtered = []
    for u in urls:
        low = u.lower()
        if any(p in low for p in LINK_SKIP_PATTERNS):
            continue
        filtered.append(u)
    return filtered[:MAX_LINKS_PER_EMAIL]


def extract_image_urls_from_html(html_text, base_url):
    """
    Finds <img src="..."> URLs in a fetched HTML page and resolves them to
    absolute URLs. Many newsletter/CMS pages (calendars, info cards, event
    flyers) deliver their actual content as designed graphics rather than
    real text -- plain HTML text extraction alone would silently miss all of
    that, so these need to be OCR'd separately.
    """
    from urllib.parse import urljoin
    raw_srcs = re.findall(r'<img[^>]+src=["\']([^"\']+)["\']', html_text, re.IGNORECASE)
    urls = []
    seen = set()
    for src in raw_srcs:
        if src.startswith("data:"):
            continue  # skip inline base64 images (tiny icons/tracking pixels, not content)
        absolute = urljoin(base_url, src)
        if absolute not in seen:
            seen.add(absolute)
            urls.append(absolute)
    return urls[:MAX_IMAGES_PER_PAGE]


JS_PLACEHOLDER_SIGNS = [
    "requires javascript", "enable javascript", "please enable scripts",
    "javascript is disabled", "browser is either blocking scripts",
]


def looks_like_js_placeholder(html_text, visible_text):
    """
    Detects the tell-tale sign of a JS-rendered page (like Microsoft Sway)
    that a plain HTTP fetch can't actually render -- the response is just a
    thin shell telling the (non-existent) user to enable JavaScript, not the
    real content. Used to decide whether the more expensive headless-browser
    fallback is worth attempting for this particular URL.
    """
    low = (html_text or "").lower()
    if any(sign in low for sign in JS_PLACEHOLDER_SIGNS):
        return True
    # A suspiciously short amount of visible text on an otherwise normal-
    # sized HTML response is also a common symptom of a JS shell page.
    if len(html_text or "") > 500 and len((visible_text or "").strip()) < 200:
        return True
    return False


def render_page_with_headless_browser(url, timeout_ms=30000):
    """
    Falls back to an actual headless browser (Playwright/Chromium) to fetch
    a page's FULLY RENDERED HTML, for pages that need JavaScript to show
    their real content (a plain HTTP fetch only gets the empty pre-JS
    shell for these). Returns the rendered HTML string, or None if this
    fails for any reason -- callers should treat that as "couldn't get this
    one" rather than crash the run over a single flaky page load.
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return None
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch()
            try:
                page = browser.new_page()
                page.goto(url, timeout=timeout_ms, wait_until="networkidle")
                html = page.content()
                return html
            finally:
                browser.close()
    except Exception as e:
        print(f"WARNING: headless-browser render failed for {url}: {e}")
        return None


MAX_PDF_EMBEDDED_LINKS = 10  # cap on how many links found INSIDE a linked PDF get followed


def fetch_linked_content_text(urls, _depth=0):
    """
    Follows a small number of links found in an email body and pulls text from
    what they point to -- some school newsletters put the actual content on a
    linked page or hosted PDF rather than in the email itself. HTML pages have
    their plain text extracted AND any embedded images OCR'd (newsletter
    calendars/info cards are frequently delivered as graphics, not real text);
    PDFs go through the same full-page OCR pipeline as attachments.

    Pages that need JavaScript to render their real content (some newsletter
    platforms, e.g. Microsoft Sway, do) come back as an empty "please enable
    JavaScript" shell from a plain HTTP fetch -- when that's detected, this
    falls back to actually rendering the page with a real (headless) browser
    before extracting text/images, at the cost of that one link taking
    noticeably longer.

    A linked PDF's own CLICKABLE LINKS are also followed, one level deep only
    (_depth guards against an unbounded chain) -- some newsletters are a
    master PDF whose real content sits behind a further link (e.g. a
    year-group-specific page linked from a "Comm's Corner" grid), which plain
    text/OCR extraction of the master PDF alone would never surface.
    """
    import requests
    blocks = []
    for url in urls:
        try:
            resp = requests.get(url, timeout=15, headers={"User-Agent": "Mozilla/5.0"})
            if resp.url != url:
                # Tracking-redirect links (SendGrid, etc.) show a friendly
                # destination as the link TEXT but point the real href at a
                # redirector -- without this, the log only ever shows the
                # redirector's URL, never the page actually being read.
                print(f"DEBUG: '{url}' redirected to '{resp.url}'")
            if resp.status_code != 200:
                print(f"WARNING: linked content '{url}' returned HTTP {resp.status_code}, skipping")
                continue
            if len(resp.content) > MAX_LINK_BYTES:
                print(f"WARNING: linked content '{url}' is {len(resp.content)} bytes, "
                      f"over the {MAX_LINK_BYTES} byte cap -- skipping")
                continue
            content_type = resp.headers.get("Content-Type", "").lower()
            if "pdf" in content_type or url.lower().endswith(".pdf"):
                text = extract_pdf_bytes_text(resp.content, url)
                if text and text.strip():
                    blocks.append(f"--- Linked content: {url} ---\n{text.strip()}")
                    print(f"DEBUG: extracted {len(text.strip())} chars of text from linked PDF {url}")
                else:
                    print(f"WARNING: linked PDF '{url}' produced no extractable text "
                          f"(likely a scanned/image-only PDF, or extraction failed)")

                if _depth == 0:
                    pdf_links = extract_pdf_link_urls(resp.content, url)[:MAX_PDF_EMBEDDED_LINKS]
                    if pdf_links:
                        print(f"DEBUG: found {len(pdf_links)} embedded link(s) inside PDF {url}, following them...")
                        nested = fetch_linked_content_text(pdf_links, _depth=1)
                        if nested:
                            blocks.append(nested)
            elif "html" in content_type or not content_type:
                html_source = resp.text
                text = html_to_text(html_source)

                if looks_like_js_placeholder(html_source, text):
                    print(f"DEBUG: '{url}' looks JS-rendered, trying headless-browser fallback...")
                    rendered_html = render_page_with_headless_browser(url)
                    if rendered_html:
                        html_source = rendered_html
                        text = html_to_text(html_source)
                        print(f"DEBUG: headless-browser render succeeded for {url}")

                if text and text.strip():
                    blocks.append(f"--- Linked content: {url} ---\n{text.strip()}")

                image_urls = extract_image_urls_from_html(html_source, url)
                for img_url in image_urls:
                    try:
                        img_resp = requests.get(img_url, timeout=15, headers={"User-Agent": "Mozilla/5.0"})
                        if img_resp.status_code != 200 or len(img_resp.content) > MAX_LINK_BYTES:
                            continue
                        img_content_type = img_resp.headers.get("Content-Type", "").lower()
                        if not img_content_type.startswith("image/"):
                            continue
                        ocr_text = ocr_image_bytes(img_resp.content, img_url)
                        if ocr_text and not ocr_text.startswith("[Could not"):
                            blocks.append(f"--- OCR from image on {url} ({img_url}) ---\n{ocr_text}")
                    except Exception as e:
                        blocks.append(f"[Could not fetch/OCR embedded image {img_url}: {e}]")
            else:
                print(f"DEBUG: linked content '{url}' has content-type '{content_type}', not PDF/HTML -- skipping")
                continue
        except Exception as e:
            print(f"WARNING: error fetching linked content '{url}': {e}")
            blocks.append(f"[Could not fetch linked content {url}: {e}]")
    return "\n\n".join(blocks)


# The school also publishes every newsletter edition on its own public
# website, as a plain, direct PDF link with no email, no tracking-redirect
# wrapper, and no login -- a much simpler and more reliable path to the
# recurring weekly newsletter than chasing it through an email's SendGrid
# link and (for Primary) a further link buried inside that PDF. This is a
# SECOND, independent source for the SAME recurring documents, checked
# alongside (not instead of) normal email ingestion.
NEWSLETTER_ARCHIVE_PAGES = [
    ("Primary", "https://www.diadubai.com/primary-school-newsletter-dia-eh"),
    ("Secondary", "https://www.diadubai.com/secondary-school-newsletter-dia-eh"),
]
MAX_ARCHIVE_LINKS_PER_PAGE = 6  # only the most-recent group of links per page is checked each run


def fetch_newsletter_archive_candidates(archive_url, label):
    """
    Fetches one of the school's public newsletter archive pages and pulls
    out the (title, pdf_url) pairs from its FIRST results table -- these
    pages list editions grouped by term/year, most recent group first, so
    the first table is always the current term's listing. Only that first
    table is checked (capped further by MAX_ARCHIVE_LINKS_PER_PAGE) rather
    than the entire multi-year historical archive, since older editions
    would already have been captured via email when they were current.
    """
    import requests
    try:
        resp = requests.get(archive_url, timeout=15, headers={"User-Agent": "Mozilla/5.0"})
        if resp.status_code != 200:
            print(f"WARNING: newsletter archive page '{archive_url}' returned HTTP {resp.status_code}")
            return []
    except Exception as e:
        print(f"WARNING: could not fetch newsletter archive page '{archive_url}': {e}")
        return []

    table_match = re.search(r"<table.*?>.*?</table>", resp.text, re.DOTALL | re.IGNORECASE)
    if not table_match:
        print(f"WARNING: no results table found on newsletter archive page '{archive_url}' "
              f"-- the page's structure may have changed")
        return []

    candidates = []
    for m in re.finditer(
        r'<a[^>]+href="([^"]+\.pdf[^"]*)"[^>]*>(.*?)</a>',
        table_match.group(0), re.DOTALL | re.IGNORECASE,
    ):
        url, raw_title = m.group(1), m.group(2)
        title = html_module.unescape(re.sub(r"<[^>]+>", "", raw_title)).strip()
        if title and url:
            candidates.append((title, url))
    return candidates[:MAX_ARCHIVE_LINKS_PER_PAGE]


# Some year-group content lives on a PERSISTENT page (e.g. a Microsoft Sway
# newsletter) that the teacher updates in place week to week, rather than a
# new document being created each time -- so unlike the archive PDFs above,
# checking "have we seen this URL before" doesn't work here, since the URL
# never changes even though the content genuinely does. These are checked
# every run regardless, and only treated as newly-actionable when their
# CONTENT has actually changed since the last run (tracked by content hash).
# This list also serves as a direct, known-good path to content that's
# reachable in a browser but whose link could not be automatically
# discovered from within its parent PDF (see project notes) -- rather than
# only ever depending on solving that, a confirmed year-group link can be
# added here directly.
FIXED_YEAR_GROUP_LINKS = [
    ("Year 5", "https://sway.cloud.microsoft/jdzXaIQ3OW6pcU6s?ref=Link"),
]


def check_fixed_link_for_new_content(conn, year_group, url):
    """
    Fetches a fixed, persistent link and returns its text ONLY if the
    content has changed since the last time this was checked (by comparing
    a hash of the extracted text) -- otherwise returns None, so a page that
    hasn't been updated since last run doesn't get needlessly reprocessed
    by Gemini every single time.
    """
    text = fetch_linked_content_text([url])
    if not text.strip():
        print(f"WARNING: fixed link for {year_group} ('{url}') produced no extractable text this run")
        return None

    new_hash = hashlib.sha256(text.encode("utf-8", errors="ignore")).hexdigest()
    row = conn.execute(
        "SELECT content_hash FROM link_content_hashes WHERE url = ?", (url,)
    ).fetchone()
    old_hash = row[0] if row else None

    conn.execute(
        "INSERT INTO link_content_hashes (url, content_hash, last_checked) VALUES (?, ?, ?) "
        "ON CONFLICT(url) DO UPDATE SET content_hash = excluded.content_hash, "
        "last_checked = excluded.last_checked",
        (url, new_hash, datetime.now(timezone.utc).isoformat()),
    )
    conn.commit()

    if new_hash == old_hash:
        print(f"DEBUG: fixed link for {year_group} unchanged since last check, skipping re-extraction")
        return None
    print(f"DEBUG: fixed link for {year_group} has new/changed content since last check")
    return text


def extract_attachment_text(part):
    filename = part.get_filename() or ""
    payload = part.get_payload(decode=True)
    if not payload:
        print(f"WARNING: attachment '{filename}' has no readable payload, skipping")
        return ""
    lower = filename.lower()
    content_type = part.get_content_type()
    try:
        if lower.endswith(".pdf") or content_type == "application/pdf":
            return extract_pdf_bytes_text(payload, filename)

        elif lower.endswith(".docx") or "wordprocessingml.document" in content_type:
            import docx
            doc = docx.Document(io.BytesIO(payload))
            text_parts = [p.text for p in doc.paragraphs]
            # Word docs can carry a photo/flyer as an embedded image rather
            # than real text -- OCR every embedded image too, the same way
            # a PDF or slide deck's images already are, so nothing in a
            # docx attachment is silently missed just because it's a picture.
            try:
                for i, rel in enumerate(doc.part.rels.values()):
                    if "image" in rel.reltype:
                        img_bytes = rel.target_part.blob
                        ocr_text = ocr_image_bytes(img_bytes, f"{filename} image {i+1}")
                        if ocr_text and not ocr_text.startswith("[Could not"):
                            text_parts.append(f"[OCR from embedded image {i+1}]\n{ocr_text}")
            except Exception as e:
                text_parts.append(f"[Could not OCR images embedded in {filename}: {e}]")
            return "\n".join(text_parts)

        elif lower.endswith((".xlsx", ".xlsm")) or "spreadsheetml" in content_type:
            import openpyxl
            wb = openpyxl.load_workbook(io.BytesIO(payload), data_only=True, read_only=True)
            text_parts = []
            for sheet in wb.worksheets:
                text_parts.append(f"[Sheet: {sheet.title}]")
                for row in sheet.iter_rows(values_only=True):
                    line = " | ".join(str(c) for c in row if c is not None)
                    if line.strip():
                        text_parts.append(line)
            return "\n".join(text_parts)

        elif lower.endswith(".pptx") or "presentationml" in content_type:
            from pptx import Presentation
            prs = Presentation(io.BytesIO(payload))
            text_parts = []
            for i, slide in enumerate(prs.slides):
                text_parts.append(f"[Slide {i+1}]")
                for shape in slide.shapes:
                    if shape.has_text_frame:
                        for para in shape.text_frame.paragraphs:
                            line = "".join(run.text for run in para.runs)
                            if line.strip():
                                text_parts.append(line)
                    if shape.shape_type == 13:
                        try:
                            img_bytes = shape.image.blob
                            ocr_text = ocr_image_bytes(img_bytes, f"slide {i+1} image")
                            if ocr_text and not ocr_text.startswith("[Could not"):
                                text_parts.append(f"[OCR from slide {i+1} image]\n{ocr_text}")
                        except Exception:
                            pass
            return "\n".join(text_parts)

        elif lower.endswith((".txt", ".rtf")) or content_type == "text/plain":
            return payload.decode("utf-8", errors="ignore")

        elif lower.endswith(".csv") or content_type == "text/csv":
            return payload.decode("utf-8", errors="ignore")

        elif lower.endswith(IMAGE_EXTENSIONS) or content_type.startswith("image/"):
            ocr_text = ocr_image_bytes(payload, filename)
            return f"[OCR from image {filename}]\n{ocr_text}" if ocr_text else ""

        else:
            # Anything not covered above (video, audio, .pages/.key/.numbers,
            # zip archives, etc.) previously vanished here with zero trace.
            # We still can't extract text from most of these, but at least
            # this makes the gap visible in the log instead of invisible --
            # a parent (or future debugging session) can see exactly which
            # attachment got skipped and why, rather than data just disappearing.
            print(f"WARNING: attachment '{filename}' (type: {content_type}) has no extraction "
                  f"path -- its content is NOT included. Recognized types: PDF, DOCX, XLSX/XLSM, "
                  f"PPTX, TXT/RTF, CSV, and images (incl. HEIC).")
            return ""
    except Exception as e:
        print(f"WARNING: error extracting attachment '{filename}': {e}")
        return f"[Could not extract {filename}: {e}]"


def get_email_body_and_attachments(msg):
    plain_body = ""
    html_body = ""
    attachment_text_blocks = []
    attachment_filenames = []

    if msg.is_multipart():
        for part in msg.walk():
            content_disposition = str(part.get("Content-Disposition") or "")
            content_type = part.get_content_type()
            filename = part.get_filename()

            is_attachment_like = (
                "attachment" in content_disposition
                or filename
                or content_type.startswith("image/")
            )

            if is_attachment_like and content_type != "text/plain":
                text = extract_attachment_text(part)
                if text.strip():
                    label = filename or content_type
                    attachment_text_blocks.append(f"--- Attachment: {label} ---\n{text}")
                if filename:
                    attachment_filenames.append(decode_mime_words(filename))
            elif content_type == "text/plain" and not plain_body:
                payload = part.get_payload(decode=True)
                if payload:
                    plain_body = payload.decode(part.get_content_charset() or "utf-8", errors="ignore")
            elif content_type == "text/html" and not html_body:
                payload = part.get_payload(decode=True)
                if payload:
                    html_body = payload.decode(part.get_content_charset() or "utf-8", errors="ignore")
    else:
        payload = msg.get_payload(decode=True)
        if payload:
            decoded = payload.decode(msg.get_content_charset() or "utf-8", errors="ignore")
            if msg.get_content_type() == "text/html":
                html_body = decoded
            else:
                plain_body = decoded

    body = plain_body.strip()
    if not body and html_body:
        body = html_to_text(html_body)

    link_urls = extract_links_from_text(html_body, plain_body)
    if link_urls:
        linked_text = fetch_linked_content_text(link_urls)
        if linked_text.strip():
            attachment_text_blocks.append(linked_text)

    return body, "\n\n".join(attachment_text_blocks), attachment_filenames


def get_folder_names(imap_conn):
    status, folder_list = imap_conn.list()
    if status != "OK" or not folder_list:
        return []
    folder_names = []
    for entry in folder_list:
        if not entry:
            continue
        decoded = entry.decode(errors="ignore")
        if '"' in decoded:
            parts = decoded.split('"')
            if len(parts) >= 2:
                folder_names.append(parts[-2])
    return folder_names


def fetch_new_school_emails(imap_conn, db_conn, total_limit=TOTAL_EMAIL_LIMIT):
    """
    Two-pass fetch:
      1. Scan every non-skipped folder and pull just headers (cheap) for
         unprocessed diadubai.com emails since 2025, recording each one's
         actual email date.
      2. Sort ALL candidates across every folder by date, newest first, and
         only download the full body/attachments for the top `total_limit`.

    This matters because folders are otherwise visited in whatever order
    imap_conn.list() returns them. If an earlier folder (e.g. Inbox) has a
    large backlog of unprocessed matches, it can consume the entire
    per-run budget before a nested folder (e.g. "Dubai/DIA/DIA Secondary")
    ever gets a look — silently starving it of new items run after run,
    even though the pipeline reports success. Sorting candidates globally
    by date fixes that: the newest email always wins regardless of which
    folder it happens to live in.
    """
    all_folders = get_folder_names(imap_conn)
    folder_names = [f for f in all_folders if not any(kw in f.lower() for kw in SKIP_FOLDER_KEYWORDS)]
    print(f"DEBUG: searching {len(folder_names)} folders (skipped {len(all_folders) - len(folder_names)} junk/trash/sent/etc.)")

    candidates = []

    for folder in folder_names:
        try:
            status, _ = imap_conn.select(f'"{folder}"', readonly=True)
        except Exception:
            continue
        if status != "OK":
            continue

        message_nums_set = set()
        for domain in SCHOOL_DOMAINS:
            search_query = f'(FROM "{domain}" SINCE "01-Jan-2025")'
            status, data = imap_conn.search(None, search_query)
            if status == "OK" and data and data[0]:
                message_nums_set.update(data[0].split())
        if not message_nums_set:
            continue
        message_nums = list(message_nums_set)

        for num in message_nums:
            status, header_data = imap_conn.fetch(
                num, "(BODY.PEEK[HEADER.FIELDS (MESSAGE-ID SUBJECT FROM DATE)])"
            )
            if status != "OK" or not header_data or not header_data[0]:
                continue
            raw_header = header_data[0][1]
            if not raw_header:
                continue
            header_msg = email.message_from_bytes(raw_header)
            message_id = header_msg.get("Message-ID", f"no-id-{folder}-{num.decode()}")

            if already_processed(db_conn, message_id):
                continue

            subject = decode_mime_words(header_msg.get("Subject", ""))
            sender = decode_mime_words(header_msg.get("From", ""))
            date_str = header_msg.get("Date", "")

            candidates.append({
                "folder": folder,
                "num": num,
                "message_id": message_id,
                "subject": subject,
                "sender": sender,
                "date_str": date_str,
                "date_parsed": parse_email_date(date_str),
            })

    print(f"DEBUG: found {len(candidates)} unprocessed email(s) across all folders (before per-run limit)")

    candidates.sort(key=lambda c: c["date_parsed"], reverse=True)
    selected = candidates[:total_limit]

    emails = []
    current_folder = None
    for cand in selected:
        if cand["folder"] != current_folder:
            try:
                status, _ = imap_conn.select(f'"{cand["folder"]}"', readonly=True)
            except Exception:
                continue
            if status != "OK":
                continue
            current_folder = cand["folder"]

        # Everything from here through body/attachment extraction is wrapped:
        # IMAP servers occasionally return a malformed/unexpected response
        # shape for a single message (observed: a plain bytes object instead
        # of the expected (metadata, literal) tuple, which crashes with
        # "'int' object has no attribute 'decode'" when indexed like a
        # tuple). One oddball email must never take down an entire run that
        # may have already done many minutes of real work -- skip it and
        # keep going instead.
        try:
            status, msg_data = imap_conn.fetch(cand["num"], "(RFC822)")
            if status != "OK" or not msg_data or not msg_data[0]:
                continue
            raw_email = msg_data[0][1]
            if not raw_email or not isinstance(raw_email, bytes):
                print(f"WARNING: skipping '{cand['subject'][:60]}' -- IMAP returned an unexpected "
                      f"response shape instead of the raw message bytes")
                continue
            msg = email.message_from_bytes(raw_email)

            print(f"DEBUG: reading '{cand['subject'][:60]}' from '{cand['folder']}' (extracting attachments/images/HTML)...")
            body, attachment_text, attachment_filenames = get_email_body_and_attachments(msg)
        except Exception as e:
            print(f"WARNING: skipping '{cand['subject'][:60]}' after an error while fetching/reading it: {e}")
            continue

        emails.append({
            "message_id": cand["message_id"],
            "subject": cand["subject"],
            "sender": cand["sender"],
            "date": cand["date_str"],
            "body": body,
            "attachment_text": attachment_text,
            "attachment_files": ", ".join(attachment_filenames),
        })

    print(f"DEBUG: collected {len(emails)} new unprocessed email(s) to send to Gemini (most recent first)")
    return emails


def full_reprocess_wipe(conn):
    """
    Wipes the entire processed-messages history, all extracted events, and
    the email log, so the next fetch treats every single email since Jan
    2025 as brand new and re-runs it through whatever the current
    extraction logic is. Useful after a meaningful pipeline improvement
    (e.g. full-page OCR, link-following), since emails processed under an
    older, buggier version are otherwise permanently stuck with incomplete
    results -- this forces a genuinely clean slate.
    """
    conn.execute("DELETE FROM events")
    conn.execute("DELETE FROM processed_messages")
    conn.execute("DELETE FROM email_log")
    conn.commit()
    print("DEBUG: FULL REPROCESS requested -- wiped all events, processed-message history, and email log")


def force_reprocess_by_keyword(conn, keyword):
    """
    Lets a specific past email be re-processed under the current (improved)
    extraction logic, for cases where an email was marked "processed" before a
    fix (e.g. full-page OCR) existed, and so is permanently stuck with its old,
    incomplete results. Matches against the events table's email_subject, the
    processed_messages table's subject column, and the email_log table's
    subject column, so it finds a message regardless of whether it produced
    any events. Clears the matching message(s) from all three tables, so the
    next fetch treats them as brand new.
    """
    keyword = (keyword or "").strip()
    if not keyword:
        return
    like = f"%{keyword}%"
    ids_from_events = conn.execute(
        "SELECT DISTINCT source_message_id FROM events WHERE email_subject LIKE ?", (like,)
    ).fetchall()
    ids_from_processed = conn.execute(
        "SELECT DISTINCT message_id FROM processed_messages WHERE subject LIKE ?", (like,)
    ).fetchall()
    ids_from_log = conn.execute(
        "SELECT DISTINCT message_id FROM email_log WHERE subject LIKE ?", (like,)
    ).fetchall()
    message_ids = {row[0] for row in (ids_from_events + ids_from_processed + ids_from_log) if row[0]}
    if not message_ids:
        print(f"DEBUG: force-reprocess keyword '{keyword}' matched no known message subject")
        return
    conn.executemany("DELETE FROM processed_messages WHERE message_id = ?", [(m,) for m in message_ids])
    conn.executemany("DELETE FROM events WHERE source_message_id = ?", [(m,) for m in message_ids])
    conn.executemany("DELETE FROM email_log WHERE message_id = ?", [(m,) for m in message_ids])
    conn.commit()
    print(f"DEBUG: cleared {len(message_ids)} message(s) matching '{keyword}' for reprocessing")


def year_group_tokens(yg):
    """
    Splits a (possibly multi-value, comma-separated) year_group string into a
    set of tokens, then expands any stage-wide tag into its constituent
    specific years/Kindergarten -- so "Primary" and "Kindergarten, Primary"
    are recognized as overlapping (they share "Primary" directly, and this
    expansion also catches cases like "Primary" vs "Year 5" that don't share
    a literal token but clearly refer to the same population).
    """
    tokens = {t.strip() for t in (yg or "").split(",") if t.strip()}
    expanded = set(tokens)
    if "Primary" in tokens:
        expanded.update({"Kindergarten", "Year 1", "Year 2", "Year 3", "Year 4", "Year 5", "Year 6"})
    if "Secondary" in tokens:
        expanded.update({f"Year {n}" for n in range(7, 14)})
    return expanded


def dedupe_similar_events(conn):
    """
    Removes duplicate/superseded events that build up when the same recurring
    item (e.g. "CCA sign-ups begin") gets mentioned across multiple separate
    emails over time. Two events are only ever considered possible duplicates
    if they share the same category, year_group, AND a compatible date: two
    dated events must share the exact same date (different dates mean
    genuinely different occurrences, even with similar wording -- "CCA
    Sign-Up" for two different clubs, for example); two UNDATED events are
    NEVER merged with each other, since there's no reliable way to tell a
    true duplicate from two distinct undated announcements; but an undated
    event CAN be merged away in favor of a DATED version of the same thing
    (e.g. an early "sign-ups coming soon" mention, later followed by "sign-
    ups open Sept 22" in a newer email) -- the dated version is strictly
    more informative, so there's a real basis to prefer it, unlike the
    undated-vs-undated case. On top of the date check, titles must also be
    a very close textual match (>= 0.85 similarity, or a strong substring/
    word-overlap match). Within each matched group, keeps the most useful
    version (preferring "All"-tagged, then dated, then most recent).
    """
    rows = conn.execute(
        "SELECT id, title, category, year_group, date, email_date, created_at FROM events"
    ).fetchall()

    def sort_key(row):
        _, _, _, _, _, email_date, created_at = row
        dt = parse_email_date(email_date)
        if dt == datetime.min.replace(tzinfo=timezone.utc) and created_at:
            try:
                return datetime.fromisoformat(created_at)
            except Exception:
                pass
        return dt

    used = set()
    to_delete = []
    n = len(rows)
    for i in range(n):
        id_i = rows[i][0]
        if id_i in used:
            continue
        title_i, cat_i, yg_i, date_i = (rows[i][1] or ""), rows[i][2], rows[i][3], rows[i][4]
        group = [rows[i]]
        for j in range(i + 1, n):
            id_j = rows[j][0]
            if id_j in used:
                continue
            title_j, cat_j, yg_j, date_j = (rows[j][1] or ""), rows[j][2], rows[j][3], rows[j][4]
            if cat_j != cat_i:
                continue
            # year_group must OVERLAP -- not necessarily match exactly. "All"
            # is compatible with anything (it's a strict superset), and two
            # multi-value or stage tags that share any specific year/stage
            # (e.g. "Primary" and "Kindergarten, Primary", or "Primary" and
            # "Year 5") are treated as the same population, even though the
            # raw strings differ. Two genuinely disjoint tags (e.g. "Year 5"
            # vs "Year 8") still correctly fail this check.
            tokens_i = year_group_tokens(yg_i)
            tokens_j = year_group_tokens(yg_j)
            yg_compatible = "All" in tokens_i or "All" in tokens_j or bool(tokens_i & tokens_j)
            if not yg_compatible:
                continue
            # Two undated items are NEVER merged with each other, even with
            # identical titles -- there's no reliable way to tell a true
            # duplicate from two distinct undated announcements. But an
            # undated item CAN be recognized as an early, vague "heads up"
            # for something a LATER email then gave a firm date for (e.g.
            # "Tunes on Tuesday Begins" in one week's newsletter, then
            # "Tunes on Tuesday Start Date: Sept 22" in the next) -- the
            # dated version is strictly more informative, so this is safe:
            # unlike two undated items, there's a clear, information-based
            # reason to prefer one over the other, not just a guess.
            if date_i and date_j and date_i != date_j:
                continue
            if not date_i and not date_j:
                continue
            title_i_norm = title_i.lower().strip()
            title_j_norm = title_j.lower().strip()
            ratio = difflib.SequenceMatcher(None, title_i_norm, title_j_norm).ratio()
            # A long title fully contained inside another (e.g. "Inclusion
            # Coffee Morning" inside "Secondary Inclusion Coffee Morning") is
            # a strong, specific signal of the same event described with an
            # extra qualifier word -- catches cases the similarity ratio
            # alone scores just under threshold on, without the looseness
            # that caused genuinely different titles to over-merge before
            # (unrelated titles essentially never contain one another).
            substring_match = (
                len(title_i_norm) >= 10 and len(title_j_norm) >= 10
                and (title_i_norm in title_j_norm or title_j_norm in title_i_norm)
            )
            # Word-overlap: catches cases like "Staff PD Day (Early
            # Dismissal)" vs "Innoventures Education PD Day (Early
            # Dismissal)", or "Tunes on Tuesday Begins" vs "Tunes on Tuesday
            # Start Date" -- the same event named with extra/different
            # words added, sharing most of the SHORTER title's meaningful
            # words even though the longer title adds enough extra words
            # that a union-based ratio would under-count the match. Requires
            # BOTH a high proportion of the shorter title's words to be
            # shared AND at least 3 shared words, so short titles with one
            # incidental shared word (e.g. "PE Day" / "PE Uniform Check")
            # can't false-positive.
            words_i = set(re.findall(r"[a-z0-9]+", title_i_norm))
            words_j = set(re.findall(r"[a-z0-9]+", title_j_norm))
            shared_words = words_i & words_j
            word_overlap = (
                len(shared_words) / min(len(words_i), len(words_j))
                if (words_i and words_j) else 0
            )
            word_match = word_overlap >= 0.6 and len(shared_words) >= 3
            if ratio >= 0.85 or substring_match or word_match:
                group.append(rows[j])
        if len(group) > 1:
            # If the group contains an "All"-tagged version, prefer keeping
            # that one -- it's a strict superset of any narrower tag in the
            # same group, so keeping it never hides the event from anyone
            # who should see it. Otherwise, prefer a DATED version over an
            # undated one -- a firm date is strictly more useful than a
            # vague "coming soon" mention of the same thing. Recency is
            # still the final tiebreak within whichever pool applies.
            all_tagged = [r for r in group if r[3] == "All"]
            dated = [r for r in group if r[4]]
            preferred_pool = all_tagged if all_tagged else (dated if dated else group)
            keep = sorted(preferred_pool, key=sort_key, reverse=True)[0]
            for row in group:
                if row[0] != keep[0]:
                    to_delete.append(row[0])
                used.add(row[0])
        else:
            used.add(id_i)

    if to_delete:
        conn.executemany("DELETE FROM events WHERE id = ?", [(rid,) for rid in to_delete])
        conn.commit()
        print(f"DEBUG: removed {len(to_delete)} duplicate/superseded event(s) (kept most recent per group)")


def retag_legacy_topics(conn):
    """
    Maps the old 7-value topic taxonomy (Academic/Admin/CCAs/Clinic/Events/PE/Payments)
    used before the simplified Academics/School Info/Activities system onto the new
    values, so old rows don't keep showing stale topic chips on the board. This is a
    straightforward 1:1 lookup (not a heuristic guess), so it's safe to run every time.
    """
    TOPIC_MIGRATION = {
        "Academic": "Academics",
        "Admin": "School Info",
        "Events": "School Info",
        "Clinic": "School Info",
        "Payments": "School Info",
        "CCAs": "Activities",
        "PE": "Activities",
    }
    updated = 0
    for old, new in TOPIC_MIGRATION.items():
        cur = conn.execute("UPDATE events SET topic = ? WHERE topic = ?", (new, old))
        updated += cur.rowcount
    if updated:
        conn.commit()
        print(f"DEBUG: migrated {updated} event(s) from the old topic taxonomy to Academics/School Info/Activities")


def retag_kindergarten_only_items(conn):
    """
    Fixes existing events that were tagged "Primary" or "All" before the
    dedicated "Kindergarten" tag existed, even though they're specifically
    about KG1/KG2 only (e.g. "KG1 & KG2 Parent Information Session"). Without
    this, such items incorrectly show up under a Year 5 child's filter, since
    "Primary" is treated as covering that child's stage. Only retags rows
    that mention KG1/KG2/Kindergarten AND do not also mention any Year 1+
    group -- genuinely whole-Primary content (which legitimately spans
    KG1 through Year 6, e.g. many "KG1-Y6" newsletters) is left untouched.
    """
    year_mention_re = re.compile(r"\byear\s*([1-9]|1[0-3])\b", re.IGNORECASE)
    kg_mention_re = re.compile(r"\bkg\s*1\b|\bkg\s*2\b|\bkindergarten\b", re.IGNORECASE)

    rows = conn.execute(
        "SELECT id, title, summary, email_subject FROM events WHERE year_group IN ('Primary', 'All')"
    ).fetchall()
    updated = 0
    for row_id, title, summary, email_subject in rows:
        text = f"{title or ''} {summary or ''} {email_subject or ''}"
        if kg_mention_re.search(text) and not year_mention_re.search(text):
            conn.execute("UPDATE events SET year_group = 'Kindergarten' WHERE id = ?", (row_id,))
            updated += 1
    if updated:
        conn.commit()
        print(f"DEBUG: retagged {updated} KG-only item(s) from Primary/All to Kindergarten")


def extract_mentioned_years(text):
    """Finds explicit Year mentions in text, including both single mentions
    ("Year 5") and hyphenated ranges ("Year 3-4", "Years 4 to 6"). Returns a
    set of integer year numbers (1-13)."""
    years = set()
    for m in re.finditer(r"years?\s*(\d{1,2})\s*(?:-|to)\s*(\d{1,2})", text, re.IGNORECASE):
        lo, hi = int(m.group(1)), int(m.group(2))
        if 1 <= lo <= hi <= 13 and (hi - lo) <= 12:
            years.update(range(lo, hi + 1))
    for m in re.finditer(r"\byears?\s*(\d{1,2})\b", text, re.IGNORECASE):
        n = int(m.group(1))
        if 1 <= n <= 13:
            years.add(n)
    return years


def retag_year_subset_items(conn):
    """
    Fixes existing events tagged broadly "Primary"/"Secondary"/"All" that
    actually name an explicit SUBSET of years -- e.g. "Year 1 & Year 2 Parent
    Information Session" -- rather than the whole stage. Schools often bundle
    several separate sessions in one email, each for a different pair of
    years; without this, every one of them gets rounded up to "Primary" and
    incorrectly matches every Primary child's filter, not just the ones it's
    actually for. Only uses the item's own title/summary (not the shared
    email_subject, which can mention a broader range for the newsletter as a
    whole and would cause false narrowing). Leaves alone anything that
    mentions the FULL stage range (e.g. "Year 1-6") or spans both stages, or
    has no explicit year mention at all.
    """
    rows = conn.execute(
        "SELECT id, title, summary, year_group FROM events WHERE year_group IN ('Primary', 'Secondary', 'All')"
    ).fetchall()
    updated = 0
    for row_id, title, summary, current_yg in rows:
        text = f"{title or ''} {summary or ''}"
        years = extract_mentioned_years(text)
        if not years:
            continue
        primary_years = {y for y in years if 1 <= y <= 6}
        secondary_years = {y for y in years if 7 <= y <= 13}
        if primary_years and not secondary_years and primary_years != set(range(1, 7)):
            new_yg = ", ".join(f"Year {y}" for y in sorted(primary_years))
            conn.execute("UPDATE events SET year_group = ? WHERE id = ?", (new_yg, row_id))
            updated += 1
        elif secondary_years and not primary_years and secondary_years != set(range(7, 14)):
            new_yg = ", ".join(f"Year {y}" for y in sorted(secondary_years))
            conn.execute("UPDATE events SET year_group = ? WHERE id = ?", (new_yg, row_id))
            updated += 1
        # Mixed-stage mentions or a full-stage match are left as-is.
    if updated:
        conn.commit()
        print(f"DEBUG: retagged {updated} item(s) from broad Primary/Secondary/All to specific year subsets")




def extract_events_batch(model, emails_batch, max_retries=3):
    combined = ""
    for i, ed in enumerate(emails_batch):
        combined += f"\n=== EMAIL {i} ===\nSubject: {ed['subject']}\nFrom: {ed['sender']}\nDate: {ed['date']}\n\nBody:\n{ed['body']}\n\n{ed['attachment_text']}\n"

    response = None
    last_error = None
    for attempt in range(max_retries):
        try:
            response = model.generate_content(
                [EXTRACTION_SYSTEM_PROMPT, combined],
                generation_config={"response_mime_type": "application/json"},
            )
            break
        except Exception as e:
            last_error = e
            err_str = str(e)
            if "perday" in err_str.lower():
                # This is a DAILY quota cap (Google's free tier allows a small
                # fixed number of requests per day for this model), not a
                # transient per-minute rate limit. Retrying within the same
                # run cannot help -- only waiting for the daily window to
                # reset will. Give up immediately instead of wasting minutes
                # on pointless retries.
                print(f"WARNING: hit the Gemini free-tier DAILY quota — this cannot be fixed by retrying today. Error: {e}")
                return None
            if "429" in err_str or "quota" in err_str.lower():
                wait = 30 * (attempt + 1)
                print(f"WARNING: rate limited, waiting {wait}s before retry... (error: {e})")
                time.sleep(wait)
                continue
            else:
                print(f"WARNING: Gemini call failed: {e}")
                return None
    else:
        print(f"WARNING: gave up after retries due to rate limiting. Last error: {last_error}")
        return None

    if response is None:
        return None

    text = (response.text or "").strip()
    text = text.removeprefix("```json").removeprefix("```").removesuffix("```").strip()

    try:
        parsed = json.loads(text)
        if isinstance(parsed, dict) and "items" in parsed:
            return parsed
        # Defensive fallback: if the model ignored the object format and
        # returned a bare array (old format), wrap it so callers always see
        # the same shape.
        if isinstance(parsed, list):
            return {"items": parsed, "email_summaries": []}
        return {"items": [], "email_summaries": []}
    except json.JSONDecodeError:
        print(f"WARNING: could not parse Gemini batch response:\n{text[:500]}")
        return {"items": [], "email_summaries": []}


def save_events(conn, email_data, events):
    attachment_files = email_data.get("attachment_files", "")
    for ev in events:
        conn.execute("""
            INSERT INTO events (source_message_id, title, date, category, year_group, class_group, topic,
                                 summary, conflict_note, email_subject, email_sender, email_date,
                                 attachment_files, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            email_data["message_id"],
            ev.get("title"),
            ev.get("date"),
            ev.get("category"),
            ev.get("year_group"),
            ev.get("class_group"),
            ev.get("topic"),
            ev.get("summary"),
            ev.get("conflict_note"),
            email_data["subject"],
            email_data["sender"],
            email_data["date"],
            attachment_files,
            datetime.now(timezone.utc).isoformat(),
        ))
    conn.commit()


def save_email_log(conn, email_data, topic, summary, has_actionable):
    """
    Records that this email was processed, regardless of whether it produced
    any actionable items -- this is what powers the Email Summaries view on
    the board, which (unlike the Deadlines/Events tabs) is meant to show a
    running log of every email seen, not just the ones with dated items.
    """
    conn.execute("""
        INSERT OR REPLACE INTO email_log
            (message_id, subject, sender, email_date, topic, summary, has_actionable, attachment_files, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        email_data["message_id"],
        email_data["subject"],
        email_data["sender"],
        email_data["date"],
        topic,
        summary,
        1 if has_actionable else 0,
        email_data.get("attachment_files", ""),
        datetime.now(timezone.utc).isoformat(),
    ))
    conn.commit()


def main():
    if not GEMINI_API_KEY:
        sys.exit("ERROR: set GEMINI_API_KEY environment variable")

    genai.configure(api_key=GEMINI_API_KEY)
    model = genai.GenerativeModel(MODEL)
    db_conn = init_db()

    full_reprocess = os.environ.get("FULL_REPROCESS", "").strip().lower() in ("true", "1", "yes")
    if full_reprocess:
        full_reprocess_wipe(db_conn)

    retag_legacy_all_items(db_conn)
    retag_legacy_topics(db_conn)
    retag_kindergarten_only_items(db_conn)
    retag_year_subset_items(db_conn)

    force_reprocess_keyword = os.environ.get("FORCE_REPROCESS_KEYWORD", "").strip()
    if force_reprocess_keyword:
        force_reprocess_by_keyword(db_conn, force_reprocess_keyword)

    email_limit = TOTAL_EMAIL_LIMIT
    limit_override = os.environ.get("EMAIL_LIMIT_OVERRIDE", "").strip()
    if limit_override.isdigit():
        email_limit = int(limit_override)
        print(f"DEBUG: per-run email limit overridden to {email_limit} (default is {TOTAL_EMAIL_LIMIT})")

    chunk_size = GEMINI_CHUNK_SIZE
    chunk_override = os.environ.get("GEMINI_CHUNK_SIZE_OVERRIDE", "").strip()
    if chunk_override.isdigit() and int(chunk_override) > 0:
        chunk_size = int(chunk_override)
        print(f"DEBUG: Gemini chunk size overridden to {chunk_size} (default is {GEMINI_CHUNK_SIZE}) "
              f"-- larger means less thorough per-email reading, but conserves daily quota for backlog catch-up")

    deleted = db_conn.execute("DELETE FROM events WHERE date IS NOT NULL AND date < '2025-01-01'").rowcount
    db_conn.commit()
    print(f"DEBUG: cleared {deleted} old event(s) from before 2025")

    print("Connecting to iCloud Mail...")
    imap_conn = imap_connect()

    print(f"Fetching emails from: {', '.join(SCHOOL_DOMAINS)}...")
    emails = fetch_new_school_emails(imap_conn, db_conn, total_limit=email_limit)

    # Also check the school's own public newsletter archive pages directly --
    # a more reliable second source for the SAME recurring weekly newsletters
    # (see fetch_newsletter_archive_candidates' docstring for why). Each
    # not-yet-seen edition is wrapped as a synthetic "email" so it flows
    # through the exact same Gemini extraction and save pipeline as a real
    # email, unchanged.
    for label, archive_url in NEWSLETTER_ARCHIVE_PAGES:
        for title, pdf_url in fetch_newsletter_archive_candidates(archive_url, label):
            synthetic_id = f"archive:{pdf_url}"
            if already_processed(db_conn, synthetic_id):
                continue
            print(f"DEBUG: found not-yet-seen {label} newsletter on the school website: '{title}'")
            pdf_text = fetch_linked_content_text([pdf_url])
            if not pdf_text.strip():
                print(f"WARNING: '{title}' from the newsletter archive produced no extractable "
                      f"text -- marking as seen anyway so it isn't retried every run")
            emails.append({
                "message_id": synthetic_id,
                "subject": title,
                "sender": f"{label.lower()}-school-newsletter-archive@diadubai.com (website archive)",
                "date": datetime.now(timezone.utc).strftime("%a, %d %b %Y %H:%M:%S +0000"),
                "body": "",
                "attachment_text": pdf_text,
                "attachment_files": os.path.basename(pdf_url.split("?")[0]),
            })

    # Fixed, persistent year-group links (see FIXED_YEAR_GROUP_LINKS) --
    # checked every run; only turned into a synthetic "email" when their
    # content has actually changed since last time.
    for year_group, url in FIXED_YEAR_GROUP_LINKS:
        new_text = check_fixed_link_for_new_content(db_conn, year_group, url)
        if new_text:
            emails.append({
                "message_id": f"fixedlink:{url}:{datetime.now(timezone.utc).date().isoformat()}",
                "subject": f"{year_group} Newsletter (updated)",
                "sender": "year-group-newsletter@diadubai.com (persistent page, checked directly)",
                "date": datetime.now(timezone.utc).strftime("%a, %d %b %Y %H:%M:%S +0000"),
                "body": "",
                "attachment_text": new_text,
                "attachment_files": "",
            })

    if not emails:
        print("No new emails to process.")
    else:
        # Send Gemini calls in chunks rather than one single call covering
        # everything. This still isolates failures (if one chunk's call
        # fails, only that chunk's emails stay unprocessed for next run,
        # not the whole batch) while keeping the chunk size large enough
        # that the total number of calls stays low -- important given the
        # free tier's daily (not per-minute) request cap.
        chunks = [emails[i:i + chunk_size] for i in range(0, len(emails), chunk_size)]
        print(f"Sending {len(emails)} email(s) to Gemini in {len(chunks)} batch(es) of up to {chunk_size}...")

        for chunk_index, chunk in enumerate(chunks):
            print(f"--- Batch {chunk_index + 1}/{len(chunks)} ({len(chunk)} email(s)) ---")
            result = extract_events_batch(model, chunk)

            if result is None:
                print("Batch call failed after retries — leaving these emails unprocessed for next run.")
            else:
                all_items = result.get("items", [])
                email_summaries = result.get("email_summaries", [])

                by_index = {}
                for item in all_items:
                    idx = item.get("email_index")
                    by_index.setdefault(idx, []).append(item)

                summary_by_index = {}
                for es in email_summaries:
                    idx = es.get("email_index")
                    if idx is not None:
                        summary_by_index[idx] = es

                for i, email_data in enumerate(chunk):
                    items = by_index.get(i, [])
                    es = summary_by_index.get(i)

                    if items:
                        save_events(db_conn, email_data, items)
                        print(f"  -> '{email_data['subject'][:50]}': extracted {len(items)} item(s)")
                    else:
                        print(f"  -> '{email_data['subject'][:50]}': no actionable items")

                    # Log this email regardless of whether it had actionable items,
                    # so the board's Email Summaries view has a complete record.
                    # Fall back sensibly if the model didn't return a summary entry
                    # for this particular email.
                    if es:
                        log_topic = es.get("topic") or "School Info"
                        log_summary = es.get("summary") or ""
                        has_actionable = bool(es.get("has_actionable", bool(items)))
                    else:
                        log_topic = items[0].get("topic") if items else "School Info"
                        log_summary = items[0].get("summary") if items else "No summary available."
                        has_actionable = bool(items)
                    save_email_log(db_conn, email_data, log_topic, log_summary, has_actionable)

                    mark_processed(db_conn, email_data["message_id"], email_data.get("subject"))

            if chunk_index < len(chunks) - 1:
                time.sleep(5)  # brief pause between batches to ease rate-limit pressure

    dedupe_similar_events(db_conn)

    print("\nDone.")
    try:
        imap_conn.logout()
    except Exception as e:
        # By this point all extraction/saving is already committed to the DB --
        # a dropped connection during logout (the server closing an idle/long
        # session) shouldn't be allowed to fail the whole job and skip the
        # board rebuild + publish steps that come after this script.
        print(f"WARNING: IMAP logout failed (connection likely already closed by the server) — ignoring: {e}")
    db_conn.close()


if __name__ == "__main__":
    main()
