"""HTTP hardening for Mirko's API/site: rate limits, token lockout, security headers.

In-memory and per-process (the API runs in one process next to the bot), so no
extra service is needed. Client IP = first X-Forwarded-For hop (Railway's edge
proxy sets it) falling back to the socket address.
"""
from __future__ import annotations

import os
import threading
import time
from collections import deque

_lock = threading.Lock()
_hits: dict = {}          # (ip, bucket) -> deque[timestamps]
_fails: dict = {}         # ip -> {"n": int, "until": float, "last": float}

LIMITS = {                 # bucket -> (requests, window seconds)
    "api": (int(os.environ.get("RL_API_PER_MIN", "240")), 60),
    "owner": (30, 60),
    "write": (10, 60),
    "static": (600, 60),
}
FAIL_FREE = 5              # failed token attempts before lockout
FAIL_WINDOW = 900
MAX_LOCK = 3600


def client_ip(req) -> str:
    # Railway's edge sets X-Real-IP / appends to X-Forwarded-For; a client can forge the
    # FIRST XFF hop, never the last one the proxy appended.
    ip = req.headers.get("X-Real-IP", "").strip()
    if not ip:
        xff = req.headers.get("X-Forwarded-For", "")
        ip = xff.split(",")[-1].strip() if xff else (req.remote_addr or "?")
    return ip[:64]


def allow(ip: str, bucket: str, now: float | None = None) -> bool:
    n, win = LIMITS.get(bucket, LIMITS["api"])
    now = now or time.time()
    with _lock:
        q = _hits.setdefault((ip, bucket), deque())
        while q and q[0] <= now - win:
            q.popleft()
        if len(q) >= n:
            return False
        q.append(now)
        if len(_hits) > 20000:      # bound memory under a flood of spoofed IPs
            for k in list(_hits)[:5000]:
                _hits.pop(k, None)
        return True


def locked_for(ip: str, now: float | None = None) -> float:
    now = now or time.time()
    with _lock:
        f = _fails.get(ip)
        return max(0.0, f["until"] - now) if f else 0.0


def record_fail(ip: str, now: float | None = None) -> float:
    """Exponential backoff after FAIL_FREE failures: 30s, 60s, 120s ... capped at 1h."""
    now = now or time.time()
    with _lock:
        f = _fails.get(ip)
        if not f or now - f["last"] > FAIL_WINDOW:
            f = {"n": 0, "until": 0.0, "last": now}
        f["n"] += 1
        f["last"] = now
        if f["n"] >= FAIL_FREE:
            f["until"] = now + min(MAX_LOCK, 30 * 2 ** (f["n"] - FAIL_FREE))
        _fails[ip] = f
        if len(_fails) > 20000:
            for k in list(_fails)[:5000]:
                _fails.pop(k, None)
        return max(0.0, f["until"] - now)


def record_success(ip: str):
    with _lock:
        _fails.pop(ip, None)


CSP = ("default-src 'self'; script-src 'self'; style-src 'self' https://fonts.googleapis.com; "
       "style-src-attr 'unsafe-inline'; font-src https://fonts.gstatic.com; img-src 'self' data:; "
       "connect-src 'self'; object-src 'none'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'; "
       "upgrade-insecure-requests")


def apply_headers(resp, path: str):
    h = resp.headers
    h["Content-Security-Policy"] = CSP
    h["X-Frame-Options"] = "DENY"
    h["X-Content-Type-Options"] = "nosniff"
    h["Referrer-Policy"] = "no-referrer"
    h["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    h["Permissions-Policy"] = "camera=(), microphone=(), geolocation=(), payment=(), usb=()"
    h["Cross-Origin-Opener-Policy"] = "same-origin"
    h["Cross-Origin-Resource-Policy"] = "same-origin"
    h.pop("Server", None)
    if path.startswith("/api/"):
        h["Cache-Control"] = "no-store"
    elif path.endswith((".jpg", ".png")):
        h["Cache-Control"] = "public, max-age=86400"
    return resp
