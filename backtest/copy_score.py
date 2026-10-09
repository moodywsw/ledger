"""
copy_score.py — re-rank wallets by what COPYING them would have made us,
not by their own PnL. For each wallet's recent buys (collect_wallet_buys)
and minute candles (fetch_prices) it simulates our copy with a given
profile's exits at a given detection latency, after costs, and reports:

  n, win rate, mean return / copy, shrunk R (mean R pulled toward 0 with
  EDGE_PRIOR_TRADES pseudo-trades, same as the live sizing), and raw
  forward returns +5m / +30m from our fill.

A trader can be +$6M on the leaderboard and still be uncopyable: big
size, slow exits, buys we can only fill after the move. This measures
the copy, which is the only thing the bot can actually trade.

    python -m backtest.copy_score --buys backtest/data/fomo_buys.json \
        --prices backtest/data/fomo_prices.json --profile scalper --latency 15 \
        --update backtest/data/fomo_candidates.json
"""
from __future__ import annotations

import argparse
import json
import statistics
from collections import defaultdict
from pathlib import Path

from backtest.replay import PricePath
from risk_engine import RiskConfig
from tuning import per_signal_returns


def score(signals, paths, cfg: RiskConfig, latency: float) -> dict:
    by_w = defaultdict(list)
    for s in signals:
        by_w[s["wallet"]].append(s)
    out = {}
    for w, sigs in by_w.items():
        rets = [r for _, r in per_signal_returns(sigs, paths, cfg, latency)]
        fwd5, fwd30 = [], []
        for s in sigs:
            p = paths.get(s["mint"])
            if not p:
                continue
            p0, _ = p.price_at(s["block_time"] + latency)
            if not p0:
                continue
            a, _ = p.price_at(s["block_time"] + latency + 300)
            b, _ = p.price_at(s["block_time"] + latency + 1800)
            if a:
                fwd5.append(a / p0 - 1)
            if b:
                fwd30.append(b / p0 - 1)
        n = len(rets)
        r_mult = [x / cfg.stop_loss_pct for x in rets]
        out[w] = {
            "handle": sigs[0].get("handle", w[:6]), "signals": len(sigs), "n": n,
            "win_rate": (sum(1 for x in rets if x > 0) / n) if n else None,
            "mean_ret": statistics.mean(rets) if n else None,
            "shrunk_r": (sum(r_mult) / (n + cfg.edge_prior_trades)) if n else None,
            "fwd5_median": statistics.median(fwd5) if fwd5 else None,
            "fwd30_median": statistics.median(fwd30) if fwd30 else None,
        }
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--buys", required=True)
    ap.add_argument("--prices", required=True)
    ap.add_argument("--profile", default="scalper")
    ap.add_argument("--latency", type=float, default=15)
    ap.add_argument("--update", help="candidates json to annotate with copy_score (shrunk R)")
    ap.add_argument("--json-out")
    args = ap.parse_args()
    buys = sorted([b for b in json.loads(Path(args.buys).read_text())["buys"] if b.get("block_time")],
                  key=lambda b: b["block_time"])
    seen, signals = set(), []
    for b in buys:
        k = (b["wallet"], b["mint"])
        if k not in seen:
            seen.add(k)
            signals.append(b)
    prices = json.loads(Path(args.prices).read_text())
    paths = {m: PricePath(r["candles"]) for m, r in prices.items() if r.get("candles")}
    cfg = RiskConfig.from_env(args.profile)
    res = score(signals, paths, cfg, args.latency)
    rows = sorted(res.items(), key=lambda kv: -(kv[1]["shrunk_r"] if kv[1]["shrunk_r"] is not None else -99))
    f = lambda x: "—" if x is None else f"{x:+.1%}"
    print(f"profile={args.profile} latency={args.latency:.0f}s  (per-copy returns after {cfg.paper_cost_per_side_pct:.1%}/side costs)")
    print("| handle | signals | copies | win | mean/copy | shrunk R | fwd +5m med | fwd +30m med |")
    print("|---|---|---|---|---|---|---|---|")
    for w, r in rows:
        wr = "—" if r["win_rate"] is None else f"{r['win_rate']:.0%}"
        sr = "—" if r["shrunk_r"] is None else f"{r['shrunk_r']:+.2f}"
        print(f"| {r['handle']} | {r['signals']} | {r['n']} | {wr} | {f(r['mean_ret'])} | {sr} | {f(r['fwd5_median'])} | {f(r['fwd30_median'])} |")
    if args.json_out:
        Path(args.json_out).write_text(json.dumps(res, indent=1))
    if args.update:
        doc = json.loads(Path(args.update).read_text())
        for c in doc["candidates"]:
            scores = [res[w] for w in c.get("solana_wallets", []) if w in res and res[w]["n"]]
            if scores:
                best = max(scores, key=lambda r: r["shrunk_r"])
                c["copy_score"] = round(best["shrunk_r"], 3)
                c["copy_stats"] = {k: best[k] for k in ("n", "win_rate", "mean_ret", "fwd5_median", "fwd30_median")}
                c["copy_score_basis"] = f"{args.profile} exits @ {args.latency:.0f}s latency"
        Path(args.update).write_text(json.dumps(doc, indent=1))


if __name__ == "__main__":
    main()
