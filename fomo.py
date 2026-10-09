"""
fomo.py — Fomo (fomo.family) top traders + trending tokens, via fomoapi.io.

What's reachable (checked 2026-10-09):
  * fomo.family itself has NO public API, and its robots.txt disallows
    /profile/, /user, /u/, /token, /coin — so scraping the app is out.
  * fomoapi.io is an UNOFFICIAL third-party API (not affiliated with
    fomo.family) that serves the Fomo leaderboards. Every data endpoint
    needs `authorization: Bearer <key>`; only /health and /status are
    keyless (the leaderboard answers 401 "API key required").
    Free key: https://fomoapi.io/dashboard — 250,000 credits/month, a
    leaderboard call costs 250 credits => ~1,000 calls/month.

ToS caveat (read before enabling): fomo.family's Terms forbid accessing or
extracting data from the Services "using any unauthorized or automated
means", "any ... unauthorized third-party application", and using
automated means to "control or interact with account activity". The
official API (prod-api.fomo.family) needs a logged-in session for
users/leaderboard data (only /public/prices is open) and we do NOT use
it. fomoapi.io is an unauthorized third party, so enabling it is the
owner's call. The ToS-clean route is ON-CHAIN: a Fomo account's Solana
wallet address is public; put it (and any top trader's address) in
wallets.json / FOMO-independent tracking and everything is read from
Solana RPC, not from Fomo. This module is OFF unless FOMO_API_KEY is set.

Set FOMO_API_KEY to enable. Without it every function logs once and
returns [] so nothing else changes. The default schedule (7d traders
every 3h + trending tokens every 2h ≈ 600 calls/month) stays inside the
free tier. The key is only ever sent as a header; it is never logged.

Feeds: learning.discover_candidates() (Fomo top-trader Solana wallets
-> shadow book) and own_thesis (Fomo trending tokens as one input).
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

import requests

FOMO_API_BASE = os.environ.get("FOMO_API_BASE", "https://api.fomoapi.io/v2")
FOMO_LEADERBOARD_PERIOD = os.environ.get("FOMO_LEADERBOARD_PERIOD", "7d")  # 24h | 7d | 30d | all
FOMO_LEADERBOARD_LIMIT = int(os.environ.get("FOMO_LEADERBOARD_LIMIT", "10"))
FOMO_TRADERS_EVERY_MIN = float(os.environ.get("FOMO_TRADERS_EVERY_MIN", "180"))
FOMO_TRENDING_EVERY_MIN = float(os.environ.get("FOMO_TRENDING_EVERY_MIN", "120"))
CACHE_FILE = Path(os.environ.get("DATA_DIR", ".")) / "fomo_cache.json"

_warned = [False]


def _key() -> str:
    return os.environ.get("FOMO_API_KEY", "").strip()


def enabled() -> bool:
    return bool(_key())


def _load_cache() -> dict:
    try:
        return json.loads(CACHE_FILE.read_text())
    except Exception:
        return {}


def _save_cache(c: dict):
    try:
        CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        CACHE_FILE.write_text(json.dumps(c))
    except Exception as e:
        print(f"[FOMO] cache write failed: {e}")


def _get(path: str, params: dict | None = None):
    if not enabled():
        if not _warned[0]:
            print("[FOMO] FOMO_API_KEY not set — Fomo leaderboard/trending disabled "
                  "(free key: https://fomoapi.io/dashboard; unofficial third-party API).")
            _warned[0] = True
        return None
    try:
        r = requests.get(FOMO_API_BASE + path, params=params, timeout=15,
                         headers={"authorization": f"Bearer {_key()}", "accept": "application/json"})
        if r.status_code in (401, 403):
            print(f"[FOMO] {path}: HTTP {r.status_code} — key rejected or out of credits")
            return None
        if r.status_code == 429:
            print(f"[FOMO] {path}: rate limited / out of credits")
            return None
        r.raise_for_status()
        return r.json()
    except Exception as e:
        print(f"[FOMO] {path} failed: {type(e).__name__}")
        return None


def _cached(name: str, every_min: float, fetch):
    c = _load_cache()
    hit = c.get(name)
    if hit and time.time() - hit.get("ts", 0) < every_min * 60:
        return hit.get("data") or []
    data = fetch()
    if data is None:
        return (hit or {}).get("data") or []  # stale beats nothing
    c[name] = {"ts": time.time(), "data": data}
    _save_cache(c)
    return data


def parse_leaderboard(payload) -> list:
    """-> [{rank, name, wallet, pnl_usd, volume_usd, trades}] (Solana wallets only)."""
    rows = payload if isinstance(payload, list) else (payload or {}).get("leaderboard") or (payload or {}).get("data") or (payload or {}).get("users") or []
    out = []
    for i, r in enumerate(rows):
        wallets = r.get("wallets") or {}
        sol = wallets.get("solana") if isinstance(wallets, dict) else None
        if isinstance(sol, list):
            sol = sol[0] if sol else None
        if not sol:
            continue
        out.append({
            "rank": r.get("rank") or i + 1, "name": r.get("displayName") or r.get("username") or sol[:6],
            "wallet": sol, "pnl_usd": r.get("pnlUsd"), "volume_usd": r.get("volumeUsd"), "trades": r.get("trades"),
        })
    return out


def parse_tokens(payload) -> list:
    """-> [{rank, mint, symbol, name, holders, price_usd, change_24h, mcap_usd}] (Solana-looking mints)."""
    rows = (payload or {}).get("tokens") or [] if isinstance(payload, dict) else payload or []
    out = []
    for i, r in enumerate(rows):
        t = r.get("token") or {}
        mint = t.get("address") or r.get("address")
        if not mint or mint.startswith("0x"):
            continue
        out.append({
            "rank": r.get("rank") or i + 1, "mint": mint, "symbol": t.get("symbol"), "name": t.get("name"),
            "holders": r.get("holders"), "price_usd": r.get("priceUsd"), "change_24h": r.get("change24h"),
            "mcap_usd": r.get("marketCapUsd"),
        })
    return out


def top_traders(period: str | None = None, limit: int | None = None) -> list:
    period = period or FOMO_LEADERBOARD_PERIOD
    limit = limit or FOMO_LEADERBOARD_LIMIT
    return _cached(f"traders:{period}:{limit}", FOMO_TRADERS_EVERY_MIN,
                   lambda: (lambda p: parse_leaderboard(p) if p is not None else None)(
                       _get(f"/leaderboard/{period}", {"limit": limit})))


def trending_tokens(board: str = "trending", limit: int = 10) -> list:
    """board: trending | most-held | graduated"""
    return _cached(f"tokens:{board}:{limit}", FOMO_TRENDING_EVERY_MIN,
                   lambda: (lambda p: parse_tokens(p) if p is not None else None)(
                       _get(f"/leaderboard/tokens/{board}", {"limit": limit})))


# ── Per-user (public profile by handle) ──────────────────────────────
# fomoapi.io also serves /v2/users/{handle} and /v2/users/{handle}/trades
# (same key). Used to turn a Fomo handle — e.g. the owner's own account or
# a trader seen on the leaderboard — into the Solana wallet the bot can
# watch on-chain. Only PUBLIC profile data; no Fomo login/password is ever
# needed or stored. FOMO_HANDLES="alice,bob" queues them for shadow trading.
FOMO_HANDLES = [h.strip().lstrip("@") for h in os.environ.get("FOMO_HANDLES", "").split(",") if h.strip()]


def user_wallet(handle: str) -> str | None:
    """Solana wallet address on a public Fomo profile, or None (no key / unknown handle)."""
    def fetch():
        p = _get(f"/users/{handle}")
        if p is None:
            return None
        u = p.get("user") if isinstance(p, dict) and "user" in p else p
        rows = parse_leaderboard([u]) if isinstance(u, dict) else []
        return rows  # [] if no Solana wallet
    rows = _cached(f"user:{handle}", 24 * 60, fetch)
    return rows[0]["wallet"] if rows else None


def user_trades(handle: str, limit: int = 50) -> list:
    """Raw recent trades of a public profile (schema not yet verified with a live key)."""
    def fetch():
        p = _get(f"/users/{handle}/trades", {"limit": limit})
        if p is None:
            return None
        return p if isinstance(p, list) else (p.get("trades") or p.get("data") or [])
    return _cached(f"trades:{handle}:{limit}", 60, fetch)
