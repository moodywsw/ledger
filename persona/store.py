"""Persisted persona state (DATA_DIR/persona_state.json)."""
import json, os, threading, time
from pathlib import Path

LOCK = threading.RLock()


def path() -> Path:
    return Path(os.environ.get("DATA_DIR", ".")) / "persona_state.json"


DEFAULT = {
    "mood": {"confidence": 0.5, "greed_fear": 0.0, "tilt": 0.0,
             "win_streak": 0, "loss_streak": 0, "regime": "neutral",
             "pnl_today_sol": 0.0, "day": ""},
    "lessons": [],        # {ts, wallet, setup, outcome, pnl_sol, change_pct, note}
    "facts": [],          # {ts, source, token, text}
    "beliefs": [],        # short strings, distilled from lessons
    "beliefs_ts": 0,
    "posts": [],          # {ts, kind, text, outlets, key}
    "seen_keys": [],      # dedupe keys
    "journal_offset": 0,
    "open_tokens": {},    # ticker -> {wallet, ts}
    "outlet_counts": {},  # "x:2026-10-10" -> n
    "last_recap_day": "",
    "last_mood_post": 0,
}

CAPS = {"lessons": 300, "facts": 200, "posts": 200, "seen_keys": 1000}


def load() -> dict:
    with LOCK:
        try:
            d = json.loads(path().read_text())
        except Exception:
            d = {}
        out = json.loads(json.dumps(DEFAULT))
        for k, v in d.items():
            if k == "mood" and isinstance(v, dict):
                out["mood"].update(v)
            else:
                out[k] = v
        return out


def save(d: dict) -> None:
    with LOCK:
        for k, n in CAPS.items():
            if isinstance(d.get(k), list) and len(d[k]) > n:
                d[k] = d[k][-n:]
        p = path()
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            tmp = p.with_suffix(".tmp")
            tmp.write_text(json.dumps(d))
            os.replace(tmp, p)
        except Exception as e:  # never take the bot down
            print(f"[PERSONA] save failed: {e}")


def now() -> float:
    return time.time()
