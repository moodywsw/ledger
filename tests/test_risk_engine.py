import pytest

from risk_engine import (
    RiskConfig, PortfolioSnapshot, TokenSafetyInfo, UNKNOWN,
    position_size, check_portfolio_limits, check_token_safety, estimate_price_impact_pct,
    new_exit_state, evaluate_exit, score_wallets, consecutive_losses,
)

CFG = RiskConfig(tp_ladder=[(0.5, 0.33), (1.0, 0.33)])  # ladder pinned: these tests check mechanics, not defaults


def snap(**kw):
    base = dict(equity=10.0, cash=10.0, exposure=0.0, open_positions=0,
                day_start_equity=10.0, realized_pnl_today=0.0)
    base.update(kw)
    return PortfolioSnapshot(**base)


# ── sizing ──────────────────────────────────────────────────────────
def test_size_is_risk_based_and_capped():
    # 1% risk / (20% stop * 1.5 buffer) = 3.33% of equity, under the 5% cap
    assert position_size(10.0, CFG) == pytest.approx(10 * 0.01 / 0.30)
    # very tight stop would imply a huge size -> capped at 5%
    assert position_size(10.0, CFG, stop_loss_pct=0.01) == pytest.approx(0.5)


def test_size_scale_only_shrinks_and_min_ticket():
    assert position_size(10.0, CFG, scale=5.0) == position_size(10.0, CFG)
    assert position_size(10.0, CFG, scale=0.5) == pytest.approx(position_size(10.0, CFG) / 2)
    assert position_size(0.1, CFG) == 0.0  # below min ticket
    assert position_size(0, CFG) == 0.0


# ── portfolio limits ────────────────────────────────────────────────
def test_portfolio_ok():
    ok, _ = check_portfolio_limits(snap(), "T", 0.3, 1000, CFG)
    assert ok


@pytest.mark.parametrize("kw,size,needle", [
    (dict(open_positions=4), 0.3, "concurrent"),
    (dict(exposure=1.9), 0.3, "exposure"),
    (dict(realized_pnl_today=-0.5), 0.3, "daily loss"),
    (dict(cash=0.1), 0.3, "cash"),
    (dict(trades_last_hour=6), 0.3, "hourly"),
    (dict(held_tokens={"T"}), 0.3, "already"),
    (dict(), 0.0, "zero"),
])
def test_portfolio_blocks(kw, size, needle):
    ok, reason = check_portfolio_limits(snap(**kw), "T", size, 1000, CFG)
    assert not ok and needle in reason


def test_loss_cooldown_expires():
    s = snap(consecutive_losses=3, last_loss_ts=1000)
    assert not check_portfolio_limits(s, "T", 0.3, 1000 + 59 * 60, CFG)[0]
    assert check_portfolio_limits(s, "T", 0.3, 1000 + 61 * 60, CFG)[0]


def test_token_reentry_cooldown():
    s = snap(token_last_exit_ts={"T": 0})
    assert not check_portfolio_limits(s, "T", 0.3, 100, CFG)[0]
    assert check_portfolio_limits(s, "T", 0.3, 5 * 3600, CFG)[0]


# ── token safety ────────────────────────────────────────────────────
def safe_info(**kw):
    base = dict(liquidity_usd=100_000, market_cap_usd=500_000, top10_pct=20,
                mint_authority=None, freeze_authority=None, signal_age_seconds=10, chase_pct=0.0)
    base.update(kw)
    return TokenSafetyInfo(**base)


def test_safe_token_passes():
    assert check_token_safety(safe_info(), 100, CFG) == (True, "ok")


@pytest.mark.parametrize("kw,needle", [
    (dict(liquidity_usd=5_000), "thin"),
    (dict(liquidity_usd=None), "unknown"),
    (dict(market_cap_usd=10_000_000), "too large"),
    (dict(market_cap_usd=5_000), "too small"),
    (dict(top10_pct=60), "top-10"),
    (dict(mint_authority="SomeAuth"), "mint authority"),
    (dict(freeze_authority="SomeAuth"), "freeze"),
    (dict(freeze_authority=UNKNOWN), "unknown"),
    (dict(extensions=["permanentDelegate"]), "Token-2022"),
    (dict(signal_age_seconds=900), "stale"),
    (dict(chase_pct=0.8), "chasing"),
    (dict(is_stablecoin=True), "stablecoin"),
])
def test_token_safety_blocks(kw, needle):
    ok, reason = check_token_safety(safe_info(**kw), 100, CFG)
    assert not ok and needle in reason


def test_price_impact_filter():
    assert estimate_price_impact_pct(1000, 100_000) == pytest.approx(2.0)
    ok, reason = check_token_safety(safe_info(liquidity_usd=20_000), 1000, CFG)
    assert not ok and "impact" in reason


def test_fail_open_mode_allows_unknowns():
    cfg = RiskConfig(fail_closed_on_missing_data=False)
    assert check_token_safety(safe_info(liquidity_usd=None, freeze_authority=UNKNOWN, mint_authority=UNKNOWN), 100, cfg)[0]


# ── exit engine ─────────────────────────────────────────────────────
def pos_at(entry=1.0, size=1.0, opened=0.0, **kw):
    p = new_exit_state(entry, size, opened)
    p.update(kw)
    return p


def run(p, price, t, cfg=CFG):
    actions, updates = evaluate_exit(p, price, t, cfg)
    p.update(updates)
    for a in actions:
        p["size"] -= p["size"] * a["fraction"]
    return actions


def test_hard_stop():
    p = pos_at()
    assert run(p, 0.85, 60) == []
    assert run(p, 0.79, 90) == [{"fraction": 1.0, "reason": "stop_loss"}]


def test_no_action_in_range():
    assert run(pos_at(), 1.05, 60) == []


def test_ladder_sells_fraction_of_original_then_breakeven():
    p = pos_at()
    a = run(p, 1.55, 60)
    assert [x["reason"] for x in a] == ["take_profit_1"]
    assert p["size"] == pytest.approx(0.67)
    # second rung: 0.33 of ORIGINAL out of 0.67 remaining
    a = run(p, 2.05, 120)
    assert a[0]["reason"] == "take_profit_2" and a[0]["fraction"] == pytest.approx(0.33 / 0.67)
    assert p["size"] == pytest.approx(0.34)
    # rungs never re-fire
    assert all(x["reason"].startswith("trailing") or x["reason"].startswith("breakeven") for x in run(p, 1.0, 180))


def test_gap_through_multiple_rungs_fires_all_in_order():
    p = pos_at()
    a = run(p, 3.0, 60)
    assert [x["reason"] for x in a] == ["take_profit_1", "take_profit_2"]


def test_breakeven_stop_after_tp1():
    p = pos_at()
    run(p, 1.55, 60)
    a = run(p, 1.02, 120)  # under entry + 2*1.5% costs
    assert a == [{"fraction": 1.0, "reason": "breakeven_stop"}]


def test_trailing_stop_arms_and_tightens():
    cfg = RiskConfig(tp_ladder=[])
    p = pos_at()
    assert run(p, 1.4, 60, cfg) == []           # peak +40% arms trailing (25%)
    assert run(p, 1.06, 100, cfg) == []        # 1.4*0.75 = 1.05 trail level
    assert run(p, 1.04, 120, cfg)[0]["reason"] == "trailing_stop"
    p = pos_at()
    run(p, 2.5, 60, cfg)                        # peak +150% -> tight 20% trail at 2.0
    assert run(p, 2.1, 120, cfg) == []
    assert run(p, 1.99, 180, cfg)[0]["reason"] == "trailing_stop"


def test_time_stop_only_when_not_working():
    p = pos_at()
    assert run(p, 1.02, 31 * 60) == [{"fraction": 1.0, "reason": "time_stop"}]
    p = pos_at()
    run(p, 1.2, 60)  # peak +20% >= +10% threshold
    assert run(p, 1.15, 31 * 60) == []


def test_max_hold_and_no_price_writeoff():
    p = pos_at()
    run(p, 1.2, 60)
    assert run(p, 1.25, 25 * 3600)[0]["reason"] == "max_hold"
    p = pos_at()
    assert run(p, None, 30 * 60) == []
    a = evaluate_exit(p, None, 61 * 60, CFG)[0]
    assert a[0]["reason"] == "no_price_writeoff" and a[0]["price_override"] == 0.0


# ── wallet scoring ──────────────────────────────────────────────────
def test_wallet_scoring_disables_losers_only_with_enough_evidence():
    trades = [{"wallet": "A", "pnl_pct": -0.2, "closed_ts": 100}] * 5 + \
             [{"wallet": "B", "pnl_pct": -0.5, "closed_ts": 100}] * 2 + \
             [{"wallet": "C", "pnl_pct": 0.3, "closed_ts": 100}] * 6
    s = score_wallets(trades, 200, CFG)
    assert s["A"]["enabled"] is False
    assert s["B"]["enabled"] is True   # only 2 trades: not enough evidence
    assert s["C"]["enabled"] is True and s["C"]["win_rate"] == 1.0


def test_wallet_scoring_lookback():
    trades = [{"wallet": "A", "pnl_pct": -0.2, "closed_ts": 0}] * 10
    assert score_wallets(trades, 40 * 86400, CFG) == {}


def test_consecutive_losses():
    t = [{"pnl_pct": 0.1, "closed_ts": 1}, {"pnl_pct": -0.1, "closed_ts": 2}, {"pnl_pct": -0.2, "closed_ts": 3}]
    assert consecutive_losses(t) == (2, 3)
    assert consecutive_losses([]) == (0, None)


def test_default_ladder_takes_half_early():
    # Default changed after the replay: +25% sells half, +60% another quarter.
    d = RiskConfig()
    assert d.tp_ladder == [(0.25, 0.5), (0.6, 0.25)]
    p = new_exit_state(1.0, 1.0, 0)
    acts, upd = evaluate_exit(p, 1.26, 60, d)
    assert [a["fraction"] for a in acts] == [pytest.approx(0.5)]
