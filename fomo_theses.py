"""fomo_theses.py — public Fomo 'Thesis' posts as a rule-based signal (no LLM).

Pattern we trade: tracked KOL buys -> pump -> dip -> KOL posts a thesis -> second leg.
Source: fomoapi.io (unofficial; same FOMO_API_KEY + ToS caveat as fomo.py, OFF without a key).
  * /v2/alerts?type=thesis&chain=solana   polled every FOMO_THESIS_EVERY_MIN (125 credits/call)
  * /v2/thesis/token/{mint}?sort=likes    only at a first-TP gem decision on a held token,
                                          cached 6h (1,250 credits/page)
Scores (rule-based):
  re-entry  author 0.30 (tracked + hit rate) · freshness 0.25 · size 0.20 · catalyst 0.125 · conviction 0.125; no likes
  gem       size 0.35 · catalyst 0.30 · conviction 0.25 · likes 0.10 (only theses >6h old)
  penalties isDev -0.3, <40 chars -0.2, shill words -0.1
Uses:
  1) fresh (<= FOMO_THESIS_FRESH_MIN) thesis from a TRACKED handle on a Solana token, score >=
     FOMO_THESIS_MIN_SCORE -> re-entry / buy-the-dip copy of that trader (normal copy path,
     normal safety + risk gates).
  2) gem score: best thesis score on the token adds signals to trader_profile.classify_token
     (>=0.5 counts as a gem signal, >=0.75 as two) and keeps the moonbag alive.
"""
from __future__ import annotations
import os, re, time
import fomo

EVERY_MIN = float(os.environ.get("FOMO_THESIS_EVERY_MIN", "45"))
FRESH_MIN = float(os.environ.get("FOMO_THESIS_FRESH_MIN", "90"))
MIN_SCORE = float(os.environ.get("FOMO_THESIS_MIN_SCORE", "0.45"))
CATALYST = re.compile(r"listing|list(ed)? on|partner|launch|v2|upgrade|burn|buyback|etf|cex|binance|coinbase|roadmap|revenue|airdrop|narrative|catalyst|integration|mainnet|staking|news|announce", re.I)
CONVICTION = re.compile(r"holding|adding|added|long term|conviction|not selling|accumulat|bottom|undervalued|send it|higher", re.I)
SHILL = re.compile(r"100x|1000x|guarantee|can't lose|free money", re.I)


def _base(t: dict) -> tuple:
    text = (t.get("text") or "").strip()
    size = min(float(t.get("equity") or t.get("tradeUsd") or t.get("usdValue") or 0) / 20000, 1)
    cat = min(len(set(m.group(0).lower() for m in CATALYST.finditer(text))) / 2, 1)
    conv = 0.5 * (len(text) >= 120) + 0.5 * bool(CONVICTION.search(text))
    pen = (0.3 if t.get("isDev") else 0) + (0.2 if len(text) < 40 else 0) + (0.1 if SHILL.search(text) else 0)
    return size, cat, conv, pen


def _age_min(t: dict, now: float) -> float:
    ts = t.get("ts") or 0
    if isinstance(ts, str):
        try:
            from datetime import datetime
            ts = datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
        except Exception:
            ts = 0
    ts = ts / 1000 if ts > 1e12 else ts
    return (now - ts) / 60 if ts else 1e9


def author_score(hit_rate: float | None, tracked: bool) -> float:
    """0..1: tracked trader, boosted by their first-profit hit rate from trader_timing.json."""
    if not tracked:
        return 0.0
    return 0.5 + 0.5 * (hit_rate if hit_rate is not None else 0.5)


def reentry_score(t: dict, author: float, now: float | None = None) -> float:
    """Fresh-thesis buy-the-dip score. NO likes (they arrive after the pump).
    author 0.30 · freshness 0.25 (1.0 at 0 min -> 0 at FRESH_MIN) · size 0.20 · catalyst 0.125 · conviction 0.125."""
    now = now or time.time()
    size, cat, conv, pen = _base(t)
    fresh = max(0.0, 1 - _age_min(t, now) / FRESH_MIN)
    s = 0.30 * author + 0.25 * fresh + 0.20 * size + 0.125 * cat + 0.125 * conv - pen
    return round(max(0.0, min(1.0, s)), 3)


def gem_score(t: dict, now: float | None = None) -> float:
    """Thesis quality for the gem/moonbag check. Likes only count (10%) once the thesis is >6h old."""
    now = now or time.time()
    size, cat, conv, pen = _base(t)
    likes = min((t.get("likes") or 0) / 200, 1) if _age_min(t, now) >= 360 else 0.0
    s = 0.35 * size + 0.30 * cat + 0.25 * conv + 0.10 * likes - pen
    return round(max(0.0, min(1.0, s)), 3)


score = gem_score  # backwards-compatible name


def _rows(p):
    if p is None: return None
    return p if isinstance(p, list) else (p.get("alerts") or p.get("theses") or p.get("data") or [])


def recent_tracked(tracked_handles: set[str], hit_rates: dict | None = None) -> list:
    """Fresh theses by tracked handles: [{handle, mint, symbol, score, ts, text}]."""
    rows = fomo._cached("theses:feed", EVERY_MIN, lambda: _rows(fomo._get("/alerts", {"type": "thesis", "chain": "solana", "limit": 100})))
    low = {h.lower().replace("fomo:", "").split(" ")[0] for h in tracked_handles}
    out, now = [], time.time()
    for r in rows or []:
        h = (r.get("trader") or r.get("handle") or "").lower()
        if h not in low: continue
        ts = r.get("ts") or 0
        ts = ts / 1000 if ts > 1e12 else ts
        if isinstance(ts, (int, float)) and now - ts > FRESH_MIN * 60: continue
        tok = r.get("token")
        mint = r.get("tokenAddress") or (tok.get("address") if isinstance(tok, dict) else None)
        if not mint: continue
        out.append({"handle": h, "mint": mint, "symbol": r.get("token") if isinstance(r.get("token"), str) else None,
                    "score": reentry_score(r, author_score((hit_rates or {}).get(h), True), now), "ts": ts, "text": (r.get("text") or "")[:200], "id": r.get("eventId") or r.get("id")})
    return out


def token_best_score(mint: str) -> float:
    """Best thesis score on a token (0 when disabled / none). Cached 6h per mint."""
    if not fomo.enabled(): return 0.0
    rows = fomo._cached(f"theses:tok:{mint}", 360, lambda: _rows(fomo._get(f"/thesis/token/{mint}", {"sort": "likes", "limit": 25, "threshold": 100})))
    return max((gem_score(t) for t in rows or []), default=0.0)
