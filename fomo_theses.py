"""fomo_theses.py — public Fomo 'Thesis' posts as a rule-based signal (no LLM).

Pattern we trade: tracked KOL buys -> pump -> dip -> KOL posts a thesis -> second leg.
Source: fomoapi.io (unofficial; same FOMO_API_KEY + ToS caveat as fomo.py, OFF without a key).
  * /v2/alerts?type=thesis&chain=solana   polled every FOMO_THESIS_EVERY_MIN (125 credits/call)
  * /v2/thesis/token/{mint}?sort=likes    only at a first-TP gem decision on a held token,
                                          cached 6h (1,250 credits/page)
Score (0..1) per thesis, all rules:
  likes      min(likes/200, 1)            * 0.35   (OHB's BOAR thesis, 227 likes -> full)
  size       min(equity_or_trade_usd/20k,1)* 0.25   (skin in the game)
  catalyst   keyword hits (listing, partnership, launch, v2, burn, buyback, ETF, CEX, roadmap,
             revenue, airdrop, narrative...)  * 0.20
  conviction length>=120 chars + words (holding, adding, long, conviction, not selling) * 0.20
  penalties  isDev -0.3, very short (<40 chars) -0.2, shill words (100x, moon, guaranteed) -0.1
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


def score(t: dict) -> float:
    text = (t.get("text") or "").strip()
    s = 0.35 * min((t.get("likes") or 0) / 200, 1)
    s += 0.25 * min(float(t.get("equity") or t.get("tradeUsd") or t.get("usdValue") or 0) / 20000, 1)
    s += 0.20 * min(len(set(m.group(0).lower() for m in CATALYST.finditer(text))) / 2, 1)
    s += 0.20 * (0.5 * (len(text) >= 120) + 0.5 * bool(CONVICTION.search(text)))
    if t.get("isDev"): s -= 0.3
    if len(text) < 40: s -= 0.2
    if SHILL.search(text): s -= 0.1
    return round(max(0.0, min(1.0, s)), 3)


def _rows(p):
    if p is None: return None
    return p if isinstance(p, list) else (p.get("alerts") or p.get("theses") or p.get("data") or [])


def recent_tracked(tracked_handles: set[str]) -> list:
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
                    "score": score(r), "ts": ts, "text": (r.get("text") or "")[:200], "id": r.get("eventId") or r.get("id")})
    return out


def token_best_score(mint: str) -> float:
    """Best thesis score on a token (0 when disabled / none). Cached 6h per mint."""
    if not fomo.enabled(): return 0.0
    rows = fomo._cached(f"theses:tok:{mint}", 360, lambda: _rows(fomo._get(f"/thesis/token/{mint}", {"sort": "likes", "limit": 25, "threshold": 100})))
    return max((score(t) for t in rows or []), default=0.0)
