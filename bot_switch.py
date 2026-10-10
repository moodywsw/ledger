"""Persisted ON/OFF kill switch for NEW entries (exits/stops keep running).

State lives in $DATA_DIR/bot_switch.json (the Railway volume). Missing or
unreadable file = ON (default behaviour unchanged).

Admin token for the toggle API: LEDGER_ADMIN_TOKEN env if set; otherwise a
random token is generated once into $DATA_DIR/admin_token and printed in the
boot log so the owner can copy it from Railway logs.
"""
import json, os, secrets, time
from pathlib import Path


def _dir() -> Path:
    return Path(os.environ.get("DATA_DIR", "."))


def _file() -> Path:
    return _dir() / "bot_switch.json"


def status() -> dict:
    try:
        d = json.loads(_file().read_text())
        return {"enabled": bool(d.get("enabled", True)), "updated_at": d.get("updated_at"), "by": d.get("by")}
    except Exception:
        return {"enabled": True, "updated_at": None, "by": None}


def is_enabled() -> bool:
    return status()["enabled"]


def set_enabled(enabled: bool, by: str = "api") -> dict:
    d = {"enabled": bool(enabled), "updated_at": time.time(), "by": by}
    f = _file(); f.parent.mkdir(parents=True, exist_ok=True)
    tmp = f.with_suffix(".tmp"); tmp.write_text(json.dumps(d)); tmp.replace(f)
    return status()


def admin_token() -> str:
    t = os.environ.get("LEDGER_ADMIN_TOKEN", "").strip()
    if t:
        return t
    f = _dir() / "admin_token"
    try:
        t = f.read_text().strip()
        if t:
            return t
    except Exception:
        pass
    t = secrets.token_urlsafe(18)
    try:
        f.parent.mkdir(parents=True, exist_ok=True); f.write_text(t)
    except Exception:
        pass
    return t


def check_token(given: str) -> bool:
    return bool(given) and secrets.compare_digest(given.strip(), admin_token())
