import time
import fomo_theses as f

NOW = 1_800_000_000
TXT = "Holding, CEX listing next week and buyback live. Roadmap shipping, not selling until v2 launches, conviction play."


def test_reentry_ignores_likes():
    a = {"text": TXT, "equity": 20000, "ts": NOW - 60, "likes": 0}
    b = dict(a, likes=5000)
    assert f.reentry_score(a, 1.0, NOW) == f.reentry_score(b, 1.0, NOW)


def test_reentry_prefers_fresh_and_good_author():
    fresh = {"text": TXT, "equity": 20000, "ts": NOW - 60}
    stale = dict(fresh, ts=NOW - 80 * 60)
    assert f.reentry_score(fresh, 1.0, NOW) > f.reentry_score(stale, 1.0, NOW)
    assert f.reentry_score(fresh, 1.0, NOW) > f.reentry_score(fresh, 0.5, NOW)
    assert f.author_score(None, False) == 0.0
    assert f.author_score(0.8, True) > f.author_score(0.2, True)


def test_gem_likes_only_after_6h_and_capped():
    young = {"text": TXT, "equity": 20000, "ts": NOW - 3600, "likes": 1000}
    old = dict(young, ts=NOW - 7 * 3600)
    assert f.gem_score(young, NOW) == f.gem_score(dict(young, likes=0), NOW)
    assert 0 < f.gem_score(old, NOW) - f.gem_score(dict(old, likes=0), NOW) <= 0.10 + 1e-9


def test_penalties():
    assert f.reentry_score({"text": "100x moon", "ts": NOW}, 1.0, NOW) < 0.4
