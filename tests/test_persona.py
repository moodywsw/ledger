import json, os, time
import pytest

for k in ("GEMINI_API_KEY", "GROQ_API_KEY", "LEDGER_PERSONA_WEBHOOK", "X_API_KEY", "X_API_SECRET",
          "X_ACCESS_TOKEN", "X_ACCESS_SECRET", "FOMO_API_KEY"):
    os.environ.pop(k, None)

from persona import engine, values, mood, outlets, store, voice


@pytest.fixture
def P(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    return engine.Persona()


def _journal(p, *entries):
    with p.journal.open("a") as f:
        for e in entries:
            f.write(json.dumps(e) + "\n")


def test_guardrails_strip_nfa_shill_and_secrets():
    t = values.clean("Buy now $WIF NFA 100x guaranteed, wallet 7xKXtg2CW87d97TXJSDpbD5jBkheTqA83TZRuJosgAsU dyor")
    assert t and "NFA" not in t and "100x" not in t.lower() and "7xKX" not in t and "buy now" not in t.lower()
    assert values.clean("seed=abc private key 0x" + "a" * 64) is None or "0x" not in values.clean("ok fine text 0x" + "a" * 64)
    assert values.is_scam_reason("top holders look bundled") and not values.is_scam_reason("low liquidity")


def test_mood_reacts_to_losses_and_wins():
    m = dict(store.DEFAULT["mood"])
    for _ in range(3):
        mood.on_close(m, -0.1, -20)
    assert m["loss_streak"] == 3 and m["confidence"] < 0.5 and m["tilt"] > 0.3
    assert "cautious after 3 losses" in mood.label(m)
    mood.on_close(m, 0.2, 40)
    assert m["loss_streak"] == 0 and m["win_streak"] == 1


def test_journal_events_to_posts_lessons_and_feed(P):
    P.journal.write_text("")
    _journal(P, {"kind": "commentary", "text": "old"})
    P.read_new_journal()  # first run seeks to end, no replay
    assert P.s["posts"] == []
    now = "2026-10-10T10:00:00+00:00"
    _journal(P,
             {"timestamp": now, "kind": "did", "token_ticker": "WIF", "text": "🟦 TRADE OPENED — WIF", "meta": {"wallet": "ansem", "size_sol": 0.1}},
             {"timestamp": now + "1", "kind": "did", "token_ticker": "WIF", "text": "💰 TRADE CLOSED — WIF", "meta": {"reason": "stop_loss", "pnl_sol": -0.02, "change_pct": -20}},
             {"timestamp": now + "2", "kind": "refused", "token_ticker": "RUGX", "text": "Skipped RUGX — honeypot: can't sell", "meta": {}},
             {"timestamp": now + "3", "kind": "commentary", "token_ticker": "POPCAT", "text": "🧠 THESIS", "meta": {"own_thesis": True, "why": ["smart wallets accumulating"]}})
    assert P.read_new_journal() == 4
    kinds = [p["kind"] for p in P.s["posts"]]
    assert kinds == ["entry", "exit_loss", "refusal", "thesis_own"]
    assert P.s["lessons"][0]["wallet"] == "ansem" and P.s["lessons"][0]["outcome"] == "loss"
    assert all("NFA" not in p["text"] for p in P.s["posts"])
    # dedupe: re-handling same event doesn't post again
    P.handle({"timestamp": now + "2", "kind": "refused", "token_ticker": "RUGX", "text": "Skipped RUGX — honeypot: can't sell"})
    assert len(P.s["posts"]) == 4
    store.save(P.s)
    f = engine.feed()
    assert f["posts"][0]["kind"] == "thesis_own" and f["outlets"] == {"discord": False, "x": False, "llm": "templates"}


def test_beliefs_distilled(P):
    for i in range(4):
        P._lesson("frank", "tp", 0.1, 30, "tp1")
    for i in range(3):
        P._lesson("omo", "stop_loss", -0.1, -20, "stop_loss")
    b = P.distil_beliefs()
    assert not any("frank" in x or "omo" in x for x in b) and any("best signal" in x for x in b) and any("cold" in x for x in b) and len(b) <= 8


def test_x_rate_limit_and_discord(P, monkeypatch):
    sent = {"x": 0, "d": 0}
    monkeypatch.setattr(outlets, "x_enabled", lambda: True)
    monkeypatch.setattr(outlets, "discord_enabled", lambda: True)
    monkeypatch.setattr(outlets, "post_x", lambda t: sent.__setitem__("x", sent["x"] + 1) or True)
    monkeypatch.setattr(outlets, "post_discord", lambda t: sent.__setitem__("d", sent["d"] + 1) or True)
    for i in range(20):
        P.publish("exit_win", {"tk": f"T{i}", "chg": "+30%"}, key=f"k{i}")
    assert sent["x"] == engine.X_MAX_PER_DAY and sent["d"] == 20


def test_quiet_hours(monkeypatch):
    monkeypatch.setattr(engine, "QUIET_HOURS", "22-6")
    assert engine._quiet(time.mktime((2026, 1, 1, 23, 0, 0, 0, 0, 0)) - time.timezone)
    monkeypatch.setattr(engine, "QUIET_HOURS", "")
    assert not engine._quiet()


def test_oauth1_signature_known_vector():
    # Twitter docs example (https://developer.x.com/en/docs/authentication/oauth-1-0a/creating-a-signature) adapted: deterministic output
    h = outlets.oauth1_header("POST", "https://api.twitter.com/2/tweets", "ck", "cs", "tk", "ts", nonce="n", timestamp="1")
    assert h.startswith("OAuth ") and 'oauth_signature="' in h and 'oauth_nonce="n"' in h
    assert h == outlets.oauth1_header("POST", "https://api.twitter.com/2/tweets", "ck", "cs", "tk", "ts", nonce="n", timestamp="1")


def test_templates_never_empty():
    for k in voice.TEMPLATES:
        assert voice.template(k, {"tk": "BONK", "mood_state": store.DEFAULT["mood"]})


def test_feed_endpoint():
    import api_server
    r = api_server.app.test_client().get("/api/persona/feed")
    assert r.status_code == 200 and "posts" in r.get_json()


def test_musings_rotate_topics_and_respect_interval(P):
    ctx = {"fng": {"value": 20, "label": "Extreme Fear"}, "trending": ["PEPE", "TAO"], "headlines": ["ETF flows hit record"]}
    P.s["beliefs"] = ["Stops keep me alive."]
    t0 = 1_800_000_000
    posts = []
    for i in range(6):
        p = P.muse(now=t0 + i * 4 * 3600, ctx=ctx)
        assert p and p["kind"] == "musing" and p["text"]
        posts.append(p)
    assert P.muse(now=t0 + 5 * 4 * 3600 + 60, ctx=ctx) is None
    topics = [p["topic"] for p in posts]
    assert len(set(topics)) >= 4 and all(a != b for a, b in zip(topics, topics[1:]))  # opinions ~40%, never back-to-back repeats
    assert all("NFA" not in p["text"] for p in posts)
    store.save(P.s)
    assert engine.feed()["posts"][0]["kind"] == "musing"


def test_musing_templates_work_without_inputs(P):
    p = P.muse(now=1_800_000_000, ctx={})
    assert p and p["topic"] not in ("market", "trending", "headline", "lessons") and len(p["text"]) > 20
