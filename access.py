"""Owner-approved access to Ask Mirko: personal codes (stored as salted SHA-256 only) + Discord role/allowlist."""
from __future__ import annotations

import hashlib, hmac, json, os, secrets, threading, time
from pathlib import Path

DATA = Path(os.environ.get("DATA_DIR", "."))
FILE = DATA / "ask_access.json"
_lock = threading.Lock()
_SALT = os.environ.get("ASK_SALT") or hashlib.sha256((os.environ.get("RAILWAY_PROJECT_ID", "") + "mirko-codes").encode()).hexdigest()


def _h(code: str) -> str:
    return hashlib.sha256((_SALT + code.strip()).encode()).hexdigest()


def _load() -> dict:
    try:
        d = json.loads(FILE.read_text())
    except Exception:
        d = {}
    d.setdefault("codes", []); d.setdefault("public_feed", False)
    return d


def _save(d: dict):
    tmp = FILE.with_suffix(".tmp"); tmp.write_text(json.dumps(d, indent=1)); tmp.replace(FILE)


def create(label: str, daily: int = 30) -> dict:
    code = "mk-" + secrets.token_urlsafe(12)
    with _lock:
        d = _load()
        e = {"id": secrets.token_hex(4), "label": str(label or "guest")[:40], "hash": _h(code),
             "daily": max(1, min(500, int(daily or 30))), "created": int(time.time()), "revoked": False, "uses": 0, "last": 0}
        d["codes"].append(e); _save(d)
    return {"id": e["id"], "label": e["label"], "daily": e["daily"], "code": code}   # plain code shown ONCE


def listing() -> dict:
    d = _load()
    return {"public_feed": d["public_feed"], "codes": [{k: v for k, v in c.items() if k != "hash"} for c in d["codes"]]}


def revoke(cid: str) -> bool:
    with _lock:
        d = _load(); hit = False
        for c in d["codes"]:
            if c["id"] == cid:
                c["revoked"] = True; hit = True
        _save(d)
    return hit


def set_feed(on: bool):
    with _lock:
        d = _load(); d["public_feed"] = bool(on); _save(d)


def public_feed() -> bool:
    return _load()["public_feed"]


def check(code: str) -> dict | None:
    if not code or len(code) > 80:
        return None
    h = _h(code)
    with _lock:
        d = _load()
        for c in d["codes"]:
            if not c["revoked"] and hmac.compare_digest(c["hash"], h):
                c["uses"] += 1; c["last"] = int(time.time()); _save(d)
                return {"id": c["id"], "label": c["label"], "daily": c["daily"]}
    return None


ROLE = os.environ.get("MIRKO_ACCESS_ROLE", "Mirko Access").lower()


def discord_allowed(member) -> bool:
    ids = {x.strip() for x in os.environ.get("MIRKO_ALLOWED_USER_IDS", "").split(",") if x.strip()}
    owner = os.environ.get("DISCORD_OWNER_ID", "").strip()
    uid = str(getattr(member, "id", ""))
    if uid and (uid in ids or uid == owner):
        return True
    return any(getattr(r, "name", "").lower() == ROLE for r in getattr(member, "roles", []) or [])
