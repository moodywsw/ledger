"""Strategy Lab — one model thinks, another system verifies, the owner decides.

Pipeline for every proposal (nothing goes live automatically):
  1. propose   LLM (Gemini/Groq free tier, if a key is set) or rule-based search
               proposes exit-parameter tweaks, per-trader exit overrides, or a
               market-regime size filter. Every value is clamped to
               risk_engine.TUNABLE_BOUNDS; hard rails are never touchable.
  2. backtest  fee-aware replay on cached data (backtest/data + live recorded
               signals) with the SAME exit engine the bot runs live.
  3. risk      checks: sample size, drawdown, win-rate collapse, beats current.
  4. stress    2x fees/slippage + delayed entries (120s instead of 45s).
  5. paper     forward test on the next LAB_PAPER_TRADES live signals.
  6. approve   owner-only (admin token). Only then does live config change:
               exits -> tuned_params.json (bot hot-reloads RISK),
               trader exits / regime scale -> strategy_live.json.

State: $DATA_DIR/strategy_lab.json, live overlay: $DATA_DIR/strategy_live.json.
"""
from __future__ import annotations

import json, os, random, re, statistics, threading, time, uuid
from pathlib import Path

from risk_engine import RiskConfig, TUNABLE_BOUNDS, apply_tuned, tuned_params_path

ROOT = Path(__file__).resolve().parent
EXTRA_FEE = float(os.environ.get("LAB_EXTRA_FEE_PER_SIDE", "0.003"))   # priority fee + gas on top of slippage model
BASE_LATENCY = 45
STRESS_LATENCY = 120
PAPER_TRADES = int(os.environ.get("LAB_PAPER_TRADES", "10"))
SIM_POSITION_PCT = 0.07          # equity fraction per trade for drawdown simulation (mid of 5-10%)
MAX_DD = 0.25
MIN_TRADES = int(os.environ.get("LAB_MIN_BACKTEST_TRADES", "20"))
MIN_EDGE = 0.005                 # must beat current by +0.5pp/trade
PROPOSE_EVERY_H = float(os.environ.get("LAB_PROPOSE_EVERY_H", "24"))
PAPER_EVERY_H = float(os.environ.get("LAB_PAPER_EVERY_H", "2"))
MAX_OPEN = 4
_lock = threading.RLock()


def _dir() -> Path:
    return Path(os.environ.get("DATA_DIR", "."))


def lab_path() -> Path:
    return _dir() / "strategy_lab.json"


def live_path() -> Path:
    return _dir() / "strategy_live.json"


def _read(p: Path, default):
    try:
        return json.loads(p.read_text())
    except Exception:
        return default


def _write(p: Path, d):
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp"); tmp.write_text(json.dumps(d, indent=1, default=str)); tmp.replace(p)


def load() -> dict:
    return _read(lab_path(), {"strategies": [], "last_propose": 0, "last_paper": 0})


def save(d):
    d["strategies"] = d["strategies"][-60:]
    _write(lab_path(), d)


# ---------------- live overlay (read by the bot) ----------------
def live() -> dict:
    return _read(live_path(), {"trader_exits": {}, "regime_risk_off_scale": None, "approved": []})


def cfg_for(base: RiskConfig, trader: str | None) -> RiskConfig:
    """Exit config for a position: base RISK + APPROVED per-trader overrides (clamped)."""
    if not trader:
        return base
    ov = (live().get("trader_exits") or {}).get(str(trader).lower())
    return apply_tuned(base, ov) if ov else base


def regime_scale_override() -> float | None:
    v = live().get("regime_risk_off_scale")
    return float(v) if isinstance(v, (int, float)) else None


# ---------------- data ----------------
def _clamp(params: dict) -> dict:
    out = {}
    for k, v in (params or {}).items():
        if k in TUNABLE_BOUNDS and isinstance(v, (int, float)):
            lo, hi = TUNABLE_BOUNDS[k]
            out[k] = round(float(min(max(v, lo), hi)), 4)
    return out


def load_dataset() -> tuple[list, dict]:
    """Signals [{mint, block_time, handle}] + {mint: PricePath} from cached data (no network)."""
    from backtest.replay import PricePath
    sigs, paths = [], {}
    for bf, pf in (("wallet_buys.json", "prices.json"), ("fomo_buys.json", "fomo_prices.json")):
        try:
            buys = json.loads((ROOT / "backtest/data" / bf).read_text())
            buys = buys.get("buys", buys) if isinstance(buys, dict) else buys
            prices = json.loads((ROOT / "backtest/data" / pf).read_text())
        except Exception:
            continue
        for m, r in prices.items():
            if isinstance(r, dict) and r.get("candles"):
                paths.setdefault(m, PricePath(r["candles"]))
        for b in buys if isinstance(buys, list) else []:
            if b.get("mint") and b.get("block_time"):
                sigs.append({"mint": b["mint"], "block_time": b["block_time"], "handle": (b.get("handle") or "").lower()})
    seen, out = set(), []
    for s in sorted(sigs, key=lambda s: s["block_time"]):
        k = (s["handle"], s["mint"])
        if k not in seen and s["mint"] in paths:
            seen.add(k); out.append(s)
    return out, paths


def _ret(exits, entry, cost):
    remaining, ret = 1.0, 0.0
    for _, px, frac, _r in exits:
        sold = remaining * min(1.0, frac)
        ret += sold * ((px or 0.0) * (1 - cost) / (entry * (1 + cost)) - 1)
        remaining -= sold
        if remaining <= 1e-9:
            break
    return ret


def returns(signals, paths, cfg: RiskConfig, latency=BASE_LATENCY, cost_mult=1.0, trader_params=None, size_fn=None):
    from backtest.replay import exits_v2
    out = []
    cost = (cfg.paper_cost_per_side_pct + EXTRA_FEE) * cost_mult
    for s in signals:
        p = paths.get(s["mint"])
        if not p:
            continue
        t = s["block_time"] + latency
        entry, _ = p.price_at(t)
        if not entry:
            continue
        c = cfg
        if trader_params and s.get("handle") in trader_params:
            c = apply_tuned(cfg, trader_params[s["handle"]])
        r = _ret(exits_v2(entry, p, t, c), entry, cost)
        out.append(r * (size_fn(s) if size_fn else 1.0))
    return out


def metrics(rs: list) -> dict:
    if not rs:
        return {"n": 0}
    eq, peak, dd = 1.0, 1.0, 0.0
    for r in rs:
        eq *= 1 + SIM_POSITION_PCT * r
        peak = max(peak, eq); dd = max(dd, 1 - eq / peak)
    wins = [r for r in rs if r > 0]; losses = [-r for r in rs if r <= 0]
    return {"n": len(rs), "mean": statistics.mean(rs), "median": statistics.median(rs),
            "win_rate": len(wins) / len(rs), "profit_factor": (sum(wins) / sum(losses)) if sum(losses) else None,
            "max_dd": dd, "equity_mult": eq}


# ---------------- proposal ----------------
def _regime_size_fn(scale):
    """Historical regime per signal from SOL daily candles (risk-off = SOL -5% over 3 days)."""
    days = _sol_daily()
    def f(s):
        d = int(s["block_time"] // 86400)
        a, b = days.get(d - 3), days.get(d)
        return scale if (a and b and b / a - 1 <= -0.05) else 1.0
    return f


_SOL_CACHE: dict = {}


def _sol_daily() -> dict:
    if _SOL_CACHE:
        return _SOL_CACHE
    try:
        import market_thoughts as mt
        for k in mt.spot("/api/v3/klines", {"symbol": "SOLUSDT", "interval": "1d", "limit": 1000}):
            _SOL_CACHE[int(k[0] // 86400000)] = float(k[4])
    except Exception:
        pass
    return _SOL_CACHE


def _llm(prompt: str) -> str | None:
    import requests
    sysmsg = ("You are a quantitative strategist for a Solana memecoin copy-trading bot. Propose ONE change. "
              "Reply with JSON only: {\"name\":str,\"kind\":\"exits\"|\"trader_exits\"|\"regime\",\"params\":{...},"
              "\"trader\":str|null,\"rationale\":str}. For regime use params {\"risk_off_scale\": 0.25-1.0}.")
    g, q = os.environ.get("GEMINI_API_KEY", "").strip(), os.environ.get("GROQ_API_KEY", "").strip()
    try:
        if g:
            r = requests.post(f"https://generativelanguage.googleapis.com/v1beta/models/{os.environ.get('GEMINI_MODEL', 'gemini-2.0-flash')}:generateContent",
                              params={"key": g}, timeout=30, json={"systemInstruction": {"parts": [{"text": sysmsg}]},
                              "contents": [{"role": "user", "parts": [{"text": prompt}]}],
                              "generationConfig": {"temperature": 0.7, "maxOutputTokens": 400, "responseMimeType": "application/json"}})
            r.raise_for_status(); return r.json()["candidates"][0]["content"]["parts"][0]["text"]
        if q:
            r = requests.post("https://api.groq.com/openai/v1/chat/completions", timeout=30, headers={"Authorization": f"Bearer {q}"},
                              json={"model": os.environ.get("GROQ_MODEL", "llama-3.1-8b-instant"), "temperature": 0.7, "max_tokens": 400,
                                    "response_format": {"type": "json_object"},
                                    "messages": [{"role": "system", "content": sysmsg}, {"role": "user", "content": prompt}]})
            r.raise_for_status(); return r.json()["choices"][0]["message"]["content"]
    except Exception as e:
        print(f"[LAB] llm proposal failed: {str(e)[:120]}")
    return None


def _new(name, kind, params, rationale, proposer, trader=None) -> dict:
    return {"id": uuid.uuid4().hex[:10], "name": name[:80], "kind": kind, "params": params, "trader": trader,
            "rationale": (rationale or "")[:400], "proposer": proposer, "created": time.time(), "status": "proposed",
            "results": {}, "history": [{"ts": time.time(), "status": "proposed"}]}


def rule_proposals(base: RiskConfig, signals, paths, rng=random) -> list:
    """Cheap search: a few exit variants on the first 70% (the rest is the held-out test in backtest())."""
    train = signals[: int(len(signals) * 0.7)]
    cur = metrics(returns(train, paths, base)).get("mean") or 0
    grid = {"stop_loss_pct": [0.15, 0.2, 0.25, 0.3], "tp1_gain": [0.2, 0.3, 0.5], "tp1_fraction": [0.33, 0.5],
            "time_stop_minutes": [20.0, 30.0, 45.0, 60.0], "trailing_stop_pct": [0.2, 0.25, 0.3]}
    best = None
    for _ in range(24):
        p = {k: rng.choice(v) for k, v in grid.items()}
        m = metrics(returns(train, paths, apply_tuned(base, p))).get("mean")
        if m is not None and (best is None or m > best[0]):
            best = (m, p)
    out = []
    if best and best[0] > cur:
        out.append(_new("Exit ladder retune", "exits", _clamp(best[1]),
                        f"Random search on 70% of cached signals: {best[0]:+.1%}/trade vs current {cur:+.1%}.", "rules"))
    by = {}
    for s in train:
        by.setdefault(s["handle"], []).append(s)
    for h, ss in sorted(by.items(), key=lambda kv: -len(kv[1]))[:3]:
        if len(ss) < 8 or not h:
            continue
        c = metrics(returns(ss, paths, base)).get("mean") or 0
        bp = None
        for st in (0.15, 0.25, 0.3):
            for tp in (0.2, 0.4, 0.6):
                p = {"stop_loss_pct": st, "tp1_gain": tp}
                m = metrics(returns(ss, paths, apply_tuned(base, p))).get("mean")
                if m is not None and (bp is None or m > bp[0]):
                    bp = (m, p)
        if bp and bp[0] > c + MIN_EDGE:
            out.append(_new(f"Custom exits for {h}", "trader_exits", _clamp(bp[1]),
                            f"This trader's signals did {bp[0]:+.1%}/trade with these exits vs {c:+.1%} on the shared ladder.",
                            "rules", trader=h))
    out.insert(0, _new("Risk-off size filter", "regime", {"risk_off_scale": 0.5},
                    "Halve new-entry size when the market regime is risk-off (memecoins bleed hardest when majors trend down).", "rules"))
    return out


def llm_proposal(base: RiskConfig, signals, paths) -> dict | None:
    if not (os.environ.get("GEMINI_API_KEY") or os.environ.get("GROQ_API_KEY")):
        return None
    m = metrics(returns(signals, paths, base))
    try:
        import market_thoughts as mt
        reg = (mt.cached() or {}).get("regime", {})
    except Exception:
        reg = {}
    txt = _llm(json.dumps({"current_exit_params": {k: getattr(base, k, None) for k in TUNABLE_BOUNDS if hasattr(base, k)},
                           "tp_ladder": base.tp_ladder, "bounds": TUNABLE_BOUNDS, "backtest_current": m,
                           "traders": sorted({s["handle"] for s in signals if s["handle"]})[:20], "market_regime": reg}, default=str))
    if not txt:
        return None
    try:
        d = json.loads(re.search(r"\{.*\}", txt, re.S).group(0))
    except Exception:
        return None
    kind = d.get("kind") if d.get("kind") in ("exits", "trader_exits", "regime") else "exits"
    if kind == "regime":
        v = (d.get("params") or {}).get("risk_off_scale")
        if not isinstance(v, (int, float)):
            return None
        params = {"risk_off_scale": round(min(1.0, max(0.25, float(v))), 2)}
    else:
        params = _clamp(d.get("params") or {})
        if not params:
            return None
    trader = (d.get("trader") or "").lower() or None if kind == "trader_exits" else None
    if kind == "trader_exits" and not trader:
        return None
    proposer = "gemini" if os.environ.get("GEMINI_API_KEY") else "groq"
    return _new(str(d.get("name") or "LLM idea"), kind, params, str(d.get("rationale") or ""), proposer, trader)


# ---------------- verification ----------------
def evaluate(st: dict, base: RiskConfig, signals, paths) -> dict:
    """Backtest (held-out last 30%) + risk checks + stress test. Mutates and returns st."""
    test = signals[int(len(signals) * 0.7):] if st["proposer"] == "rules" else signals
    kind, p = st["kind"], st["params"]
    if kind == "trader_exits":
        test = [s for s in signals if s["handle"] == st.get("trader")]
        test = test[int(len(test) * 0.5):] if st["proposer"] == "rules" else test
    def run(latency=BASE_LATENCY, cost_mult=1.0, candidate=True):
        if not candidate:
            return returns(test, paths, base, latency, cost_mult)
        if kind == "exits":
            return returns(test, paths, apply_tuned(base, p), latency, cost_mult)
        if kind == "trader_exits":
            return returns(test, paths, base, latency, cost_mult, trader_params={st["trader"]: p})
        return returns(test, paths, base, latency, cost_mult, size_fn=_regime_size_fn(p["risk_off_scale"]))
    cur, cand = metrics(run(candidate=False)), metrics(run())
    stress_cur, stress = metrics(run(STRESS_LATENCY, 2.0, False)), metrics(run(STRESS_LATENCY, 2.0))
    checks = []
    def chk(name, ok, detail):
        checks.append({"name": name, "ok": bool(ok), "detail": detail})
    n_ok = cand.get("n", 0) >= (MIN_TRADES if kind != "trader_exits" else 8)
    chk("Sample size", n_ok, f"{cand.get('n', 0)} trades")
    if cand.get("n"):
        chk("Beats current (fee-aware)", cand["mean"] >= cur["mean"] + (MIN_EDGE if kind != "regime" else 0),
            f"{cand['mean']:+.2%} vs {cur['mean']:+.2%} per trade")
        chk("Positive expectancy after fees", cand["mean"] > 0, f"{cand['mean']:+.2%} per trade incl. {(base.paper_cost_per_side_pct + EXTRA_FEE):.1%}/side costs")
        chk("Max drawdown", cand["max_dd"] <= MAX_DD, f"{cand['max_dd']:.1%} (limit {MAX_DD:.0%}) at {SIM_POSITION_PCT:.0%} size")
        chk("Win rate holds", cand["win_rate"] >= cur["win_rate"] - 0.10, f"{cand['win_rate']:.0%} vs {cur['win_rate']:.0%}")
        chk("Stress: 2x fees + 120s delay", stress.get("n") and stress["mean"] >= stress_cur["mean"] - 0.002,
            f"{stress.get('mean', 0):+.2%} vs current {stress_cur.get('mean', 0):+.2%}")
    chk("Within hard rails", True, "all values clamped to tunable bounds; sizing caps & safety filters untouched")
    st["results"].update(backtest={"current": cur, "candidate": cand, "fee_per_side": base.paper_cost_per_side_pct + EXTRA_FEE},
                         stress={"current": stress_cur, "candidate": stress}, checks=checks)
    _status(st, "paper" if all(c["ok"] for c in checks) else "failed_checks")
    if st["status"] == "paper":
        st["results"]["paper"] = {"since": time.time(), "n": 0, "candidate": [], "current": []}
    return st


def _status(st, s):
    st["status"] = s
    st["history"].append({"ts": time.time(), "status": s})


def paper_step(st: dict, base: RiskConfig, new_signals: list, paths: dict):
    """Forward test on live signals recorded after paper started (shadow, no orders)."""
    pp = st["results"].setdefault("paper", {"since": time.time(), "n": 0, "candidate": [], "current": [], "seen": []})
    seen = set(pp.setdefault("seen", []))
    fresh = [s for s in new_signals if s["block_time"] >= pp["since"] and f"{s['handle']}:{s['mint']}" not in seen]
    if st["kind"] == "trader_exits":
        fresh = [s for s in fresh if s["handle"] == st["trader"]]
    for s in fresh:
        if s["mint"] not in paths:
            continue
        c = returns([s], paths, base)
        if not c:
            continue
        if st["kind"] == "exits":
            k = returns([s], paths, apply_tuned(base, st["params"]))
        elif st["kind"] == "trader_exits":
            k = returns([s], paths, base, trader_params={st["trader"]: st["params"]})
        else:
            k = returns([s], paths, base, size_fn=_regime_size_fn(st["params"]["risk_off_scale"]))
        pp["candidate"].append(k[0]); pp["current"].append(c[0]); pp["seen"].append(f"{s['handle']}:{s['mint']}")
    pp["n"] = len(pp["candidate"])
    if pp["n"] >= PAPER_TRADES:
        ok = statistics.mean(pp["candidate"]) >= statistics.mean(pp["current"])
        pp["verdict"] = (f"paper {statistics.mean(pp['candidate']):+.2%}/trade vs current "
                         f"{statistics.mean(pp['current']):+.2%} over {pp['n']} live signals")
        _status(st, "awaiting_approval" if ok else "paper_failed")


# ---------------- owner decisions ----------------
def decide(sid: str, approve: bool, base_profile: str) -> dict:
    with _lock:
        d = load()
        st = next((s for s in d["strategies"] if s["id"] == sid), None)
        if not st:
            raise KeyError("unknown strategy")
        if not approve:
            _status(st, "rejected"); save(d); return st
        if st["status"] != "awaiting_approval":
            raise ValueError(f"can't approve a strategy in status '{st['status']}' (it must finish backtest, checks and paper first)")
        lv = live()
        if st["kind"] == "exits":
            prev = _read(tuned_params_path(), {})
            keep = (prev.get("params") or {}) if (prev.get("profile") == base_profile and prev.get("accepted")) else {}
            params = {**keep, **st["params"]}
            _write(tuned_params_path(), {"profile": base_profile, "accepted": True, "params": params, "ts": time.time(),
                                         "source": "strategy_lab", "strategy_id": sid})
            try:
                import learning
                learning._reload_flag.set()
            except Exception:
                pass
        elif st["kind"] == "trader_exits":
            lv.setdefault("trader_exits", {})[st["trader"]] = st["params"]
        else:
            lv["regime_risk_off_scale"] = st["params"]["risk_off_scale"]
        lv.setdefault("approved", []).append({"id": sid, "ts": time.time(), "kind": st["kind"], "params": st["params"]})
        _write(live_path(), lv)
        _status(st, "approved"); save(d)
        return st


def add_from_tuning(res: dict):
    """learning.run_tuning hands accepted walk-forward results here instead of applying them."""
    with _lock:
        d = load()
        st = _new("Walk-forward exit tune", "exits", _clamp(res.get("params") or {}),
                  f"Walk-forward OOS {res.get('oos_mean_tuned') or 0:+.1%}/trade vs current {res.get('oos_mean_current') or 0:+.1%} "
                  f"over {res.get('oos_n')} trades.", "tuning")
        d["strategies"].append(st); save(d)


# ---------------- loop ----------------
def tick(profile: str | None = None, now: float | None = None, force_propose=False):
    now = now or time.time()
    base = RiskConfig.from_env(profile)
    with _lock:
        d = load()
        open_n = sum(1 for s in d["strategies"] if s["status"] in ("proposed", "paper", "awaiting_approval"))
        need_data = force_propose or any(s["status"] == "proposed" for s in d["strategies"]) or \
            (now - d.get("last_propose", 0) >= PROPOSE_EVERY_H * 3600 and open_n < MAX_OPEN)
        signals, paths = load_dataset() if need_data else ([], {})
        if (force_propose or now - d.get("last_propose", 0) >= PROPOSE_EVERY_H * 3600) and open_n < MAX_OPEN and signals:
            d["last_propose"] = now
            have = {(s["kind"], s.get("trader"), json.dumps(s["params"], sort_keys=True)) for s in d["strategies"]}
            new = [x for x in [llm_proposal(base, signals, paths)] if x] + rule_proposals(base, signals, paths)
            for x in new[: MAX_OPEN - open_n]:
                if (x["kind"], x.get("trader"), json.dumps(x["params"], sort_keys=True)) not in have:
                    d["strategies"].append(x)
        for st in d["strategies"]:
            if st["status"] == "proposed" and signals:
                evaluate(st, base, signals, paths)
        if now - d.get("last_paper", 0) >= PAPER_EVERY_H * 3600 and any(s["status"] == "paper" for s in d["strategies"]):
            d["last_paper"] = now
            try:
                import learning
                since = min(s["results"]["paper"]["since"] for s in d["strategies"] if s["status"] == "paper")
                sigs = [{"mint": s["mint"], "block_time": s["block_time"], "handle": (s.get("handle") or "").lower()}
                        for s in learning.recorded_signals(21) if s.get("block_time") and s["block_time"] >= since
                        and s.get("source") != "sniper" and s["block_time"] < now - 6 * 3600]  # give exits time to play out
                ppaths = learning._candles_for(sigs[-40:]) if sigs else {}
                for st in d["strategies"]:
                    if st["status"] == "paper":
                        paper_step(st, base, sigs, ppaths)
            except Exception as e:
                print(f"[LAB] paper step failed: {str(e)[:160]}")
        save(d)
        return d


def public_view(d: dict | None = None) -> dict:
    d = d or load()
    return {"strategies": list(reversed(d["strategies"]))[:30], "live": live(),
            "paper_trades_required": PAPER_TRADES, "llm": "gemini" if os.environ.get("GEMINI_API_KEY") else
            "groq" if os.environ.get("GROQ_API_KEY") else "rules"}


_started = False


def start(profile: str | None = None):
    global _started
    if _started or os.environ.get("STRATEGY_LAB_ENABLED", "true").lower() in ("0", "false", "no"):
        return
    _started = True

    def loop():
        time.sleep(90)
        while True:
            try:
                tick(profile)
            except Exception as e:
                print(f"[LAB] tick error: {type(e).__name__}: {str(e)[:160]}")
            time.sleep(900)
    threading.Thread(target=loop, daemon=True, name="strategy-lab").start()
    print("[LAB] Strategy Lab on — proposals need backtest + risk + stress + paper + owner approval")
