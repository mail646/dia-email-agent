#!/usr/bin/env python3
"""
DIA Emirates Hills email ingestion agent.
Connects to iCloud Mail via IMAP, pulls new emails from the school domain,
extracts text from PDFs/DOCX/images (via OCR) and HTML bodies,
batches everything into a single Gemini call for structured extraction,
and stores results in SQLite.
"""

import imaplib
import email
from email.header import decode_header
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

SCHOOL_DOMAIN = "diadubai.com"
DB_PATH = os.environ.get("DIA_DB_PATH", os.path.join(os.path.dirname(__file__), "dia_events.db"))

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
MODEL = "gemini-3.6-flash"

TOTAL_EMAIL_LIMIT = 10  # cap per run, across all folders combined
SKIP_FOLDER_KEYWORDS = ["trash", "junk", "deleted", "sent", "draft", "notes"]
IMAGE_EXTENSIONS = (".jpg", ".jpeg", ".png", ".gif", ".bmp", ".tiff", ".webp")

YEAR_GROUPS = ["Year 4", "Year 5", "Year 6", "Year 7", "Year 8", "Year 9", "Year 10", "Year 11", "Year 12", "Year 13", "All"]

EXTRACTION_SYSTEM_PROMPT = f"""You extract actionable information from school emails for busy parents.

You will receive several emails, each marked with a line like "=== EMAIL 0 ===", "=== EMAIL 1 ===", etc.
Some emails include text extracted from PDF/Word attachments or OCR'd from images (photos of flyers, posters,
forms) — treat this extracted text with the same importance as the email body itself, even if it has OCR noise/typos.

For each email, identify:
- Any deadlines, events, tasks, or announcements with a date attached
- Which year group(s) it applies to (choose from: {", ".join(YEAR_GROUPS)}; use "All" if it applies to the whole school or is unclear)
- A short, clear summary a busy parent can scan in 5 seconds

Respond ONLY with a single JSON array (no markdown fences, no preamble) combining items from ALL emails. Each item:
{{
  "email_index": 0,
  "title": "short title, e.g. 'Year 8 Sports Day'",
  "date": "YYYY-MM-DD or null if no specific date",
  "category": "deadline | event | task | announcement",
  "year_group": "one of the allowed values above",
  "summary": "1-2 sentence summary of what the parent needs to know or do"
}}

The "email_index" field MUST match the number in that email's "=== EMAIL N ===" marker.
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
            summary TEXT,
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
    conn.commit()
    return conn


def already_processed(conn, message_id):
    cur = conn.execute("SELECT 1 FROM processed_messages WHERE message_id = ?", (message_id,))
    return cur.fetchone() is not None


def mark_processed(conn, message_id):
    conn.execute(
        "INSERT OR IGNORE INTO processed_messages (message_id, processed_at) VALUES (?, ?)",
        (message_id, datetime.now(timezone.utc).isoformat()),
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


def html_to_text(raw_html):
    """Minimal HTML-to-text fallback: strip tags, unescape entities, collapse whitespace."""
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


def extract_attachment_text(part):
    filename = part.get_filename() or ""
    payload = part.get_payload(decode=True)
    if not payload:
        return ""
    lower = filename.lower()
    content_type = part.get_content_type()
    try:
        if lower.endswith(".pdf") or content_type == "application/pdf":
            from pypdf import PdfReader
            reader = PdfReader(io.BytesIO(payload))
            text_parts = []
            for page in reader.pages:
                page_text = page.extract_text() or ""
                text_parts.append(page_text)
                # PDFs can also contain scanned images with no extractable text layer
                if not page_text.strip():
                    for img_obj in getattr(page, "images", []):
                        ocr_text = ocr_image_bytes(img_obj.data, filename)
                        if ocr_text and not ocr_text.startswith("[Could not"):
                            text_parts.append(f"[OCR from page image]\n{ocr_text}")
            return "\n".join(text_parts)
        elif lower.endswith(".docx") or "wordprocessingml" in content_type:
            import docx
            doc = docx.Document(io.BytesIO(payload))
            return "\n".join(p.text for p in doc.paragraphs)
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
    emails = []

    all_folders = get_folder_names(imap_conn)
    folder_names = [
        f for f in all_folders
        if not any(kw in f.lower() for kw in SKIP_FOLDER_KEYWORDS)
    ]
    print(f"DEBUG: searching {len(folder_names)} folders (skipped {len(all_folders) - len(folder_names)} junk/trash/sent/etc.)")

    for folder in folder_names:
        if len(emails) >= total_limit:
            break

        try:
            status, _ = imap_conn.select(f'"{folder}"', readonly=True)
        except Exception:
            continue
        if status != "OK":
            continue

        search_query = f'(FROM "{SCHOOL_DOMAIN}")'
        status, data = imap_conn.search(None, search_query)
        if status != "OK" or not data or data[0] is None:
            continue

        message_nums = data[0].split()
        if not message_nums:
            continue

        message_nums = list(reversed(message_nums))  # most recent first

        for num in message_nums:
            if len(emails) >= total_limit:
                break

            status, msg_data = imap_conn.fetch(num, "(RFC822)")
            if status != "OK":
                continue
            raw_email = msg_data[0][1]
            msg = email.message_from_bytes(raw_email)

            message_id = msg.get("Message-ID", f"no-id-{folder}-{num.decode()}")

            if already_processed(db_conn, message_id):
                continue

            subject = decode_mime_words(msg.get("Subject", ""))
            sender = decode_mime_words(msg.get("From", ""))
            date_str = msg.get("Date", "")

            print(f"DEBUG: reading '{subject[:60]}' (extracting attachments/images/HTML)...")
            body, attachment_text = get_email_body_and_attachments(msg)

            emails.append({
                "message_id": message_id,
                "subject": subject,
                "sender": sender,
                "date": date_str,
                "body": body,
                "attachment_text": attachment_text,
            })

    print(f"DEBUG: collected {len(emails)} new unprocessed emails to send to Gemini")
    return emails


def extract_events_batch(model, emails_batch, max_retries=3):
    """Send multiple emails in ONE Gemini call to conserve free-tier quota."""
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
            INSERT INTO events (source_message_id, title, date, category, year_group,
                                 summary, email_subject, email_sender, email_date, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            email_data["message_id"],
            ev.get("title"),
            ev.get("date"),
            ev.get("category"),
            ev.get("year_group"),
            ev.get("summary"),
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

    print("Connecting to iCloud Mail...")
    imap_conn = imap_connect()

    print(f"Fetching emails from {SCHOOL_DOMAIN}...")
    emails = fetch_new_school_emails(imap_conn, db_conn)

    if not emails:
        print("No new emails to process.")
    else:
        print(f"Sending {len(emails)} email(s) to Gemini in a single batch call...")
        all_items = extract_events_batch(model, emails)

        if all_items is None:
            print("Batch call failed after retries — leaving these emails unprocessed for next run.")
        else:
            by_index = {}
            for item in all_items:
                idx = item.get("email_index")
                by_index.setdefault(idx, []).append(item)

            for i, email_data in enumerate(emails):
                items = by_index.get(i, [])
                if items:
                    save_events(db_conn, email_data, items)
                    print(f"  -> '{email_data['subject'][:50]}': extracted {len(items)} item(s)")
                else:
                    print(f"  -> '{email_data['subject'][:50]}': no actionable items")
                mark_processed(db_conn, email_data["message_id"])

    print("\nDone.")

    imap_conn.logout()
    db_conn.close()


if __name__ == "__main__":
    main()
