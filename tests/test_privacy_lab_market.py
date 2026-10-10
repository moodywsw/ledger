import json, os, time
from pathlib import Path

import api_server, privacy, bot_switch, strategy_lab, market_thoughts

FRANK_SOL = "498g1rVnFcnjBjpfw1xyqA1WvgQXUU8RWuELjxkjAayQ"
FRANK_EVM = "0x696d1265c8fc4f14797abebfae3c43ebfa9d8e28"
MINT = "7pSMFY9EVpaoo8eiSWe1ksqCHxabteaBpZe4G51epump"


def _data_dir():
    return Path(os.environ["DATA_DIR"])


def test_scrub_text_names_and_wallets():
    t = privacy.scrub_text(f"Skipped copying frank's buy of X ({FRANK_SOL}, {FRANK_EVM}) mint {MINT}", keep={MINT})
    assert "frank" not in t.lower() and FRANK_SOL not in t and FRANK_EVM not in t
    assert MINT in t and "a tracked trader's" in t
    t2 = privacy.scrub_text(f"@ansem and fomo:Lizzerd bought; random {MINT}")
    assert "ansem" not in t2.lower() and "lizzerd" not in t2.lower() and MINT not in t2


def test_scrub_obj_drops_trader_fields():
    o = privacy.scrub_obj({"opened_by": "frank", "wallet": FRANK_SOL, "x": [{"handle": "ansem", "ok": 1}]})
    assert o == {"x": [{"ok": 1}]}


def _seed_state():
    st = {"balance_sol": 1.0, "realized_pnl_sol": 0.1, "open_positions": {MINT: {"symbol": "AAA", "entry_price": 1.0, "size_sol": 0.1,
          "opened_by": "frank", "source": "copy frank", "thesis": f"frank ({FRANK_SOL}) bought"}},
          "trade_log": [{"action": "close", "token": MINT, "pnl_sol": 0.1, "opened_by": "frank", "reason": "tp by frank", "at": "2026-10-10T10:00:00+00:00"}]}
    (_data_dir() / "ledger_state.json").write_text(json.dumps(st))
    api_server.STATE_FILE = _data_dir() / "ledger_state.json"
    api_server.get_token_prices_usd = lambda m: {}


def test_public_endpoints_hide_traders():
    _seed_state()
    c = api_server.app.test_client()
    for path in ("/api/state", "/api/overview"):
        body = c.get(path).get_data(as_text=True)
        assert "frank" not in body.lower() and FRANK_SOL not in body and "traders" not in json.loads(body), path
    assert c.get("/api/owner/fomo_theses").status_code == 401
    assert c.get("/api/owner/overview").status_code == 401


def test_owner_endpoints_with_token():
    _seed_state()
    tok = bot_switch.admin_token()
    c = api_server.app.test_client()
    r = c.get("/api/owner/overview", headers={"Authorization": f"Bearer {tok}"})
    assert r.status_code == 200 and any(t["handle"] == "frank" for t in r.get_json()["traders"])
    r = c.get("/api/owner/state", headers={"X-Admin-Token": tok})
    assert r.get_json()["open_positions"][0]["opened_by"] == "frank"
    assert c.get("/api/owner/check", headers={"Authorization": "Bearer wrong"}).status_code == 401


def test_lab_requires_full_pipeline_then_approval():
    tok = bot_switch.admin_token()
    st = strategy_lab._new("t", "trader_exits", {"stop_loss_pct": 0.25}, "r", "rules", trader="frank")
    strategy_lab.save({"strategies": [st], "last_propose": time.time(), "last_paper": time.time()})
    c = api_server.app.test_client()
    h = {"Authorization": f"Bearer {tok}"}
    assert c.post(f"/api/owner/lab/{st['id']}/approve").status_code == 401
    assert c.post(f"/api/owner/lab/{st['id']}/approve", headers=h).status_code == 409  # not through paper yet
    assert strategy_lab.live().get("trader_exits", {}) == {}
    d = strategy_lab.load(); d["strategies"][0]["status"] = "awaiting_approval"; strategy_lab.save(d)
    assert c.post(f"/api/owner/lab/{st['id']}/approve", headers=h).status_code == 200
    assert strategy_lab.live()["trader_exits"]["frank"] == {"stop_loss_pct": 0.25}
    from risk_engine import RiskConfig
    assert strategy_lab.cfg_for(RiskConfig.from_env(), "Frank").stop_loss_pct == 0.25


def test_lab_paper_gate():
    from risk_engine import RiskConfig
    st = strategy_lab._new("x", "exits", {"stop_loss_pct": 0.3}, "", "rules")
    strategy_lab._status(st, "paper"); st["results"]["paper"] = {"since": 0, "n": 0, "candidate": [], "current": []}
    strategy_lab.paper_step(st, RiskConfig.from_env(), [], {})
    assert st["status"] == "paper"


def _asset(price, trend=1):
    closes = [100 + trend * i for i in range(60)]
    return {"name": "BTC", "price": price, "chg24": 1.0, "chg7": 5.0 * trend, "hi30": 170, "lo30": 90, "ma20": 150 if trend > 0 else 170,
            "ma50": 130 if trend > 0 else 180, "ma20_prev": 140 if trend > 0 else 175, "rsi_d": 60 if trend > 0 else 35, "rsi_4h": 55,
            "hi7": 165, "lo7": 140, "atr": 5.0, "res": [165, 175], "sup": [145, 135]}


def test_market_read_analysis_and_regime_scale(monkeypatch):
    a = _asset(155)
    r = market_thoughts.asset_read(a, {"funding_agg": 0.05, "oi_chg24": 5}, {"longs": 8, "shorts": 1})
    assert r["tone"] == "bull" and r["plan"].startswith("Buy dips") and not any("funding" in n for n in r["notes"])
    reg = market_thoughts.regime({"BTC": _asset(150, -1) | {"price": 150}}, None, {"v": 80, "cls": "Greed"})
    assert reg["key"] == "risk_off"
    ideas = market_thoughts.trade_ideas({"BTC": a}, None)
    assert ideas and ideas[0]["side"] in ("Long", "Short")
    p = market_thoughts.cache_path(); p.write_text(json.dumps({"ts": time.time(), "regime": reg}))
    s, note = market_thoughts.regime_size_scale()
    assert s < 1.0 and "regime" in note
    p.write_text(json.dumps({"ts": time.time() - 7 * 3600, "regime": reg}))
    assert market_thoughts.regime_size_scale()[0] == 1.0


def test_persona_market_read_musing():
    from persona import muse
    mr = {"regime": {"label": "Range / indecision"}, "assets": [{"name": "BTC", "bias": "Neutral · pullback"}],
          "stance": "Neutral: normal size.", "summary": "Regime: range."}
    t = muse.template("market_read", {"market_read": mr}, [])
    assert "range" in t.lower() and "{" not in t


def test_security_headers_ratelimit_and_lockout():
    import security
    c = api_server.app.test_client()
    r = c.get("/api/bot_switch")
    assert r.headers["X-Frame-Options"] == "DENY" and "frame-ancestors 'none'" in r.headers["Content-Security-Policy"]
    assert "Access-Control-Allow-Origin" not in r.headers and r.headers["X-Content-Type-Options"] == "nosniff"
    h = {"X-Real-IP": "9.9.9.9"}
    codes = [c.get("/api/owner/check", headers={**h, "Authorization": "Bearer nope"}).status_code for _ in range(7)]
    assert codes[:5] == [401] * 5 and 429 in codes[5:]
    ok = c.get("/api/owner/check", headers={"X-Real-IP": "8.8.8.8", "Authorization": f"Bearer {bot_switch.admin_token()}"})
    assert ok.status_code == 200
    assert c.post("/api/bot_switch", json={"enabled": "yes"}, headers={"X-Real-IP": "7.7.7.7", "Authorization": f"Bearer {bot_switch.admin_token()}"}).status_code == 400
    assert c.get("/../wallets.json").status_code == 404 and c.get("/wallets.json").status_code == 404
    assert c.delete("/api/state").status_code == 405
    assert not security.allow("1.1.1.1", "write") or all(security.allow("1.1.1.1", "write") for _ in range(9)) and not security.allow("1.1.1.1", "write")


def test_world_musing_guardrails():
    from persona import muse
    ctx = {"world": ["Gunmen kill 12 in market attack", "Fed holds rates as inflation cools"]}
    h, take = muse.pick_world(ctx)
    assert h.startswith("Fed") and "rates" in take
    t = muse.template("world_markets", ctx, [])
    assert "Fed holds" in t and "kill" not in t
