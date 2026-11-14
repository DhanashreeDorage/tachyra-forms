"""Tachyra Forms — receives website form submissions, stores them, and emails info@.

A small WSGI app (run with gunicorn on Render). Standard library only, plus
psycopg2 for Postgres. With no DATABASE_URL set it uses a local SQLite file,
which is how it is tested.

Environment variables (set in Render, never in code):
  DATABASE_URL     Postgres connection string (Render "Internal Database URL")
  ALLOWED_ORIGINS  Comma-separated, e.g. https://tachyradiagnostics.com,https://www.tachyradiagnostics.com
  ADMIN_TOKEN      Long random string; required to download entries as CSV
  SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASS   Outgoing mail for notifications
  NOTIFY_TO        Where notifications go (default info@tachyradiagnostics.com)
  NOTIFY_FROM      Sender address (default SMTP_USER)
"""
import csv
import hashlib
import hmac
import io
import json
import logging
import os
import re
import smtplib
import sqlite3
import ssl
import threading
import time
from collections import defaultdict, deque
from email.message import EmailMessage

log = logging.getLogger("tachyra-forms")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

# ---------------------------------------------------------------- form definitions
# Each field: (name, label, kind, required, extra)
#   kind: text | email | phone | choice | bool | longtext | npi | zip
#   extra: max length for text kinds, list of options for choice
US_STATES = ["AL","AK","AZ","AR","CA","CO","CT","DE","DC","FL","GA","HI","ID","IL","IN","IA","KS","KY","LA","ME","MD","MA","MI","MN","MS","MO","MT","NE","NV","NH","NJ","NM","NY","NC","ND","OH","OK","OR","PA","RI","SC","SD","TN","TX","UT","VT","VA","WA","WV","WI","WY","PR"]

FORMS = {
    "pilot": ("Pilot application", [
        ("full_name", "Full name", "text", True, 120),
        ("email", "Email", "email", True, 200),
        ("phone", "Phone", "phone", False, 40),
        ("credential", "Credential", "choice", True, ["MD", "DO", "Other"]),
        ("specialty", "Specialty", "text", True, 120),
        ("practice", "Practice name", "text", True, 200),
        ("city_state", "City and state", "text", True, 120),
        ("active_patients", "Approx. active patients", "choice", False, ["Up to 300", "301-750", "751-1,500", "Over 1,500"]),
        ("ehr", "EHR used", "text", False, 120),
        ("notes", "Anything else", "longtext", False, 2000),
        ("ack_no_phi", "Confirmed no patient information in form", "bool", True, None),
    ]),
    "founding": ("Founding Physician application", [
        ("first_name", "First name", "text", True, 80),
        ("last_name", "Last name", "text", True, 80),
        ("email", "Email", "email", True, 200),
        ("phone", "Phone", "phone", False, 40),
        ("credential", "Credential", "choice", True, ["MD", "DO"]),
        ("specialty", "Specialty", "text", True, 120),
        ("license_state", "State of licensure", "choice", True, US_STATES),
        ("license_number", "License number", "text", True, 40),
        ("practice", "Practice or institution", "text", False, 200),
        ("heard_about", "How they heard about us", "text", False, 200),
        ("ack_test_data", "Agreed to use test data only", "bool", True, None),
        ("consent_recognition", "Consents to Founding Physician recognition", "bool", False, None),
    ]),
    "directory": ("Physician Directory listing request", [
        ("first_name", "First name", "text", True, 80),
        ("last_name", "Last name", "text", True, 80),
        ("credential", "Credential", "choice", True, ["MD", "DO"]),
        ("npi", "NPI number", "npi", True, None),
        ("specialty", "Specialty", "text", True, 120),
        ("email", "Email (private)", "email", True, 200),
        ("practice", "Practice name", "text", True, 200),
        ("street", "Practice street address", "text", True, 200),
        ("city", "City", "text", True, 80),
        ("state", "State", "choice", True, US_STATES),
        ("zip", "ZIP code", "zip", True, None),
        ("practice_phone", "Practice phone (public)", "phone", True, 40),
        ("website", "Practice website", "text", False, 200),
        ("accepting_new", "Accepting new patients", "choice", False, ["Yes", "No", "Not sure"]),
        ("consent_listing", "Consents to public listing", "bool", True, None),
    ]),
    "directory_notify": ("Directory opening notification", [
        ("email", "Email", "email", True, 200),
        ("zip", "ZIP code", "zip", False, None),
    ]),
    "investor": ("Seed round waiting list", [
        ("first_name", "First name", "text", True, 80),
        ("last_name", "Last name", "text", True, 80),
        ("email", "Email", "email", True, 200),
        ("phone", "Phone", "phone", False, 40),
        ("state", "State of residence", "choice", True, US_STATES + ["Outside the U.S."]),
        ("accredited", "Accredited investor", "choice", True, ["Yes", "No", "Not sure"]),
        ("amount", "Amount considering", "choice", False, ["$25,000-$49,999", "$50,000-$99,999", "$100,000-$249,999", "$250,000 or more", "Prefer not to say"]),
        ("heard_about", "How they heard about us", "text", False, 200),
        ("ack_not_offer", "Acknowledged this is not an offer", "bool", True, None),
    ]),
}

EMAIL_RE = re.compile(r"^[^@\s]{1,64}@[^@\s]{1,190}\.[A-Za-z]{2,24}$")
PHONE_RE = re.compile(r"^[0-9+()\-.\s]{7,40}$")
MIN_FILL_MS = 3000          # bots submit instantly
RATE_LIMIT = (8, 600)       # 8 submissions per 10 minutes per IP
MAX_BODY = 20_000


class Invalid(Exception):
    pass


def clean(form_name, payload):
    """Validate and normalize one submission. Unknown fields are dropped."""
    _, fields = FORMS[form_name]
    out = {}
    for name, label, kind, required, extra in fields:
        raw = payload.get(name)
        if kind == "bool":
            val = raw is True or str(raw).lower() in ("true", "on", "yes", "1")
            if required and not val:
                raise Invalid(f"Please confirm: {label}.")
            out[name] = val
            continue
        val = "" if raw is None else str(raw).strip()
        val = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", val)
        if not val:
            if required:
                raise Invalid(f"{label} is required.")
            out[name] = ""
            continue
        if kind in ("text", "longtext", "phone", "email") and extra and len(val) > extra:
            raise Invalid(f"{label} is too long.")
        if kind == "email" and not EMAIL_RE.match(val):
            raise Invalid("Please enter a valid email address.")
        if kind == "phone" and not PHONE_RE.match(val):
            raise Invalid("Please enter a valid phone number.")
        if kind == "npi" and not re.fullmatch(r"\d{10}", val):
            raise Invalid("NPI number must be 10 digits.")
        if kind == "zip" and not re.fullmatch(r"\d{5}(-\d{4})?", val):
            raise Invalid("Please enter a valid ZIP code.")
        if kind == "choice" and val not in extra:
            raise Invalid(f"Please choose a valid option for {label}.")
        out[name] = val
    return out


# ---------------------------------------------------------------- storage
_db_lock = threading.Lock()


def _connect():
    url = os.environ.get("DATABASE_URL")
    if url:
        import psycopg2  # installed on Render via requirements.txt
        return psycopg2.connect(url), "pg"
    return sqlite3.connect(os.environ.get("SQLITE_PATH", "forms_local.db")), "sqlite"


def init_db():
    conn, kind = _connect()
    with conn:
        cur = conn.cursor()
        if kind == "pg":
            cur.execute("""CREATE TABLE IF NOT EXISTS form_submissions (
                id SERIAL PRIMARY KEY, form TEXT NOT NULL,
                created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                data JSONB NOT NULL, ip_hash TEXT)""")
        else:
            cur.execute("""CREATE TABLE IF NOT EXISTS form_submissions (
                id INTEGER PRIMARY KEY AUTOINCREMENT, form TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                data TEXT NOT NULL, ip_hash TEXT)""")
    conn.close()


def save(form_name, data, ip_hash):
    conn, kind = _connect()
    try:
        with conn:
            cur = conn.cursor()
            if kind == "pg":
                cur.execute("INSERT INTO form_submissions (form, data, ip_hash) VALUES (%s, %s, %s) RETURNING id",
                            (form_name, json.dumps(data), ip_hash))
                return cur.fetchone()[0]
            cur.execute("INSERT INTO form_submissions (form, data, ip_hash) VALUES (?, ?, ?)",
                        (form_name, json.dumps(data), ip_hash))
            return cur.lastrowid
    finally:
        conn.close()


def export_rows(form_name):
    conn, kind = _connect()
    try:
        cur = conn.cursor()
        q = "SELECT id, created_at, data FROM form_submissions WHERE form = {} ORDER BY id"
        cur.execute(q.format("%s" if kind == "pg" else "?"), (form_name,))
        rows = cur.fetchall()
    finally:
        conn.close()
    out = []
    for rid, created, data in rows:
        d = data if isinstance(data, dict) else json.loads(data)
        out.append((rid, str(created), d))
    return out


# ---------------------------------------------------------------- email
def notify(form_name, rid, data):
    host = os.environ.get("SMTP_HOST")
    if not host:
        log.info("SMTP not configured; skipped notification for %s #%s", form_name, rid)
        return
    title, fields = FORMS[form_name]
    msg = EmailMessage()
    msg["Subject"] = f"New {title} (#{rid})"
    msg["From"] = os.environ.get("NOTIFY_FROM") or os.environ.get("SMTP_USER")
    msg["To"] = os.environ.get("NOTIFY_TO", "info@tachyradiagnostics.com")
    if data.get("email"):
        msg["Reply-To"] = data["email"]
    lines = [f"{title} received through tachyradiagnostics.com", ""]
    for name, label, kind, _, _ in fields:
        v = data.get(name, "")
        if kind == "bool":
            v = "Yes" if v else "No"
        lines.append(f"{label}: {v}")
    msg.set_content("\n".join(lines))
    try:
        port = int(os.environ.get("SMTP_PORT", "587"))
        ctx = ssl.create_default_context()
        if port == 465:
            with smtplib.SMTP_SSL(host, port, context=ctx, timeout=20) as s:
                s.login(os.environ["SMTP_USER"], os.environ["SMTP_PASS"])
                s.send_message(msg)
        else:
            with smtplib.SMTP(host, port, timeout=20) as s:
                s.starttls(context=ctx)
                s.login(os.environ["SMTP_USER"], os.environ["SMTP_PASS"])
                s.send_message(msg)
        log.info("Notification sent for %s #%s", form_name, rid)
    except Exception as e:  # the submission is already saved; never lose it over email
        log.error("Notification failed for %s #%s: %s", form_name, rid, type(e).__name__)


# ---------------------------------------------------------------- rate limiting
_hits = defaultdict(deque)
_hits_lock = threading.Lock()


def rate_limited(ip_hash):
    limit, window = RATE_LIMIT
    now = time.time()
    with _hits_lock:
        q = _hits[ip_hash]
        while q and now - q[0] > window:
            q.popleft()
        if len(q) >= limit:
            return True
        q.append(now)
        return False


# ---------------------------------------------------------------- WSGI app
def _origins():
    return [o.strip() for o in os.environ.get("ALLOWED_ORIGINS", "").split(",") if o.strip()]


def _respond(start, status, body, origin=None, ctype="application/json"):
    headers = [("Content-Type", ctype), ("Cache-Control", "no-store"), ("X-Content-Type-Options", "nosniff")]
    if origin and origin in _origins():
        headers += [("Access-Control-Allow-Origin", origin), ("Vary", "Origin"),
                    ("Access-Control-Allow-Methods", "POST, OPTIONS"),
                    ("Access-Control-Allow-Headers", "Content-Type"), ("Access-Control-Max-Age", "600")]
    data = body if isinstance(body, bytes) else json.dumps(body).encode()
    start(status, headers + [("Content-Length", str(len(data)))])
    return [data]


def _ip_hash(environ):
    ip = (environ.get("HTTP_X_FORWARDED_FOR", "").split(",")[0].strip() or environ.get("REMOTE_ADDR", ""))
    salt = os.environ.get("ADMIN_TOKEN", "local")
    return hashlib.sha256((salt + ip).encode()).hexdigest()[:16]


def application(environ, start):
    method = environ.get("REQUEST_METHOD", "GET")
    path = environ.get("PATH_INFO", "/")
    origin = environ.get("HTTP_ORIGIN")

    if path == "/healthz":
        return _respond(start, "200 OK", {"ok": True})

    m = re.fullmatch(r"/submit/([a-z_]+)", path)
    if m:
        form_name = m.group(1)
        if method == "OPTIONS":
            return _respond(start, "204 No Content", b"", origin)
        if method != "POST":
            return _respond(start, "405 Method Not Allowed", {"ok": False}, origin)
        if form_name not in FORMS:
            return _respond(start, "404 Not Found", {"ok": False, "error": "Unknown form."}, origin)
        if _origins() and origin not in _origins():
            return _respond(start, "403 Forbidden", {"ok": False, "error": "Not allowed."}, origin)
        try:
            length = int(environ.get("CONTENT_LENGTH") or 0)
        except ValueError:
            length = 0
        if length <= 0 or length > MAX_BODY:
            return _respond(start, "413 Payload Too Large", {"ok": False, "error": "Submission too large."}, origin)
        try:
            payload = json.loads(environ["wsgi.input"].read(length))
            assert isinstance(payload, dict)
        except Exception:
            return _respond(start, "400 Bad Request", {"ok": False, "error": "Invalid submission."}, origin)
        iph = _ip_hash(environ)
        # Spam checks: hidden honeypot field must be empty, and humans take a few seconds.
        if payload.get("company_website") or int(payload.get("elapsed_ms") or 0) < MIN_FILL_MS:
            log.info("Discarded likely spam for %s", form_name)
            return _respond(start, "200 OK", {"ok": True}, origin)  # don't tell bots
        if rate_limited(iph):
            return _respond(start, "429 Too Many Requests", {"ok": False, "error": "Too many submissions. Please try again later."}, origin)
        try:
            data = clean(form_name, payload)
        except Invalid as e:
            return _respond(start, "422 Unprocessable Entity", {"ok": False, "error": str(e)}, origin)
        with _db_lock:
            rid = save(form_name, data, iph)
        log.info("Saved %s #%s", form_name, rid)
        threading.Thread(target=notify, args=(form_name, rid, data), daemon=True).start()
        return _respond(start, "200 OK", {"ok": True}, origin)

    m = re.fullmatch(r"/export/([a-z_]+)\.csv", path)
    if m and method == "GET":
        token = os.environ.get("ADMIN_TOKEN", "")
        given = environ.get("HTTP_X_ADMIN_TOKEN", "")
        if not token or not hmac.compare_digest(token, given):
            return _respond(start, "401 Unauthorized", {"ok": False})
        form_name = m.group(1)
        if form_name not in FORMS:
            return _respond(start, "404 Not Found", {"ok": False})
        _, fields = FORMS[form_name]
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["id", "submitted_at"] + [f[1] for f in fields])
        for rid, created, d in export_rows(form_name):
            w.writerow([rid, created] + [d.get(f[0], "") for f in fields])
        return _respond(start, "200 OK", buf.getvalue().encode(), ctype="text/csv; charset=utf-8")

    return _respond(start, "404 Not Found", {"ok": False})


init_db()
app = application  # gunicorn entry point: app:app

if __name__ == "__main__":
    from wsgiref.simple_server import make_server
    port = int(os.environ.get("PORT", "8000"))
    print(f"Tachyra Forms running locally on http://127.0.0.1:{port}")
    make_server("127.0.0.1", port, application).serve_forever()
