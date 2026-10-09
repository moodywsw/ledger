"""
own_thesis.py — Ledger forms its own calls (not copies).

Every OWN_THESIS_EVERY_MIN (default 240) at most OWN_THESIS_MAX_PER_DAY
(default 3), it:
  1. gathers candidates: GeckoTerminal trending pools (free), Fomo trending
     tokens (if FOMO_API_KEY), and tokens our tracked/shadow wallets bought
     in the last 6h (signals.jsonl);
  2. reads the market regime (BTC/SOL 24h on CoinGecko, free);
  3. scores confluence (below), drops anything that fails the SAME token
     safety gate as copies (mint/freeze authority, Token-2022, liquidity,
     impact, mcap band) — injected by the bot as `safety_fn`;
  4. if the best score ≥ OWN_THESIS_MIN_SCORE, posts a short 🧠 THESIS
     card and (OWN_THESIS_PAPER_TRADE=true, default) opens a small paper
     position tagged source="own_thesis" so its calls get scored like any
     other source (learning.score_everything -> "own_thesis" edge).

Scoring (integers, transparent on purpose):
  +2 per distinct tracked wallet that bought it in the last 6h (max +4)
  +1 on Fomo trending   +1 on GeckoTerminal trending
  +1 1h change +10..+150%   −1 if > +150% (chasing a vertical candle)
  +1 1h buys/sells ≥ 1.3   +1 liquidity ≥ $30K   −1 liquidity/MC < 3%
  +1 regime risk-on        −2 regime risk-off
"""
from __future__ import annotations

import os
import time

import requests

OWN_THESIS_ENABLED = os.environ.get("OWN_THESIS_ENABLED", "true").lower() == "true"
OWN_THESIS_EVERY_MIN = float(os.environ.get("OWN_THESIS_EVERY_MIN", "240"))
OWN_THESIS_MAX_PER_DAY = int(os.environ.get("OWN_THESIS_MAX_PER_DAY", "3"))
OWN_THESIS_MIN_SCORE = int(os.environ.get("OWN_THESIS_MIN_SCORE", "4"))
OWN_THESIS_PAPER_TRADE = os.environ.get("OWN_THESIS_PAPER_TRADE", "true").lower() == "true"

_state = {"last_run": time.time() - 3.5 * 3600, "posted": []}  # first pass ~30 min after boot


def market_regime() -> dict:
    """{'regime': risk_on|chop|risk_off, 'btc_24h': %, 'sol_24h': %} — chop on any error."""
    try:
        r = requests.get("https://api.coingecko.com/api/v3/simple/price",
                         params={"ids": "bitcoin,solana", "vs_currencies": "usd", "include_24hr_change": "true"},
                         timeout=10).json()
        btc = float(r["bitcoin"]["usd_24h_change"])
        sol = float(r["solana"]["usd_24h_change"])
    except Exception:
        return {"regime": "chop", "btc_24h": None, "sol_24h": None}
    if sol <= -3 or btc <= -2:
        regime = "risk_off"
    elif sol >= 1 and btc >= 0:
        regime = "risk_on"
    else:
        regime = "chop"
    return {"regime": regime, "btc_24h": btc, "sol_24h": sol}


def score_candidate(c: dict, regime: str) -> tuple:
    """c: {mint, symbol, wallets:set, on_fomo, on_gt, pair}. Returns (score, reasons[])."""
    s, why = 0, []
    nw = len(c.get("wallets") or ())
    if nw:
        s += min(4, 2 * nw)
        why.append(f"{nw} tracked wallet{'s' if nw > 1 else ''} bought in the last 6h")
    if c.get("on_fomo"):
        s += 1
        why.append("trending on Fomo")
    if c.get("on_gt"):
        s += 1
    pair = c.get("pair") or {}
    h1 = (pair.get("priceChange") or {}).get("h1")
    if h1 is not None:
        if 10 <= h1 <= 150:
            s += 1
            why.append(f"+{h1:.0f}% 1h, not vertical")
        elif h1 > 150:
            s -= 1
    tx = (pair.get("txns") or {}).get("h1") or {}
    b, se = tx.get("buys") or 0, tx.get("sells") or 0
    if se and b / se >= 1.3:
        s += 1
        why.append(f"buyers lead {b}/{se} txns 1h")
    liq = (pair.get("liquidity") or {}).get("usd") or 0
    mc = pair.get("marketCap") or pair.get("fdv") or 0
    if liq >= 30_000:
        s += 1
    if mc and liq / mc < 0.03:
        s -= 1
    if regime == "risk_on":
        s += 1
    elif regime == "risk_off":
        s -= 2
    return s, why


def gather_candidates(recent_signals: list, gt_trending: list, fomo_trending: list, best_pair_fn) -> dict:
    cands: dict = {}
    for p in gt_trending:
        m = (p.get("baseToken") or {}).get("address")
        if m:
            c = cands.setdefault(m, {"mint": m, "symbol": (p.get("baseToken") or {}).get("symbol"), "wallets": set()})
            c["on_gt"], c["pair"] = True, p
    for t in fomo_trending:
        c = cands.setdefault(t["mint"], {"mint": t["mint"], "symbol": t.get("symbol"), "wallets": set()})
        c["on_fomo"] = True
    cutoff = time.time() - 6 * 3600
    for s in recent_signals:
        if (s.get("ts") or 0) < cutoff or not s.get("mint"):
            continue
        c = cands.setdefault(s["mint"], {"mint": s["mint"], "symbol": None, "wallets": set()})
        c["wallets"].add(s.get("wallet"))
    # base/quote junk (SOL, USDC...) never makes a thesis
    for junk in ("So11111111111111111111111111111111111111112", "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"):
        cands.pop(junk, None)
    for m, c in cands.items():
        if not c.get("pair"):
            c["pair"] = best_pair_fn(m) if (c["wallets"] or c.get("on_fomo")) else None
    return cands


def due(now: float | None = None) -> bool:
    now = now or time.time()
    _state["posted"] = [t for t in _state["posted"] if now - t < 86400]
    return (OWN_THESIS_ENABLED and now - _state["last_run"] >= OWN_THESIS_EVERY_MIN * 60
            and len(_state["posted"]) < OWN_THESIS_MAX_PER_DAY)


def form_thesis(*, recent_signals, gt_trending, fomo_trending, best_pair_fn, safety_fn, held: set,
                regime: dict | None = None, now: float | None = None) -> dict | None:
    """Pure-ish core (network only via the injected fns + regime). Returns the
    best thesis dict or None. safety_fn(mint, symbol) -> (ok, reason, info)."""
    now = now or time.time()
    _state["last_run"] = now
    regime = regime or market_regime()
    cands = gather_candidates(recent_signals, gt_trending, fomo_trending, best_pair_fn)
    ranked = []
    for m, c in cands.items():
        if m in held or not c.get("pair"):
            continue
        sc, why = score_candidate(c, regime["regime"])
        if sc >= OWN_THESIS_MIN_SCORE:
            ranked.append((sc, m, why, c))
    ranked.sort(key=lambda x: -x[0])
    for sc, m, why, c in ranked[:5]:
        ok, reason, info = safety_fn(m, c.get("symbol") or "")
        if not ok:
            print(f"[THESIS] {c.get('symbol') or m[:6]} score {sc} failed safety: {reason}")
            continue
        pair = c["pair"]
        if regime.get("sol_24h") is not None:
            why = why + [f"SOL {regime['sol_24h']:+.1f}% 24h ({regime['regime'].replace('_', '-')})"]
        conviction = "high" if sc >= OWN_THESIS_MIN_SCORE + 3 else "medium" if sc >= OWN_THESIS_MIN_SCORE + 1 else "low"
        _state["posted"].append(now)
        return {"mint": m, "symbol": c.get("symbol") or (pair.get("baseToken") or {}).get("symbol"),
                "name": (pair.get("baseToken") or {}).get("name"), "score": sc, "why": why[:3],
                "conviction": conviction, "mcap_usd": pair.get("marketCap") or pair.get("fdv"),
                "price_usd": float(pair["priceUsd"]) if pair.get("priceUsd") else None,
                "regime": regime, "wallets": sorted(w for w in c["wallets"] if w)}
    return None
