# app.py
from __future__ import annotations

from flask import Flask, request, jsonify
import threading
import requests
from bs4 import BeautifulSoup
import time
from datetime import datetime
import re
import smtplib
from email.mime.text import MIMEText
from dateutil import parser as date_parser
import os
import random
from typing import List, Dict, Any, Optional

# =========================
# Config (env-first)
# =========================
URL = os.getenv(
    "BCIT_URL",
    "https://www.bcit.ca/apprenticeship/students/training/carpentry-apprentice-harmonized/",
)
TARGET_LEVEL = os.getenv("TARGET_LEVEL", "Level 04")
SPECIFIC_INTAKE_KEYWORDS = os.getenv(
    "SPECIFIC_INTAKE_KEYWORDS",
    "Jan 05|Feb 20, 2026",   # pipe-separated list
).split("|")
CHECK_PRE_APRIL_YEAR = int(os.getenv("CHECK_PRE_APRIL_YEAR", "2026"))

# Email
EMAIL_ALERT = os.getenv("EMAIL_ALERT", "true").lower() == "true"
RECIPIENTS = [e.strip() for e in os.getenv(
    "RECIPIENTS",
    "jamesalexdownie@gmail.com,maxwelldownie@gmail.com"
).split(",") if e.strip()]
SMTP_SERVER = os.getenv("SMTP_SERVER", "smtp.gmail.com")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER = os.getenv("SMTP_USER", "")
SMTP_PASS = os.getenv("SMTP_PASS", "")

# Testing / security
TEST_TOKEN = os.getenv("TEST_TOKEN", "")

# Health + state files
STATUS_FILE = os.getenv("STATUS_FILE", "last_success.flag")     # updated on successful scrape
FAILURE_FILE = os.getenv("FAILURE_FILE", "consecutive_failures.count")

# Behavior
DEBUG = os.getenv("DEBUG", "true").lower() == "true"
CHECK_INTERVAL_SEC = int(os.getenv("CHECK_INTERVAL_SEC", "1800"))   # 30 min default
STALE_AFTER_SEC = int(os.getenv("STALE_AFTER_SEC", "1800"))         # health 500 if >30 min since success

# =========================
# Flask
# =========================
app = Flask(__name__)

def _log(msg: str) -> None:
    # Uniform, timestamped logging
    print(f"[{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S.%f')[:-3]}Z] {msg}", flush=True)

@app.route("/")
def root():
    return "BCIT Monitor is alive!"

@app.route("/test")
def test():
    ua = request.headers.get("User-Agent", "")
    _log(f"/test HIT UA='{ua}'")
    return "✅ BCIT Monitor test endpoint is alive!", 200

@app.route("/health")
def health():
    ua = request.headers.get("User-Agent", "")
    age = time_since_last_success()
    _log(f"/health HIT UA='{ua}' age={int(age)}s")
    if age > STALE_AFTER_SEC:
        return f"❌ Stale: {age/60:.1f} minutes since last success", 500
    return f"✅ Healthy: Last success {age/60:.1f} minutes ago", 200

@app.route("/level4")
def level4_listing():
    """
    Returns JSON of all Level 04 classes parsed from the page:
    [
      {
        "status_text": "...",
        "date_text": "...",
        "start_date_iso": "YYYY-MM-DD",
        "end_date_iso": "YYYY-MM-DD",
        "seats_left": 6 | null,
        "is_full": true/false
      }, ...
    ]
    """
    try:
        html = fetch_html(URL)
        records = scrape_level4_rows(html)
        return jsonify({"level": TARGET_LEVEL, "count": len(records), "classes": records}), 200
    except Exception as e:
        _log(f"/level4 ERROR: {e}")
        return jsonify({"error": str(e)}), 500

@app.route("/smtp-test")
def smtp_test():
    if not _is_allowed():
        return "Forbidden", 403
    ok, info = send_plain(
        subject="SMTP Test ✅",
        body=f"Hello from BCIT monitor at {datetime.now().isoformat()}.\nThis verifies SMTP config."
    )
    return (f"OK: {info}", 200) if ok else (f"ERROR: {info}", 500)

@app.route("/scrape-and-email")
def scrape_and_email():
    if not _is_allowed():
        return "Forbidden", 403
    result_text, summary = run_check_once()
    subject = f"BCIT Monitor manual test @ {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
    body = (
        "On-demand scrape result:\n\n"
        f"{result_text}\n\n"
        "Summary:\n"
        f"{summary}\n\n"
        f"Health age (sec): {int(time_since_last_success())}\n"
        f"URL checked: {URL}\n"
    )
    ok, info = send_plain(subject, body)
    return (f"OK: {info}\n\n{result_text}\n", 200) if ok else (f"ERROR: {info}\n\n{result_text}\n", 500)

# =========================
# Email helpers
# =========================
def send_plain(subject: str, body: str, to_list: Optional[List[str]] = None) -> (bool, str):
    to_list = to_list or RECIPIENTS
    msg = MIMEText(body)
    msg["Subject"] = subject
    msg["From"] = SMTP_USER
    msg["To"] = ", ".join(to_list)
    if not SMTP_USER or not SMTP_PASS:
        return False, "SMTP_USER / SMTP_PASS not set"
    try:
        server = smtplib.SMTP(SMTP_SERVER, SMTP_PORT, timeout=20)
        server.starttls()
        server.login(SMTP_USER, SMTP_PASS)
        server.sendmail(SMTP_USER, to_list, msg.as_string())
        server.quit()
        return True, "sent"
    except Exception as e:
        _log(f"❌ SMTP send failed: {e}")
        return False, str(e)

def send_alert(msg: str, subject_override: Optional[str] = None) -> None:
    if not EMAIL_ALERT:
        _log("EMAIL_ALERT=false; skipping send_alert")
        return
    subject = subject_override or f"{TARGET_LEVEL} Seat Alert! 🎉"
    body = f"BCIT Update:\n\n{msg}\n\nCheck: {URL}"
    ok, info = send_plain(subject, body)
    _log(f"send_alert -> {('OK' if ok else 'ERROR')} {info}")

# =========================
# State helpers
# =========================
def update_status_file() -> None:
    with open(STATUS_FILE, "w") as f:
        f.write(datetime.now().isoformat())

def time_since_last_success() -> float:
    try:
        with open(STATUS_FILE, "r") as f:
            last = datetime.fromisoformat(f.read().strip())
            return (datetime.now() - last).total_seconds()
    except Exception:
        return float("inf")

def get_failure_count() -> int:
    try:
        return int(open(FAILURE_FILE).read().strip())
    except Exception:
        return 0

def set_failure_count(n: int) -> None:
    with open(FAILURE_FILE, "w") as f:
        f.write(str(n))

def _is_allowed() -> bool:
    token = request.args.get("token", "")
    return TEST_TOKEN and token == TEST_TOKEN

# =========================
# Fetch & Parse
# =========================
SESSION = requests.Session()
UA_POOL = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 13_5) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/118 Safari/537.36",
]
SUSPICIOUS_MARKERS = (
    "attention required", "access denied", "just a moment", "verify you are a human"
)
DATE_RX = re.compile(
    r"\b(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+\d{1,2}(?:,?\s*\d{4})?\b",
    re.I
)

def fetch_html(url: str, max_attempts: int = 4) -> str:
    last_err: Exception = Exception("Unknown fetch error")
    for attempt in range(1, max_attempts + 1):
        try:
            headers = {
                "User-Agent": random.choice(UA_POOL),
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-CA,en;q=0.9",
                "Cache-Control": "no-cache",
                "Pragma": "no-cache",
                "Connection": "close",
            }
            resp = SESSION.get(url, headers=headers, timeout=15)
            resp.raise_for_status()
            html = resp.text
            lower = html.lower()
            if any(s in lower for s in SUSPICIOUS_MARKERS) or len(html) < 2000:
                raise RuntimeError("Fetched HTML looks incomplete or bot-defended")
            return html
        except Exception as e:
            last_err = e
            sleep_s = 2 * attempt + random.uniform(0, 1.5)
            if DEBUG:
                _log(f"DEBUG: fetch_html attempt {attempt}/{max_attempts} failed: {e} -> retrying in {sleep_s:.1f}s")
            time.sleep(sleep_s)
    raise last_err

def parse_date_span(date_text: str) -> (Optional[datetime], Optional[datetime], Optional[int]):
    """
    Supports formats like:
    'Jan 05 to Feb 20, 2026'  OR  'Nov 03 to Dec 19, 2025'
    Returns (start_date, end_date, year)
    """
    cleaned = re.sub(r'\s+', ' ', date_text.strip().replace('\n', ' '))
    cleaned = re.sub(r',(?!\s)', ', ', cleaned)

    # Common "X to Y, YEAR"
    m = re.match(r'(\w+\s+\d{1,2})\s+to\s+(\w+\s+\d{1,2}),\s*(\d{4})', cleaned)
    if m:
        start = date_parser.parse(f"{m.group(1)} {m.group(3)}")
        end = date_parser.parse(f"{m.group(2)} {m.group(3)}")
        return start, end, int(m.group(3))

    # Single date or other simple forms: take the first date in the string
    try:
        first = cleaned.split(' to ')[0] if ' to ' in cleaned else cleaned
        dt = date_parser.parse(first)
        return dt, None, dt.year
    except Exception:
        return None, None, None

def _extract_seats(status_text: str) -> Optional[int]:
    # Try to find "N seats left" patterns
    m = re.search(r'(\d+)\s+seats?\s+left', status_text, re.I)
    if m:
        return int(m.group(1))
    return None

def _is_full(status_text: str) -> bool:
    return "FULL" in status_text.upper()

def find_level_table(soup: BeautifulSoup) -> Optional[BeautifulSoup]:
    # 1) Normal path: find LEVEL header then next table
    for h in soup.find_all(['h2','h3','h4']):
        if TARGET_LEVEL.lower() in h.get_text(strip=True).lower():
            t = h.find_next('table')
            if t:
                return t
    # 2) Fallback: any table that looks "date-y"
    for t in soup.find_all("table"):
        if DATE_RX.search(t.get_text(" ", strip=True)):
            return t
    return None

def scrape_level4_rows(html: str) -> List[Dict[str, Any]]:
    soup = BeautifulSoup(html, "html.parser")
    table = find_level_table(soup)
    if not table:
        with open("last_error.html", "w", encoding="utf-8") as f:
            f.write(html)
        raise RuntimeError("No Level 04 table found")

    rows = table.find_all("tr")
    records: List[Dict[str, Any]] = []
    for r in rows[1:]:
        cells = r.find_all(["td","th"])
        if len(cells) < 2:
            continue
        status_text = cells[0].get_text(" ", strip=True)
        date_text = cells[1].get_text(" ", strip=True)

        start_dt, end_dt, year = parse_date_span(date_text)
        rec = {
            "status_text": status_text,
            "date_text": re.sub(r',(?!\s)', ', ', re.sub(r'\s+', ' ', date_text.strip())),
            "start_date_iso": start_dt.strftime("%Y-%m-%d") if start_dt else None,
            "end_date_iso": end_dt.strftime("%Y-%m-%d") if end_dt else None,
            "year": year,
            "seats_left": _extract_seats(status_text),
            "is_full": _is_full(status_text),
        }
        records.append(rec)

    return records

# =========================
# Business logic (check)
# =========================
def summarize_result(records: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Return structured summary used by logs/emails."""
    now_dt = datetime.now()
    specific_statuses: List[str] = []
    pre_april_opens: List[str] = []

    for rec in records:
        # future intakes only
        start_iso = rec.get("start_date_iso")
        if not start_iso:
            continue
        try:
            start_dt = datetime.fromisoformat(start_iso)
        except Exception:
            continue
        if start_dt <= now_dt:
            continue

        # Check specific target intakes by keywords
        date_text = rec["date_text"]
        if any(k in date_text for k in SPECIFIC_INTAKE_KEYWORDS):
            if rec["is_full"]:
                specific_statuses.append("FULL")
            elif (rec.get("seats_left") is not None) or ("seats left" in rec["status_text"].lower()):
                specific_statuses.append("OPEN")
            else:
                specific_statuses.append("UNKNOWN")

        # Pre-April opens for the configured year
        if start_dt.year == CHECK_PRE_APRIL_YEAR and start_dt.month <= 3:
            if (rec.get("seats_left") is not None) or ("seats left" in rec["status_text"].lower()):
                pre_april_opens.append(f"{date_text}: {rec['status_text']}")

    # Roll up specific statuses
    if specific_statuses:
        uq = set(specific_statuses)
        specific_status = list(uq)[0] if len(uq) == 1 else "MIXED"
        if len(specific_statuses) > 1:
            specific_status += f" ({len(specific_statuses)} intakes)"
    else:
        specific_status = "UNKNOWN (no match)"

    pre_msg = "OPEN pre-April: " + ", ".join(pre_april_opens) if pre_april_opens else "No pre-April opens"

    return {
        "specific_status": specific_status,
        "pre_april_msg": pre_msg,
        "pre_april_list": pre_april_opens,
        "specific_matches_count": len(specific_statuses),
    }

def run_check_once() -> (str, str):
    """
    Runs one scrape, returns (result_text, summary_for_logs).
    Updates status file on success.
    Handles consecutive failure counter + throttled error alerts.
    """
    try:
        _log("DEBUG: Fetching page...")
        html = fetch_html(URL)
        records = scrape_level4_rows(html)
        summary = summarize_result(records)

        # Update health timestamp (success)
        update_status_file()
        set_failure_count(0)

        # Build human string for logs & email
        result_text = f"{TARGET_LEVEL} summary: {summary['specific_status']} for target intake(s). {summary['pre_april_msg']}"

        # Log the parsed top lines to help you verify accuracy
        recent_lines = []
        for rec in records[:8]:
            recent_lines.append(
                f"- {rec['date_text']}  |  status='{rec['status_text']}'  "
                f"start={rec['start_date_iso']} end={rec['end_date_iso']} seats={rec['seats_left']} full={rec['is_full']}"
            )
        log_block = "\n".join(recent_lines) if recent_lines else "(no rows parsed)"
        full_summary = (
            f"Parsed {len(records)} rows.\n"
            f"Specific: {summary['specific_status']}  |  {summary['pre_april_msg']}\n"
            f"Sample:\n{log_block}"
        )
        _log(f"CHECK RESULT\n{full_summary}")

        # Send seat alert if anything is open
        if "OPEN" in summary["specific_status"] or summary["pre_april_list"]:
            send_alert(f"🚨 {TARGET_LEVEL} Alert: {summary['specific_status']} for target intake(s)! {summary['pre_april_msg']}")

        return result_text, full_summary

    except Exception as e:
        # failure path
        n = get_failure_count() + 1
        set_failure_count(n)
        _log(f"❌ Error in monitor: {e} (consecutive={n})")

        # only email on persistent failure to avoid noise
        if n >= 3:
            send_alert(f"❌ Error in monitor: {e} (consecutive={n})", subject_override="❗ BCIT Monitor Error (persistent)")
        # do NOT update_status_file() on failure (keeps /health meaningful)
        return f"❌ Error: {e}", f"Failure (consecutive={n})"

# =========================
# Background loop
# =========================
def monitor_loop():
    # One-time startup email (optional)
    if not os.path.exists("started.flag"):
        send_alert("✅ BCIT Monitor started. You will be alerted on changes.")
        with open("started.flag", "w") as f:
            f.write("done")

    while True:
        start = time.time()
        result_text, summary = run_check_once()
        _log(f"LOOP COMPLETED in {time.time()-start:.1f}s → {result_text}")
        time.sleep(CHECK_INTERVAL_SEC)

# Start the monitor thread at import time (so a single Gunicorn worker runs it)
_monitor_started = False
def _start_monitor_once():
    global _monitor_started
    if not _monitor_started:
        t = threading.Thread(target=monitor_loop, daemon=True)
        t.start()
        _monitor_started = True
        _log("Monitor thread started.")

_start_monitor_once()

# Export Flask app object for Gunicorn
# gunicorn -w 1 -k gthread --bind 0.0.0.0:$PORT app:app
