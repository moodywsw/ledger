"""Discord trade-card layout + wiring tests (no network)."""
import json
import pytest

import trade_cards as tc
import ledger_bot as lb

MINT = "7GCihgDB8fe6KNjn2MYtkzZcRjQy3t9GHdC8uHYmW2hr"


def _limits_ok(card):
    assert len(card["title"]) <= 256 and len(card["description"]) <= 4096
    assert all(len(f["value"]) <= 1024 for f in card["fields"])
    assert len(json.dumps(card)) < 6000


def _vals(card):
    return {f["name"]: f["value"] for f in card["fields"]}


def test_entry_card_is_short_and_complete():
    c = tc.entry_card(mint=MINT, symbol="POPCAT", name="Popcat Fan Club", price_usd=0.00012345,
                      mcap_usd=123_456, size_sol=0.1, size_usd=15.2, thesis="x " * 300)
    _limits_ok(c)
    assert c["title"] == "🟢 ENTRY · 🪙 Popcat Fan Club (POPCAT)"
    assert "$POPCAT" not in c["title"]  # "$TICKER" pings other bots
    lines = c["description"].splitlines()
    assert lines[0] == f"📋 `{MINT}`"
    for host in ("dexscreener.com/solana/", "pump.fun/coin/", "solscan.io/token/"):
        assert host + MINT in lines[1]
    assert lines[2].startswith("🧠 ") and lines[2].endswith("…") and len(lines[2]) <= 145
    assert _vals(c) == {"💵 Entry": "$0.0001234 · $123.5K MC", "💰 Size": "0.100 SOL · $15.20"}
    assert c["color"] == tc.COLOR_ENTRY
    # trimmed spec: no paper/real tag, no footer, no source/reason/hold
    blob = json.dumps(c, ensure_ascii=False)
    for gone in ("PAPER", "REAL", "footer", "Source", "Reason", "Held"):
        assert gone not in blob


def test_exit_card_win_loss_and_trim():
    win = tc.exit_card(mint=MINT, symbol="WIF", entry_mcap_usd=200_000, exit_mcap_usd=300_000,
                       pnl_sol=0.05, pnl_usd=7.5, pnl_pct=0.5)
    _limits_ok(win)
    assert win["title"] == "✅ EXIT · 🪙 WIF" and win["color"] == tc.COLOR_WIN
    assert _vals(win) == {"📈 PnL": "**+50.0%** · +0.050 SOL · +$7.50", "💵 MC": "$200.0K → $300.0K"}
    loss = tc.exit_card(mint=MINT, symbol="WIF", pnl_sol=-0.02, pnl_usd=-3.0, pnl_pct=-0.2, partial_fraction=0.33)
    assert loss["title"] == "🔴 TRIM 33% · 🪙 WIF" and loss["color"] == tc.COLOR_LOSS
    assert "📉 PnL" in _vals(loss)


@pytest.mark.parametrize("v,exp", [(0.00001234, "$1.234e-05"), (0.0123, "$0.0123"), (2.5, "$2.5000")])
def test_price_format(v, exp):
    assert tc.fmt_price(v) == exp


def test_text_fallback_is_compact():
    t = tc.card_to_text(tc.entry_card(mint=MINT, symbol="X", size_usd=5.0))
    assert len(t.splitlines()) <= 4 and MINT in t


def test_speak_posts_embed_and_respects_post_discord(monkeypatch):
    sent = []
    monkeypatch.setattr(lb, "DISCORD_WEBHOOK_URLS", ["https://discord.invalid/hook"])
    monkeypatch.setattr(lb, "DISCORD_TRADE_FORMAT", "embed")
    monkeypatch.setattr(lb.requests, "post", lambda url, json=None, timeout=None: sent.append(json))
    monkeypatch.setattr(lb, "log_journal", lambda **k: None)
    card = tc.entry_card(mint=MINT, symbol="X", size_sol=0.1)
    lb.speak(title="t", description="d", embed=card, journal_kind="did")
    assert sent[0]["embeds"] == [card] and "content" not in sent[0]
    lb.speak(title="t", description="d", embed=card, post_discord=False)
    assert len(sent) == 1
    monkeypatch.setattr(lb, "DISCORD_TRADE_FORMAT", "text")
    lb.speak(title="t", description="d", embed=card)
    assert "embeds" not in sent[1] and MINT in sent[1]["content"]


def _capture(monkeypatch):
    calls = []
    monkeypatch.setattr(lb, "speak", lambda *a, **k: calls.append(k))
    monkeypatch.setattr(lb, "get_sol_price_usd", lambda: 100.0)
    monkeypatch.setattr(lb, "get_liquidity_and_market_cap", lambda m: (100_000, 500_000))
    monkeypatch.setattr(lb, "get_exit_opinion", lambda *a, **k: "")
    monkeypatch.setattr(lb, "check_is_narrative_token", lambda *a: False)
    monkeypatch.setattr(lb, "upsert_thesis", lambda *a, **k: None)
    monkeypatch.setattr(lb, "RISK", lb.RiskConfig(paper_cost_per_side_pct=0.0))
    return calls


def test_paper_round_trip_cards(monkeypatch):
    calls = _capture(monkeypatch)
    st = lb.LedgerState()
    st.balance_sol = 10.0
    lb.open_paper_position(st, MINT, 1.0, 0.3, opened_by="W1", source="priority_copy")
    lb.partial_close_paper_position(st, MINT, 1.5, 0.5, reason="tp")
    lb.close_paper_position(st, MINT, 0.8, reason="stop")
    trim, final = [k["embed"] for k in calls if k.get("embed")]
    assert trim["title"].startswith("✅ TRIM 50%")
    # final card reports the WHOLE round trip (+0.075 trim, -0.03 stop = +0.045 SOL, +15%)
    assert final["title"].startswith("✅ EXIT")
    assert _vals(final)["📈 PnL"] == "**+15.0%** · +0.045 SOL · +$4.50"


def test_scale_ins_are_not_posted(monkeypatch):
    calls = _capture(monkeypatch)
    st = lb.LedgerState()
    st.balance_sol = 10.0
    lb.open_paper_position(st, MINT, 1.0, 0.1, target_size_sol=0.4, source="conviction", price_source="jupiter")
    lb.top_up_conviction_position(st, MINT, 1.5, 0.5)
    topups = [k for k in calls if "TOPPED UP" in (k.get("title") or "")]
    assert topups and all(k.get("post_discord") is False for k in topups)
    # real dip buy (scale-in) is journaled, never posted
    calls.clear()
    monkeypatch.setattr(lb.real_only_positions, "load_real_only_positions", lambda: {})
    lb._report_real_result({"status": "success", "usdc_spent": 5.0, "signature": "S"}, "X", MINT, "buy", reason="dip_buy")
    assert calls and calls[0]["post_discord"] is False


def test_real_mirror_of_paper_trade_not_double_posted(monkeypatch):
    calls = _capture(monkeypatch)
    monkeypatch.setattr(lb.real_only_positions, "load_real_only_positions", lambda: {})
    monkeypatch.setattr(lb, "execute_real_trade", lambda *a, **k: {"status": "success", "usdc_spent": 30.0, "signature": "S"})
    st = lb.LedgerState()
    st.balance_sol = 10.0
    lb.open_paper_position(st, MINT, 1.0, 0.3, source="sniper", mirror_real=True)
    real = [k for k in calls if k.get("journal_kind") == "did_real"]
    assert real and real[0]["post_discord"] is False


def test_real_only_sell_card(monkeypatch):
    calls = _capture(monkeypatch)
    monkeypatch.setattr(lb.real_only_positions, "load_real_only_positions", lambda: {MINT: {"entry_mcap": 200_000}})
    lb._report_real_result({"status": "success", "signature": "SIG", "usdc_received": 24.0,
                            "realized_pnl_usdc": 4.0, "fraction_sold": 1.0}, "BONK", MINT, "sell", reason="stop")
    k = calls[0]
    c = k["embed"]
    assert k["post_discord"] is True and c["title"] == "✅ EXIT · 🪙 BONK"
    assert _vals(c) == {"📈 PnL": "**+20.0%** · +$4.00", "💵 MC": "$200.0K → $500.0K"}
