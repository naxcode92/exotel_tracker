import os
import sqlite3
import secrets
import logging
from datetime import datetime, timedelta, timezone
from functools import wraps

import requests
from flask import Flask, render_template, jsonify, request, session, redirect, url_for
from werkzeug.middleware.proxy_fix import ProxyFix
from requests.auth import HTTPBasicAuth
from authlib.integrations.flask_client import OAuth
from apscheduler.schedulers.background import BackgroundScheduler
from dotenv import load_dotenv

load_dotenv()

# ---------------------------------------------------------------------------
# Logging — make sure scheduler/background logs are visible in server output
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

app = Flask(__name__)
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)
app.secret_key = os.getenv("SECRET_KEY", secrets.token_hex(32))

# -- Security: cookie hardening --
app.config["SESSION_COOKIE_SECURE"] = True
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"


@app.after_request
def set_security_headers(response):
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    return response

# ---------------------------------------------------------------------------
# Google OAuth config
# ---------------------------------------------------------------------------
GOOGLE_CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID")
GOOGLE_CLIENT_SECRET = os.getenv("GOOGLE_CLIENT_SECRET")
ALLOWED_DOMAIN = os.getenv("ALLOWED_DOMAIN", "futwork.com")

oauth = OAuth(app)
google = oauth.register(
    name="google",
    client_id=GOOGLE_CLIENT_ID,
    client_secret=GOOGLE_CLIENT_SECRET,
    server_metadata_url="https://accounts.google.com/.well-known/openid-configuration",
    client_kwargs={"scope": "openid email profile"},
)

# ---------------------------------------------------------------------------
# Exotel accounts
# ---------------------------------------------------------------------------
ACCOUNTS = {
    "futwork1m": {
        "label": "Futwork 1M",
        "sid": os.getenv("FUTWORK1M_SID", "futwork1m"),
        "api_key": os.getenv("FUTWORK1M_API_KEY"),
        "api_token": os.getenv("FUTWORK1M_API_TOKEN"),
    },
    "futwork12m": {
        "label": "Futwork 12M",
        "sid": os.getenv("FUTWORK12M_SID", "futwork12m"),
        "api_key": os.getenv("FUTWORK12M_API_KEY"),
        "api_token": os.getenv("FUTWORK12M_API_TOKEN"),
    },
}

EXOTEL_BASE_URL = "https://api.in.exotel.com/v1/Accounts"

# ---------------------------------------------------------------------------
# SQLite for stream-count history
# ---------------------------------------------------------------------------
DB_PATH = os.getenv(
    "DB_PATH",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "streams_history.db"),
)
os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
logger.info("Stream log DB path: %s", DB_PATH)


def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        """CREATE TABLE IF NOT EXISTS stream_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT NOT NULL,
            account_key TEXT NOT NULL,
            stream_count INTEGER NOT NULL
        )"""
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_stream_log_ts ON stream_log (ts, account_key)"
    )
    conn.commit()
    return conn


# initialise table on startup
get_db().close()


# ---------------------------------------------------------------------------
# Auth decorator
# ---------------------------------------------------------------------------
def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not GOOGLE_CLIENT_ID:
            # No OAuth configured — allow open access (local dev)
            return f(*args, **kwargs)
        if not session.get("user"):
            if request.path.startswith("/api/"):
                return jsonify({"error": "Unauthorized"}), 401
            return redirect(url_for("login"))
        return f(*args, **kwargs)

    return decorated


# ---------------------------------------------------------------------------
# Exotel API helper
# ---------------------------------------------------------------------------
def fetch_active_streams(account):
    url = f"{EXOTEL_BASE_URL}/{account['sid']}/ActiveStreams.json"
    try:
        resp = requests.get(
            url,
            auth=HTTPBasicAuth(account["api_key"], account["api_token"]),
            headers={"Accept": "application/json"},
            timeout=10,
        )
        resp.raise_for_status()
        return resp.json()
    except ValueError:
        return {"error": "Invalid response from Exotel API"}
    except requests.RequestException as e:
        logger.error("Exotel API error for %s: %s", account["sid"], e)
        return {"error": "Failed to reach Exotel API"}


def count_streams(data):
    """Extract stream count from an Exotel API response dict."""
    if data.get("error"):
        return 0

    # Actual Exotel response: {"Streams": [{"ActiveStreamCount": 3, ...}]}
    streams_list = data.get("Streams")
    if isinstance(streams_list, list) and streams_list:
        asc = streams_list[0].get("ActiveStreamCount")
        if asc is not None:
            try:
                return int(asc)
            except (ValueError, TypeError):
                pass

    # Legacy / alternative response shapes
    for wrapper in [data, data.get("TelephonyResponse", {})]:
        active = wrapper.get("ActiveStreams")
        if active:
            streams = active.get("Stream")
            if streams:
                return len(streams) if isinstance(streams, list) else 1
            asc = active.get("ActiveStreamCount")
            if asc is not None:
                try:
                    return int(asc)
                except (ValueError, TypeError):
                    pass
        asc = wrapper.get("ActiveStreamCount")
        if asc is not None:
            try:
                return int(asc)
            except (ValueError, TypeError):
                pass
    return 0


# ---------------------------------------------------------------------------
# Background job — log stream counts every minute
# ---------------------------------------------------------------------------
def log_stream_counts():
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    try:
        conn = get_db()
        logged = {}
        for key, account in ACCOUNTS.items():
            data = fetch_active_streams(account)
            count = count_streams(data)
            conn.execute(
                "INSERT INTO stream_log (ts, account_key, stream_count) VALUES (?, ?, ?)",
                (now, key, count),
            )
            logged[key] = count
        # Prune entries older than 48 hours
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=48)).strftime(
            "%Y-%m-%d %H:%M:%S"
        )
        conn.execute("DELETE FROM stream_log WHERE ts < ?", (cutoff,))
        conn.commit()
        conn.close()
        logger.info("[Scheduler] Logged stream counts at %s: %s", now, logged)
    except Exception:
        logger.exception("[Scheduler] Failed to log stream counts")


scheduler = BackgroundScheduler(daemon=True)
scheduler.add_job(log_stream_counts, "interval", minutes=1, misfire_grace_time=30)
scheduler.start()
logger.info("Background scheduler started — logging stream counts every 1 minute")

# Log once at startup so the graph isn't empty
log_stream_counts()


# ---------------------------------------------------------------------------
# Routes — Auth
# ---------------------------------------------------------------------------
@app.route("/login")
def login():
    if not GOOGLE_CLIENT_ID:
        return redirect(url_for("index"))
    redirect_uri = url_for("auth_callback", _external=True)
    return google.authorize_redirect(redirect_uri)


@app.route("/auth/callback")
def auth_callback():
    token = google.authorize_access_token()
    user_info = token.get("userinfo") or google.userinfo()
    email = user_info.get("email", "")

    if not email.endswith(f"@{ALLOWED_DOMAIN}"):
        return render_template("login_error.html", email=email, domain=ALLOWED_DOMAIN), 403

    session["user"] = {
        "email": email,
        "name": user_info.get("name", email),
        "picture": user_info.get("picture", ""),
    }
    return redirect(url_for("index"))


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


# ---------------------------------------------------------------------------
# Routes — Dashboard
# ---------------------------------------------------------------------------
@app.route("/")
@login_required
def index():
    user = session.get("user", {})
    return render_template("index.html", user=user)


# ---------------------------------------------------------------------------
# Routes — API
# ---------------------------------------------------------------------------
@app.route("/api/streams")
@login_required
def streams():
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    results = {}
    counts = {}
    for key, account in ACCOUNTS.items():
        data = fetch_active_streams(account)
        count = count_streams(data)
        results[key] = {
            "label": account["label"],
            "data": data,
            "count": count,
        }
        counts[key] = count

    # Log counts to DB on every fetch so history builds up in real time
    try:
        conn = get_db()
        for key, count in counts.items():
            conn.execute(
                "INSERT INTO stream_log (ts, account_key, stream_count) VALUES (?, ?, ?)",
                (now, key, count),
            )
        conn.commit()
        conn.close()
    except Exception:
        logger.exception("Failed to log stream counts from /api/streams")

    return jsonify(results)


@app.route("/api/streams/<account_key>")
@login_required
def streams_for_account(account_key):
    account = ACCOUNTS.get(account_key)
    if not account:
        return jsonify({"error": "Unknown account"}), 404
    data = fetch_active_streams(account)
    return jsonify({"label": account["label"], "data": data})


@app.route("/api/history")
@login_required
def history():
    try:
        hours = min(int(request.args.get("hours", 24)), 48)
    except (ValueError, TypeError):
        hours = 24
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).strftime(
        "%Y-%m-%d %H:%M:%S"
    )
    conn = get_db()
    rows = conn.execute(
        "SELECT ts, account_key, stream_count FROM stream_log WHERE ts >= ? ORDER BY ts",
        (cutoff,),
    ).fetchall()
    conn.close()

    # Group by account
    result = {}
    for ts, account_key, count in rows:
        result.setdefault(account_key, []).append({"ts": ts, "count": count})
    return jsonify(result)


@app.route("/api/peaks")
@login_required
def peaks():
    """Return the highest stream count per account in the last 24 hours."""
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=24)).strftime(
        "%Y-%m-%d %H:%M:%S"
    )
    conn = get_db()
    rows = conn.execute(
        "SELECT account_key, MAX(stream_count) FROM stream_log WHERE ts >= ? GROUP BY account_key",
        (cutoff,),
    ).fetchall()
    conn.close()
    return jsonify({key: peak for key, peak in rows})


@app.route("/api/log")
@login_required
def stream_log():
    """Return recent stream_log rows for the table view."""
    try:
        limit = min(int(request.args.get("limit", 100)), 500)
    except (ValueError, TypeError):
        limit = 100
    conn = get_db()
    rows = conn.execute(
        "SELECT ts, account_key, stream_count FROM stream_log ORDER BY ts DESC LIMIT ?",
        (limit,),
    ).fetchall()
    conn.close()
    return jsonify([{"ts": ts, "account": acc, "count": cnt} for ts, acc, cnt in rows])


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    port = int(os.getenv("PORT", 5000))
    app.run(debug=os.getenv("FLASK_DEBUG", "false").lower() == "true", host="0.0.0.0", port=port)
