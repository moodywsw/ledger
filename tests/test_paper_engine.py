"""Exit/bookkeeping tests for ledger_bot's paper engine, with every network call stubbed."""
import time
import pytest

import ledger_bot as lb


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    monkeypatch.setattr(lb, "get_token_metadata", lambda m: {"symbol": "TST", "name": "Test", "description": ""})
    monkeypatch.setattr(lb, "get_sol_price_usd", lambda: 100.0)
    monkeypatch.setattr(lb, "get_liquidity_and_market_cap", lambda m: (100_000, 500_000))
    monkeypatch.setattr(lb, "speak", lambda *a, **k: None)
    monkeypatch.setattr(lb, "get_exit_opinion", lambda *a, **k: "")
    monkeypatch.setattr(lb, "check_is_narrative_token", lambda *a: False)
    monkeypatch.setattr(lb, "upsert_thesis", lambda *a, **k: None)
    monkeypatch.setattr(lb, "PAPER_TRADING_ENABLED", True)
    monkeypatch.setattr(lb, "RISK", lb.RiskConfig(paper_cost_per_side_pct=0.0, tp_ladder=[(0.5, 0.33), (1.0, 0.33)]))


def fresh_state():
    s = lb.LedgerState()
    s.balance_sol = 10.0
    return s


def test_open_close_round_trip_records_pnl_and_cooldown():
    st = fresh_state()
    lb.open_paper_position(st, "MINT", 1.0, 0.3, opened_by="W1", source="priority_copy")
    assert st.balance_sol == pytest.approx(9.7)
    lb.partial_close_paper_position(st, "MINT", 1.5, 0.5, reason="tp")
    lb.close_paper_position(st, "MINT", 0.8, reason="stop")
    # 0.15 @ +50% = +0.075 ; 0.15 @ -20% = -0.03
    assert st.balance_sol == pytest.approx(10.045)
    rt = st.closed_positions[-1]
    assert rt["wallet"] == "W1" and rt["source"] == "priority_copy"
    assert rt["pnl_sol"] == pytest.approx(0.045) and rt["pnl_pct"] == pytest.approx(0.15)
    assert "MINT" in st.token_last_exit_ts


def test_paper_costs_applied_both_sides(monkeypatch):
    monkeypatch.setattr(lb, "RISK", lb.RiskConfig(paper_cost_per_side_pct=0.01))
    st = fresh_state()
    lb.open_paper_position(st, "MINT", 1.0, 0.5)
    lb.close_paper_position(st, "MINT", 1.0)  # flat price -> pays ~2% round trip
    assert st.balance_sol == pytest.approx(10 - 0.5 + 0.5 * (0.99 / 1.01), rel=1e-9)


def test_daily_loss_uses_today_not_lifetime():
    st = fresh_state()
    st.realized_pnl_sol = -50.0  # huge LIFETIME loss: old code blocked forever on this
    ok, reason = lb.can_open_position(st, 0.1)
    assert ok, reason
    st.trade_log.append({"action": "close", "pnl_sol": -2.5, "at": lb.datetime.now(lb.timezone.utc).isoformat()})
    ok, reason = lb.can_open_position(st, 0.1)
    assert not ok and "daily loss" in reason


def test_v2_manager_stop_and_ladder(monkeypatch):
    st = fresh_state()
    lb.open_paper_position(st, "A", 1.0, 0.3, source="priority_copy", price_source="dexscreener")
    lb.open_paper_position(st, "B", 1.0, 0.3, source="priority_copy", price_source="dexscreener")
    prices = {"A": 0.7, "B": 1.6}
    monkeypatch.setattr(lb, "get_sniper_exit_price", lambda m: prices[m])
    lb.manage_paper_positions_v2(st)
    assert "A" not in st.open_positions                       # hard stop
    assert st.open_positions["B"]["size_sol"] == pytest.approx(0.3 * 0.67)  # TP1 sold 33% of original
    assert st.open_positions["B"]["tp_rungs_hit"] == [0]
    prices["B"] = 1.0  # back to entry -> breakeven stop
    lb.manage_paper_positions_v2(st)
    assert "B" not in st.open_positions
    assert [c["reason"] for c in st.closed_positions] == ["🛑 Stop Loss", "🛡️ Breakeven Stop"]


def test_v2_no_price_writeoff(monkeypatch):
    st = fresh_state()
    lb.open_paper_position(st, "R", 1.0, 0.3, price_source="dexscreener")
    st.open_positions["R"]["last_price_ts"] = time.time() - 2 * 3600
    monkeypatch.setattr(lb, "get_sniper_exit_price", lambda m: None)
    lb.manage_paper_positions_v2(st)
    assert "R" not in st.open_positions
    assert st.closed_positions[-1]["pnl_pct"] == pytest.approx(-1.0)


def test_mirror_sell_uses_fraction_of_real_cost_basis(monkeypatch):
    calls = []
    monkeypatch.setattr(lb, "get_open_real_positions_summary", lambda: [{"mint": "M", "cost_basis_usdc": 20.0}])
    monkeypatch.setattr(lb, "execute_real_trade", lambda t, amt, side, **k: calls.append((t, amt, side)) or {"status": "unarmed", "reason": "x"})
    monkeypatch.setattr(lb, "_report_real_result", lambda *a, **k: None)
    # paper position is 1 SOL (=$100) but the real leg was clamped to $20:
    lb._mirror_real_sell({"real_trading": True, "symbol": "M"}, "M", 0.5)
    assert calls == [("M", 10.0, "sell")]  # old code asked to sell $50 -> 100% of the real position


def test_state_load_tolerates_unknown_keys_and_corruption(tmp_path, monkeypatch):
    f = tmp_path / "ledger_state.json"
    monkeypatch.setattr(lb, "STATE_FILE", f)
    f.write_text('{"balance_sol": 3.0, "some_future_key": 1}')
    assert lb.LedgerState.load().balance_sol == 3.0
    f.write_text('{"balance_sol": 3.')
    st = lb.LedgerState.load()
    assert st.balance_sol == lb.STARTING_PAPER_BALANCE_SOL
    assert list(tmp_path.glob("ledger_state.corrupt-*.json"))
    st.save()
    assert lb.LedgerState.load().balance_sol == lb.STARTING_PAPER_BALANCE_SOL


def test_extract_new_buys_spend_and_blocktime():
    W = "Wallet1111111111111111111111111111111111111"
    tx = {
        "blockTime": 1_700_000_000,
        "meta": {
            "err": None, "fee": 5000,
            "preBalances": [2_000_005_000], "postBalances": [1_000_000_000],
            "preTokenBalances": [],
            "postTokenBalances": [{"accountIndex": 1, "owner": W, "mint": "TOK", "uiTokenAmount": {"uiAmount": 1000.0}}],
            "innerInstructions": [],
        },
        "transaction": {"signatures": ["SIG"], "message": {
            "accountKeys": [{"pubkey": W}],
            "instructions": [{"program": "system", "parsed": {"type": "transfer", "info": {"source": W, "lamports": 10**9}}}],
        }},
    }
    (b,) = lb.extract_new_buys([tx], W)
    assert b["block_time"] == 1_700_000_000 and b["sol_spent"] == pytest.approx(1.0)
