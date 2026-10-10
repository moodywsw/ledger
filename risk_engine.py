"""
risk_engine.py — Ledger's risk desk: position sizing, pre-trade limits,
token safety filters, the exit engine, and tracked-wallet scoring.

Everything in here is PURE (no network, no file I/O, no clock reads
unless you pass `now`), so it can be unit-tested and replayed against
historical data byte-for-byte the same way it runs live. ledger_bot.py
gathers the inputs (prices, holders, balances) and acts on the outputs.

Every threshold comes from RiskConfig, which reads env vars with
conservative defaults — tune on Railway without a code change. Nothing
here can arm real trading; REAL_TRADING_ENABLED lives in real_trading.py
and is untouched by this module.

No rule set guarantees profit. Memecoins routinely gap straight through
stops (rugs, dev dumps), which is why sizing below assumes a stop can
slip by STOP_GAP_BUFFER and why the per-trade risk budget is small.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field, asdict, replace as dc_replace
from datetime import datetime, timezone
from typing import Optional


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def _env_int(name: str, default: int) -> int:
    try:
        return int(float(os.environ.get(name, default)))
    except (TypeError, ValueError):
        return default


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _env_ladder(name: str, default: list) -> list:
    """Parses "0.5:0.33,1.0:0.33" -> [(0.5, 0.33), (1.0, 0.33)]."""
    raw = os.environ.get(name)
    if not raw:
        return list(default)
    try:
        rungs = []
        for part in raw.split(","):
            gain, frac = part.split(":")
            rungs.append((float(gain), float(frac)))
        return sorted(rungs)
    except ValueError:
        return list(default)


@dataclass
class RiskConfig:
    profile: str = "conservative"           # RISK_PROFILE: conservative | balanced | degen | scalper
    # ── Sizing ────────────────────────────────────────────────────────
    # sizing_mode "risk": fixed-fractional (risk_per_trade_pct / stop).
    # sizing_mode "edge": % of equity chosen from the copied wallet's measured
    # edge (see wallet_edge / edge_position_pct) — top wallets get the most.
    sizing_mode: str = "edge"
    risk_per_trade_pct: float = 0.01        # equity lost if the stop is hit (after gap buffer): 1%
    max_position_pct: float = 0.10          # hard per-position cap: 10% of equity (= HARD_RAIL max)
    stop_gap_buffer: float = 1.5            # assume stops slip 50% further than planned when sizing
    min_position_sol: float = 0.01          # below this, fees dominate — skip instead
    # ── Portfolio limits ─────────────────────────────────────────────
    max_concurrent_positions: int = 4
    max_total_exposure_pct: float = 0.40    # ≤40% of equity in open positions at once
    daily_loss_limit_pct: float = 0.10      # stop opening trades after -10% on the UTC day
    max_consecutive_losses: int = 3         # then cool down...
    loss_cooldown_minutes: float = 60.0     # ...for this long
    token_reentry_cooldown_minutes: float = 240.0  # don't re-buy a token we just exited
    max_trades_per_hour: int = 6
    # ── Entry filters (token safety) ─────────────────────────────────
    min_liquidity_usd: float = 15_000
    min_market_cap_usd: float = 25_000
    max_market_cap_usd: float = 5_000_000
    max_top10_holder_pct: float = 35.0
    require_mint_authority_revoked: bool = True
    require_freeze_authority_revoked: bool = True
    max_entry_price_impact_pct: float = 2.0  # estimated from size vs pool liquidity
    max_signal_age_seconds: float = 120.0    # copying a buy older than this = chasing
    max_chase_pct: float = 0.25              # skip if price already ran >25% past the copied wallet's fill
    fail_closed_on_missing_data: bool = True # unknown liquidity/authority = reject (old code failed open)
    # ── Exits ─────────────────────────────────────────────────────────
    stop_loss_pct: float = 0.20              # hard stop -20% from entry
    tp_ladder: list = field(default_factory=lambda: [(0.25, 0.5), (0.6, 0.25)])  # (gain, fraction of ORIGINAL size)
    breakeven_after_first_tp: bool = True    # after TP1, stop moves to entry (+costs)
    trailing_activation_pct: float = 0.30    # trailing stop arms once peak gain ≥ +30%
    trailing_stop_pct: float = 0.25          # exit if price falls 25% off the peak...
    trailing_stop_tight_pct: float = 0.20    # ...or 20% once peak gain ≥ +100%
    time_stop_minutes: float = 30.0          # if not working after 30 min...
    time_stop_min_gain_pct: float = 0.10     # ...(peak gain < +10%), exit
    max_hold_hours: float = 24.0
    no_price_writeoff_minutes: float = 60.0  # no price at all for this long = treat as rugged
    # ── Costs (paper realism) ─────────────────────────────────────────
    paper_cost_per_side_pct: float = 0.015   # fee + slippage per side, applied to paper fills
    # ── Wallet scoring ────────────────────────────────────────────────
    wallet_min_trades: int = 5
    wallet_min_expectancy_pct: float = 0.0   # avg pnl per closed copy must stay ≥ this
    wallet_lookback_days: float = 30.0
    # ── Edge-based sizing (sizing_mode="edge") ───────────────────────
    edge_unknown_pct: float = 0.05           # wallet with < wallet_min_trades outcomes: start small
    edge_weak_pct: float = 0.05              # measured edge between bench and 0
    edge_base_pct: float = 0.07              # edge just above 0
    edge_max_pct: float = 0.09               # edge at edge_target_r
    edge_top_pct: float = 0.10               # edge ≥ 2x target with a solid sample
    edge_target_r: float = 0.5               # shrunk avg R that earns edge_max_pct
    edge_bench_r: float = -0.25              # shrunk avg R below this (with enough trades) = stop copying
    edge_prior_trades: float = 5.0           # Bayesian shrinkage: pseudo-trades of zero edge
    edge_half_life_days: float = 7.0         # recency weighting of outcomes
    # ── Other sources ─────────────────────────────────────────────────
    sniper_position_pct: float = 0.01        # pump.fun sniper entries (% of equity)
    sniper_min_liquidity_usd: float = 5_000  # bonding-curve pools are thin; impact cap still applies
    sniper_age_min_seconds: float = 120.0    # earliest a fresh launch may be bought
    sniper_age_max_seconds: float = 2700.0   # give up after this
    own_thesis_position_pct: float = 0.005   # the bot's own calls (paper by default)
    # ── Legacy behaviour switches ────────────────────────────────────
    allow_llm_dip_buys: bool = False         # old: LLM could average down instead of stopping out
    max_dip_buys: int = 1
    # Rails that were clamped by apply_hard_rails (for the boot log)
    rails_applied: list = field(default_factory=list)

    @classmethod
    def from_env(cls, profile: Optional[str] = None) -> "RiskConfig":
        """profile defaults (RISK_PROFILE) < tuned overrides (caller) < explicit env vars; then hard rails."""
        name = (profile or os.environ.get("RISK_PROFILE") or "conservative").strip().lower()
        if name not in PROFILES:
            name = "conservative"
        d = profile_config(name)
        d = apply_tuned(d, load_tuned(name))
        cfg = cls._env_overrides(d)
        return apply_hard_rails(cfg)

    @classmethod
    def _env_overrides(cls, d: "RiskConfig") -> "RiskConfig":
        extra = {k: (_env_float(env, getattr(d, k)) if isinstance(getattr(d, k), float) else _env_int(env, getattr(d, k)))
                 for k, env in _EXTRA_ENV.items()}
        return dc_replace(d, **extra, **cls._base_env(d))

    @classmethod
    def _base_env(cls, d: "RiskConfig") -> dict:
        return dict(
            risk_per_trade_pct=_env_float("RISK_PER_TRADE_PCT", d.risk_per_trade_pct),
            max_position_pct=_env_float("RISK_MAX_POSITION_PCT", d.max_position_pct),
            stop_gap_buffer=_env_float("RISK_STOP_GAP_BUFFER", d.stop_gap_buffer),
            min_position_sol=_env_float("RISK_MIN_POSITION_SOL", d.min_position_sol),
            max_concurrent_positions=_env_int("RISK_MAX_CONCURRENT_POSITIONS", d.max_concurrent_positions),
            max_total_exposure_pct=_env_float("RISK_MAX_TOTAL_EXPOSURE_PCT", d.max_total_exposure_pct),
            daily_loss_limit_pct=_env_float("RISK_DAILY_LOSS_LIMIT_PCT", d.daily_loss_limit_pct),
            max_consecutive_losses=_env_int("RISK_MAX_CONSECUTIVE_LOSSES", d.max_consecutive_losses),
            loss_cooldown_minutes=_env_float("RISK_LOSS_COOLDOWN_MINUTES", d.loss_cooldown_minutes),
            token_reentry_cooldown_minutes=_env_float("RISK_TOKEN_REENTRY_COOLDOWN_MINUTES", d.token_reentry_cooldown_minutes),
            max_trades_per_hour=_env_int("RISK_MAX_TRADES_PER_HOUR", d.max_trades_per_hour),
            min_liquidity_usd=_env_float("FILTER_MIN_LIQUIDITY_USD", d.min_liquidity_usd),
            min_market_cap_usd=_env_float("FILTER_MIN_MARKET_CAP_USD", d.min_market_cap_usd),
            max_market_cap_usd=_env_float("FILTER_MAX_MARKET_CAP_USD", d.max_market_cap_usd),
            max_top10_holder_pct=_env_float("FILTER_MAX_TOP10_HOLDER_PCT", d.max_top10_holder_pct),
            require_mint_authority_revoked=_env_bool("FILTER_REQUIRE_MINT_AUTHORITY_REVOKED", d.require_mint_authority_revoked),
            require_freeze_authority_revoked=_env_bool("FILTER_REQUIRE_FREEZE_AUTHORITY_REVOKED", d.require_freeze_authority_revoked),
            max_entry_price_impact_pct=_env_float("FILTER_MAX_ENTRY_PRICE_IMPACT_PCT", d.max_entry_price_impact_pct),
            max_signal_age_seconds=_env_float("FILTER_MAX_SIGNAL_AGE_SECONDS", d.max_signal_age_seconds),
            max_chase_pct=_env_float("FILTER_MAX_CHASE_PCT", d.max_chase_pct),
            fail_closed_on_missing_data=_env_bool("FILTER_FAIL_CLOSED", d.fail_closed_on_missing_data),
            stop_loss_pct=_env_float("EXIT_STOP_LOSS_PCT", d.stop_loss_pct),
            tp_ladder=_env_ladder("EXIT_TP_LADDER", d.tp_ladder),
            breakeven_after_first_tp=_env_bool("EXIT_BREAKEVEN_AFTER_TP1", d.breakeven_after_first_tp),
            trailing_activation_pct=_env_float("EXIT_TRAILING_ACTIVATION_PCT", d.trailing_activation_pct),
            trailing_stop_pct=_env_float("EXIT_TRAILING_STOP_PCT", d.trailing_stop_pct),
            trailing_stop_tight_pct=_env_float("EXIT_TRAILING_STOP_TIGHT_PCT", d.trailing_stop_tight_pct),
            time_stop_minutes=_env_float("EXIT_TIME_STOP_MINUTES", d.time_stop_minutes),
            time_stop_min_gain_pct=_env_float("EXIT_TIME_STOP_MIN_GAIN_PCT", d.time_stop_min_gain_pct),
            max_hold_hours=_env_float("EXIT_MAX_HOLD_HOURS", d.max_hold_hours),
            no_price_writeoff_minutes=_env_float("EXIT_NO_PRICE_WRITEOFF_MINUTES", d.no_price_writeoff_minutes),
            paper_cost_per_side_pct=_env_float("PAPER_COST_PER_SIDE_PCT", d.paper_cost_per_side_pct),
            wallet_min_trades=_env_int("WALLET_SCORE_MIN_TRADES", d.wallet_min_trades),
            wallet_min_expectancy_pct=_env_float("WALLET_SCORE_MIN_EXPECTANCY_PCT", d.wallet_min_expectancy_pct),
            wallet_lookback_days=_env_float("WALLET_SCORE_LOOKBACK_DAYS", d.wallet_lookback_days),
            allow_llm_dip_buys=_env_bool("ALLOW_LLM_DIP_BUYS", d.allow_llm_dip_buys),
            max_dip_buys=_env_int("MAX_DIP_BUYS", d.max_dip_buys),
            sizing_mode=os.environ.get("RISK_SIZING_MODE", d.sizing_mode).strip().lower(),
        )

    def as_dict(self) -> dict:
        return asdict(self)


# Env names for the newer fields (the original ones are in _base_env).
_EXTRA_ENV = {
    "edge_unknown_pct": "EDGE_UNKNOWN_WALLET_PCT", "edge_weak_pct": "EDGE_WEAK_WALLET_PCT",
    "edge_base_pct": "EDGE_BASE_PCT", "edge_max_pct": "EDGE_MAX_PCT", "edge_top_pct": "EDGE_TOP_PCT",
    "edge_target_r": "EDGE_TARGET_R", "edge_bench_r": "EDGE_BENCH_R",
    "edge_prior_trades": "EDGE_PRIOR_TRADES", "edge_half_life_days": "EDGE_HALF_LIFE_DAYS",
    "sniper_position_pct": "SNIPER_POSITION_PCT", "sniper_min_liquidity_usd": "SNIPER_MIN_LIQUIDITY_USD",
    "sniper_age_min_seconds": "SNIPER_AGE_MIN_SECONDS", "sniper_age_max_seconds": "SNIPER_AGE_MAX_SECONDS",
    "own_thesis_position_pct": "OWN_THESIS_POSITION_PCT",
}

# ── Profiles ─────────────────────────────────────────────────────────
# Only the differences from the dataclass defaults (= conservative).
PROFILES = {
    "conservative": {},
    "balanced": dict(
        sizing_mode="edge", max_position_pct=0.05,
        edge_unknown_pct=0.015, edge_weak_pct=0.0075, edge_base_pct=0.02, edge_max_pct=0.04, edge_top_pct=0.05,
        max_concurrent_positions=6, max_total_exposure_pct=0.30, daily_loss_limit_pct=0.08,
        max_consecutive_losses=4, loss_cooldown_minutes=45.0, token_reentry_cooldown_minutes=120.0,
        max_trades_per_hour=10, min_liquidity_usd=10_000, min_market_cap_usd=15_000,
        max_top10_holder_pct=40.0, max_entry_price_impact_pct=2.5, max_signal_age_seconds=150.0,
        max_chase_pct=0.35, stop_loss_pct=0.25, tp_ladder=[(0.3, 0.4), (0.8, 0.3)],
        trailing_activation_pct=0.4, trailing_stop_pct=0.25, trailing_stop_tight_pct=0.2,
        time_stop_minutes=45.0, time_stop_min_gain_pct=0.08,
        sniper_position_pct=0.015, sniper_min_liquidity_usd=4_000,
        sniper_age_min_seconds=60.0, sniper_age_max_seconds=1800.0, own_thesis_position_pct=0.01,
    ),
    "degen": dict(
        sizing_mode="edge", max_position_pct=0.10,
        edge_unknown_pct=0.02, edge_weak_pct=0.01, edge_base_pct=0.03, edge_max_pct=0.08, edge_top_pct=0.10,
        edge_bench_r=-0.20,
        max_concurrent_positions=10, max_total_exposure_pct=0.50, daily_loss_limit_pct=0.15,
        max_consecutive_losses=5, loss_cooldown_minutes=30.0, token_reentry_cooldown_minutes=60.0,
        max_trades_per_hour=20, min_liquidity_usd=8_000, min_market_cap_usd=10_000,
        max_market_cap_usd=10_000_000, max_top10_holder_pct=45.0, max_entry_price_impact_pct=3.0,
        max_signal_age_seconds=180.0, max_chase_pct=0.50, stop_loss_pct=0.30,
        tp_ladder=[(0.4, 0.35), (1.0, 0.25), (3.0, 0.15)],
        trailing_activation_pct=0.5, trailing_stop_pct=0.30, trailing_stop_tight_pct=0.25,
        time_stop_minutes=60.0, time_stop_min_gain_pct=0.05, max_hold_hours=48.0,
        sniper_position_pct=0.03, sniper_min_liquidity_usd=2_500,
        sniper_age_min_seconds=30.0, sniper_age_max_seconds=900.0, own_thesis_position_pct=0.02,
    ),
    # Fast copies, quick in/out: only FRESH signals (≤ 45 s old), tight stop,
    # quick partial take-profits, trailing stop arms early, minutes-long holds.
    # Needs fast detection (WALLET_POLL_SECONDS ≈ 10-15 s): at the default
    # 60 s poll most signals arrive too stale and are skipped by design.
    "scalper": dict(
        sizing_mode="edge", max_position_pct=0.08,
        edge_unknown_pct=0.02, edge_weak_pct=0.01, edge_base_pct=0.03, edge_max_pct=0.06, edge_top_pct=0.08,
        edge_bench_r=-0.30,
        max_concurrent_positions=8, max_total_exposure_pct=0.40, daily_loss_limit_pct=0.12,
        max_consecutive_losses=4, loss_cooldown_minutes=20.0, token_reentry_cooldown_minutes=30.0,
        max_trades_per_hour=30, min_liquidity_usd=10_000, min_market_cap_usd=10_000,
        max_market_cap_usd=20_000_000, max_top10_holder_pct=45.0, max_entry_price_impact_pct=2.0,
        max_signal_age_seconds=45.0, max_chase_pct=0.15, stop_loss_pct=0.10,
        tp_ladder=[(0.12, 0.5), (0.25, 0.3)],
        trailing_activation_pct=0.15, trailing_stop_pct=0.08, trailing_stop_tight_pct=0.10,
        time_stop_minutes=5.0, time_stop_min_gain_pct=0.04, max_hold_hours=0.5,
        no_price_writeoff_minutes=10.0,
        sniper_position_pct=0.02, sniper_min_liquidity_usd=4_000,
        sniper_age_min_seconds=15.0, sniper_age_max_seconds=300.0, own_thesis_position_pct=0.01,
    ),
}

# Hard safety rails — enforced in EVERY profile, whatever the env says.
HARD_RAILS = {
    "daily_loss_limit_pct": (0.005, 0.20),
    "max_total_exposure_pct": (0.01, 0.60),
    "max_position_pct": (0.001, 0.10),
    "edge_top_pct": (0.0, 0.10), "edge_max_pct": (0.0, 0.10), "edge_base_pct": (0.0, 0.10),
    "edge_unknown_pct": (0.0, 0.05), "edge_weak_pct": (0.0, 0.05),
    "sniper_position_pct": (0.0, 0.05), "own_thesis_position_pct": (0.0, 0.03),
    "max_entry_price_impact_pct": (0.1, 5.0),
    "max_concurrent_positions": (1, 15),
    "max_trades_per_hour": (1, 30),
    "stop_loss_pct": (0.05, 0.50),
    "max_consecutive_losses": (1, 8),
}


def profile_config(name: str) -> "RiskConfig":
    return dc_replace(RiskConfig(), profile=name, **PROFILES.get(name, {}))


def apply_hard_rails(cfg: "RiskConfig") -> "RiskConfig":
    """Clamp to the rails and force the non-negotiable safety checks on. Records what was clamped."""
    changes, notes = {}, []
    for k, (lo, hi) in HARD_RAILS.items():
        v = getattr(cfg, k)
        nv = type(v)(min(max(v, lo), hi))
        if nv != v:
            changes[k] = nv
            notes.append(f"{k} {v} -> {nv}")
    if not cfg.require_mint_authority_revoked or not cfg.require_freeze_authority_revoked:
        changes.update(require_mint_authority_revoked=True, require_freeze_authority_revoked=True)
        notes.append("mint/freeze authority checks are mandatory (env override ignored)")
    if cfg.sizing_mode not in ("risk", "edge"):
        changes["sizing_mode"] = "risk"
        notes.append(f"sizing_mode {cfg.sizing_mode!r} -> 'risk'")
    if cfg.edge_top_pct < cfg.edge_max_pct:
        changes["edge_top_pct"] = cfg.edge_max_pct
    return dc_replace(cfg, rails_applied=notes, **changes)


# ── Learned (walk-forward tuned) exit parameters ─────────────────────
# tuning.py may only move these, only inside these bounds, and only after
# beating the current values on held-out data. Load order:
#   profile defaults < tuned_params.json < explicit env vars < HARD_RAILS
TUNABLE_BOUNDS = {
    "stop_loss_pct": (0.12, 0.40),
    "trailing_activation_pct": (0.20, 1.00),
    "trailing_stop_pct": (0.15, 0.35),
    "time_stop_minutes": (15.0, 120.0),
    "time_stop_min_gain_pct": (0.03, 0.20),
    "tp1_gain": (0.15, 1.50),
    "tp1_fraction": (0.20, 0.60),
}


def tuned_params_path():
    from pathlib import Path
    return Path(os.environ.get("DATA_DIR", ".")) / "tuned_params.json"


def load_tuned(profile: str) -> dict:
    """{param: value} for this profile, or {} (missing file, other profile, or LEARNING_APPLY_TUNED=false)."""
    if not _env_bool("LEARNING_APPLY_TUNED", True):
        return {}
    try:
        import json
        data = json.loads(tuned_params_path().read_text())
    except Exception:
        return {}
    if data.get("profile") != profile or not data.get("accepted"):
        return {}
    return data.get("params") or {}


def apply_tuned(cfg: "RiskConfig", params: dict) -> "RiskConfig":
    """Overlay tuned params, each clamped to TUNABLE_BOUNDS; unknown keys ignored."""
    params = params or {}
    ch = {}
    for k, v in params.items():
        if k in ("tp1_gain", "tp1_fraction") or k not in TUNABLE_BOUNDS or not isinstance(v, (int, float)):
            continue
        lo, hi = TUNABLE_BOUNDS[k]
        ch[k] = float(min(max(v, lo), hi))
    if "tp1_gain" in params or "tp1_fraction" in params:
        ladder = [list(r) for r in cfg.tp_ladder] or [[0.25, 0.5]]
        g = float(params.get("tp1_gain", ladder[0][0]))
        f = float(params.get("tp1_fraction", ladder[0][1]))
        ladder[0] = [min(max(g, TUNABLE_BOUNDS["tp1_gain"][0]), TUNABLE_BOUNDS["tp1_gain"][1]),
                     min(max(f, TUNABLE_BOUNDS["tp1_fraction"][0]), TUNABLE_BOUNDS["tp1_fraction"][1])]
        for r in ladder[1:]:  # later rungs stay above rung 1
            r[0] = max(r[0], ladder[0][0] * 1.5)
        ch["tp_ladder"] = [tuple(r) for r in ladder]
    return dc_replace(cfg, **ch) if ch else cfg


# ── Sizing ───────────────────────────────────────────────────────────

def position_size(equity: float, cfg: RiskConfig, stop_loss_pct: Optional[float] = None, scale: float = 1.0) -> float:
    """
    Fixed-fractional risk sizing: size so that hitting the stop (plus an
    assumed gap/slippage buffer) loses ~risk_per_trade_pct of equity,
    then hard-cap at max_position_pct. `scale` (0..1) can only shrink a
    position (e.g. drawdown mode) — never grow it past the cap. Returns 0
    when the result is below min_position_sol (caller should skip).
    """
    if equity <= 0:
        return 0.0
    stop = stop_loss_pct if stop_loss_pct is not None else cfg.stop_loss_pct
    effective_loss = max(1e-6, stop * cfg.stop_gap_buffer)
    size = equity * cfg.risk_per_trade_pct / effective_loss
    size = min(size, equity * cfg.max_position_pct)
    size *= max(0.0, min(1.0, scale))
    return size if size >= cfg.min_position_sol else 0.0


# ── Edge-based sizing (per copied wallet / per source) ──────────────

def wallet_edge(closed_trades: list, now_ts: float, cfg: RiskConfig, key: str = "wallet") -> dict:
    """
    Rolling, recency-weighted edge per wallet (or per any `key`, e.g. "source").

    R = pnl_pct / stop_loss_pct (one stop-loss = -1R). Each outcome is weighted
    0.5 ** (age_days / half_life). The weighted mean R is shrunk toward 0 with
    `edge_prior_trades` pseudo-trades, so 2 lucky wins don't earn top size:
        shrunk_r = Σ w·R / (Σ w + prior)
    Returns {k: {n, n_eff, win_rate, avg_r, shrunk_r, expectancy_pct, tier}}.
    tier: unknown (< wallet_min_trades) | benched | weak | core | top.
    """
    stop = max(cfg.stop_loss_pct, 1e-6)
    hl = max(cfg.edge_half_life_days, 1e-6)
    cutoff = now_ts - cfg.wallet_lookback_days * 86400
    acc: dict = {}
    for t in closed_trades:
        k = t.get(key)
        ts = t.get("closed_ts") or 0
        if not k or ts < cutoff:
            continue
        pnl = float(t.get("pnl_pct") or 0.0)
        w = 0.5 ** (max(0.0, now_ts - ts) / 86400 / hl)
        a = acc.setdefault(k, {"n": 0, "w": 0.0, "wr": 0.0, "wwin": 0.0, "pnl": 0.0})
        a["n"] += 1
        a["w"] += w
        a["wr"] += w * (pnl / stop)
        a["wwin"] += w * (1.0 if pnl > 0 else 0.0)
        a["pnl"] += pnl
    out = {}
    for k, a in acc.items():
        avg_r = a["wr"] / a["w"] if a["w"] else 0.0
        shrunk = a["wr"] / (a["w"] + cfg.edge_prior_trades)
        if a["n"] < cfg.wallet_min_trades:
            tier = "unknown"
        elif shrunk < cfg.edge_bench_r:
            tier = "benched"
        elif shrunk < 0:
            tier = "weak"
        elif shrunk >= 2 * cfg.edge_target_r and a["n"] >= 2 * cfg.wallet_min_trades:
            tier = "top"
        else:
            tier = "core"
        out[k] = {"n": a["n"], "n_eff": round(a["w"], 2), "win_rate": a["wwin"] / a["w"] if a["w"] else 0.0,
                  "avg_r": avg_r, "shrunk_r": shrunk, "expectancy_pct": a["pnl"] / a["n"], "tier": tier}
    return out


def edge_position_pct(stat: Optional[dict], cfg: RiskConfig) -> float:
    """Fraction of equity for one copy of a wallet with this edge stat (None = never seen)."""
    if not stat or stat.get("tier") == "unknown":
        return cfg.edge_unknown_pct
    tier = stat["tier"]
    if tier == "benched":
        return 0.0
    if tier == "weak":
        return cfg.edge_weak_pct
    if tier == "top":
        return cfg.edge_top_pct
    x = max(0.0, min(1.0, stat["shrunk_r"] / max(cfg.edge_target_r, 1e-6)))
    return cfg.edge_base_pct + (cfg.edge_max_pct - cfg.edge_base_pct) * x


def size_for_signal(equity: float, cfg: RiskConfig, source: str, wallet_stat: Optional[dict] = None,
                    scale: float = 1.0) -> tuple:
    """
    One sizing entry point for every entry type. Returns (size, note).
    size is in the equity's unit; 0 means skip (note says why).
    """
    if equity <= 0:
        return 0.0, "no equity"
    if wallet_stat and wallet_stat.get("tier") == "benched":
        return 0.0, f"wallet benched (edge {wallet_stat['shrunk_r']:+.2f}R over {wallet_stat['n']} trades)"
    if source == "sniper":
        pct = cfg.sniper_position_pct if cfg.sizing_mode == "edge" else None
    elif source == "own_thesis":
        pct = cfg.own_thesis_position_pct
    elif cfg.sizing_mode == "edge":
        pct = edge_position_pct(wallet_stat, cfg)
    else:
        pct = None
    if pct is None:
        size = position_size(equity, cfg, scale=scale)
        note = f"risk {cfg.risk_per_trade_pct:.1%}/trade"
    else:
        size = equity * min(pct, cfg.max_position_pct) * max(0.0, min(1.0, scale))
        tier = (wallet_stat or {}).get("tier", "unknown") if source not in ("sniper", "own_thesis") else source
        note = f"{pct:.1%} of equity ({tier})"
        if size < cfg.min_position_sol:
            size = 0.0
    if size <= 0:
        return 0.0, note + " — below minimum ticket"
    return size, note


def fit_size_to_impact(size_usd: float, liquidity_usd: Optional[float], cfg: RiskConfig) -> float:
    """Largest size (USD) whose estimated impact stays within max_entry_price_impact_pct (the rail)."""
    if not liquidity_usd or liquidity_usd <= 0:
        return size_usd
    cap = cfg.max_entry_price_impact_pct / 100.0 * liquidity_usd / 2.0
    return min(size_usd, cap)


# ── Portfolio-level pre-trade checks ─────────────────────────────────

@dataclass
class PortfolioSnapshot:
    equity: float                       # cash + marked-to-market open positions
    cash: float
    exposure: float                     # cost basis currently committed to open positions
    open_positions: int
    day_start_equity: float
    realized_pnl_today: float
    consecutive_losses: int = 0
    last_loss_ts: Optional[float] = None
    trades_last_hour: int = 0
    token_last_exit_ts: dict = field(default_factory=dict)
    held_tokens: set = field(default_factory=set)


def check_portfolio_limits(snap: PortfolioSnapshot, token: str, size: float, now_ts: float, cfg: RiskConfig) -> tuple:
    """Returns (ok, reason). Every rule a desk would apply before ANY new risk."""
    if token in snap.held_tokens:
        return False, "already holding this token"
    if size <= 0:
        return False, "position size rounds to zero (below minimum ticket)"
    if size > snap.cash:
        return False, "insufficient cash"
    if snap.open_positions >= cfg.max_concurrent_positions:
        return False, f"max concurrent positions ({cfg.max_concurrent_positions}) reached"
    if snap.equity > 0 and (snap.exposure + size) / snap.equity > cfg.max_total_exposure_pct + 1e-9:
        return False, f"total exposure would exceed {cfg.max_total_exposure_pct:.0%} of equity"
    if snap.day_start_equity > 0:
        day_pnl_pct = snap.realized_pnl_today / snap.day_start_equity
        if day_pnl_pct <= -cfg.daily_loss_limit_pct:
            return False, f"daily loss limit hit ({day_pnl_pct:.1%} today, limit -{cfg.daily_loss_limit_pct:.0%})"
    if snap.consecutive_losses >= cfg.max_consecutive_losses and snap.last_loss_ts is not None:
        remaining = cfg.loss_cooldown_minutes * 60 - (now_ts - snap.last_loss_ts)
        if remaining > 0:
            return False, f"cooldown after {snap.consecutive_losses} straight losses ({remaining / 60:.0f} min left)"
    last_exit = snap.token_last_exit_ts.get(token)
    if last_exit is not None and now_ts - last_exit < cfg.token_reentry_cooldown_minutes * 60:
        return False, "token re-entry cooldown (exited this token recently)"
    if snap.trades_last_hour >= cfg.max_trades_per_hour:
        return False, f"hourly trade limit ({cfg.max_trades_per_hour}) hit"
    return True, "ok"


# ── Token safety ─────────────────────────────────────────────────────

UNKNOWN = "unknown"

# Token-2022 extensions that let someone other than the holder control
# or tax the token — any of these is a honeypot/rug vector for a copier.
DANGEROUS_TOKEN2022_EXTENSIONS = {
    "transferFeeConfig", "permanentDelegate", "nonTransferable",
    "transferHook", "defaultAccountState", "pausableConfig",
}


@dataclass
class TokenSafetyInfo:
    liquidity_usd: Optional[float] = None
    market_cap_usd: Optional[float] = None
    top10_pct: Optional[float] = None
    mint_authority: Optional[str] = UNKNOWN    # None = revoked, UNKNOWN = couldn't read
    freeze_authority: Optional[str] = UNKNOWN
    extensions: list = field(default_factory=list)
    signal_age_seconds: Optional[float] = None
    chase_pct: Optional[float] = None           # price now vs the copied wallet's fill, -1..inf
    is_stablecoin: bool = False


def estimate_price_impact_pct(size_usd: float, liquidity_usd: Optional[float]) -> Optional[float]:
    """Constant-product estimate: buying x against a pool whose quote side is L/2 moves price ≈ x/(L/2)."""
    if not liquidity_usd or liquidity_usd <= 0:
        return None
    return size_usd / (liquidity_usd / 2.0) * 100.0


def check_token_safety(info: TokenSafetyInfo, size_usd: float, cfg: RiskConfig, apply_mcap_floor: bool = True) -> tuple:
    """Returns (ok, reason). Missing data rejects when fail_closed_on_missing_data is on."""
    fc = cfg.fail_closed_on_missing_data
    if info.is_stablecoin:
        return False, "stablecoin — nothing to trade"
    if info.signal_age_seconds is not None and info.signal_age_seconds > cfg.max_signal_age_seconds:
        return False, f"signal is stale ({info.signal_age_seconds:.0f}s old > {cfg.max_signal_age_seconds:.0f}s)"
    if info.chase_pct is not None and info.chase_pct > cfg.max_chase_pct:
        return False, f"price already {info.chase_pct:+.0%} above the copied wallet's fill — chasing"
    if info.liquidity_usd is None:
        if fc:
            return False, "liquidity unknown"
    elif info.liquidity_usd < cfg.min_liquidity_usd:
        return False, f"liquidity too thin (${info.liquidity_usd:,.0f} < ${cfg.min_liquidity_usd:,.0f})"
    if info.market_cap_usd is not None:
        if apply_mcap_floor and info.market_cap_usd < cfg.min_market_cap_usd:
            return False, f"market cap too small (${info.market_cap_usd:,.0f})"
        if info.market_cap_usd > cfg.max_market_cap_usd:
            return False, f"market cap too large (${info.market_cap_usd:,.0f})"
    if info.top10_pct is not None and info.top10_pct > cfg.max_top10_holder_pct:
        return False, f"top-10 holders own {info.top10_pct:.0f}% (> {cfg.max_top10_holder_pct:.0f}%)"
    if cfg.require_mint_authority_revoked:
        if info.mint_authority == UNKNOWN:
            if fc:
                return False, "mint authority unknown"
        elif info.mint_authority:
            return False, "mint authority not revoked (supply can be inflated)"
    if cfg.require_freeze_authority_revoked:
        if info.freeze_authority == UNKNOWN:
            if fc:
                return False, "freeze authority unknown"
        elif info.freeze_authority:
            return False, "freeze authority not revoked (holders can be frozen — honeypot risk)"
    bad_ext = DANGEROUS_TOKEN2022_EXTENSIONS.intersection(info.extensions or [])
    if bad_ext:
        return False, f"dangerous Token-2022 extension(s): {', '.join(sorted(bad_ext))}"
    impact = estimate_price_impact_pct(size_usd, info.liquidity_usd)
    if impact is not None and impact > cfg.max_entry_price_impact_pct:
        return False, f"estimated price impact {impact:.1f}% > {cfg.max_entry_price_impact_pct:.1f}%"
    return True, "ok"


# ── Exit engine ──────────────────────────────────────────────────────

def new_exit_state(entry_price: float, size: float, opened_ts: float) -> dict:
    """Fields the exit engine tracks on a position (merged into the position dict)."""
    return {
        "entry_price": entry_price, "size": size, "original_size": size,
        "opened_ts": opened_ts, "peak_price": entry_price, "tp_rungs_hit": [],
        "last_price_ts": opened_ts,
    }


def evaluate_exit(pos: dict, price: Optional[float], now_ts: float, cfg: RiskConfig) -> tuple:
    """
    Decides what to do with one open position at one price tick.

    Returns (actions, updates):
      actions — list of {"fraction": f, "reason": str}; fraction is of the
                REMAINING size at the moment it executes, in order. A
                fraction of 1.0 closes the position; nothing after it runs.
      updates — dict of fields to write back onto the position
                (peak_price, tp_rungs_hit, last_price_ts).

    Order of precedence: no-price write-off > hard/breakeven stop >
    trailing stop > take-profit ladder > time stop > max hold. Stops are
    mechanical — no LLM can override them (the old code let an LLM
    "buy the dip" instead of stopping out).
    """
    actions, updates = [], {}
    held_s = now_ts - pos["opened_ts"]

    if price is None or price <= 0:
        last_ts = pos.get("last_price_ts", pos["opened_ts"])
        if now_ts - last_ts >= cfg.no_price_writeoff_minutes * 60:
            actions.append({"fraction": 1.0, "reason": "no_price_writeoff", "price_override": 0.0})
        return actions, updates

    updates["last_price_ts"] = now_ts
    entry = pos["entry_price"]
    peak = max(pos.get("peak_price") or entry, price)
    updates["peak_price"] = peak
    gain = price / entry - 1.0
    peak_gain = peak / entry - 1.0
    rungs_hit = list(pos.get("tp_rungs_hit") or [])

    # 1. Stops
    stop_price = entry * (1.0 - cfg.stop_loss_pct)
    stop_reason = "stop_loss"
    if cfg.breakeven_after_first_tp and rungs_hit:
        be = entry * (1.0 + 2 * cfg.paper_cost_per_side_pct)
        if be > stop_price:
            stop_price, stop_reason = be, "breakeven_stop"
    if price <= stop_price:
        actions.append({"fraction": 1.0, "reason": stop_reason})
        return actions, updates

    # 2. Trailing stop
    if peak_gain >= cfg.trailing_activation_pct:
        trail = cfg.trailing_stop_tight_pct if peak_gain >= 1.0 else cfg.trailing_stop_pct
        if price <= peak * (1.0 - trail):
            actions.append({"fraction": 1.0, "reason": "trailing_stop"})
            return actions, updates

    # 3. Take-profit ladder (fractions are of ORIGINAL size)
    remaining = pos["size"]
    original = pos.get("original_size") or remaining
    for i, (rung_gain, rung_frac) in enumerate(cfg.tp_ladder):
        if i in rungs_hit or gain < rung_gain:
            continue
        sell_abs = min(remaining, rung_frac * original)
        if remaining <= 0:
            break
        frac = sell_abs / remaining
        actions.append({"fraction": min(1.0, frac), "reason": f"take_profit_{i + 1}"})
        remaining -= sell_abs
        rungs_hit.append(i)
    if rungs_hit != list(pos.get("tp_rungs_hit") or []):
        updates["tp_rungs_hit"] = rungs_hit
    if remaining <= 1e-12:
        return actions, updates

    # 4. Time stop — capital stuck in a trade that isn't working
    if held_s >= cfg.time_stop_minutes * 60 and peak_gain < cfg.time_stop_min_gain_pct and not rungs_hit:
        actions.append({"fraction": 1.0, "reason": "time_stop"})
        return actions, updates

    # 5. Max hold
    if held_s >= cfg.max_hold_hours * 3600:
        actions.append({"fraction": 1.0, "reason": "max_hold"})
    return actions, updates


# ── Wallet scoring ───────────────────────────────────────────────────

def score_wallets(closed_trades: list, now_ts: float, cfg: RiskConfig) -> dict:
    """
    closed_trades: [{"wallet": str, "pnl_pct": float, "closed_ts": float}, ...]
    (one entry per fully-closed position, pnl_pct on total capital put in).
    Returns {wallet: {"n", "win_rate", "expectancy_pct", "enabled"}}. A wallet
    is disabled once it has ≥ wallet_min_trades closed copies in the
    lookback window AND its average result is below wallet_min_expectancy_pct.
    Wallets with too few trades stay enabled (not enough evidence yet).
    """
    cutoff = now_ts - cfg.wallet_lookback_days * 86400
    by_wallet: dict = {}
    for t in closed_trades:
        w = t.get("wallet")
        if not w or (t.get("closed_ts") or 0) < cutoff:
            continue
        by_wallet.setdefault(w, []).append(float(t.get("pnl_pct") or 0.0))
    scores = {}
    for w, pnls in by_wallet.items():
        n = len(pnls)
        exp = sum(pnls) / n
        scores[w] = {
            "n": n,
            "win_rate": sum(1 for p in pnls if p > 0) / n,
            "expectancy_pct": exp,
            "enabled": not (n >= cfg.wallet_min_trades and exp < cfg.wallet_min_expectancy_pct),
        }
    return scores


# ── Day bookkeeping ──────────────────────────────────────────────────

def utc_day(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")


def consecutive_losses(closed_trades: list) -> tuple:
    """(count of most-recent consecutive losing closes, ts of the latest loss or None). Expects chronological order."""
    count, last_loss_ts = 0, None
    for t in reversed(closed_trades):
        if (t.get("pnl_pct") or 0) < 0:
            count += 1
            if last_loss_ts is None:
                last_loss_ts = t.get("closed_ts")
        else:
            break
    return count, last_loss_ts
