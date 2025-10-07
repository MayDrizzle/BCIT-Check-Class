# app.py
from flask import Flask, request
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

try:
    import pandas as pd
except Exception:
    pd = None

app = Flask(__name__)

TARGET_LEVEL = "Level 04"
URL = "https://www.bcit.ca/apprenticeship/students/training/carpentry-apprentice-harmonized/"
SPECIFIC_INTAKE_KEYWORDS = ["Jan 05", "Feb 20, 2026"]
CHECK_PRE_APRIL = True
DEBUG = True

EMAIL_ALERT = True
RECIPIENTS = os.getenv("RECIPIENTS", "you@example.com").split(",")
SMTP_SERVER = os.getenv("SMTP_SERVER", "smtp.gmail.com")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER = os.getenv("SMTP_USER", "")
SMTP_PASS = os.getenv("SMTP_PASS", "")
STATUS_FILE = "last_success.flag"
FAILURE_FILE = "consecutive_failures.count"

# ---------- helpers ----------
def send_alert(msg, subject_override=None):
    if not SMTP_USER or not SMTP_PASS:
        print("⚠️ SMTP creds missing; skipping email:", msg)
        return
    subject = subject_override or f'{TARGET_LEVEL} Seat Alert! 🎉'
    full_msg = MIMEText(f"BCIT Update:\n\n{msg}\n\nCheck: {URL}")
    full_msg['Subject'] = subject
    full_msg['From'] = SMTP_USER
    full_msg['To'] = ", ".join(RECIPIENTS)
    try:
        server = smtplib.SMTP(SMTP_SERVER, SMTP_PORT, timeout=20)
        server.starttls()
        server.login(SMTP_USER, SMTP_PASS)
        server.sendmail(SMTP_USER, RECIPIENTS, full_msg.as_string())
        server.quit()
    except Exception as e:
        print(f"❌ Failed to send email alert: {e}")

def update_status_file():
    with open(STATUS_FILE, "w") as f:
        f.write(datetime.now().isoformat())

def time_since_last_success():
    try:
        with open(STATUS_FILE, "r") as f:
            last = datetime.fromisoformat(f.read().strip())
            return (datetime.now() - last).total_seconds()
    except:
        return float('inf')

def parse_date(date_str):
    cleaned = re.sub(r'\s+', ' ', date_str.strip().replace('\n', ' '))
    cleaned = re.sub(r',(?!\s)', ', ', cleaned)
    try:
        match = re.match(r'(\w+ \d+) to (\w+ \d+), (\d{4})', cleaned)
        if match:
            return date_parser.parse(f"{match.group(1)} {match.group(3)}")
        return date_parser.parse(cleaned.split(' to ')[0] if ' to ' in cleaned else cleaned)
    except:
        return None

SESSION = requests.Session()
UA_POOL = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 13_5) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/118 Safari/537.36",
]
SUSPICIOUS_MARKERS = ("attention required", "access denied", "just a moment", "verify you are a human")

def fetch_html(url, max_attempts=4):
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
                print(f"DEBUG: fetch_html attempt {attempt}/{max_attempts} failed: {e} -> retrying in {sleep_s:.1f}s")
            time.sleep(sleep_s)
    raise last_err

DATE_RX = re.compile(r"\b(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+\d{2}\b", re.I)

def parse_level_table_from_html(html):
    soup = BeautifulSoup(html, "html.parser")
    # 1) header "Level 04" → next table
    for h in soup.find_all(['h2', 'h3', 'h4']):
        if TARGET_LEVEL.lower() in h.get_text(strip=True).lower():
            t = h.find_next('table')
            if t:
                return t
    # 2) any table that looks date-y
    for t in soup.find_all("table"):
        if DATE_RX.search(t.get_text(" ", strip=True)):
            return t
    # 3) pandas salvage (optional)
    if pd is not None:
        try:
            dfs = pd.read_html(html)
            for df in dfs:
                if df.shape[1] >= 2:
                    contains_dates = bool(
                        df.astype(str)
                          .apply(lambda col: col.str.contains(DATE_RX, na=False).any(), axis=0)
                          .any()
                    )
                    if contains_dates:
                        return BeautifulSoup(df.to_html(index=False), "html.parser").find("table")
        except Exception as e:
            if DEBUG:
                print(f"DEBUG: pandas.read_html failed: {e}")
    return None

def check_class_status():
    try:
        if DEBUG:
            print("DEBUG: Fetching page...")
        html = fetch_html(URL)
        table = parse_level_table_from_html(html)
        if table is None:
            with open("last_error.html", "w", encoding="utf-8") as f:
                f.write(html)
            raise Exception("No tables found on page.")
        # success → reset consecutive failures
        with open(FAILURE_FILE, "w") as f:
            f.write("0")

        rows = table.find_all('tr')
        if DEBUG:
            print(f"DEBUG: Found {len(rows)} rows in table.")

        specific_statuses, pre_april_opens = [], []
        now_dt = datetime.now()

        for row in rows[1:]:
            cells = row.find_all(['td', 'th'])
            if len(cells) < 2:
                continue
            status = cells[0].get_text(strip=True)
            date_str = cells[1].get_text(strip=True)
            norm = re.sub(r',(?!\s)', ', ', re.sub(r'\s+', ' ', date_str.replace('\n', ' ')))
            intake_date = parse_date(norm)
            if intake_date and intake_date > now_dt:
                if any(k in norm for k in SPECIFIC_INTAKE_KEYWORDS):
                    if "FULL" in status.upper():
                        specific_statuses.append("FULL")
                    elif "seats left" in status.lower():
                        specific_statuses.append("OPEN")
                    else:
                        specific_statuses.append("UNKNOWN")
                if CHECK_PRE_APRIL and intake_date.year == 2026 and intake_date.month <= 3:
                    if "seats left" in status.lower():
                        pre_april_opens.append(f"{norm}: {status}")

        if DEBUG:
            print(f"DEBUG: Specific statuses: {specific_statuses}")

        if specific_statuses:
            u = set(specific_statuses)
            specific_status = list(u)[0] if len(u) == 1 else "MIXED"
            if len(specific_statuses) > 1:
                specific_status += f" ({len(specific_statuses)} intakes)"
        else:
            specific_status = "UNKNOWN (no match)"

        pre_msg = "OPEN pre-April: " + ", ".join(pre_april_opens) if pre_april_opens else "No pre-April opens"
        msg = f"🚨 {TARGET_LEVEL} Alert: {specific_status} for Jan intake! {pre_msg}"

        update_status_file()
        if "OPEN" in specific_status or pre_april_opens:
            if EMAIL_ALERT:
                send_alert(msg)

        return f"{TARGET_LEVEL} still {specific_status} for Jan intake. {pre_msg}"

    except Exception as e:
        try:
            n = int(open(FAILURE_FILE).read().strip())
        except:
            n = 0
        n += 1
        with open(FAILURE_FILE, "w") as f:
            f.write(str(n))
        if DEBUG:
            print(f"DEBUG: consecutive failures = {n}")
        err = f"❌ Error in monitor: {e} (consecutive={n})"
        print(err)
        if n >= 3 and EMAIL_ALERT:
            send_alert(err, subject_override="❗ BCIT Monitor Error (persistent)")
        return err

# ---------- Flask routes ----------
@app.route('/health')
def health():
    ua = request.headers.get('User-Agent', '')
    print(f"[{datetime.now()}] /health HIT UA='{ua}'")
    age = time_since_last_success()
    if age > 1800:
        return f"❌ Stale: {age/60:.1f} minutes since last success", 500
    return f"✅ Healthy: Last success {age/60:.1f} minutes ago", 200

@app.route('/test')
def test():
    ua = request.headers.get('User-Agent', '')
    print(f"[{datetime.now()}] /test  HIT UA='{ua}'")
    return "✅ BCIT Monitor test endpoint is alive!", 200

@app.route('/')
def home():
    return "BCIT Monitor is alive!"

# ---------- monitor loop ----------
def monitor_loop():
    while True:
        now_str = time.strftime('%Y-%m-%d %H:%M:%S')
        result = check_class_status()
        print(f"[{now_str}] {result}")
        time.sleep(1800)  # 30 minutes

# Start monitor thread at import time so Gunicorn workers kick it off.
# Limit to a single worker in Gunicorn to avoid duplicate loops.
_monitor_started = False
def _start_monitor_once():
    global _monitor_started
    if not _monitor_started:
        t = threading.Thread(target=monitor_loop, daemon=True)
        t.start()
        _monitor_started = True

_start_monitor_once()

def create_app():
    # Optional factory if you prefer: return app
    return app
