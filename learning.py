"""
learning.py — Ledger's learning loop. Nothing here retrains a model: it
keeps score of what actually made money and moves capital toward it.

  (a) Scoring       wallets, signal sources and entry-feature buckets are
                    scored continuously from realized outcomes
                    (risk_engine.wallet_edge: recency-weighted, shrunk R).
  (b) Roster        wallet_roster.json overlays wallets.json:
                      candidate -> shadow -> active -> benched -> (shadow again)
                    Discovered wallets (top traders on winning tokens, Fomo
                    leaderboard) are SHADOW-traded on paper — no balance
                    impact — and only promoted once they show an edge.
                    Live wallets that lose their edge are benched and keep
                    being shadow-traded so they can earn their way back.
  (c) Tuning        tuning.py: walk-forward grid over exit parameters on
                    recorded signals, inside SAFE bounds; written to
                    tuned_params.json only if it beats the current set on
                    held-out data.
  (d) Journal       every promotion/demotion/discovery/tune is appended to
                    learning_journal.jsonl (and the main journal, kind
                    "learning") so the Discord bot can cite it.

Promotion only ever affects PAPER copies unless LEARNING_PROMOTE_TO_REAL=true.
All state is files under DATA_DIR; every public function swallows its
own errors (the trading loop must never die because of bookkeeping).
"""
from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path

from risk_engine import RiskConfig, wallet_edge, new_exit_state, evaluate_exit

DATA_DIR = Path(os.environ.get("DATA_DIR", "."))
ROSTER_FILE = DATA_DIR / "wallet_roster.json"
SHADOW_FILE = DATA_DIR / "shadow_book.json"
LEARNING_JOURNAL = DATA_DIR / "learning_journal.jsonl"
SIGNALS_FILE = DATA_DIR / "signals.jsonl"
SCORES_FILE = DATA_DIR / "learning_scores.json"


def _env_bool(n, d):
    return os.environ.get(n, str(d)).strip().lower() in ("1", "true", "yes", "on")


LEARNING_ENABLED = _env_bool("LEARNING_ENABLED", True)
LEARNING_PROMOTE_TO_REAL = _env_bool("LEARNING_PROMOTE_TO_REAL", False)
LEARNING_MAX_SHADOW_WALLETS = int(os.environ.get("LEARNING_MAX_SHADOW_WALLETS", "10"))
LEARNING_PROMOTE_MIN_TRADES = int(os.environ.get("LEARNING_PROMOTE_MIN_TRADES", "8"))
LEARNING_PROMOTE_MIN_R = float(os.environ.get("LEARNING_PROMOTE_MIN_R", "0.25"))   # shrunk avg R
LEARNING_DROP_MAX_R = float(os.environ.get("LEARNING_DROP_MAX_R", "-0.10"))
LEARNING_REINSTATE_MIN_NEW = int(os.environ.get("LEARNING_REINSTATE_MIN_NEW", "5"))
LEARNING_SHADOW_MAX_OPEN = int(os.environ.get("LEARNING_SHADOW_MAX_OPEN", "40"))
LEARNING_REVIEW_EVERY_MIN = float(os.environ.get("LEARNING_REVIEW_EVERY_MIN", "60"))
LEARNING_DISCOVERY_EVERY_MIN = float(os.environ.get("LEARNING_DISCOVERY_EVERY_MIN", "240"))
LEARNING_DISCOVERY_MIN_PROFIT_USD = float(os.environ.get("LEARNING_DISCOVERY_MIN_PROFIT_USD", "1000"))

_lock = threading.RLock()


# ── tiny file helpers ────────────────────────────────────────────────

def _read_json(path: Path, default):
    try:
        return json.loads(path.read_text())
    except Exception:
        return default


def _write_json(path: Path, data):
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(data))
        tmp.replace(path)  # atomic: a crash mid-write can't corrupt the roster
    except Exception as e:
        print(f"[LEARN] write {path.name} failed: {e}")


def _append_jsonl(path: Path, row: dict):
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row) + "\n")
    except Exception as e:
        print(f"[LEARN] append {path.name} failed: {e}")


def _read_jsonl(path: Path, since_ts: float = 0) -> list:
    out = []
    try:
        with path.open("r", encoding="utf-8") as f:
            for line in f:
                try:
                    r = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if (r.get("ts") or 0) >= since_ts:
                    out.append(r)
    except FileNotFoundError:
        pass
    return out


# ── (d) learning journal ─────────────────────────────────────────────

def note(event: str, text: str, **meta):
    """One learning event -> learning_journal.jsonl + main journal (kind 'learning')."""
    row = {"ts": time.time(), "event": event, "text": text, **meta}
    _append_jsonl(LEARNING_JOURNAL, row)
    try:
        from journal_store import log_journal
        log_journal(kind="learning", text=text, meta={"event": event, **meta})
    except Exception:
        pass
    print(f"[LEARN] {text}")


def recent_notes(limit: int = 20) -> list:
    rows = _read_jsonl(LEARNING_JOURNAL)
    return rows[-limit:][::-1]


# ── (b) roster ───────────────────────────────────────────────────────

def load_roster() -> dict:
    return _read_json(ROSTER_FILE, {})


def save_roster(roster: dict):
    _write_json(ROSTER_FILE, roster)


def wallet_status(wallet: str) -> str | None:
    """'active' | 'benched' | 'shadow' | 'candidate' | 'dropped' | None (not in roster = wallets.json default)."""
    if not LEARNING_ENABLED or not wallet:
        return None
    return (load_roster().get(wallet) or {}).get("status")


def shadow_wallets() -> dict:
    """{address: handle} of wallets to poll but only shadow-trade."""
    return {w: r.get("handle") or w[:6] for w, r in load_roster().items() if r.get("status") == "shadow"}


def promoted_wallets() -> dict:
    """{address: handle}: discovered wallets that earned 'active' (copied on paper; real only if LEARNING_PROMOTE_TO_REAL)."""
    return {w: r.get("handle") or w[:6] for w, r in load_roster().items()
            if r.get("status") == "active" and r.get("origin") != "wallets.json"}


def set_status(wallet: str, status: str, reason: str, handle: str | None = None, origin: str | None = None, **meta):
    with _lock:
        roster = load_roster()
        r = roster.get(wallet) or {"added_ts": time.time(), "origin": origin or "wallets.json"}
        old = r.get("status")
        if old == status:
            return False
        r.update(status=status, reason=reason, updated_ts=time.time())
        if handle:
            r["handle"] = handle
        if origin and not r.get("origin"):
            r["origin"] = origin
        r.update(meta)
        roster[wallet] = r
        save_roster(roster)
    note("status", f"{r.get('handle') or wallet[:6]}: {old or 'tracked'} → {status} — {reason}",
         wallet=wallet, old=old, new=status)
    return True


def add_candidate(wallet: str, handle: str, origin: str, evidence: str, known: set) -> bool:
    """Queue a discovered wallet for shadow trading (capped at LEARNING_MAX_SHADOW_WALLETS)."""
    if not wallet or wallet in known:
        return False
    roster = load_roster()
    if wallet in roster:
        return False
    n_shadow = sum(1 for r in roster.values() if r.get("status") == "shadow")
    status = "shadow" if n_shadow < LEARNING_MAX_SHADOW_WALLETS else "candidate"
    return set_status(wallet, status, f"discovered via {origin}: {evidence}", handle=handle, origin=origin)


# ── shadow book (paper-on-paper, no balance impact) ──────────────────

def _load_shadow() -> dict:
    return _read_json(SHADOW_FILE, {"open": {}, "closed": []})


def shadow_open(wallet: str, mint: str, price: float | None, symbol: str = "", source: str = "shadow"):
    if not LEARNING_ENABLED or not price:
        return
    with _lock:
        book = _load_shadow()
        key = f"{wallet}:{mint}"
        if key in book["open"] or len(book["open"]) >= LEARNING_SHADOW_MAX_OPEN:
            return
        now = time.time()
        st = new_exit_state(price, 1.0, now)
        st.update(wallet=wallet, mint=mint, symbol=symbol, source=source, realized=0.0)
        book["open"][key] = st
        _write_json(SHADOW_FILE, book)


def shadow_tick(price_fn, cfg: RiskConfig, now: float | None = None):
    """Run every open shadow position through the same exit engine as live.
    price_fn(list_of_mints) -> {mint: price_usd}."""
    if not LEARNING_ENABLED:
        return
    now = now or time.time()
    with _lock:
        book = _load_shadow()
        if not book["open"]:
            return
        prices = price_fn(sorted({p["mint"] for p in book["open"].values()})) or {}
        c = cfg.paper_cost_per_side_pct
        changed = False
        for key, pos in list(book["open"].items()):
            px = prices.get(pos["mint"])
            actions, updates = evaluate_exit(pos, px, now, cfg)
            if updates:
                pos.update(updates)
                changed = True
            for a in actions:
                sold = pos["size"] * min(1.0, a["fraction"])  # fraction of REMAINING size (evaluate_exit contract)
                exit_px = a.get("price_override", px) or 0.0
                pos["realized"] += sold * (exit_px * (1 - c) / (pos["entry_price"] * (1 + c)) - 1)
                pos["size"] = 0.0 if a["fraction"] >= 0.999 else pos["size"] - sold
                changed = True
                if pos["size"] <= 1e-6:
                    book["closed"].append({"wallet": pos["wallet"], "mint": pos["mint"], "symbol": pos.get("symbol"),
                                           "source": pos.get("source", "shadow"), "pnl_pct": pos["realized"],
                                           "opened_ts": pos.get("opened_ts"), "closed_ts": now,
                                           "reason": a.get("reason"), "shadow": True})
                    book["open"].pop(key, None)
                    break
        if changed:
            book["closed"] = book["closed"][-3000:]
            _write_json(SHADOW_FILE, book)


def shadow_closed_trades(since_days: float = 60) -> list:
    cut = time.time() - since_days * 86400
    return [t for t in _load_shadow()["closed"] if (t.get("closed_ts") or 0) >= cut]


# ── signal recorder (feeds tuning) ───────────────────────────────────

def record_signal(wallet: str, mint: str, block_time: float | None, source: str, handle: str = "",
                  price_usd: float | None = None, features: dict | None = None):
    if not LEARNING_ENABLED:
        return
    _append_jsonl(SIGNALS_FILE, {"ts": time.time(), "wallet": wallet, "handle": handle, "mint": mint,
                                 "block_time": block_time, "source": source, "price_usd": price_usd,
                                 "features": features or {}})


def recorded_signals(since_days: float = 21) -> list:
    return _read_jsonl(SIGNALS_FILE, time.time() - since_days * 86400)


# ── (a) scoring ──────────────────────────────────────────────────────

def feature_buckets(f: dict | None) -> dict:
    """Coarse buckets so each has enough samples to mean something."""
    f = f or {}
    out = {}
    liq = f.get("liquidity_usd")
    if liq is not None:
        out["liq"] = "<15k" if liq < 15e3 else "15-50k" if liq < 50e3 else "50-250k" if liq < 250e3 else "250k+"
    mc = f.get("market_cap_usd")
    if mc is not None:
        out["mcap"] = "<50k" if mc < 50e3 else "50-250k" if mc < 250e3 else "250k-1M" if mc < 1e6 else "1M+"
    age = f.get("signal_age_seconds")
    if age is not None:
        out["sig_age"] = "<30s" if age < 30 else "30-90s" if age < 90 else "90s+"
    ch = f.get("chase_pct")
    if ch is not None:
        out["chase"] = "<0" if ch < 0 else "0-10%" if ch < 0.10 else "10-25%" if ch < 0.25 else "25%+"
    ts = f.get("opened_ts")
    if ts:
        h = time.gmtime(ts).tm_hour
        out["utc_hour"] = f"{(h // 6) * 6:02d}-{(h // 6) * 6 + 6:02d}"
    return out


def score_everything(closed_trades: list, cfg: RiskConfig, now: float | None = None) -> dict:
    """Edge per wallet (live + shadow), per source, per feature bucket."""
    now = now or time.time()
    trades = [t for t in closed_trades if t.get("pnl_pct") is not None]
    out = {"ts": now,
           "wallets": wallet_edge([t for t in trades if t.get("wallet")], now, cfg, key="wallet"),
           "sources": wallet_edge([dict(t, source=("shadow" if t.get("shadow") else t.get("source") or "?")) for t in trades],
                                  now, cfg, key="source"),
           "features": {}}
    feat_rows = {}
    for t in trades:
        if t.get("shadow"):
            continue
        for k, v in feature_buckets(dict(t.get("features") or {}, opened_ts=t.get("opened_ts"))).items():
            feat_rows.setdefault(k, []).append(dict(t, bucket=v))
    for k, rows in feat_rows.items():
        out["features"][k] = wallet_edge(rows, now, cfg, key="bucket")
    return out


def review_roster(closed_live: list, cfg: RiskConfig, tracked: dict, now: float | None = None) -> list:
    """
    Promote / demote from measured edge. `tracked` = {address: handle} from
    wallets.json. Returns the list of status changes made.
    Rules (all on recency-weighted, shrunk avg R; see risk_engine.wallet_edge):
      tracked/active, tier 'benched'             -> benched (still shadow-traded)
      benched, ≥ N new shadow trades, shrunk R ≥ 0 -> active (probation: edge sizing starts small)
      shadow, n ≥ PROMOTE_MIN_TRADES, R ≥ PROMOTE_MIN_R -> active
      shadow, n ≥ PROMOTE_MIN_TRADES, R ≤ DROP_MAX_R    -> dropped (frees a shadow slot)
      candidate -> shadow when a slot frees up
    """
    if not LEARNING_ENABLED:
        return []
    now = now or time.time()
    shadow = shadow_closed_trades()
    edges = wallet_edge([t for t in closed_live + shadow if t.get("wallet")], now, cfg)
    roster = load_roster()
    changes = []

    for w, handle in tracked.items():
        r = roster.get(w) or {}
        st = edges.get(w)
        if r.get("status") in (None, "active") and st and st["tier"] == "benched":
            if set_status(w, "benched", f"edge {st['shrunk_r']:+.2f}R over {st['n']} trades (win {st['win_rate']:.0%})",
                          handle=handle, origin="wallets.json", benched_ts=now):
                changes.append((w, "benched"))

    roster = load_roster()
    for w, r in roster.items():
        st = edges.get(w) or {"n": 0, "shrunk_r": 0.0, "win_rate": 0.0, "tier": "unknown"}
        status = r.get("status")
        if status == "benched":
            new = [t for t in shadow if t.get("wallet") == w and (t.get("closed_ts") or 0) > (r.get("benched_ts") or 0)]
            if len(new) >= LEARNING_REINSTATE_MIN_NEW:
                st_new = wallet_edge(new, now, cfg).get(w)
                if st_new and st_new["shrunk_r"] >= 0:
                    if set_status(w, "active", f"recovered in shadow: {st_new['shrunk_r']:+.2f}R over {len(new)} trades"):
                        changes.append((w, "active"))
        elif status == "shadow" and st["n"] >= LEARNING_PROMOTE_MIN_TRADES:
            if st["shrunk_r"] >= LEARNING_PROMOTE_MIN_R:
                if set_status(w, "active", f"promoted from shadow: {st['shrunk_r']:+.2f}R, win {st['win_rate']:.0%}, n={st['n']}"):
                    changes.append((w, "active"))
            elif st["shrunk_r"] <= LEARNING_DROP_MAX_R:
                if set_status(w, "dropped", f"no edge in shadow: {st['shrunk_r']:+.2f}R, win {st['win_rate']:.0%}, n={st['n']}"):
                    changes.append((w, "dropped"))

    roster = load_roster()
    free = LEARNING_MAX_SHADOW_WALLETS - sum(1 for r in roster.values() if r.get("status") == "shadow")
    for w, r in sorted(roster.items(), key=lambda kv: kv[1].get("added_ts") or 0):
        if free <= 0:
            break
        if r.get("status") == "candidate":
            if set_status(w, "shadow", "shadow slot freed"):
                changes.append((w, "shadow"))
                free -= 1
    return changes


# ── (b) discovery ────────────────────────────────────────────────────

def profitable_traders_from_trades(trades: list, min_profit_usd: float) -> list:
    """From one pool's recent trades: wallets that both bought and sold,
    with sells − buys ≥ min_profit_usd. Returns [(wallet, profit, buys, sells)]."""
    agg = {}
    for t in trades:
        w = t.get("wallet")
        if not w:
            continue
        a = agg.setdefault(w, {"buy": 0.0, "sell": 0.0, "nb": 0, "ns": 0})
        if t.get("kind") == "buy":
            a["buy"] += t.get("usd") or 0.0
            a["nb"] += 1
        elif t.get("kind") == "sell":
            a["sell"] += t.get("usd") or 0.0
            a["ns"] += 1
    out = [(w, a["sell"] - a["buy"], a["nb"], a["ns"]) for w, a in agg.items()
           if a["nb"] >= 1 and a["ns"] >= 1 and a["sell"] - a["buy"] >= min_profit_usd
           and a["nb"] + a["ns"] <= 40]  # very high counts = MEV/market-making bots, not traders
    return sorted(out, key=lambda x: -x[1])


def discover_candidates(known: set, max_pools: int = 4) -> int:
    """Free sources first: GeckoTerminal trending pools that are UP big ->
    wallets that bought AND took ≥ $X profit inside the visible window.
    Then the Fomo leaderboard (if FOMO_API_KEY). Returns # queued."""
    import market_data
    import fomo
    added = 0
    winners = [p for p in market_data.gt_trending_pools("6h")
               if (p.get("priceChange") or {}).get("h24") and p["priceChange"]["h24"] >= 100
               and ((p.get("liquidity") or {}).get("usd") or 0) >= 20_000]
    for p in winners[:max_pools]:
        sym = (p.get("baseToken") or {}).get("symbol") or "?"
        for w, profit, nb, ns in profitable_traders_from_trades(
                market_data.gt_pool_trades(p["pairAddress"]), LEARNING_DISCOVERY_MIN_PROFIT_USD)[:3]:
            if add_candidate(w, f"disc:{w[:4]}", "geckoterminal",
                             f"+${profit:,.0f} realized on {sym} ({nb} buys/{ns} sells)", known):
                added += 1
    for h in fomo.FOMO_HANDLES:
        w = fomo.user_wallet(h)
        if w and add_candidate(w, f"fomo:@{h}"[:24], "fomo_handle", f"Fomo profile @{h} (FOMO_HANDLES)", known):
            added += 1
    for t in fomo.top_traders():
        if add_candidate(t["wallet"], f"fomo:{t['name']}"[:24], "fomo",
                         f"Fomo {fomo.FOMO_LEADERBOARD_PERIOD} #{t['rank']} PnL ${t.get('pnl_usd') or 0:,.0f}", known):
            added += 1
    return added


# ── (c) walk-forward tuning job ──────────────────────────────────────

LEARNING_TUNE_EVERY_HOURS = float(os.environ.get("LEARNING_TUNE_EVERY_HOURS", "24"))
LEARNING_TUNE_MIN_SIGNALS = int(os.environ.get("LEARNING_TUNE_MIN_SIGNALS", "60"))
_reload_flag = threading.Event()
_tune_running = threading.Event()


def consume_reload_flag() -> bool:
    """True once after a tuning run wrote new accepted params (bot then rebuilds RISK)."""
    if _reload_flag.is_set():
        _reload_flag.clear()
        return True
    return False


def _candles_for(signals: list) -> dict:
    """{mint: PricePath} from GeckoTerminal minute candles (throttled; runs in a background thread)."""
    import market_data
    from backtest.replay import PricePath
    paths = {}
    for mint in sorted({s["mint"] for s in signals}):
        pair = market_data.gt_best_pair(mint)
        if not pair or not pair.get("pairAddress"):
            continue
        last_bt = max(s["block_time"] for s in signals if s["mint"] == mint)
        before = int(min(time.time(), last_bt + 12 * 3600))
        rows = market_data.gt_ohlcv_minutes(pair["pairAddress"], before_ts=before)
        if rows:
            paths[mint] = PricePath([[int(r[0]), r[1], r[2], r[3], r[4], r[5]] for r in rows])
    return paths


def run_tuning(profile: str):
    try:
        import tuning
        sigs = [s for s in recorded_signals(21) if s.get("block_time") and s.get("source") != "sniper"]
        seen, uniq = set(), []
        for s in sorted(sigs, key=lambda s: s["block_time"]):
            k = (s["wallet"], s["mint"])
            if k not in seen:
                seen.add(k)
                uniq.append(s)
        if len(uniq) < LEARNING_TUNE_MIN_SIGNALS:
            note("tuning", f"tuning skipped: {len(uniq)} recorded signals < {LEARNING_TUNE_MIN_SIGNALS} needed")
            return
        os.environ.setdefault("LEARNING_APPLY_TUNED", "true")
        base = RiskConfig.from_env(profile)
        res = tuning.walk_forward(uniq, _candles_for(uniq), base)
        tuning.save_result(res)
        verdict = "ACCEPTED" if res["accepted"] else "rejected (kept current)"
        note("tuning", f"walk-forward tune {verdict}: out-of-sample {res['oos_mean_tuned'] or 0:+.1%}/trade vs "
                       f"current {res['oos_mean_current'] or 0:+.1%} over {res['oos_n']} trades; params {res['params']}",
             accepted=res["accepted"], params=res["params"])
        if res["accepted"]:
            _reload_flag.set()
    except Exception as e:
        print(f"[LEARN] tuning failed: {type(e).__name__}: {e}")
    finally:
        _tune_running.clear()


# ── seed list (e.g. Fomo leaderboard handles -> wallets) ──────────────

LEARNING_SEED_FILE = os.environ.get("LEARNING_SEED_FILE", "backtest/data/fomo_candidates.json")
_seeded = [False]


def seed_candidates(known: set, path: str | None = None) -> int:
    """Queue wallets from a seed file ({"candidates": [{handle, solana_wallets, copy_score?, prior_weight}]}).
    Ordered by our own measured copy_score when present (replay), else the
    leaderboard prior. Everything enters as shadow/candidate — never straight to live."""
    p = Path(path or LEARNING_SEED_FILE)
    data = _read_json(p, {})
    rows = data.get("candidates") or []
    rows = sorted(rows, key=lambda r: (-(r.get("copy_score") if r.get("copy_score") is not None else -9),
                                       -(r.get("prior_weight") or 0)))
    added = 0
    for r in rows:
        if r.get("exclude"):
            continue
        for w in r.get("solana_wallets") or []:
            ev = ", ".join(f"{k} #{v['rank']}" for k, v in (r.get("boards") or {}).items())
            if r.get("copy_score") is not None:
                ev += f"; replay copy-score {r['copy_score']:+.2f}R"
            if add_candidate(w, f"fomo:{r['handle']}"[:24], "fomo_leaderboard", f"Fomo {ev}", known):
                added += 1
    if added:
        note("discovery", f"seeded {added} Fomo leaderboard wallet(s) into shadow/candidate", count=added)
    return added


# ── orchestration (called from the bot's housekeeping) ───────────────

_last = {"review": 0.0, "discovery": 0.0, "tune": time.time() - 23 * 3600}  # first tune ~1h after boot


def periodic(closed_live: list, cfg: RiskConfig, tracked: dict, now: float | None = None):
    if not LEARNING_ENABLED:
        return
    now = now or time.time()
    if not _seeded[0]:
        _seeded[0] = True
        seed_candidates(set(tracked))
    if now - _last["review"] >= LEARNING_REVIEW_EVERY_MIN * 60:
        _last["review"] = now
        review_roster(closed_live, cfg, tracked, now)
        scores = score_everything(closed_live + shadow_closed_trades(), cfg, now)
        _write_json(SCORES_FILE, scores)
    if now - _last["discovery"] >= LEARNING_DISCOVERY_EVERY_MIN * 60:
        _last["discovery"] = now
        known = set(tracked) | set(load_roster())
        n = discover_candidates(known)
        if n:
            note("discovery", f"queued {n} new wallet(s) for shadow trading", count=n)
    if now - _last["tune"] >= LEARNING_TUNE_EVERY_HOURS * 3600 and not _tune_running.is_set():
        _last["tune"] = now
        _tune_running.set()
        threading.Thread(target=run_tuning, args=(cfg.profile,), daemon=True).start()


def summary_for_discord(limit: int = 8) -> str:
    """Plain-text block the Discord persona can quote verbatim."""
    rows = recent_notes(limit)
    scores = _read_json(SCORES_FILE, {})
    lines = []
    srcs = scores.get("sources") or {}
    if srcs:
        lines.append("Edge by source: " + ", ".join(
            f"{k} {v['shrunk_r']:+.2f}R (n={v['n']}, win {v['win_rate']:.0%})" for k, v in srcs.items()))
    for r in rows:
        lines.append(f"- {time.strftime('%Y-%m-%d %H:%M', time.gmtime(r['ts']))} UTC: {r['text']}")
    return "\n".join(lines)
