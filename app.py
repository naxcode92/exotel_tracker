import os
import secrets
import requests
from flask import Flask, render_template, jsonify, request, session, redirect, url_for
from requests.auth import HTTPBasicAuth
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY", secrets.token_hex(32))

DASHBOARD_PASSWORD = os.getenv("DASHBOARD_PASSWORD")


def login_required(f):
    from functools import wraps

    @wraps(f)
    def decorated(*args, **kwargs):
        if not DASHBOARD_PASSWORD:
            return f(*args, **kwargs)
        if not session.get("authenticated"):
            if request.path.startswith("/api/"):
                return jsonify({"error": "Unauthorized"}), 401
            return redirect(url_for("login"))
        return f(*args, **kwargs)

    return decorated

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
        return {"error": str(e)}


@app.route("/login", methods=["GET", "POST"])
def login():
    if not DASHBOARD_PASSWORD:
        return redirect(url_for("index"))
    error = None
    if request.method == "POST":
        if secrets.compare_digest(request.form.get("password", ""), DASHBOARD_PASSWORD):
            session["authenticated"] = True
            return redirect(url_for("index"))
        error = "Incorrect password"
    return render_template("login.html", error=error)


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/")
@login_required
def index():
    return render_template("index.html")


@app.route("/api/streams")
@login_required
def streams():
    results = {}
    for key, account in ACCOUNTS.items():
        data = fetch_active_streams(account)
        results[key] = {
            "label": account["label"],
            "data": data,
        }
    return jsonify(results)


@app.route("/api/streams/<account_key>")
@login_required
def streams_for_account(account_key):
    account = ACCOUNTS.get(account_key)
    if not account:
        return jsonify({"error": "Unknown account"}), 404
    data = fetch_active_streams(account)
    return jsonify({"label": account["label"], "data": data})


if __name__ == "__main__":
    port = int(os.getenv("PORT", 5000))
    app.run(debug=True, host="0.0.0.0", port=port)
