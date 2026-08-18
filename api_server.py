"""
api_server.py — Ledger's read-only HTTP API

Serves the current paper-trading state, journal, and theses over
plain HTTP/JSON, for the static dashboard in /site (or anything else
that wants to read Ledger's state without touching the JSON files
directly). Runs as a Flask app in a background thread inside the SAME
process as ledger_bot.py's main trading loop — no separate Railway
service needed, per the deliberate choice to keep this a single dyno.

This module is intentionally self-contained (doesn't import from
ledger_bot.py) to avoid a circular import, since ledger_bot.py is what
starts this server. That means get_token_prices_usd() below is a
small, deliberate duplicate of the one in ledger_bot.py — same
Jupiter Price API V3 call, trimmed down since the API only ever needs
a handful of mints per request (the open positions), not the batching
ledger_bot.py needs for dozens of tracked wallets' tokens.

Read-only by design: every route only reads from disk
(ledger_state.json, journal.jsonl, theses.json) — nothing here writes
state or can influence trading decisions.

Env vars:
  API_PORT - port to listen on (default 8080)
"""

import os
import json
import threading
import requests
from pathlib import Path
from flask import Flask, jsonify, request

from journal_store import get_recent_journal
from theses_store import get_theses

API_PORT = int(os.environ.get("API_PORT", "8080"))

STATE_FILE = Path("ledger_state.json")
JUPITER_PRICE_API = "https://lite-api.jup.ag/price/v3"

app = Flask(__name__)


@app.after_request
def add_cors_headers(response):
    # This is read-only, non-sensitive paper-trading data — open CORS
    # so the static site (served from a different origin/port) can
    # fetch it directly without needing a proxy.
    response.headers["Access-Control-Allow-Origin"] = "*"
    return response


def get_token_prices_usd(mints: list) -> dict:
    """Trimmed duplicate of ledger_bot.py's price lookup — see module docstring for why this isn't imported."""
    if not mints:
        return {}
    try:
        resp = requests.get(JUPITER_PRICE_API, params={"ids": ",".join(mints)}, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        return {
            mint: float(info["usdPrice"])
            for mint, info in data.items()
            if info and "usdPrice" in info
        }
    except Exception as e:
        print(f"[WARN] api_server price lookup failed: {e}")
        return {}


def load_state() -> dict:
    if not STATE_FILE.exists():
        return {}
    try:
        return json.loads(STATE_FILE.read_text())
    except json.JSONDecodeError:
        return {}


@app.route("/api/state")
def api_state():
    state = load_state()
    open_positions_raw = state.get("open_positions", {})

    mints = list(open_positions_raw.keys())
    current_prices = get_token_prices_usd(mints)

    positions = []
    for mint, pos in open_positions_raw.items():
        entry_price = pos.get("entry_price")
        size_sol = pos.get("size_sol")
        current_price = current_prices.get(mint)

        pnl_current = None
        if current_price is not None and entry_price:
            pnl_current = (current_price - entry_price) / entry_price * size_sol

        positions.append({
            "ticker": pos.get("symbol") or mint[:8],
            "mint": mint,
            "size_sol": size_sol,
            "avg_entry": entry_price,
            "pnl_current_sol": pnl_current,
            "thesis": pos.get("thesis", ""),
        })

    return jsonify({
        "balance_sol": state.get("balance_sol"),
        "realized_pnl_sol": state.get("realized_pnl_sol"),
        "open_positions": positions,
    })


@app.route("/api/journal")
def api_journal():
    limit = request.args.get("limit", default=50, type=int)
    limit = max(1, min(limit, 500))  # sane bounds — never dump the whole file on a bad query param
    return jsonify(get_recent_journal(limit=limit))


@app.route("/api/theses")
def api_theses():
    active = get_theses(statuses={"stalking", "holding"})
    return jsonify(active)


def start_api_server():
    """Starts the Flask app in a daemon background thread — call once from ledger_bot.py's main()."""
    def run():
        # use_reloader=False is required outside the main thread (Flask's
        # reloader uses signals, which only work in the main thread) —
        # debug stays off too, this is a long-running background service.
        app.run(host="0.0.0.0", port=API_PORT, debug=False, use_reloader=False)

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    print(f"[API] Ledger's HTTP API listening on port {API_PORT}")
