"""trader_profile.py — per-trader timing params + gem-vs-pump exit classifier.

Timing (trader_timing.json, built by backtest/trader_timing.py from cached data):
  max_entry_delay_s  skip a copy if our entry would land later than this after their buy
  tp_first_pct       take profit on the first candle that reaches this gain (that trader's
                     median first-profitable-close return)
  max_hold_s         their typical time-to-peak; flat exit after this unless moonbagged
Exit classifier (classify_token): at the first TP we either
  * GEM  -> sell all but MOONBAG_FRACTION and ride it with a wide trailing stop
  * PUMP -> sell 100%
  * neutral -> sell 100% (default to taking money)
Env: TRADER_TIMING_ENABLED (default true), MOONBAG_FRACTION (0.2), MOONBAG_TRAIL_PCT (0.40).
"""
from __future__ import annotations
import json, os, time
from pathlib import Path

ENABLED = os.environ.get("TRADER_TIMING_ENABLED", "true").lower() == "true"
MOONBAG_FRACTION = float(os.environ.get("MOONBAG_FRACTION", "0.20"))
MOONBAG_TRAIL_PCT = float(os.environ.get("MOONBAG_TRAIL_PCT", "0.40"))
DEFAULT = {"max_entry_delay_s": 20, "tp_first_pct": 0.08, "max_hold_s": 300}
_FILE = Path(__file__).parent / "trader_timing.json"
_cache = [0.0, {}]


def _load() -> dict:
    if time.time() - _cache[0] > 300:
        try:
            _cache[1] = json.loads(_FILE.read_text())
        except Exception:
            _cache[1] = {}
        _cache[0] = time.time()
    return _cache[1]


def params_for(wallet: str) -> dict:
    p = dict(DEFAULT)
    p.update(((_load().get(wallet) or {}).get("params")) or {})
    return p


def classify_token(pair: dict | None, top10_pct: float | None = None, entry_liq_usd: float | None = None,
                   smart_holding: bool | None = None, thesis_score: float = 0.0) -> tuple[str, list]:
    """pair = DexScreener-shaped dict. Returns ('gem'|'pump'|'neutral', reasons)."""
    if not pair:
        return "neutral", ["no pair data"]
    gem, pump = [], []
    liq = (pair.get("liquidity") or {}).get("usd") or 0
    age_h = (time.time() * 1000 - (pair.get("pairCreatedAt") or time.time() * 1000)) / 3.6e6
    tx = pair.get("txns") or {}
    h1, m5 = tx.get("h1") or {}, tx.get("m5") or {}
    vol = pair.get("volume") or {}
    pc = pair.get("priceChange") or {}
    # pump-and-dump signals
    if entry_liq_usd and liq < entry_liq_usd * 0.7: pump.append("liquidity pulled >30%")
    if (m5.get("sells", 0) > 2 * max(1, m5.get("buys", 0))): pump.append("sell wall last 5m")
    if (pc.get("m5") or 0) > 60 and (pc.get("h1") or 0) < (pc.get("m5") or 0) * 1.2: pump.append("one-candle spike")
    if top10_pct is not None and top10_pct > 45: pump.append(f"top10 {top10_pct:.0f}% (bundle/dev)")
    if (pc.get("h1") or 0) < -35: pump.append("h1 dump")
    # gem signals
    if age_h >= 24: gem.append(f"age {age_h:.0f}h")
    if entry_liq_usd and liq > entry_liq_usd * 1.2: gem.append("liquidity growing")
    if h1.get("buys", 0) > 1.2 * max(1, h1.get("sells", 0)) and h1.get("buys", 0) >= 50: gem.append("organic buy flow")
    if liq and (vol.get("h24") or 0) / liq < 30: gem.append("volume/liquidity sane (not wash)")
    if smart_holding: gem.append("tracked wallet still holding")
    if top10_pct is not None and top10_pct < 30: gem.append("holders spread")
    if thesis_score >= 0.5: gem.append(f"Fomo thesis {thesis_score:.2f}")
    if thesis_score >= 0.75: gem.append("strong Fomo thesis")
    if pump:
        return "pump", pump
    if len(gem) >= 3:
        return "gem", gem
    return "neutral", gem


def timing_exit(pos: dict, price: float | None, now: float, pair_fn=None, top10_fn=None, thesis_fn=None) -> tuple[list, dict]:
    """Per-trader layer run BEFORE risk_engine.evaluate_exit. Returns (actions, updates)."""
    if not ENABLED or price is None or not pos.get("opened_by") or not pos.get("entry_price"):
        return [], {}
    if pos["opened_by"] not in _load():
        return [], {}  # only wallets with a timing profile (the active copy list)
    p = params_for(pos["opened_by"])
    gain = price / pos["entry_price"] - 1
    age = now - pos.get("opened_ts_x", now)
    if pos.get("moonbag"):
        peak = max(pos.get("moon_peak") or price, price)
        upd = {"moon_peak": peak}
        if price <= peak * (1 - MOONBAG_TRAIL_PCT):
            return [{"fraction": 1.0, "reason": "moonbag_trail"}], upd
        return [], upd
    if gain >= p["tp_first_pct"]:
        pair = pair_fn(pos["mint"]) if pair_fn else None
        top10 = None
        try: top10 = top10_fn(pos["mint"]) if top10_fn else None
        except Exception: pass
        ts = 0.0
        try: ts = thesis_fn(pos["mint"]) if thesis_fn else 0.0
        except Exception: pass
        kind, why = classify_token(pair, top10, pos.get("entry_liq_usd"), thesis_score=ts)
        if kind == "gem":
            return [{"fraction": 1 - MOONBAG_FRACTION, "reason": "first_tp_gem"}], {"moonbag": True, "moon_peak": price, "gem_why": why}
        return [{"fraction": 1.0, "reason": f"first_tp_{kind}"}], {"exit_why": why}
    if age >= p["max_hold_s"]:
        return [{"fraction": 1.0, "reason": "trader_max_hold"}], {}
    return [], {}
