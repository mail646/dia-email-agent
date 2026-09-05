#!/usr/bin/env python3
"""
DIA Emirates Hills email ingestion agent.
Connects to iCloud Mail via IMAP, pulls new emails from the school domain,
extracts attachment text, sends each email to Gemini for structured
extraction (deadlines / events / tasks), and stores results in SQLite.
"""

import imaplib
import email
from email.header import decode_header
import sqlite3
import json
import os
import sys
import time
from datetime import datetime, timezone
import io

import google.generativeai as genai

ICLOUD_EMAIL = os.environ.get("ICLOUD_EMAIL", "your_icloud_email@icloud.com")
ICLOUD_APP_PASSWORD = os.environ.get("ICLOUD_APP_PASSWORD", "xxxx-xxxx-xxxx-xxxx")
IMAP_SERVER = "imap.mail.me.com"
IMAP_PORT = 993

SCHOOL_DOMAIN = "diadubai.com"
DB_PATH = os.environ.get("DIA_DB_PATH", os.path.join(os.path.dirname(__file__), "dia_events.db"))

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
MODEL = "gemini-3.6-flash"

YEAR_GROUPS = ["Year 4", "Year 5", "Year 6", "Year 7", "Year 8", "Year 9", "Year 10", "Year 11", "Year 12", "Year 13", "All"]

EXTRACTION_SYSTEM_PROMPT = f"""You extract actionable information from school emails for busy parents.

Given an email's subject, sender, date, and body (plus any attachment text), identify:
- Any deadlines, events, tasks, or announcements with a date attached
- Which year group(s) it applies to (choose from: {", ".join(YEAR_GROUPS)}; use "All" if it applies to the whole school or is unclear)
- A short, clear summary a busy parent can scan in 5 seconds

Respond ONLY with a JSON array (no markdown fences, no preamble). Each item:
{{
  "title": "short title, e.g. 'Year 8 Sports Day'",
  "date": "YYYY-MM-DD or null if no specific date",
  "category": "deadline | event | task | announcement",
  "year_group": "one of the allowed values above",
  "summary": "1-2 sentence summary of what the parent needs to know or do"
}}

If the email contains NO actionable dated information, return an empty array [].
If an email covers multiple distinct items, return multiple objects.
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


def extract_attachment_text(part):
    filename = part.get_filename() or ""
    payload = part.get_payload(decode=True)
    if not payload:
        return ""
    lower = filename.lower()
    try:
        if lower.endswith(".pdf"):
            from pypdf import PdfReader
            reader = PdfReader(io.BytesIO(payload))
            return "\n".join((page.extract_text() or "") for page in reader.pages)
        elif lower.endswith(".docx"):
            import docx
            doc = docx.Document(io.BytesIO(payload))
            return "\n".join(p.text for p in doc.paragraphs)
        elif lower.endswith(".txt"):
            return payload.decode("utf-8", errors="ignore")
    except Exception as e:
        return f"[Could not extract {filename}: {e}]"
    return ""


def get_email_body_and_attachments(msg):
    body = ""
    attachment_text_blocks = []

    if msg.is_multipart():
        for part in msg.walk():
            content_disposition = str(part.get("Content-Disposition") or "")
            content_type = part.get_content_type()

            if "attachment" in content_disposition or part.get_filename():
                text = extract_attachment_text(part)
                if text.strip():
                    attachment_text_blocks.append(f"--- Attachment: {part.get_filename()} ---\n{text}")
            elif content_type == "text/plain" and not body:
                payload = part.get_payload(decode=True)
                if payload:
                    body = payload.decode(part.get_content_charset() or "utf-8", errors="ignore")
    else:
        payload = msg.get_payload(decode=True)
        if payload:
            body = payload.decode(msg.get_content_charset() or "utf-8", errors="ignore")

    return body.strip(), "\n\n".join(attachment_text_blocks)


def fetch_new_school_emails(imap_conn, limit=3):
    emails = []

    status, folder_list = imap_conn.list()
    if status != "OK" or not folder_list:
        print("WARNING: could not list folders")
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

    print(f"DEBUG: found {len(folder_names)} folders: {folder_names}")

    for folder in folder_names:
        try:
            status, _ = imap_conn.select(f'"{folder}"', readonly=True)
        except Exception as e:
            print(f"DEBUG: could not select folder {folder}: {e}")
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

        print(f"DEBUG: folder '{folder}' has {len(message_nums)} matching emails")
        message_nums = message_nums[-limit:]

        for num in message_nums:
            status, msg_data = imap_conn.fetch(num, "(RFC822)")
            if status != "OK":
                continue
            raw_email = msg_data[0][1]
            msg = email.message_from_bytes(raw_email)

            message_id = msg.get("Message-ID", f"no-id-{folder}-{num.decode()}")
            subject = decode_mime_words(msg.get("Subject", ""))
            sender = decode_mime_words(msg.get("From", ""))
            date_str = msg.get("Date", "")

            body, attachment_text = get_email_body_and_attachments(msg)

            emails.append({
                "message_id": message_id,
                "subject": subject,
                "sender": sender,
                "date": date_str,
                "body": body,
                "attachment_text": attachment_text,
            })

    return emails


def extract_events_with_gemini(model, email_data, max_retries=3):
    full_content = f"""Subject: {email_data['subject']}
From: {email_data['sender']}
Date: {email_data['date']}

Body:
{email_data['body']}

{email_data['attachment_text']}
"""

    response = None
    for attempt in range(max_retries):
        try:
            response = model.generate_content(
                [EXTRACTION_SYSTEM_PROMPT, full_content],
                generation_config={"response_mime_type": "application/json"},
            )
            break
        except Exception as e:
            if "429" in str(e) or "quota" in str(e).lower():
                wait = 40 * (attempt + 1)
                print(f"WARNING: rate limited, waiting {wait}s before retry...")
                time.sleep(wait)
                continue
            else:
                print(f"WARNING: Gemini call failed: {e}")
                return []
    else:
        print("WARNING: gave up after retries due to rate limiting")
        return []

    if response is None:
        return []

    text = (response.text or "").strip()
    text = text.removeprefix("```json").removeprefix("```").removesuffix("```").strip()

    try:
        return json.loads(text)
    except json.JSONDecodeError:
        print(f"WARNING: could not parse Gemini response for '{email_data['subject']}':\n{text}")
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
    emails = fetch_new_school_emails(imap_conn)
    print(f"Found {len(emails)} emails from school domain...")

    new_count = 0
    for email_data in emails:
        if already_processed(db_conn, email_data["message_id"]):
            continue

        new_count += 1
        print(f"Processing: {email_data['subject'][:60]}...")

        events = extract_events_with_gemini(model, email_data)
        time.sleep(15)

        if events:
            save_events(db_conn, email_data, events)
            print(f"  -> extracted {len(events)} item(s)")
        else:
            print("  -> no actionable items")

        mark_processed(db_conn, email_data["message_id"])

    print(f"\nDone. {new_count} new email(s) processed.")

    imap_conn.logout()
    db_conn.close()


if __name__ == "__main__":
    main()
