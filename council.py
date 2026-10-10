"""Mirko's agent council: four internal AI agent personas debate the current market read with Mirko.
Rule-based and free (no LLM needed). Every line cites the real source it comes from (market read,
social scan, daily reading). Runs every ~90 min; the latest exchange is shown in thoughts and the brain."""
from __future__ import annotations

import json, os, random, threading, time
from pathlib import Path

EVERY_MIN = float(os.environ.get("COUNCIL_EVERY_MIN", "90"))
AGENTS = {"Quant": "📐", "Macro": "🌍", "Degen": "🎰", "Risk": "🛡️"}


def _path() -> Path:
    return Path(os.environ.get("DATA_DIR", "data")) / "council.json"


def _ctx() -> dict:
    out = {}
    for mod, key, age in (("market_thoughts", "mr", 12), ("social", "so", 12), ("insights", "ins", 36)):
        try:
            out[key] = __import__(mod).cached(max_age_h=age) or {}
        except Exception:
            out[key] = {}
    return out


def debate(rng=random) -> dict:
    c = _ctx(); mr, so, ins = c["mr"], c["so"], c["ins"]
    reg = (mr.get("regime") or {}); assets = mr.get("assets") or []
    lines = []
    if assets:
        a = max(assets, key=lambda x: abs(x.get("score", 0)))
        lines.append(("Quant", f"{a['name']} trend score {a.get('score', 0):+d}; {a.get('structure', '').split(';')[0].lower()}.", "market"))
    if ins.get("beliefs"):
        lines.append(("Macro", ins["beliefs"][0], "news"))
    hot = so.get("hot_tickers") or []
    tone = (so.get("tone") or {}).get("crypto")
    if hot or tone is not None:
        lines.append(("Degen", (f"Reddit can't stop talking about {', '.join('$' + h for h in hot[:3])}. " if hot else "")
                      + ("Crowd is greedy — momentum is there for a quick flip." if (tone or 0) > .1 else "Crowd is scared — that's where the cheap entries are."), "social"))
    scale = mr.get("size_scale", 1)
    lines.append(("Risk", f"Regime is {reg.get('label', 'unclear').lower()}. Size x{scale}; " + ("no new memecoins until the tape improves." if scale < 1 else "keep stops tight, no averaging down."), "risk"))
    # Mirko's synthesis
    agree = sum(1 for _, t, _ in lines if any(w in t.lower() for w in ("up", "greedy", "easier", "tailwind")))
    verdict = ("I'll lean in, but only on clean setups." if agree >= 2 and reg.get("key") != "risk_off"
               else "We wait. Patience is a position." if reg.get("key") == "risk_off" else "Split vote — I trade the edges and size down in the middle.")
    lines.append(("Mirko", f"Heard you all. {verdict}", "agents"))
    return {"ts": time.time(), "lines": [{"agent": a, "emoji": AGENTS.get(a, "🐺"), "text": t, "source": src} for a, t, src in lines]}


def cached() -> dict | None:
    try:
        return json.loads(_path().read_text())
    except Exception:
        return None


def refresh() -> dict:
    d = debate()
    hist = (cached() or {}).get("history", [])
    d["history"] = ([{"ts": d["ts"], "lines": d["lines"]}] + hist)[:12]
    p = _path(); p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp"); tmp.write_text(json.dumps(d)); tmp.replace(p)
    return d


def public_view() -> dict:
    d = cached() or {}
    return {"ts": d.get("ts"), "lines": d.get("lines", []), "history": d.get("history", [])[:6]}


_started = False


def start():
    global _started
    if _started or os.environ.get("COUNCIL_ENABLED", "true").lower() in ("0", "false", "no"):
        return
    _started = True

    def loop():
        time.sleep(90)
        while True:
            try:
                c = cached()
                if not c or time.time() - c.get("ts", 0) > EVERY_MIN * 60:
                    refresh()
            except Exception as e:
                print(f"[COUNCIL] error: {type(e).__name__}")
            time.sleep(600)
    threading.Thread(target=loop, daemon=True, name="council").start()
