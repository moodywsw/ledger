"""Public/owner split: nothing public may name a copied trader or show a wallet.

scrub_text() replaces every tracked handle (wallets.json + learning roster) and
every wallet address (any chain) in free text; scrub_obj() also drops fields
that identify a trader. Owner-only views authenticate with the same admin
token as the ON/OFF switch (bot_switch.check_token).
"""
from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path

import bot_switch

ROOT = Path(__file__).resolve().parent
WALLETS_FILE = ROOT / "wallets.json"
ANON = "a tracked trader"
SENSITIVE_KEYS = {"wallet", "wallets", "opened_by", "source_wallet", "handle", "handles", "trader", "traders",
                  "signer", "copied_from", "address", "owner", "wallet_address", "kol", "copy_of", "leader",
                  "evm", "chains", "followed", "by_trader"}
_SOL = re.compile(r"\b[1-9A-HJ-NP-Za-km-z]{32,44}\b")
_EVM = re.compile(r"\b0x[0-9a-fA-F]{40}\b")
_cache = {"ts": 0.0, "handles": [], "addrs": set(), "rx": None}


def _known() -> tuple[list, set]:
    handles, addrs = set(), set()
    try:
        for w in json.loads(WALLETS_FILE.read_text()).get("wallets", []):
            h = (w.get("handle") or "").strip()
            if h:
                handles.add(h)
                handles.add(re.sub(r"^fomo:", "", h, flags=re.I))
            for a in [w.get("address")] + list((w.get("chains") or {}).values()):
                if a:
                    addrs.add(a.lower())
    except Exception:
        pass
    try:
        roster = json.loads((Path(os.environ.get("DATA_DIR", ".")) / "wallet_roster.json").read_text())
        for addr, r in (roster or {}).items():
            addrs.add(addr.lower())
            if isinstance(r, dict) and r.get("handle"):
                handles.add(r["handle"])
    except Exception:
        pass
    return sorted((h for h in handles if len(h) >= 3), key=len, reverse=True), addrs


def _refresh():
    if time.time() - _cache["ts"] < 300 and _cache["rx"] is not None:
        return
    hs, addrs = _known()
    _cache.update(ts=time.time(), handles=hs, addrs=addrs,
                  rx=re.compile(r"(?<![\w$])(?:fomo:)?@?(" + "|".join(re.escape(h) for h in hs) + r")(?:'s)?(?!\w)", re.I) if hs else None)


def scrub_text(text, keep: set | None = None):
    """Remove trader names and wallet addresses. `keep` = strings allowed through (e.g. token mints)."""
    if not isinstance(text, str) or not text:
        return text
    _refresh()
    keep = keep or set()
    t = _EVM.sub(lambda m: m.group(0) if m.group(0) in keep else "[wallet]", text)
    def sol(m):
        a = m.group(0)
        if a.lower() in _cache["addrs"]:
            return "[wallet]"
        return a if a in keep else "[address]"
    t = _SOL.sub(sol, t)
    if _cache["rx"] is not None:
        t = _cache["rx"].sub(lambda m: ANON + ("'s" if m.group(0).lower().endswith("'s") else ""), t)
    return t


def scrub_obj(obj, keep: set | None = None):
    if isinstance(obj, dict):
        return {k: scrub_obj(v, keep) for k, v in obj.items() if k not in SENSITIVE_KEYS}
    if isinstance(obj, list):
        return [scrub_obj(v, keep) for v in obj]
    if isinstance(obj, str):
        return scrub_text(obj, keep)
    return obj


def token_from_request(req) -> str:
    auth = req.headers.get("Authorization", "")
    return auth[7:] if auth.lower().startswith("bearer ") else req.headers.get("X-Admin-Token", "")


def is_owner(req) -> bool:
    return bot_switch.check_token(token_from_request(req))
