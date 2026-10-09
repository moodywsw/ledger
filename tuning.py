"""
tuning.py — walk-forward tuning of exit parameters (learning loop, part c).

Grid-searches a small set of exit parameters (risk_engine.TUNABLE_BOUNDS)
on recorded copy signals + 1-minute candles, using the SAME exit engine
the bot runs live (backtest.replay.exits_v2 -> risk_engine.evaluate_exit).

Walk-forward, so it can't just memorise the sample:
  signals sorted by time, 3 expanding folds
     train [0, 50%)  -> validate [50, 67%)
     train [0, 67%)  -> validate [67, 83%)
     train [0, 83%)  -> validate [83, 100%)
  in each fold the best grid point ON TRAIN is scored on the next, unseen
  slice. The tuned set is accepted only if those out-of-sample results beat
  the CURRENT parameters on the same slices by ≥ TUNE_MIN_IMPROVEMENT and
  there are ≥ TUNE_MIN_OOS out-of-sample trades. Otherwise nothing changes.
  Sizes, filters and every hard rail are out of scope by design.

Run offline on the backtest data:
    python -m tuning --profile degen
"""
from __future__ import annotations

import argparse
import itertools
import json
import os
import statistics
import time
from pathlib import Path

from risk_engine import RiskConfig, apply_tuned, tuned_params_path

TUNE_MIN_IMPROVEMENT = float(os.environ.get("TUNE_MIN_IMPROVEMENT", "0.01"))  # +1pp per trade, out of sample
TUNE_MIN_OOS = int(os.environ.get("TUNE_MIN_OOS", "20"))
LATENCY_S = 45

GRID = {
    "stop_loss_pct": [0.15, 0.20, 0.25, 0.30, 0.35],
    "tp1_gain": [0.20, 0.30, 0.50],
    "tp1_fraction": [0.33, 0.50],
    "time_stop_minutes": [20.0, 30.0, 45.0, 60.0],
    "trailing_stop_pct": [0.20, 0.25, 0.30],
}


def _trade_return(exits, entry, cost):
    remaining, ret = 1.0, 0.0
    for _, px, frac, _reason in exits:
        sold = remaining * min(1.0, frac)
        ret += sold * ((px or 0.0) * (1 - cost) / (entry * (1 + cost)) - 1)
        remaining -= sold
        if remaining <= 1e-9:
            break
    return ret


def per_signal_returns(signals, paths, cfg: RiskConfig, latency=LATENCY_S):
    """[(block_time, return)] for every signal with price data."""
    from backtest.replay import exits_v2
    out = []
    for s in signals:
        path = paths.get(s["mint"])
        if not path or not s.get("block_time"):
            continue
        t_entry = s["block_time"] + latency
        entry, _ = path.price_at(t_entry)
        if not entry:
            continue
        ex = exits_v2(entry, path, t_entry, cfg)
        out.append((s["block_time"], _trade_return(ex, entry, cfg.paper_cost_per_side_pct)))
    return out


def _grid_points():
    keys = list(GRID)
    for vals in itertools.product(*(GRID[k] for k in keys)):
        yield dict(zip(keys, vals))


def walk_forward(signals, paths, base: RiskConfig, folds=((0.50, 0.67), (0.67, 0.83), (0.83, 1.0))):
    signals = sorted([s for s in signals if s.get("block_time")], key=lambda s: s["block_time"])
    n = len(signals)
    # precompute every grid point's per-signal returns once
    points = list(_grid_points())
    rets = []
    for p in points:
        cfg = apply_tuned(base, p)
        rets.append(dict(per_signal_returns(signals, paths, cfg)))
    cur = dict(per_signal_returns(signals, paths, base))
    times = [s["block_time"] for s in signals]

    def mean_in(r, lo, hi):
        xs = [r[t] for t in times[int(lo * n):int(hi * n)] if t in r]
        return (statistics.mean(xs) if xs else None), len(xs)

    oos_tuned, oos_cur, chosen = [], [], []
    for tr_hi, va_hi in folds:
        best_i, best_v = None, None
        for i, r in enumerate(rets):
            v, k = mean_in(r, 0, tr_hi)
            if v is not None and k >= 10 and (best_v is None or v > best_v):
                best_i, best_v = i, v
        if best_i is None:
            continue
        vt = [rets[best_i][t] for t in times[int(tr_hi * n):int(va_hi * n)] if t in rets[best_i]]
        vc = [cur[t] for t in times[int(tr_hi * n):int(va_hi * n)] if t in cur]
        oos_tuned += vt
        oos_cur += vc
        chosen.append({"fold": f"train<{tr_hi:.0%} val<{va_hi:.0%}", "params": points[best_i],
                       "train_mean": best_v, "val_mean": statistics.mean(vt) if vt else None,
                       "val_current": statistics.mean(vc) if vc else None, "val_n": len(vt)})
    # final candidate: best on ALL data (what would be deployed)
    full = [(statistics.mean(r.values()) if r else -9, i) for i, r in enumerate(rets)]
    best_full = points[max(full)[1]] if full else {}
    t_mean = statistics.mean(oos_tuned) if oos_tuned else None
    c_mean = statistics.mean(oos_cur) if oos_cur else None
    accepted = (t_mean is not None and c_mean is not None and len(oos_tuned) >= TUNE_MIN_OOS
                and t_mean >= c_mean + TUNE_MIN_IMPROVEMENT)
    return {"profile": base.profile, "ts": time.time(), "n_signals": n, "n_priced": len(cur),
            "oos_mean_tuned": t_mean, "oos_mean_current": c_mean, "oos_n": len(oos_tuned),
            "folds": chosen, "params": best_full, "accepted": bool(accepted),
            "in_sample_mean_current": statistics.mean(cur.values()) if cur else None,
            "in_sample_mean_tuned": max(full)[0] if full else None}


def save_result(res: dict, path: Path | None = None):
    path = path or tuned_params_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    if res["accepted"]:
        path.write_text(json.dumps(res, indent=1))
    else:
        # keep any previously accepted set; just log the attempt next to it
        path.with_name("tuning_last_attempt.json").write_text(json.dumps(res, indent=1))


def main():
    from backtest.replay import PricePath
    ap = argparse.ArgumentParser()
    ap.add_argument("--profile", default="conservative")
    ap.add_argument("--buys", default="backtest/data/wallet_buys.json")
    ap.add_argument("--prices", default="backtest/data/prices.json")
    ap.add_argument("--out", default=None)
    args = ap.parse_args()
    buys = json.loads(Path(args.buys).read_text())["buys"]
    seen, signals = set(), []
    for b in sorted(buys, key=lambda b: b.get("block_time") or 0):
        k = (b["wallet"], b["mint"])
        if k not in seen:
            seen.add(k)
            signals.append(b)
    prices = json.loads(Path(args.prices).read_text())
    paths = {m: PricePath(r["candles"]) for m, r in prices.items() if r.get("candles")}
    os.environ["LEARNING_APPLY_TUNED"] = "false"  # tune against the untuned profile
    base = RiskConfig.from_env(args.profile)
    res = walk_forward(signals, paths, base)
    print(json.dumps({k: v for k, v in res.items() if k != "folds"}, indent=1))
    for f in res["folds"]:
        print(json.dumps(f))
    if args.out:
        Path(args.out).write_text(json.dumps(res, indent=1))


if __name__ == "__main__":
    main()
