"""
replay.py — measures OLD vs NEW trading rules on the same historical signals.

Two modes:

1) Signal replay (default): every buy the tracked wallets made
   (backtest/data/wallet_buys.json, from collect_wallet_buys.py) is fed
   through each rule set against real 1-minute candles
   (backtest/data/prices.json, from fetch_prices.py). Same signals, same
   prices, same detection latency — only the rules differ.

     python -m backtest.replay

2) Trade-log metrics: summarize an existing paper trade log
   (ledger_state.json from the Railway volume) — win rate, expectancy,
   drawdown, by source / wallet / exit reason.

     python -m backtest.replay --trade-log /path/to/ledger_state.json

Rule sets replayed (all priority-copy, since every tracked wallet is
"priority": true in wallets.json — that is the strategy actually live):

  legacy_paper   check_open_positions(): SL -25%, sell 20% of what's left
                 at every 2x, recover capital at +40% then 20% trailing
                 stop; 8% x LLM-confidence sizing capped 0.5 SOL; checked
                 once per ~60-90s poll; NO fees (what the dashboard showed).
  legacy_cupsey  real-only ladder: TP1 +20% sells 50%, SL -35%, hard exit
                 after 60s; 8% x confidence sizing (cap 30% of wallet).
  new_v2         risk_engine (shipped defaults): entry filters (chase, market cap, mint/freeze
                 authority, wallet scoring), portfolio limits, risk-based
                 sizing, hard stop / TP ladder / breakeven / trailing /
                 time stop.

Honest limits: candles are 1-minute (intra-minute order assumed O-L-H-C on
red candles, O-H-L-C on green); historical liquidity and holder
concentration aren't available for free, so those two filters are NOT
replayed; LLM confidence is assumed at LEGACY_ASSUMED_CONFIDENCE; real
fills could be worse than candle prices on thin pools.
"""
from __future__ import annotations

import argparse
import dataclasses
import json
import math
import re
import statistics
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from risk_engine import (  # noqa: E402
    RiskConfig, PortfolioSnapshot, TokenSafetyInfo, check_portfolio_limits, check_token_safety,
    position_size, evaluate_exit, new_exit_state, score_wallets, consecutive_losses, utc_day,
)

START_EQUITY = 10.0                  # SOL, same as STARTING_PAPER_BALANCE_SOL
DETECTION_LATENCY_S = 45             # avg: half a 60s poll + RPC + LLM call before the fill
LEGACY_ASSUMED_CONFIDENCE = 2.0      # LLM multiplier (prompt pushes it above 1.0); 1.0-5.0 range
LEGACY_COST_PER_SIDE = 0.015         # applied to "*_with_costs" variants of legacy for a fair comparison


# ── price path helpers ───────────────────────────────────────────────

def candle_ticks(c):
    ts, o, h, l, cl = c[0], float(c[1]), float(c[2]), float(c[3]), float(c[4])
    path = (o, l, h, cl) if cl < o else (o, h, l, cl)
    return [(ts + i * 15, p) for i, p in enumerate(path)]


class PricePath:
    def __init__(self, candles):
        self.candles = candles
        self.ts = [c[0] for c in candles]

    def first_index_at_or_after(self, t):
        import bisect
        return bisect.bisect_left(self.ts, t)

    def price_at(self, t):
        """Open of the first candle at/after t (≈ market price when we'd actually fill)."""
        i = self.first_index_at_or_after(t - 59)
        if i >= len(self.candles) or self.candles[i][0] - t > 300:
            return None, None
        c = self.candles[i]
        # inside the candle containing t: use open if we're at its start, else close
        return (float(c[1]) if c[0] >= t else float(c[4])), c[0]

    def ticks_from(self, t):
        i = self.first_index_at_or_after(t)
        for c in self.candles[i:]:
            for tick in candle_ticks(c):
                if tick[0] >= t:
                    yield tick


# ── exit rule sets (per position, independent of portfolio) ─────────

def exits_legacy_paper(entry, path, t_entry, check_every=75):
    """Returns list of (t, price, fraction_of_remaining, reason)."""
    out, remaining, recovered, peak, peeled = [], 1.0, False, entry, 0
    last_check = t_entry
    for t, p in path.ticks_from(t_entry + check_every):
        if t - last_check < check_every:
            continue
        last_check = t
        change = p / entry - 1
        if change <= -0.25:
            out.append((t, p, 1.0, "stop_loss")); return out
        mult = 1 + change
        nxt = 2 ** (peeled + 1)
        while mult >= nxt:
            out.append((t, p, 0.20, f"peel_{nxt}x")); remaining *= 0.8; peeled += 1; nxt *= 2
        if not recovered:
            if change >= 0.40:
                f = 1 / mult
                out.append((t, p, f, "capital_recovered")); remaining *= (1 - f)
                recovered, peak = True, p
        else:
            peak = max(peak, p)
            if (peak - p) / peak >= 0.20:
                out.append((t, p, 1.0, "trailing_stop")); return out
        if t - t_entry > 7 * 86400:
            break
    if path.candles:
        c = path.candles[-1]
        out.append((c[0], float(c[4]), 1.0, "end_of_data"))
    return out


def exits_legacy_cupsey(entry, path, t_entry, check_every=30):
    out, tp1 = [], False
    last_check = t_entry
    for t, p in path.ticks_from(t_entry + check_every):
        if t - last_check < check_every and t - t_entry < 60:
            continue
        last_check = t
        held = t - t_entry
        if held >= 60:
            out.append((t, p, 1.0, "time_exit_60s")); return out
        change = p / entry - 1
        if change <= -0.35:
            out.append((t, p, 1.0, "stop_loss")); return out
        if not tp1 and p / entry >= 1.20:
            out.append((t, p, 0.5, "tp1_20pct")); tp1 = True
        elif tp1 and p / entry >= 4.0:
            out.append((t, p, 1.0, "tp2_4x")); return out
    if path.candles:
        c = path.candles[-1]
        out.append((c[0], float(c[4]), 1.0, "end_of_data"))
    return out


def exits_v2(entry, path, t_entry, cfg: RiskConfig, check_every=15):
    pos = new_exit_state(entry, 1.0, t_entry)
    out = []
    for t, p in path.ticks_from(t_entry + check_every):
        actions, updates = evaluate_exit(pos, p, t, cfg)
        pos.update(updates)
        for a in actions:
            out.append((t, p, a["fraction"], a["reason"]))
            pos["size"] *= (1 - a["fraction"])
            if a["fraction"] >= 0.999:
                return out
    # ran out of data while still open: evaluate the no-price write-off rule
    if path.candles:
        c = path.candles[-1]
        out.append((c[0], float(c[4]), 1.0, "end_of_data"))
    return out


def normalize_reason(reason):
    r = reason.split(" (")[0].split(" — ")[0]
    if r.startswith("price already"):
        return "chasing: price already ran > max_chase above the wallet's fill"
    return re.sub(r"(?<![-\w])[-+]?\d+(\.\d+)?%?", "N", r)


def signal_quality(buys, prices, latency, horizons=(300, 1800, 7200)):
    """Raw forward returns from our (delayed) fill price, per wallet — how good the signals are before any rules."""
    rows = defaultdict(lambda: {h: [] for h in horizons})
    for b in buys:
        rec = prices.get(b["mint"])
        if not rec or not rec.get("candles"):
            continue
        path = rec["_path"]
        p0, _ = path.price_at(b["block_time"] + latency)
        if not p0:
            continue
        for h in horizons:
            p1, _ = path.price_at(b["block_time"] + latency + h)
            if p1:
                rows[b.get("handle", b["wallet"][:6])][h].append(p1 / p0 - 1)
                rows["ALL"][h].append(p1 / p0 - 1)
    out = {}
    for k, v in rows.items():
        out[k] = {f"+{h // 60}m": {"n": len(x), "median": statistics.median(x) if x else None,
                                   "pct_up": (sum(1 for r in x if r > 0) / len(x)) if x else None}
                  for h, x in v.items()}
    return out


# ── portfolio simulation ─────────────────────────────────────────────

@dataclasses.dataclass
class Trade:
    mint: str
    wallet: str
    handle: str
    t_open: float
    t_close: float
    size: float
    pnl: float
    pnl_pct: float
    reason: str
    fills: list


def simulate(name, buys, prices, rules, cfg: RiskConfig | None, sizing, cost_per_side, use_filters, latency=None):
    """
    Walks signals chronologically through one rule set. Positions are
    resolved with their exit schedule up front (exits don't depend on
    other positions), and cash/equity are rebuilt from fill events so the
    portfolio limits see the real book at each entry time.
    """
    cash = START_EQUITY
    open_pos = {}            # mint -> dict(size, close_t, ...)
    trades: list[Trade] = []
    blocked = defaultdict(int)
    events = []              # (t, cash_delta) for equity curve
    entries_ts = []
    token_last_exit = {}

    def settle_until(t):
        nonlocal cash
        for m in list(open_pos):
            p = open_pos[m]
            while p["fills"] and p["fills"][0][0] <= t:
                ft, fp, frac, reason = p["fills"].pop(0)
                qty = p["remaining"] * frac
                proceeds = qty * fp * (1 - cost_per_side)
                cash += proceeds
                p["proceeds"] += proceeds
                p["remaining"] -= qty
                p["last_reason"] = reason
                events.append((ft, cash, m))
                if frac >= 0.999 or p["remaining"] <= 1e-12:
                    pnl = p["proceeds"] - p["size"]
                    trades.append(Trade(m, p["wallet"], p["handle"], p["t_open"], ft, p["size"], pnl,
                                        pnl / p["size"], reason, p["fill_log"]))
                    token_last_exit[m] = ft
                    del open_pos[m]
                    break

    def equity_at(t):
        eq = cash
        for m, p in open_pos.items():
            px, _ = prices[m]["_path"].price_at(t)
            if px is not None:
                eq += p["remaining"] * px
            else:
                eq += p["size"]
        return eq

    latency = DETECTION_LATENCY_S if latency is None else latency
    day_start = {}
    for b in buys:
        t_sig = b["block_time"]
        t_fill = t_sig + latency
        settle_until(t_fill)
        mint = b["mint"]
        rec = prices.get(mint)
        if not rec or not rec.get("candles"):
            blocked["no price data"] += 1
            continue
        path = rec["_path"]
        px, _ = path.price_at(t_fill)
        if px is None or px <= 0:
            blocked["no price at fill time"] += 1
            continue
        if mint in open_pos:
            blocked["already holding"] += 1
            continue

        eq = equity_at(t_fill)
        day = utc_day(t_fill)
        day_start.setdefault(day, eq)

        if use_filters:
            closed = [{"wallet": tr.wallet, "pnl_pct": tr.pnl_pct, "closed_ts": tr.t_close, "mint": tr.mint}
                      for tr in trades if tr.t_close <= t_fill]
            score = score_wallets(closed, t_fill, cfg).get(b["wallet"])
            if score and not score["enabled"]:
                blocked["wallet benched by scoring"] += 1
                continue
            size = position_size(eq, cfg)
            streak, last_loss = consecutive_losses(sorted(closed, key=lambda x: x["closed_ts"]))
            realized_today = sum(tr.pnl for tr in trades if utc_day(tr.t_close) == day and tr.t_close <= t_fill)
            snap = PortfolioSnapshot(
                equity=eq, cash=cash, exposure=sum(p["size"] for p in open_pos.values()),
                open_positions=len(open_pos), day_start_equity=day_start[day],
                realized_pnl_today=realized_today, consecutive_losses=streak, last_loss_ts=last_loss,
                trades_last_hour=len([x for x in entries_ts if t_fill - x < 3600]),
                token_last_exit_ts=token_last_exit, held_tokens=set(open_pos),
            )
            ok, reason = check_portfolio_limits(snap, mint, size, t_fill, cfg)
            if not ok:
                blocked[normalize_reason(reason)] += 1
                continue
            wallet_fill = None
            if b.get("usd_spent") and b.get("amount"):
                wallet_fill = b["usd_spent"] / b["amount"]
            wallet_fill_mkt, _ = path.price_at(t_sig)
            ref = wallet_fill or wallet_fill_mkt
            mcap = None
            if rec.get("fdv_usd_now") and rec.get("price_usd_now"):
                mcap = px * rec["fdv_usd_now"] / rec["price_usd_now"]
            info = TokenSafetyInfo(
                liquidity_usd=None, market_cap_usd=mcap, top10_pct=None,
                mint_authority=rec.get("mint_authority"), freeze_authority=rec.get("freeze_authority"),
                extensions=rec.get("extensions") or [], signal_age_seconds=latency,
                chase_pct=(px / ref - 1) if ref else None,
            )
            ok, reason = check_token_safety(info, 0.0, dataclasses.replace(cfg, fail_closed_on_missing_data=False))
            if not ok:
                blocked[normalize_reason(reason)] += 1
                continue
        else:
            size = min(max(0.005, cash * 0.08 * LEGACY_ASSUMED_CONFIDENCE), sizing["cap_abs"], eq * sizing["cap_pct"])
            exposure = sum(p["size"] for p in open_pos.values())
            if size > cash or len(open_pos) >= sizing["max_concurrent"] or (exposure + size) / eq > sizing["max_exposure"]:
                blocked["legacy limits"] += 1
                continue
            if len([x for x in entries_ts if t_fill - x < 3600]) >= sizing["max_per_hour"]:
                blocked["legacy hourly limit"] += 1
                continue

        entry_px = px * (1 + cost_per_side)
        fills = rules(entry_px, path, t_fill)
        cash -= size
        entries_ts.append(t_fill)
        events.append((t_fill, cash, mint))
        open_pos[mint] = {"size": size, "remaining": size / entry_px, "fills": [(ft, fp, fr, rs) for ft, fp, fr, rs in fills],
                          "fill_log": list(fills), "proceeds": 0.0, "t_open": t_fill,
                          "wallet": b["wallet"], "handle": b.get("handle", b["wallet"][:6])}
    settle_until(float("inf"))

    # equity curve on realized cash after each close (positions are short-lived; a
    # mark-to-market curve at 1-min granularity is computed for drawdown below)
    return trades, dict(blocked), equity_curve_mtm(trades, prices)


def equity_curve_mtm(trades, prices):
    """Equity marked to market every 5 minutes across the whole replay window."""
    if not trades:
        return []
    t0 = min(t.t_open for t in trades)
    t1 = max(t.t_close for t in trades)
    curve = []
    step = 300
    t = t0
    while t <= t1 + step:
        realized = sum(tr.pnl for tr in trades if tr.t_close <= t)
        unreal = 0.0
        for tr in trades:
            if tr.t_open <= t < tr.t_close:
                px, _ = prices[tr.mint]["_path"].price_at(t)
                # mark open trades at cost +/- path move since entry (approximation:
                # ignores partial sells already taken, so it overstates swings)
                p0, _ = prices[tr.mint]["_path"].price_at(tr.t_open)
                if px and p0:
                    unreal += tr.size * (px / p0 - 1)
        curve.append((t, START_EQUITY + realized + unreal))
        t += step
    return curve


# ── metrics ──────────────────────────────────────────────────────────

def metrics(trades, curve=None, start_equity=START_EQUITY):
    n = len(trades)
    if n == 0:
        return {"trades": 0}
    wins = [t for t in trades if t.pnl > 0]
    losses = [t for t in trades if t.pnl <= 0]
    gross_win = sum(t.pnl for t in wins)
    gross_loss = -sum(t.pnl for t in losses)
    total = sum(t.pnl for t in trades)
    mdd = 0.0
    if curve:
        peak = curve[0][1]
        for _, eq in curve:
            peak = max(peak, eq)
            mdd = min(mdd, (eq - peak) / peak if peak > 0 else 0)
    return {
        "trades": n,
        "win_rate": len(wins) / n,
        "avg_win_pct": (sum(t.pnl_pct for t in wins) / len(wins)) if wins else 0.0,
        "avg_loss_pct": (sum(t.pnl_pct for t in losses) / len(losses)) if losses else 0.0,
        "expectancy_pct": sum(t.pnl_pct for t in trades) / n,
        "profit_factor": (gross_win / gross_loss) if gross_loss > 0 else math.inf,
        "total_pnl_sol": total,
        "return_pct": total / start_equity,
        "max_drawdown_pct": mdd,
        "avg_hold_min": sum(t.t_close - t.t_open for t in trades) / n / 60,
        "median_hold_min": sorted(t.t_close - t.t_open for t in trades)[n // 2] / 60,
        "worst_trade_pct": min(t.pnl_pct for t in trades),
        "best_trade_pct": max(t.pnl_pct for t in trades),
    }


def breakdown(trades, key):
    groups = defaultdict(list)
    for t in trades:
        groups[key(t)].append(t)
    return {k: {"n": len(v), "win_rate": sum(1 for t in v if t.pnl > 0) / len(v),
                "expectancy_pct": sum(t.pnl_pct for t in v) / len(v), "pnl_sol": sum(t.pnl for t in v)}
            for k, v in sorted(groups.items(), key=lambda kv: -len(kv[1]))}


def fmt_pct(x):
    return "∞" if x == math.inf else f"{x * 100:+.1f}%"


def print_table(results):
    cols = ["trades", "win_rate", "avg_win_pct", "avg_loss_pct", "expectancy_pct", "profit_factor",
            "total_pnl_sol", "return_pct", "max_drawdown_pct", "median_hold_min", "worst_trade_pct"]
    lines = ["| metric | " + " | ".join(results) + " |", "|---|" + "---|" * len(results)]
    for c in cols:
        row = []
        for name in results:
            v = results[name]["metrics"].get(c)
            if v is None:
                row.append("—")
            elif c in ("trades",):
                row.append(str(v))
            elif c == "profit_factor":
                row.append("∞" if v == math.inf else f"{v:.2f}")
            elif c in ("total_pnl_sol",):
                row.append(f"{v:+.3f}")
            elif c.endswith("_min"):
                row.append(f"{v:.1f}")
            else:
                row.append(fmt_pct(v) if c != "win_rate" else f"{v * 100:.0f}%")
        lines.append(f"| {c} | " + " | ".join(row) + " |")
    return "\n".join(lines)


# ── trade-log mode ───────────────────────────────────────────────────

def trade_log_metrics(state_path):
    """Metrics from a real ledger_state.json — uses closed_positions when present, else pairs trade_log opens/closes."""
    from datetime import datetime
    st = json.loads(Path(state_path).read_text())
    trades = []
    if st.get("closed_positions"):
        for c in st["closed_positions"]:
            trades.append(Trade(c["mint"], c.get("wallet", ""), c.get("wallet", "")[:6], c.get("opened_ts") or 0,
                                c.get("closed_ts") or 0, c.get("invested_sol") or 0, c.get("pnl_sol") or 0,
                                c.get("pnl_pct") or 0, c.get("reason") or "", []))
    else:
        opens = {}
        for e in st.get("trade_log", []):
            ts = datetime.fromisoformat(e["at"]).timestamp() if e.get("at") else 0
            if e["action"] == "open":
                opens[e["token"]] = {"t": ts, "size": e["size_sol"], "pnl": 0.0, "wallet": e.get("opened_by", "")}
            elif e["action"] in ("partial_close", "close") and e["token"] in opens:
                o = opens[e["token"]]
                o["pnl"] += e.get("pnl_sol", 0)
                if e["action"] == "close":
                    trades.append(Trade(e["token"], o["wallet"], o["wallet"][:6], o["t"], ts, o["size"], o["pnl"],
                                        o["pnl"] / o["size"] if o["size"] else 0, e.get("reason") or e.get("risk_level", ""), []))
                    del opens[e["token"]]
    trades.sort(key=lambda t: t.t_close)
    eq, curve = START_EQUITY, []
    for t in trades:
        eq += t.pnl
        curve.append((t.t_close, eq))
    return {
        "metrics": metrics(trades, [(0, START_EQUITY)] + curve),
        "by_wallet": breakdown(trades, lambda t: t.wallet or "—"),
        "by_reason": breakdown(trades, lambda t: t.reason),
    }


# ── main ─────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--buys", default="backtest/data/wallet_buys.json")
    ap.add_argument("--prices", default="backtest/data/prices.json")
    ap.add_argument("--trade-log", help="ledger_state.json to summarize instead of replaying signals")
    ap.add_argument("--out", default="backtest/data/replay_results.json")
    ap.add_argument("--latency", type=float, default=DETECTION_LATENCY_S, help="seconds from the wallet's buy to our fill")
    args = ap.parse_args()

    if args.trade_log:
        print(json.dumps(trade_log_metrics(args.trade_log), indent=2, default=str))
        return

    buys = json.loads(Path(args.buys).read_text())["buys"]
    prices = json.loads(Path(args.prices).read_text())
    for rec in prices.values():
        rec["_path"] = PricePath(rec.get("candles") or [])
    buys = sorted([b for b in buys if b.get("block_time")], key=lambda b: b["block_time"])
    # one signal per (wallet, mint): repeat buys of the same token by the same wallet are scale-ins, not new signals
    seen, signals = set(), []
    for b in buys:
        k = (b["wallet"], b["mint"])
        if k in seen:
            continue
        seen.add(k)
        signals.append(b)

    cfg = RiskConfig()  # defaults = what ships
    legacy_paper_limits = {"cap_abs": 0.5, "cap_pct": 1.0, "max_concurrent": 8, "max_exposure": 0.25, "max_per_hour": 10}
    legacy_real_limits = {"cap_abs": 1e9, "cap_pct": 0.30, "max_concurrent": 99, "max_exposure": 0.85, "max_per_hour": 10**6}
    runs = {
        "legacy_paper (no fees, as dashboard)": (lambda e, p, t: exits_legacy_paper(e, p, t), None, legacy_paper_limits, 0.0, False),
        "legacy_paper (+fees)": (lambda e, p, t: exits_legacy_paper(e, p, t), None, legacy_paper_limits, LEGACY_COST_PER_SIDE, False),
        "legacy_cupsey real-only (+fees)": (lambda e, p, t: exits_legacy_cupsey(e, p, t), None, legacy_real_limits, LEGACY_COST_PER_SIDE, False),
        "new_v2 (+fees)": (lambda e, p, t: exits_v2(e, p, t, cfg), cfg, None, cfg.paper_cost_per_side_pct, True),
        "new_v2 exits only, no filters (+fees)": (lambda e, p, t: exits_v2(e, p, t, cfg), cfg, legacy_paper_limits, cfg.paper_cost_per_side_pct, False),
    }
    results = {}
    for name, (rules, rcfg, sizing, cost, filters) in runs.items():
        trades, blocked, curve = simulate(name, signals, prices, rules, rcfg, sizing, cost, filters, args.latency)
        results[name] = {
            "metrics": metrics(trades, curve),
            "blocked": blocked,
            "by_exit_reason": breakdown(trades, lambda t: t.reason),
            "by_wallet": breakdown(trades, lambda t: t.handle),
        }
    sweep = {}
    for lat in (10, 30, 45, 90, 180):
        sweep[lat] = {}
        for name in ("legacy_paper (+fees)", "new_v2 (+fees)"):
            rules, rcfg, sizing, cost, filters = runs[name]
            tr, _, cv = simulate(name, signals, prices, rules, rcfg, sizing, cost, filters, lat)
            m = metrics(tr, cv)
            sweep[lat][name] = {k: m.get(k) for k in ("trades", "win_rate", "expectancy_pct", "return_pct", "max_drawdown_pct")}
    quality = signal_quality(signals, prices, args.latency)
    span_days = (signals[-1]["block_time"] - signals[0]["block_time"]) / 86400 if signals else 0
    print(f"signals: {len(signals)} unique (wallet, token) buys over {span_days:.1f} days; "
          f"{sum(1 for b in signals if prices.get(b['mint'], {}).get('candles'))} with candle data\n")
    print(print_table(results))
    for name, r in results.items():
        print(f"\n## {name}\nblocked: {r['blocked']}\nby exit: {json.dumps(r['by_exit_reason'], default=str)}\nby wallet: {json.dumps(r['by_wallet'], default=str)}")
    print("\n## latency sweep (seconds from wallet buy to our fill)")
    for lat, r in sweep.items():
        print(lat, json.dumps(r, default=str))
    print("\n## raw signal quality (forward return from our fill, before any rules)")
    for k, v in sorted(quality.items(), key=lambda kv: -kv[1]["+5m"]["n"]):
        print(k, json.dumps(v, default=str))
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps({"signals": len(signals), "span_days": span_days, "latency": args.latency,
                                          "results": results, "latency_sweep": sweep, "signal_quality": quality},
                                         indent=1, default=str))


if __name__ == "__main__":
    main()
