"""
api_server.py — Mirko's read-only HTTP API

Serves the current paper-trading state, journal, and theses over
plain HTTP/JSON, for the static dashboard in /site (or anything else
that wants to read Mirko's state without touching the JSON files
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

Read-only by design (except the authenticated /api/bot_switch POST): routes read from disk
(ledger_state.json, journal.jsonl, theses.json) — nothing here writes
state or can influence trading decisions.

Env vars:
  API_PORT - port to listen on. Defaults to Railway's own $PORT (the
             one it already injects and exposes publicly for a "web"
             process), falling back to 8080 for local runs where
             neither is set.
"""

import os
import json
import threading
import requests
from pathlib import Path
from flask import Flask, jsonify, request, send_from_directory

import bot_switch
import privacy
import security
import re as _re
from functools import wraps
from journal_store import get_recent_journal
from theses_store import get_theses
from real_trading import (
    REAL_TRADING_ENABLED, MAX_REAL_POSITION_PCT, MAX_TOTAL_EXPOSURE_PCT,
    get_wallet_balances, get_open_real_positions_summary, get_realized_pnl_usdc,
    get_max_real_position_usdc,
)

API_PORT = int(os.environ.get("API_PORT", os.environ.get("PORT", "8080")))

# Must resolve the same way as ledger_bot.py's STATE_FILE (same DATA_DIR
# env var) since this reads the exact file ledger_bot.py writes, from
# the same process/dyno — a mismatch here would mean the dashboard
# reads stale/empty state from the old ephemeral path while the bot
# writes to the persistent volume.
STATE_FILE = Path(os.environ.get("DATA_DIR", ".")) / "ledger_state.json"
JUPITER_PRICE_API = "https://lite-api.jup.ag/price/v3"

# The static dashboard lives in /site (index.html, style.css, app.js) and
# is served straight from this same Flask app/process/port — no separate
# static host needed. static_url_path="" puts its files at the domain
# root (e.g. site/app.js -> /app.js) instead of under /static.
SITE_DIR = Path(__file__).resolve().parent / "site"

app = Flask(__name__, static_folder=str(SITE_DIR), static_url_path="")
app.config.update(MAX_CONTENT_LENGTH=4096, JSON_SORT_KEYS=False, PROPAGATE_EXCEPTIONS=False, DEBUG=False, TESTING=False)
_SID = _re.compile(r"^[a-f0-9]{6,32}$")


def _bucket(path: str, method: str) -> str:
    if method == "POST":
        return "write"
    if path.startswith("/api/owner"):
        return "owner"
    return "api" if path.startswith("/api/") else "static"


@app.before_request
def _guard():
    if request.method not in ("GET", "HEAD", "POST", "OPTIONS"):
        return jsonify({"error": "method not allowed"}), 405
    ip = security.client_ip(request)
    b = _bucket(request.path, request.method)
    if not security.allow(ip, b):
        return jsonify({"error": "rate limited"}), 429, {"Retry-After": "60"}
    if b in ("owner", "write"):
        wait = security.locked_for(ip)
        if wait > 0:
            return jsonify({"error": "too many failed attempts, try later"}), 429, {"Retry-After": str(int(wait) + 1)}


def _check_owner() -> bool:
    ip = security.client_ip(request)
    if privacy.is_owner(request):
        security.record_success(ip)
        return True
    security.record_fail(ip)
    return False


@app.errorhandler(404)
def _nf(_e):
    return jsonify({"error": "not found"}), 404


@app.errorhandler(413)
def _big(_e):
    return jsonify({"error": "payload too large"}), 413


@app.errorhandler(Exception)
def _err(e):
    from werkzeug.exceptions import HTTPException
    if isinstance(e, HTTPException):
        return jsonify({"error": e.name}), e.code
    print(f"[API] internal error on {request.path}: {type(e).__name__}")  # no message: may contain URLs/keys
    return jsonify({"error": "internal error"}), 500


@app.route("/")
def dashboard():
    return send_from_directory(app.static_folder, "index.html")


@app.after_request
def add_cors_headers(response):
    # Same-origin only: the site is served by this app, so no CORS headers at all.
    security.apply_headers(response, request.path)
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


def owner_only(fn):
    """Owner views: same admin token as the ON/OFF switch (Bearer or X-Admin-Token)."""
    @wraps(fn)
    def inner(*a, **k):
        if request.method == "OPTIONS":
            return ("", 204)
        if not _check_owner():
            return jsonify({"error": "unauthorized"}), 401
        resp = fn(*a, **k)
        r = app.make_response(resp)
        r.headers["Cache-Control"] = "no-store"
        return r
    return inner


def _public(obj, keep=None):
    return privacy.scrub_obj(obj, keep)


@app.route("/api/state")
def api_state():
    d = _state_full()
    keep = {p["mint"] for p in d["open_positions"]}
    return jsonify(_public(d, keep))


@app.route("/api/owner/state")
@owner_only
def api_owner_state():
    return jsonify(_state_full())


def _state_full():
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
            # additive display fields (dashboard v2)
            "current_price": current_price,
            "pnl_pct": ((current_price - entry_price) / entry_price) if (current_price is not None and entry_price) else None,
            "opened_by": pos.get("opened_by", ""),
            "source": pos.get("source", ""),
            "opened_at": pos.get("opened_at"),
            "moonbag": bool(pos.get("moonbag")),
        })

    return {
        "balance_sol": state.get("balance_sol"),
        "realized_pnl_sol": state.get("realized_pnl_sol"),
        "open_positions": positions,
    }


@app.route("/api/real_state")
def api_real_state():
    d = _real_state_full()
    keep = {p.get("mint") for p in (d.get("open_real_positions") or []) if isinstance(p, dict) and p.get("mint")}
    return jsonify(_public(d, keep))


def _real_state_full():
    """
    Real (on-chain) trading state — separate from /api/state, which is
    paper-only. Kept as its own endpoint rather than folded into
    /api/state so a real_trading.py problem (missing key, RPC down)
    can't take the paper-trading dashboard down with it: every real-
    money field here is best-effort, caught individually, and reported
    as null/empty rather than raising.
    """
    balances = {"usdc": None, "sol": None}
    balances_error = None
    try:
        balances = get_wallet_balances()
    except Exception as e:
        balances_error = "unavailable"  # never echo exception text (RPC URLs can carry API keys)
        print(f"[API] balance lookup failed: {type(e).__name__}")

    try:
        open_positions = get_open_real_positions_summary()
    except Exception:
        open_positions = []

    try:
        realized_pnl_usdc = get_realized_pnl_usdc()
    except Exception:
        realized_pnl_usdc = None

    exposure_usdc = sum(p["cost_basis_usdc"] for p in open_positions)
    max_position_usdc = None
    max_total_exposure_usdc = None
    if balances.get("usdc") is not None:
        max_position_usdc = get_max_real_position_usdc(balances["usdc"])
        max_total_exposure_usdc = (balances["usdc"] + exposure_usdc) * MAX_TOTAL_EXPOSURE_PCT

    return ({
        "armed": REAL_TRADING_ENABLED,
        "balance_usdc": balances.get("usdc"),
        "balance_sol": balances.get("sol"),
        "balance_error": balances_error,
        "open_real_positions": open_positions,
        "exposure_usdc": exposure_usdc,
        "realized_pnl_usdc": realized_pnl_usdc,
        "max_real_position_usdc": max_position_usdc,
        "max_total_exposure_usdc": max_total_exposure_usdc,
        "max_real_position_pct": MAX_REAL_POSITION_PCT,
        "max_total_exposure_pct": MAX_TOTAL_EXPOSURE_PCT,
    })


@app.route("/api/bot_switch", methods=["GET"])
def api_bot_switch_get():
    return jsonify(bot_switch.status())


@app.route("/api/bot_switch", methods=["POST", "OPTIONS"])
def api_bot_switch_post():
    if request.method == "OPTIONS":
        return ("", 204)
    if not _check_owner():
        return jsonify({"error": "unauthorized"}), 401
    body = request.get_json(silent=True)
    body = body if isinstance(body, dict) else {}
    if "enabled" in body:
        if not isinstance(body["enabled"], bool):
            return jsonify({"error": "enabled must be true/false"}), 400
        enabled = body["enabled"]
    else:
        enabled = not bot_switch.is_enabled()
    return jsonify(bot_switch.set_enabled(enabled, by="dashboard"))


@app.route("/api/journal")
def api_journal():
    limit = request.args.get("limit", default=50, type=int) or 50
    limit = max(1, min(limit, 500))  # sane bounds — never dump the whole file on a bad query param
    return jsonify(_public(get_recent_journal(limit=limit)))


@app.route("/api/owner/journal")
@owner_only
def api_owner_journal():
    limit = max(1, min(request.args.get("limit", default=150, type=int), 500))
    return jsonify(get_recent_journal(limit=limit))


@app.route("/api/theses")
def api_theses():
    active = get_theses(statuses={"stalking", "holding"})
    return jsonify(_public(active))


# ── Dashboard v2: read-only aggregate endpoint ───────────────────────
WALLETS_FILE = Path(__file__).resolve().parent / "wallets.json"


def _parse_ts(iso):
    from datetime import datetime
    try:
        return datetime.fromisoformat(str(iso).replace("Z", "+00:00")).timestamp()
    except Exception:
        return None


def build_overview(state: dict, journal: list, now: float = None) -> dict:
    """Pure function (testable): closed trades, PnL windows, equity curve,
    per-trader stats, and the bot's own theses — all from data already on disk."""
    import time as _t
    now = now or _t.time()
    log = state.get("trade_log", []) or []
    symbols = {e.get("token"): e.get("symbol") for e in log if e.get("action") == "open" and e.get("symbol")}
    for m, p in (state.get("open_positions") or {}).items():
        if p.get("symbol"):
            symbols.setdefault(m, p["symbol"])
    closes = [e for e in log if e.get("action") in ("close", "partial_close") and isinstance(e.get("pnl_sol"), (int, float))]
    closes.sort(key=lambda e: _parse_ts(e.get("at")) or 0)

    def window(sec):
        return sum(e["pnl_sol"] for e in closes if sec is None or (now - (_parse_ts(e.get("at")) or 0)) <= sec)

    equity, cum = [], 0.0
    for e in closes:
        cum += e["pnl_sol"]
        equity.append({"t": e.get("at"), "v": round(cum, 6)})

    recent = [{
        "token": e.get("token"), "symbol": symbols.get(e.get("token")) or (e.get("token") or "")[:6],
        "action": e.get("action"), "pnl_sol": e.get("pnl_sol"), "reason": e.get("reason", ""),
        "opened_by": e.get("opened_by", ""), "at": e.get("at"), "fraction_sold": e.get("fraction_sold"),
    } for e in reversed(closes[-30:])]

    by_trader = {}
    for e in closes:
        if e.get("action") != "close":
            continue
        k = (e.get("opened_by") or "").lower()
        d = by_trader.setdefault(k, {"n": 0, "wins": 0, "pnl": 0.0})
        d["n"] += 1; d["wins"] += e["pnl_sol"] >= 0; d["pnl"] += e["pnl_sol"]

    traders = []
    try:
        wl = json.loads(WALLETS_FILE.read_text()).get("wallets", [])
    except Exception:
        wl = []
    for w in wl:
        h = w.get("handle", "")
        st = by_trader.get(h.lower()) or by_trader.get((w.get("address") or "").lower()) or {"n": 0, "wins": 0, "pnl": 0.0}
        chains = [c for c, a in (w.get("chains") or {}).items() if a and c in ("solana", "base", "bsc", "robinhood")] or (["solana"] if w.get("address") else [])
        traders.append({
            "handle": h, "active": bool(w.get("active", True)), "chains": chains,
            "trades": st["n"], "hit_rate": (st["wins"] / st["n"]) if st["n"] else None, "pnl_sol": round(st["pnl"], 6),
        })

    own = []
    for e in journal or []:
        m = e.get("meta") or {}
        if m.get("own_thesis"):
            own.append({"symbol": e.get("token_ticker"), "at": e.get("timestamp"), "score": m.get("score"),
                        "conviction": m.get("conviction"), "why": m.get("why") or [], "regime": m.get("regime")})
    return {
        "pnl_sol": {"today": window(86400), "d7": window(7 * 86400), "all": state.get("realized_pnl_sol", window(None))},
        "equity": equity[-300:], "closed_trades": recent, "traders": traders, "own_theses": own[:20],
        "wins": sum(1 for e in closes if e.get("action") == "close" and e["pnl_sol"] >= 0),
        "losses": sum(1 for e in closes if e.get("action") == "close" and e["pnl_sol"] < 0),
    }


def public_overview(o: dict) -> dict:
    """Overview without anything that identifies a copied trader."""
    o = dict(o)
    o.pop("traders", None)
    o["closed_trades"] = [{k: v for k, v in t.items() if k != "opened_by"} for t in o.get("closed_trades", [])]
    return _public(o, {t.get("token") for t in o["closed_trades"] if t.get("token")})


@app.route("/api/overview")
def api_overview():
    return jsonify(public_overview(build_overview(load_state(), get_recent_journal(limit=500))))


@app.route("/api/owner/overview")
@owner_only
def api_owner_overview():
    return jsonify(build_overview(load_state(), get_recent_journal(limit=500)))


@app.route("/api/owner/check", methods=["GET", "POST", "OPTIONS"])
@owner_only
def api_owner_check():
    return jsonify({"ok": True})


@app.route("/api/market_thoughts")
def api_market_thoughts():
    import market_thoughts
    d = market_thoughts.cached()
    if not d:
        return jsonify({"ready": False, "message": "First market read is being prepared (refreshes every 2h)."})
    return jsonify(dict(_public(d), ready=True))


@app.route("/api/portfolio/live")
def api_portfolio_live():
    import paper_portfolio
    return jsonify(_public(paper_portfolio.live_prices()))


@app.route("/api/portfolio")
def api_portfolio():
    """Mirko's SIMULATED paper portfolio (no real orders)."""
    import paper_portfolio
    return jsonify(_public(paper_portfolio.public_view()))


@app.route("/api/owner/lab")
@owner_only
def api_owner_lab():
    import strategy_lab
    return jsonify(strategy_lab.public_view())


@app.route("/api/owner/lab/<sid>/<action>", methods=["POST", "OPTIONS"])
@owner_only
def api_owner_lab_decide(sid, action):
    import strategy_lab
    from risk_engine import RiskConfig
    if action not in ("approve", "reject") or not _SID.match(sid or ""):
        return jsonify({"error": "bad action"}), 400
    try:
        st = strategy_lab.decide(sid, action == "approve", RiskConfig.from_env().profile)
    except KeyError:
        return jsonify({"error": "not found"}), 404
    except ValueError as e:
        return jsonify({"error": str(e)}), 409
    return jsonify(st)


@app.route("/api/owner/lab/propose", methods=["POST", "OPTIONS"])
@owner_only
def api_owner_lab_propose():
    import strategy_lab
    threading.Thread(target=lambda: strategy_lab.tick(force_propose=True), daemon=True).start()
    return jsonify({"started": True})


@app.route("/api/owner/fomo_theses")
@owner_only
def api_fomo_theses():
    """Fomo theses by tracked traders (empty when no FOMO API key is configured)."""
    try:
        import fomo, fomo_theses
        if not fomo.enabled():
            return jsonify({"enabled": False, "theses": []})
        handles = {w.get("handle", "") for w in json.loads(WALLETS_FILE.read_text()).get("wallets", []) if w.get("active", True)}
        return jsonify({"enabled": True, "theses": fomo_theses.recent_tracked(handles)})
    except Exception as e:
        return jsonify({"enabled": False, "theses": [], "error": "unavailable"})


@app.route("/api/persona/feed")
def api_persona_feed():
    """Mirko's voice: recent persona posts, mood and beliefs (for the website)."""
    try:
        import persona
        return jsonify(_public(persona.feed(limit=max(1, min(request.args.get("limit", default=30, type=int) or 30, 100)))))
    except Exception as e:
        return jsonify({"posts": [], "mood": None, "beliefs": [], "error": "unavailable"})


def start_api_server():
    """Starts the Flask app in a daemon background thread — call once from ledger_bot.py's main()."""
    def run():
        # Production WSGI server (waitress, pure Python, thread-safe in a background
        # thread). Falls back to Flask's server only if waitress isn't installed.
        try:
            from waitress import serve
            serve(app, host="0.0.0.0", port=API_PORT, threads=int(os.environ.get("API_THREADS", "6")),
                  ident="", clear_untrusted_proxy_headers=False, max_request_body_size=4096,
                  connection_limit=200, channel_timeout=30)
        except ImportError:
            print("[API] waitress missing — falling back to the Flask dev server")
            app.run(host="0.0.0.0", port=API_PORT, debug=False, use_reloader=False)

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    print(f"[API] Mirko's HTTP API listening on port {API_PORT}")
