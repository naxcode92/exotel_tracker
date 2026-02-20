import os
import requests
from flask import Flask, render_template, jsonify
from requests.auth import HTTPBasicAuth
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__)

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
    url = f"{EXOTEL_BASE_URL}/{account['sid']}/ActiveStreams"
    try:
        resp = requests.get(
            url,
            auth=HTTPBasicAuth(account["api_key"], account["api_token"]),
            timeout=10,
        )
        resp.raise_for_status()
        return resp.json()
    except requests.RequestException as e:
        return {"error": str(e)}


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/streams")
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
def streams_for_account(account_key):
    account = ACCOUNTS.get(account_key)
    if not account:
        return jsonify({"error": "Unknown account"}), 404
    data = fetch_active_streams(account)
    return jsonify({"label": account["label"], "data": data})


if __name__ == "__main__":
    app.run(debug=True, host="0.0.0.0", port=5000)
