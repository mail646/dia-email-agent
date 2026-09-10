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
import html as html_module
from datetime import datetime, timezone
import io

import google.generativeai as genai
from PIL import Image
import pytesseract

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
GEMINI_CHUNK_SIZE = 10  # emails per Gemini call; keeps calls small enough to avoid rate limits
SKIP_FOLDER_KEYWORDS = ["trash", "junk", "deleted", "sent", "draft", "notes"]
IMAGE_EXTENSIONS = (".jpg", ".jpeg", ".png", ".gif", ".bmp", ".tiff", ".webp")

# Specific years, plus the two broader stage-wide tags, plus "All" for
# genuinely whole-school items. Primary = Years 1-6, Secondary = Years 7-13
# at DIA Emirates Hills.
YEAR_GROUPS = [
    "Year 4", "Year 5", "Year 6", "Year 7", "Year 8", "Year 9",
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

For each email, identify:
- Any deadlines, events, tasks, tests/assignments, or announcements with a date attached.
- Whole-school calendar items too: closures, holidays, early-dismissal days, etc. -- capture these as "event"
  category items. IMPORTANT: if the SAME closure/dismissal has different dates or times for different stages
  (e.g. Primary finishes at 11am but Secondary finishes at 12pm, or the stages break for a holiday on different
  days), create SEPARATE items -- one per stage/year group -- each with its own correct date and year_group.
  Do not collapse per-stage differences into a single item with just one date.
- Which year group(s) it applies to (choose from: {", ".join(YEAR_GROUPS)}).
  - Use a specific year like "Year 8" when the email clearly names one year group.
  - Use "Primary" when it applies broadly across Primary (Years 1-6) but not the whole school —
    e.g. an email addressed to "Year 7-13" or "Secondary" should be tagged "Secondary", NOT "All".
  - Use "Secondary" when it applies broadly across Secondary (Years 7-13) but not the whole school.
  - Only use "All" when the email is genuinely whole-school, or you truly cannot tell which stage/year it targets.
  - Do not default to "All" just because a range of years is mentioned — pick "Primary" or "Secondary" if the
    range sits entirely within one stage.
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

Respond ONLY with a single JSON array (no markdown fences, no preamble) combining items from ALL emails. Each item:
{{
  "email_index": 0,
  "title": "short title, e.g. 'Year 8 Sports Day'",
  "date": "YYYY-MM-DD or null if no specific date",
  "category": "deadline | event | task | announcement",
  "year_group": "one of the allowed values above",
  "topic": "one of the allowed topic values above",
  "summary": "1-2 sentence summary of what the parent needs to know or do",
  "conflict_note": "explanation if this superseded an earlier conflicting mention, else null"
}}

The "email_index" field MUST match the number in that email's "=== EMAIL N ===" marker. If an item combines info
from a NEWER email that supersedes an OLDER one in this same batch, use the email_index of the NEWER email.
If an email has NO actionable dated information, simply produce no items for it.
If an email covers multiple distinct items, produce multiple objects with the same email_index.
"""


def imap_connect():
    if not ICLOUD_APP_PASSWORD or "xxxx" in ICLOUD_APP_PASSWORD:
        sys.exit("ERROR: set ICLOUD_APP_PASSWORD")
    conn = imaplib.IMAP4_SSL(IMAP_SERVER, IMAP_PORT)
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
    existing_processed_cols = {row[1] for row in conn.execute("PRAGMA table_info(processed_messages)")}
    if "subject" not in existing_processed_cols:
        try:
            conn.execute("ALTER TABLE processed_messages ADD COLUMN subject TEXT")
        except sqlite3.OperationalError:
            pass
    existing_cols = {row[1] for row in conn.execute("PRAGMA table_info(events)")}
    for col in ["topic", "conflict_note"]:
        if col not in existing_cols:
            try:
                conn.execute(f"ALTER TABLE events ADD COLUMN {col} TEXT")
            except sqlite3.OperationalError:
                pass
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
    import fitz  # PyMuPDF
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


LINK_SKIP_PATTERNS = [
    "unsubscribe", "mailto:", "facebook.com", "twitter.com", "x.com", "instagram.com",
    "linkedin.com", "youtube.com", "tiktok.com",
]
MAX_LINKS_PER_EMAIL = 4
MAX_LINK_BYTES = 8 * 1024 * 1024  # 8 MB safety cap


def extract_links_from_text(html_body, plain_body):
    """Pulls http(s) links out of an email's HTML/plain body, filtering out
    obvious non-content links (unsubscribe, social media, etc.), capped to a
    small number per email to bound fetch time/cost."""
    urls = set()
    for text in (html_body or "", plain_body or ""):
        for match in re.findall(r'href=["\']((?:https?:)?//[^"\']+)["\']', text, re.IGNORECASE):
            urls.add(match if match.startswith("http") else "https:" + match)
        for match in re.findall(r'(https?://[^\s<>"\')]+)', text):
            urls.add(match.rstrip('.,);'))
    filtered = []
    for u in urls:
        low = u.lower()
        if any(p in low for p in LINK_SKIP_PATTERNS):
            continue
        filtered.append(u)
    return filtered[:MAX_LINKS_PER_EMAIL]


def fetch_linked_content_text(urls):
    """
    Follows a small number of links found in an email body and pulls text from
    what they point to -- some school newsletters put the actual content on a
    linked page or hosted PDF rather than in the email itself. HTML pages are
    extracted as plain text; PDFs go through the same full-page OCR pipeline as
    attachments.

    NOTE: pages that render their content via JavaScript (some newsletter
    platforms do) may come back mostly blank here, since this does a plain
    HTTP fetch rather than running a real browser. That's a known gap, not a
    bug, if a link like that comes back empty -- a real fix would need a
    headless-browser fetch instead.
    """
    import requests
    blocks = []
    for url in urls:
        try:
            resp = requests.get(url, timeout=15, headers={"User-Agent": "Mozilla/5.0"})
            if resp.status_code != 200 or len(resp.content) > MAX_LINK_BYTES:
                continue
            content_type = resp.headers.get("Content-Type", "").lower()
            if "pdf" in content_type or url.lower().endswith(".pdf"):
                text = extract_pdf_bytes_text(resp.content, url)
            elif "html" in content_type or not content_type:
                text = html_to_text(resp.text)
            else:
                continue
            if text and text.strip():
                blocks.append(f"--- Linked content: {url} ---\n{text.strip()}")
        except Exception as e:
            blocks.append(f"[Could not fetch linked content {url}: {e}]")
    return "\n\n".join(blocks)


def extract_attachment_text(part):
    filename = part.get_filename() or ""
    payload = part.get_payload(decode=True)
    if not payload:
        return ""
    lower = filename.lower()
    content_type = part.get_content_type()
    try:
        if lower.endswith(".pdf") or content_type == "application/pdf":
            return extract_pdf_bytes_text(payload, filename)

        elif lower.endswith(".docx") or "wordprocessingml.document" in content_type:
            import docx
            doc = docx.Document(io.BytesIO(payload))
            return "\n".join(p.text for p in doc.paragraphs)

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

        elif lower.endswith(".txt") or content_type == "text/plain":
            return payload.decode("utf-8", errors="ignore")

        elif lower.endswith(IMAGE_EXTENSIONS) or content_type.startswith("image/"):
            ocr_text = ocr_image_bytes(payload, filename)
            return f"[OCR from image {filename}]\n{ocr_text}" if ocr_text else ""
    except Exception as e:
        return f"[Could not extract {filename}: {e}]"
    return ""


def get_email_body_and_attachments(msg):
    plain_body = ""
    html_body = ""
    attachment_text_blocks = []

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

    return body, "\n\n".join(attachment_text_blocks)


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

        status, msg_data = imap_conn.fetch(cand["num"], "(RFC822)")
        if status != "OK" or not msg_data or not msg_data[0]:
            continue
        raw_email = msg_data[0][1]
        if not raw_email:
            continue
        msg = email.message_from_bytes(raw_email)

        print(f"DEBUG: reading '{cand['subject'][:60]}' from '{cand['folder']}' (extracting attachments/images/HTML)...")
        body, attachment_text = get_email_body_and_attachments(msg)

        emails.append({
            "message_id": cand["message_id"],
            "subject": cand["subject"],
            "sender": cand["sender"],
            "date": cand["date_str"],
            "body": body,
            "attachment_text": attachment_text,
        })

    print(f"DEBUG: collected {len(emails)} new unprocessed email(s) to send to Gemini (most recent first)")
    return emails


def full_reprocess_wipe(conn):
    """
    Wipes the entire processed-messages history and all extracted events, so
    the next fetch treats every single email since Jan 2025 as brand new and
    re-runs it through whatever the current extraction logic is. Useful after
    a meaningful pipeline improvement (e.g. full-page OCR, link-following),
    since emails processed under an older, buggier version are otherwise
    permanently stuck with incomplete results -- this forces a clean slate.
    """
    conn.execute("DELETE FROM events")
    conn.execute("DELETE FROM processed_messages")
    conn.commit()
    print("DEBUG: FULL REPROCESS requested -- wiped all events and processed-message history")


def force_reprocess_by_keyword(conn, keyword):
    """
    Lets a specific past email be re-processed under the current (improved)
    extraction logic, for cases where an email was marked "processed" before a
    fix (e.g. full-page OCR) existed, and so is permanently stuck with its old,
    incomplete results. Matches against both the events table's email_subject
    (works for any email that already produced at least one event) and
    processed_messages' subject column (works even for emails that previously
    produced zero events, as long as they were processed after this column was
    added). Clears the matching message(s) from processed_messages and deletes
    their existing events, so the next fetch treats them as brand new.
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
    message_ids = {row[0] for row in (ids_from_events + ids_from_processed) if row[0]}
    if not message_ids:
        print(f"DEBUG: force-reprocess keyword '{keyword}' matched no known message subject")
        return
    conn.executemany("DELETE FROM processed_messages WHERE message_id = ?", [(m,) for m in message_ids])
    conn.executemany("DELETE FROM events WHERE source_message_id = ?", [(m,) for m in message_ids])
    conn.commit()
    print(f"DEBUG: cleared {len(message_ids)} message(s) matching '{keyword}' for reprocessing")


def dedupe_similar_events(conn):
    """
    Removes duplicate/superseded events that build up over time when the same
    recurring item (e.g. "CCA sign-ups begin") gets mentioned across several
    separate emails on different days. Groups events that share the same
    category and year_group and have a highly similar title (using Python's
    built-in difflib, no extra dependency needed), and within each group keeps
    only the one tied to the most recent underlying email, deleting the rest.
    Runs over the whole table every run, so it cleans up both pre-existing and
    newly-added duplicates alike.
    """
    rows = conn.execute(
        "SELECT id, title, category, year_group, email_date, created_at FROM events"
    ).fetchall()

    def sort_key(row):
        _, _, _, _, email_date, created_at = row
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
        title_i, cat_i, yg_i = (rows[i][1] or ""), rows[i][2], rows[i][3]
        group = [rows[i]]
        for j in range(i + 1, n):
            id_j = rows[j][0]
            if id_j in used:
                continue
            title_j, cat_j, yg_j = (rows[j][1] or ""), rows[j][2], rows[j][3]
            if cat_j != cat_i or yg_j != yg_i:
                continue
            ratio = difflib.SequenceMatcher(None, title_i.lower().strip(), title_j.lower().strip()).ratio()
            if ratio >= 0.72:
                group.append(rows[j])
        if len(group) > 1:
            group_sorted = sorted(group, key=sort_key, reverse=True)
            for row in group_sorted[1:]:
                to_delete.append(row[0])
            for row in group:
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


def extract_events_batch(model, emails_batch, max_retries=3):
    combined = ""
    for i, ed in enumerate(emails_batch):
        combined += f"\n=== EMAIL {i} ===\nSubject: {ed['subject']}\nFrom: {ed['sender']}\nDate: {ed['date']}\n\nBody:\n{ed['body']}\n\n{ed['attachment_text']}\n"

    response = None
    for attempt in range(max_retries):
        try:
            response = model.generate_content(
                [EXTRACTION_SYSTEM_PROMPT, combined],
                generation_config={"response_mime_type": "application/json"},
            )
            break
        except Exception as e:
            if "429" in str(e) or "quota" in str(e).lower():
                wait = 30 * (attempt + 1)
                print(f"WARNING: rate limited, waiting {wait}s before retry...")
                time.sleep(wait)
                continue
            else:
                print(f"WARNING: Gemini call failed: {e}")
                return None
    else:
        print("WARNING: gave up after retries due to rate limiting")
        return None

    if response is None:
        return None

    text = (response.text or "").strip()
    text = text.removeprefix("```json").removeprefix("```").removesuffix("```").strip()

    try:
        return json.loads(text)
    except json.JSONDecodeError:
        print(f"WARNING: could not parse Gemini batch response:\n{text[:500]}")
        return []


def save_events(conn, email_data, events):
    for ev in events:
        conn.execute("""
            INSERT INTO events (source_message_id, title, date, category, year_group, topic,
                                 summary, conflict_note, email_subject, email_sender, email_date, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            email_data["message_id"],
            ev.get("title"),
            ev.get("date"),
            ev.get("category"),
            ev.get("year_group"),
            ev.get("topic"),
            ev.get("summary"),
            ev.get("conflict_note"),
            email_data["subject"],
            email_data["sender"],
            email_data["date"],
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

    force_reprocess_keyword = os.environ.get("FORCE_REPROCESS_KEYWORD", "").strip()
    if force_reprocess_keyword:
        force_reprocess_by_keyword(db_conn, force_reprocess_keyword)

    email_limit = TOTAL_EMAIL_LIMIT
    limit_override = os.environ.get("EMAIL_LIMIT_OVERRIDE", "").strip()
    if limit_override.isdigit():
        email_limit = int(limit_override)
        print(f"DEBUG: per-run email limit overridden to {email_limit} (default is {TOTAL_EMAIL_LIMIT})")

    deleted = db_conn.execute("DELETE FROM events WHERE date IS NOT NULL AND date < '2025-01-01'").rowcount
    db_conn.commit()
    print(f"DEBUG: cleared {deleted} old event(s) from before 2025")

    print("Connecting to iCloud Mail...")
    imap_conn = imap_connect()

    print(f"Fetching emails from: {', '.join(SCHOOL_DOMAINS)}...")
    emails = fetch_new_school_emails(imap_conn, db_conn, total_limit=email_limit)

    if not emails:
        print("No new emails to process.")
    else:
        # Send Gemini calls in smaller chunks rather than one giant batch. A
        # single oversized call is more likely to hit rate limits, and if it
        # fails after retries, EVERYTHING in it is lost -- not just some of
        # it. Chunking means a failure only costs that chunk's emails, which
        # simply stay unprocessed for next run instead of the whole batch.
        chunks = [emails[i:i + GEMINI_CHUNK_SIZE] for i in range(0, len(emails), GEMINI_CHUNK_SIZE)]
        print(f"Sending {len(emails)} email(s) to Gemini in {len(chunks)} batch(es) of up to {GEMINI_CHUNK_SIZE}...")

        for chunk_index, chunk in enumerate(chunks):
            print(f"--- Batch {chunk_index + 1}/{len(chunks)} ({len(chunk)} email(s)) ---")
            all_items = extract_events_batch(model, chunk)

            if all_items is None:
                print("Batch call failed after retries — leaving these emails unprocessed for next run.")
            else:
                by_index = {}
                for item in all_items:
                    idx = item.get("email_index")
                    by_index.setdefault(idx, []).append(item)

                for i, email_data in enumerate(chunk):
                    items = by_index.get(i, [])
                    if items:
                        save_events(db_conn, email_data, items)
                        print(f"  -> '{email_data['subject'][:50]}': extracted {len(items)} item(s)")
                    else:
                        print(f"  -> '{email_data['subject'][:50]}': no actionable items")
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
