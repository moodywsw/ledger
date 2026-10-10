"""trader_timing.py — per-trader timing profile from cached buys + 1m candles (no network).
Writes trader_timing.json: for each active wallet, how far price already ran before the
buy (pre_run), seconds until the first candle that closes above entry+costs, typical
time-to-peak inside 60 min, and median peak. The bot uses these as per-wallet entry
delay cap, first-profit TP and max hold."""
import json, statistics as st, sys
from pathlib import Path
D = Path(__file__).parent / "data"
COST = 0.03  # round-trip fees+slippage
prices = {}
for f in ("prices.json", "fomo_prices.json"):
    p = D / f
    if p.exists(): prices.update(json.load(open(p)))
buys = []
for f in ("wallet_buys.json", "fomo_buys.json", "degencapital_buys.json"):
    p = D / f
    if p.exists():
        d = json.load(open(p)); buys += d["buys"] if isinstance(d, dict) else d
active = json.load(open(Path(__file__).parent.parent / "wallets.json"))["wallets"]
active = {w["address"]: w["handle"] for w in active if w.get("active", True)}

def prof(bs):
    pre, first, tpeak, peak, firstret = [], [], [], [], []
    for b in bs:
        c = (prices.get(b["mint"]) or {}).get("candles") or []
        t = b["block_time"]
        before = [x for x in c if t - 3600 <= x[0] < t]
        after = [x for x in c if t <= x[0] < t + 3600]
        if not after: continue
        entry = after[0][1]
        if not entry: continue
        if before: pre.append(entry / min(x[3] for x in before) - 1)
        hi = max(after, key=lambda x: x[2]); peak.append(hi[2] / entry - 1); tpeak.append(hi[0] - t)
        fp = next((x for x in after if x[4] / entry - 1 > COST), None)
        if fp: first.append(fp[0] - t + 60); firstret.append(fp[4] / entry - 1)
    n = len(peak)
    if n == 0: return {"n": 0}
    m = lambda a, d=None: round(st.median(a), 3) if a else d
    return {"n": n, "pre_run_med": m(pre), "first_profit_s_med": m(first),
            "first_profit_hit_rate": round(len(first) / n, 2), "first_profit_ret_med": m(firstret),
            "t_peak_s_med": m(tpeak), "peak_ret_med": m(peak)}

def params(p):
    # defaults = scalper profile when data is thin
    if p.get("n", 0) < 3:
        return {"max_entry_delay_s": 20, "tp_first_pct": 0.08, "max_hold_s": 300, "basis": "default (n<3)"}
    tp = max(0.05, min(0.5, (p["first_profit_ret_med"] or 0.08)))
    hold = int(max(60, min(3600, (p["t_peak_s_med"] or 300))))
    delay = 15 if (p["pre_run_med"] or 0) > 0.5 else 30  # late chasers -> must be faster
    return {"max_entry_delay_s": delay, "tp_first_pct": round(tp, 3), "max_hold_s": hold, "basis": f"n={p['n']}"}

out = {}
for a, h in active.items():
    p = prof([b for b in buys if b.get("wallet") == a])
    out[a] = {"handle": h, "stats": p, "params": params(p)}
json.dump(out, open(Path(__file__).parent.parent / "trader_timing.json", "w"), indent=1)
for a, v in out.items(): print(v["handle"], v["stats"], v["params"])
