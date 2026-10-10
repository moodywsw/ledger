import importlib
def test_switch(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path)); monkeypatch.delenv("LEDGER_ADMIN_TOKEN", raising=False)
    import bot_switch; importlib.reload(bot_switch)
    assert bot_switch.is_enabled()
    t = bot_switch.admin_token(); assert t == bot_switch.admin_token()
    import api_server
    c = api_server.app.test_client()
    assert c.post("/api/bot_switch", json={"enabled": False}).status_code == 401
    r = c.post("/api/bot_switch", json={"enabled": False}, headers={"Authorization": f"Bearer {t}"})
    assert r.status_code == 200 and r.get_json()["enabled"] is False
    assert c.get("/api/bot_switch").get_json()["enabled"] is False
    assert not bot_switch.is_enabled()
def test_defaults(monkeypatch):
    for k in ["RISK_SIZING_MODE","EDGE_TOP_PCT","RISK_MAX_POSITION_PCT","RISK_PROFILE"]: monkeypatch.delenv(k, raising=False)
    from risk_engine import RiskConfig, size_for_signal
    c = RiskConfig.from_env() if hasattr(RiskConfig,"from_env") else RiskConfig()
    assert c.sizing_mode=="edge" and c.max_position_pct==0.10 and c.edge_top_pct==0.10 and c.max_total_exposure_pct==0.40
    assert abs(size_for_signal(1.0, c, "copy", None)[0]-0.05)<1e-9
    assert abs(size_for_signal(1.0, c, "copy", {"tier":"top","n":10,"shrunk_r":1})[0]-0.10)<1e-9
