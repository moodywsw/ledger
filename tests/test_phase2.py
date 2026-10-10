"""Phase 2: profiles + hard rails, edge sizing, tuned-param overlay, sniper path,
GeckoTerminal mapping, learning loop, own theses, Fomo parsing. All offline."""
import time

import pytest

import fomo
import learning
import market_data
import own_thesis
import trade_cards
import tuning
from risk_engine import (RiskConfig, apply_tuned, edge_position_pct, fit_size_to_impact, size_for_signal,
                         wallet_edge)

NOW = 1_800_000_000.0


# ── profiles & rails ────────────────────────────────────────────────

def test_profiles_and_defaults(monkeypatch):
    monkeypatch.delenv("RISK_PROFILE", raising=False)
    c = RiskConfig.from_env()
    assert c.profile == "conservative" and c.sizing_mode == "risk"
    d = RiskConfig.from_env("degen")
    assert d.sizing_mode == "edge" and d.edge_top_pct == pytest.approx(0.10) and d.max_concurrent_positions == 10
    b = RiskConfig.from_env("balanced")
    assert b.edge_max_pct < d.edge_max_pct and b.daily_loss_limit_pct < d.daily_loss_limit_pct
    assert RiskConfig.from_env("nonsense").profile == "conservative"


def test_hard_rails_clamp_env_in_every_profile(monkeypatch):
    monkeypatch.setenv("RISK_MAX_POSITION_PCT", "0.5")
    monkeypatch.setenv("EDGE_TOP_PCT", "0.4")
    monkeypatch.setenv("RISK_DAILY_LOSS_LIMIT_PCT", "0.9")
    monkeypatch.setenv("RISK_MAX_TOTAL_EXPOSURE_PCT", "1.0")
    monkeypatch.setenv("FILTER_MAX_ENTRY_PRICE_IMPACT_PCT", "50")
    monkeypatch.setenv("FILTER_REQUIRE_MINT_AUTHORITY_REVOKED", "false")
    monkeypatch.setenv("FILTER_REQUIRE_FREEZE_AUTHORITY_REVOKED", "false")
    for p in ("conservative", "balanced", "degen"):
        c = RiskConfig.from_env(p)
        assert c.max_position_pct == pytest.approx(0.10)
        assert c.edge_top_pct == pytest.approx(0.10)
        assert c.daily_loss_limit_pct == pytest.approx(0.20)
        assert c.max_total_exposure_pct == pytest.approx(0.60)
        assert c.max_entry_price_impact_pct == pytest.approx(5.0)
        assert c.require_mint_authority_revoked and c.require_freeze_authority_revoked
        assert c.rails_applied


# ── edge sizing ─────────────────────────────────────────────────────

def _trades(wallet, pnls, start=NOW - 86400):
    return [{"wallet": wallet, "pnl_pct": p, "closed_ts": start + i * 60} for i, p in enumerate(pnls)]


def test_wallet_edge_tiers():
    cfg = RiskConfig.from_env("degen")
    t = _trades("NEW", [0.5, 0.5]) + _trades("BAD", [-0.3] * 8) + _trades("TOP", [0.9, 0.6, -0.3, 1.2, 0.8] * 3) \
        + _trades("MEH", [0.3, -0.3, -0.3, 0.2, -0.1, -0.05])
    e = wallet_edge(t, NOW, cfg)
    assert e["NEW"]["tier"] == "unknown"
    assert e["BAD"]["tier"] == "benched"
    assert e["TOP"]["tier"] == "top"
    assert e["MEH"]["tier"] in ("weak", "core")


def test_degen_sizes_by_wallet_edge():
    cfg = RiskConfig.from_env("degen")
    t = _trades("BAD", [-0.3] * 8) + _trades("TOP", [0.9, 0.6, -0.3, 1.2, 0.8] * 3)
    e = wallet_edge(t, NOW, cfg)
    eq = 10.0
    top, _ = size_for_signal(eq, cfg, "priority_copy", e["TOP"])
    unk, _ = size_for_signal(eq, cfg, "priority_copy", None)
    bad, note = size_for_signal(eq, cfg, "priority_copy", e["BAD"])
    assert top == pytest.approx(1.0)          # 10% of equity
    assert unk == pytest.approx(0.2)          # unknown wallets start small (2%)
    assert bad == 0 and "benched" in note
    snipe, _ = size_for_signal(eq, cfg, "sniper")
    assert snipe == pytest.approx(0.3)
    own, _ = size_for_signal(eq, cfg, "own_thesis")
    assert own == pytest.approx(0.2)


def test_conservative_keeps_risk_sizing():
    cfg = RiskConfig.from_env("conservative")
    s, note = size_for_signal(10.0, cfg, "priority_copy", None)
    assert "risk" in note and s <= 10.0 * cfg.max_position_pct


def test_edge_scales_between_base_and_max():
    cfg = RiskConfig.from_env("degen")
    lo = edge_position_pct({"tier": "core", "shrunk_r": 0.0}, cfg)
    hi = edge_position_pct({"tier": "core", "shrunk_r": cfg.edge_target_r}, cfg)
    assert lo == pytest.approx(cfg.edge_base_pct) and hi == pytest.approx(cfg.edge_max_pct)


def test_fit_size_to_impact():
    cfg = RiskConfig.from_env("degen")  # 3% impact cap
    assert fit_size_to_impact(1000, 20_000, cfg) == pytest.approx(300)
    assert fit_size_to_impact(100, 20_000, cfg) == 100
    assert fit_size_to_impact(100, None, cfg) == 100


# ── tuned overlay ───────────────────────────────────────────────────

def test_apply_tuned_is_bounded_and_env_wins(monkeypatch, tmp_path):
    monkeypatch.delenv("LEARNING_APPLY_TUNED", raising=False)
    base = RiskConfig.from_env("balanced")
    t = apply_tuned(base, {"stop_loss_pct": 0.9, "tp1_gain": 0.05, "max_position_pct": 0.5, "time_stop_minutes": 20})
    assert t.stop_loss_pct == pytest.approx(0.40)          # clamped to TUNABLE_BOUNDS
    assert t.tp_ladder[0][0] == pytest.approx(0.15)
    assert t.max_position_pct == base.max_position_pct      # not tunable
    assert t.time_stop_minutes == 20
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    (tmp_path / "tuned_params.json").write_text(
        '{"profile": "balanced", "accepted": true, "params": {"time_stop_minutes": 20, "stop_loss_pct": 0.2}}')
    assert RiskConfig.from_env("balanced").time_stop_minutes == 20
    assert RiskConfig.from_env("degen").time_stop_minutes != 20           # other profile untouched
    monkeypatch.setenv("EXIT_STOP_LOSS_PCT", "0.33")
    assert RiskConfig.from_env("balanced").stop_loss_pct == pytest.approx(0.33)  # explicit env beats tuned
    monkeypatch.setenv("LEARNING_APPLY_TUNED", "false")
    assert RiskConfig.from_env("balanced").time_stop_minutes == 45


def test_trade_return_accounts_partials_and_costs():
    ex = [(0, 1.5, 0.5, "tp"), (1, 0.8, 1.0, "stop")]
    assert tuning._trade_return(ex, 1.0, 0.0) == pytest.approx(0.5 * 0.5 + 0.5 * -0.2)


# ── market data ─────────────────────────────────────────────────────

def test_gt_pool_maps_to_dexscreener_shape():
    pool = {"attributes": {"address": "POOL", "name": "MYG / SOL", "base_token_price_usd": "0.0000031",
                           "reserve_in_usd": "2448.5", "fdv_usd": "3089.6", "market_cap_usd": None,
                           "pool_created_at": "2026-10-09T09:36:45Z",
                           "transactions": {"h1": {"buys": 34, "sells": 23, "buyers": 31}},
                           "price_change_percentage": {"h1": "-22.4"}, "volume_usd": {"h1": "4626"}},
            "relationships": {"dex": {"data": {"id": "pump-fun"}}, "base_token": {"data": {"id": "solana_MINTpump"}}}}
    p = market_data._pool_to_pair(pool)
    assert p["baseToken"]["address"] == "MINTpump" and p["dexId"] == "pump-fun"
    assert float(p["priceUsd"]) == pytest.approx(3.1e-6)
    assert p["liquidity"]["usd"] == pytest.approx(2448.5) and p["marketCap"] == pytest.approx(3089.6)
    assert p["txns"]["h1"] == {"buys": 34, "sells": 23} and p["priceChange"]["h1"] == pytest.approx(-22.4)


def test_pda_detection():
    from solders.pubkey import Pubkey
    pda, _ = Pubkey.find_program_address([b"bonding-curve"], Pubkey.from_string("6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"))
    assert market_data.is_program_owned(str(pda))
    assert not market_data.is_program_owned("ApXfgLxrA1Qj6Sk9kYE36wJoZNEsrnKceZqiL2sEYgBU")


def test_dexscreener_falls_back_to_geckoterminal(monkeypatch):
    import ledger_bot as lb

    class R:
        def raise_for_status(self): pass
        def json(self): return {"pairs": None}
    monkeypatch.setattr(lb.requests, "get", lambda *a, **k: R())
    monkeypatch.setattr(lb.market_data, "gt_best_pair", lambda m: {"priceUsd": "0.001", "liquidity": {"usd": 9000}, "fdv": 50_000})
    lb._DEX_CACHE.clear()
    assert lb.get_sniper_exit_price("Xpump") == pytest.approx(0.001)
    assert lb.get_liquidity_and_market_cap("Xpump") == (9000, 50_000)


# ── sniper actually trades on paper ─────────────────────────────────

def test_sniper_opens_paper_position_without_llm_or_ws_socials(monkeypatch):
    import ledger_bot as lb
    cfg = RiskConfig.from_env("degen")
    monkeypatch.setattr(lb, "RISK", cfg)
    monkeypatch.setattr(lb, "PAPER_TRADING_ENABLED", True)
    monkeypatch.setattr(lb, "SNIPER_MIN_CONFIDENCE_TO_ENTER", 0.0)
    monkeypatch.setattr(lb, "get_top10_holder_pct", lambda m: 18.0)
    monkeypatch.setattr(lb, "get_dev_holding_pct", lambda m, c: 3.0)
    monkeypatch.setattr(lb, "get_approx_holder_count", lambda m: 20)
    monkeypatch.setattr(lb.market_data, "fetch_launch_metadata", lambda uri: {"twitter": "https://x.com/proj"})
    monkeypatch.setattr(lb, "get_liquidity_and_market_cap", lambda m: (12_000, 40_000))
    monkeypatch.setattr(lb, "get_wash_trading_flag", lambda m: {"suspicious": False})
    monkeypatch.setattr(lb, "risk_gate_entry", lambda *a, **k: {"ok": True, "size": 0.3, "reason": "ok"})
    monkeypatch.setattr(lb, "get_sniper_entry_price", lambda m: 0.0001)
    monkeypatch.setattr(lb, "get_token_history", lambda *a, **k: [])
    monkeypatch.setattr(lb, "get_token_metadata", lambda m: {"symbol": "SNP", "name": "Snipe"})
    monkeypatch.setattr(lb, "check_is_narrative_token", lambda *a: False)
    monkeypatch.setattr(lb, "get_sol_price_usd", lambda: 100.0)
    monkeypatch.setattr(lb, "get_snipe_confidence", lambda *a, **k: pytest.fail("LLM must not be called"))
    posted = []
    monkeypatch.setattr(lb, "speak", lambda *a, **k: posted.append(k))
    monkeypatch.setattr(lb, "REAL_TRADING_ENABLED", False, raising=False)
    st = lb.LedgerState()
    st.balance_sol = 10.0
    cand = {"mint": "SNIPEpump", "symbol": "SNP", "name": "Snipe", "creator": "DEV", "initial_buy_sol": 1.0,
            "twitter": None, "telegram": None, "website": None, "uri": "https://ipfs.io/ipfs/x", "kind": "launch",
            "first_seen": time.time() - 60}
    lb.evaluate_snipe_candidate(cand, st)
    assert "SNIPEpump" in st.open_positions
    assert st.open_positions["SNIPEpump"]["source"] == "sniper"
    assert posted and posted[0]["embed"]["title"].startswith("🟢 ENTRY")


def test_sniper_skips_unknown_liquidity(monkeypatch):
    import ledger_bot as lb
    monkeypatch.setattr(lb, "RISK", RiskConfig.from_env("degen"))
    monkeypatch.setattr(lb, "PAPER_TRADING_ENABLED", True)
    monkeypatch.setattr(lb, "get_top10_holder_pct", lambda m: 18.0)
    monkeypatch.setattr(lb, "get_dev_holding_pct", lambda m, c: 3.0)
    monkeypatch.setattr(lb, "get_approx_holder_count", lambda m: 20)
    monkeypatch.setattr(lb.market_data, "fetch_launch_metadata", lambda uri: {"telegram": "https://t.me/proj"})
    monkeypatch.setattr(lb, "get_liquidity_and_market_cap", lambda m: (None, None))
    monkeypatch.setattr(lb, "risk_gate_entry", lambda *a, **k: pytest.fail("should stop before the gate"))
    st = lb.LedgerState()
    cand = {"mint": "Apump", "symbol": "A", "creator": "D", "initial_buy_sol": 1, "uri": "u", "first_seen": time.time()}
    results = [lb.evaluate_snipe_candidate(cand, st) for _ in range(lb.SNIPER_MAX_DATA_RETRIES + 1)]
    assert results[:-1] == ["retry"] * lb.SNIPER_MAX_DATA_RETRIES and results[-1] is None  # waits, then gives up
    assert not st.open_positions


def test_sniper_age_window_follows_profile(monkeypatch):
    import ledger_bot as lb
    monkeypatch.setattr(lb, "RISK", RiskConfig.from_env("degen"))
    assert lb.sniper_age_window_seconds() == (30.0, 900.0)


# ── learning loop ───────────────────────────────────────────────────

@pytest.fixture
def learn_dir(monkeypatch, tmp_path):
    monkeypatch.setattr(learning, "ROSTER_FILE", tmp_path / "roster.json")
    monkeypatch.setattr(learning, "SHADOW_FILE", tmp_path / "shadow.json")
    monkeypatch.setattr(learning, "LEARNING_JOURNAL", tmp_path / "lj.jsonl")
    monkeypatch.setattr(learning, "SIGNALS_FILE", tmp_path / "signals.jsonl")
    monkeypatch.setattr(learning, "SCORES_FILE", tmp_path / "scores.json")
    monkeypatch.setattr(learning, "LEARNING_ENABLED", True)
    return tmp_path


def test_shadow_book_runs_exit_engine(learn_dir):
    cfg = RiskConfig(paper_cost_per_side_pct=0.0, stop_loss_pct=0.2)
    learning.shadow_open("W", "M", 1.0, "TOK")
    learning.shadow_tick(lambda ms: {"M": 0.7}, cfg, now=time.time() + 30)
    closed = learning.shadow_closed_trades()
    assert len(closed) == 1 and closed[0]["wallet"] == "W" and closed[0]["pnl_pct"] == pytest.approx(-0.3)
    assert closed[0]["shadow"] is True


def test_roster_promote_drop_bench(learn_dir, monkeypatch):
    cfg = RiskConfig.from_env("degen")
    monkeypatch.setattr(learning, "LEARNING_PROMOTE_MIN_TRADES", 6)
    learning.add_candidate("GOOD", "disc:GOOD", "geckoterminal", "test", set())
    learning.add_candidate("LOSER", "disc:LOSE", "fomo", "test", set())
    assert learning.wallet_status("GOOD") == "shadow"
    now = time.time()
    book = {"open": {}, "closed":
            [{"wallet": "GOOD", "pnl_pct": 0.6, "closed_ts": now - i, "shadow": True} for i in range(10)] +
            [{"wallet": "LOSER", "pnl_pct": -0.3, "closed_ts": now - i, "shadow": True} for i in range(10)]}
    learning._write_json(learning.SHADOW_FILE, book)
    live = [{"wallet": "TRACKED", "pnl_pct": -0.3, "closed_ts": now - i} for i in range(8)]
    changes = dict(learning.review_roster(live, cfg, {"TRACKED": "tr"}, now))
    assert changes == {"GOOD": "active", "LOSER": "dropped", "TRACKED": "benched"}
    assert "GOOD" in learning.promoted_wallets() and "TRACKED" not in learning.promoted_wallets()
    assert any("benched" in r["text"] for r in learning.recent_notes())
    assert "GOOD" in learning.summary_for_discord() or learning.summary_for_discord()


def test_shadow_cap_queues_candidates(learn_dir, monkeypatch):
    monkeypatch.setattr(learning, "LEARNING_MAX_SHADOW_WALLETS", 1)
    learning.add_candidate("A", "a", "x", "e", set())
    learning.add_candidate("B", "b", "x", "e", set())
    assert learning.wallet_status("A") == "shadow" and learning.wallet_status("B") == "candidate"
    assert not learning.add_candidate("C", "c", "x", "e", {"C"})  # already tracked


def test_discovery_finds_profit_takers():
    trades = [{"wallet": "W1", "kind": "buy", "usd": 500}, {"wallet": "W1", "kind": "sell", "usd": 3000},
              {"wallet": "W2", "kind": "sell", "usd": 9000},                       # no visible buy: skip
              {"wallet": "W3", "kind": "buy", "usd": 100}, {"wallet": "W3", "kind": "sell", "usd": 300}]
    out = learning.profitable_traders_from_trades(trades, 1000)
    assert [w for w, *_ in out] == ["W1"]


def test_feature_scoring(learn_dir):
    cfg = RiskConfig()
    trades = [{"wallet": "W", "source": "priority_copy", "pnl_pct": 0.2, "closed_ts": time.time(),
               "opened_ts": time.time() - 60, "features": {"liquidity_usd": 20_000, "chase_pct": 0.05}}] * 3
    sc = learning.score_everything(trades, cfg)
    assert sc["features"]["liq"]["15-50k"]["n"] == 3
    assert sc["sources"]["priority_copy"]["n"] == 3


# ── own thesis ──────────────────────────────────────────────────────

def _pair(h1=40, buys=60, sells=30, liq=60_000, mc=600_000):
    return {"priceUsd": "0.001", "priceChange": {"h1": h1}, "txns": {"h1": {"buys": buys, "sells": sells}},
            "liquidity": {"usd": liq}, "marketCap": mc, "baseToken": {"symbol": "THX", "name": "Thesis"}}


def test_thesis_scoring_rewards_confluence_and_penalises_chasing():
    good, why = own_thesis.score_candidate({"wallets": {"a", "b"}, "on_fomo": True, "on_gt": True, "pair": _pair()}, "chop")
    chase, _ = own_thesis.score_candidate({"wallets": set(), "on_gt": True, "pair": _pair(h1=400)}, "risk_off")
    assert good >= 8 and any("tracked wallet" in w for w in why)
    assert chase < own_thesis.OWN_THESIS_MIN_SCORE and chase < good - 6


def test_form_thesis_respects_safety_and_cadence(monkeypatch):
    monkeypatch.setattr(own_thesis, "_state", {"last_run": 0, "posted": []})
    sigs = [{"ts": time.time(), "mint": "Mpump", "wallet": w} for w in ("a", "b")]
    kw = dict(recent_signals=sigs, gt_trending=[], fomo_trending=[{"mint": "Mpump", "symbol": "THX"}],
              best_pair_fn=lambda m: _pair(), held=set(), regime={"regime": "chop", "sol_24h": 0.5})
    assert own_thesis.form_thesis(safety_fn=lambda m, s: (False, "freeze authority live", None), **kw) is None
    th = own_thesis.form_thesis(safety_fn=lambda m, s: (True, "ok", None), **kw)
    assert th["mint"] == "Mpump" and th["conviction"] in ("medium", "high") and len(th["why"]) <= 3
    own_thesis._state["last_run"] = time.time()
    assert not own_thesis.due()  # cadence: just ran
    monkeypatch.setattr(own_thesis, "OWN_THESIS_MAX_PER_DAY", 1)
    own_thesis._state["last_run"] = 0
    assert not own_thesis.due()  # daily cap: one already posted today


def test_thesis_card_is_short_and_labelled():
    c = trade_cards.thesis_card(mint="Mpump", symbol="THX", name="Thesis", why=["a", "b", "c", "d"],
                                mcap_usd=600_000, invalidation="-25% from entry", conviction="medium")
    assert c["title"] == "🧠 THESIS · 🪙 Thesis (THX)"
    assert c["description"].count("• ") == 3 and "NFA" not in str(c) and "$THX" not in str(c)


# ── Fomo ────────────────────────────────────────────────────────────

def test_fomo_disabled_without_key(monkeypatch, tmp_path):
    monkeypatch.delenv("FOMO_API_KEY", raising=False)
    monkeypatch.setattr(fomo, "CACHE_FILE", tmp_path / "f.json")
    monkeypatch.setattr(fomo.requests, "get", lambda *a, **k: pytest.fail("no network without a key"))
    assert fomo.top_traders() == [] and fomo.trending_tokens() == []


def test_fomo_parsers():
    lb = fomo.parse_leaderboard({"leaderboard": [
        {"rank": 1, "displayName": "ace", "pnlUsd": 120000, "wallets": {"solana": "SoLWallet1"}},
        {"rank": 2, "displayName": "evm-only", "wallets": {"ethereum": "0xabc"}}]})
    assert lb == [{"rank": 1, "name": "ace", "wallet": "SoLWallet1", "pnl_usd": 120000, "volume_usd": None, "trades": None}]
    tk = fomo.parse_tokens({"board": "trending", "tokens": [
        {"rank": 1, "token": {"name": "A", "symbol": "A", "address": "Apump"}, "holders": 900, "marketCapUsd": 1e6},
        {"rank": 2, "token": {"address": "0xdead"}}]})
    assert [t["mint"] for t in tk] == ["Apump"]


def test_fomo_uses_key_header_and_caches(monkeypatch, tmp_path):
    monkeypatch.setenv("FOMO_API_KEY", "k-test")
    monkeypatch.setattr(fomo, "CACHE_FILE", tmp_path / "f.json")
    calls = []

    class R:
        status_code = 200
        def raise_for_status(self): pass
        def json(self): return [{"rank": 1, "displayName": "x", "wallets": {"solana": "S1"}}]

    def get(url, params=None, timeout=None, headers=None):
        calls.append(headers["authorization"])
        return R()
    monkeypatch.setattr(fomo.requests, "get", get)
    assert fomo.top_traders()[0]["wallet"] == "S1"
    assert fomo.top_traders()[0]["wallet"] == "S1"
    assert calls == ["Bearer k-test"]


def test_fomo_handle_resolves_wallet(monkeypatch, tmp_path):
    monkeypatch.setenv("FOMO_API_KEY", "k")
    monkeypatch.setattr(fomo, "CACHE_FILE", tmp_path / "f.json")
    monkeypatch.setattr(fomo, "_get", lambda path, params=None: {"user": {"displayName": "me", "wallets": {"solana": "MyWa11et"}}})
    assert fomo.user_wallet("me") == "MyWa11et"


def test_seed_candidates_orders_by_copy_score(learn_dir, monkeypatch):
    import json as _j
    monkeypatch.setattr(learning, "LEARNING_MAX_SHADOW_WALLETS", 1)
    f = learn_dir / "seed.json"
    f.write_text(_j.dumps({"candidates": [
        {"handle": "big", "solana_wallets": ["W_BIG"], "prior_weight": 5, "boards": {"all": {"rank": 1}}},
        {"handle": "fast", "solana_wallets": ["W_FAST"], "prior_weight": 1, "copy_score": 0.4, "boards": {"7d": {"rank": 9}}},
        {"handle": "bad", "solana_wallets": ["W_BAD"], "exclude": True}]}))
    assert learning.seed_candidates(set(), str(f)) == 2
    assert learning.wallet_status("W_FAST") == "shadow"      # measured copy edge beats leaderboard size
    assert learning.wallet_status("W_BIG") == "candidate"
    assert learning.wallet_status("W_BAD") is None


def test_scalper_profile_is_fast_and_tight(monkeypatch):
    monkeypatch.delenv("LEARNING_APPLY_TUNED", raising=False)
    c = RiskConfig.from_env("scalper")
    assert c.sizing_mode == "edge" and c.stop_loss_pct == pytest.approx(0.10)
    assert c.max_signal_age_seconds <= 45 and c.max_hold_hours <= 0.5 and c.time_stop_minutes <= 5
    assert c.tp_ladder[0][0] <= 0.15 and not c.rails_applied


def test_live_small_rails(monkeypatch):
    import importlib, real_trading as rt
    monkeypatch.setenv("REAL_BUDGET_USDC", "300")
    monkeypatch.setenv("REAL_MAX_TRADE_USDC", "9")
    monkeypatch.setenv("REAL_DAILY_LOSS_CAP_USDC", "45")
    monkeypatch.setenv("REAL_COPY_ALLOWLIST", "WalletA")
    monkeypatch.setenv("REAL_SNIPER_ENABLED", "false")
    rt = importlib.reload(rt)
    assert rt.REAL_TRADING_ENABLED is False
    assert rt.live_small_buy_check(50, 0, 0) == (True, 9, "")
    assert rt.live_small_buy_check(50, 295, 0)[1] == 5
    assert not rt.live_small_buy_check(5, 300, 0)[0]
    assert not rt.live_small_buy_check(5, 0, -45)[0]
    assert rt.real_entry_allowed("priority_copy", "WalletA")[0]
    assert not rt.real_entry_allowed("priority_copy", "WalletB")[0]
    assert not rt.real_entry_allowed("sniper", "creator")[0]
    monkeypatch.setenv("REAL_KILL_SWITCH", "true")
    rt = importlib.reload(rt)
    assert not rt.live_small_buy_check(5, 0, 0)[0]
    assert rt.execute_real_trade("x", 5, "buy")["status"] == "unarmed"
    for k in ("REAL_BUDGET_USDC", "REAL_MAX_TRADE_USDC", "REAL_DAILY_LOSS_CAP_USDC", "REAL_COPY_ALLOWLIST", "REAL_SNIPER_ENABLED", "REAL_KILL_SWITCH"):
        monkeypatch.delenv(k, raising=False)
    importlib.reload(rt)
