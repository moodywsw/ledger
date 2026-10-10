import api_server


def test_build_overview_basic():
    now = 1_760_000_000.0
    from datetime import datetime, timezone
    iso = lambda t: datetime.fromtimestamp(t, timezone.utc).isoformat()
    state = {"realized_pnl_sol": 0.05, "trade_log": [
        {"action": "open", "token": "M1", "symbol": "AAA", "at": iso(now - 9 * 86400)},
        {"action": "close", "token": "M1", "pnl_sol": 0.1, "opened_by": "frank", "at": iso(now - 8 * 86400)},
        {"action": "close", "token": "M2", "pnl_sol": -0.05, "opened_by": "frank", "at": iso(now - 3600)},
    ]}
    journal = [{"kind": "commentary", "token_ticker": "XYZ", "timestamp": iso(now), "meta": {"own_thesis": True, "score": 7, "why": ["a"]}}]
    o = api_server.build_overview(state, journal, now=now)
    assert abs(o["pnl_sol"]["today"] + 0.05) < 1e-9
    assert abs(o["pnl_sol"]["d7"] + 0.05) < 1e-9
    assert o["wins"] == 1 and o["losses"] == 1
    assert o["closed_trades"][1]["symbol"] == "AAA"
    assert [p["v"] for p in o["equity"]] == [0.1, 0.05]
    assert o["own_theses"][0]["symbol"] == "XYZ"
    frank = next(t for t in o["traders"] if t["handle"] == "frank")
    assert frank["trades"] == 2 and frank["hit_rate"] == 0.5
