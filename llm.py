"""Shared reasoning LLM: DeepSeek reasoner (cheap, if DEEPSEEK_API_KEY) -> Gemini 2.5 (free tier). Never logs keys."""
from __future__ import annotations

import json, os, re, threading, time
import requests

_day = {"d": "", "n": 0}
_last = {"deepseek": None, "gemini": None}
_lock = threading.Lock()
DAILY_CAP = int(os.environ.get("LLM_DAILY_CAP", "400"))


def _budget() -> bool:
    d = time.strftime("%Y-%m-%d")
    with _lock:
        if _day["d"] != d:
            _day.update(d=d, n=0)
        if _day["n"] >= DAILY_CAP:
            return False
        _day["n"] += 1
        return True


def deepseek(system: str, user: str, max_tokens: int = 2000, timeout: int = 120) -> str | None:
    key = os.environ.get("DEEPSEEK_API_KEY")
    if not key:
        return None
    try:
        r = requests.post("https://api.deepseek.com/chat/completions", timeout=timeout, headers={"Authorization": f"Bearer {key}"},
                          json={"model": os.environ.get("DEEPSEEK_MODEL", "deepseek-reasoner"), "max_tokens": max_tokens,
                                "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]})
    except requests.RequestException as e:
        _last["deepseek"] = type(e).__name__
        print(f"[LLM] deepseek {type(e).__name__}")
        return None
    if r.status_code != 200:
        try:
            msg = str(r.json().get("error", {}).get("message", ""))[:80]
        except Exception:
            msg = ""
        _last["deepseek"] = f"HTTP {r.status_code} {msg}".strip()
        print(f"[LLM] deepseek {_last['deepseek']}")
        return None
    _last["deepseek"] = "ok"
    return (r.json()["choices"][0]["message"].get("content") or "").strip() or None


def gemini(system: str, user: str, max_tokens: int = 4000, think: int = 2048, models=None) -> str | None:
    key = os.environ.get("GEMINI_API_KEY")
    if not key:
        return None
    from persona.voice import gemini_call
    try:
        j = gemini_call({"systemInstruction": {"parts": [{"text": system}]}, "contents": [{"role": "user", "parts": [{"text": user}]}],
                         "generationConfig": {"temperature": 0.6, "maxOutputTokens": max_tokens + think, "thinkingConfig": {"thinkingBudget": think}}},
                        key, 90, models or ["gemini-2.5-flash", "gemini-flash-latest", "gemini-2.5-flash-lite"])
    except Exception as e:
        print(f"[LLM] gemini {str(e)[:120]}")
        return None
    c = (j or {}).get("candidates") or []
    if not c or not c[0].get("content"):
        return None
    return "".join(p.get("text", "") for p in c[0]["content"].get("parts", []) if not p.get("thought")).strip() or None


def reason(system: str, user: str, max_tokens: int = 2000) -> tuple[str | None, str]:
    if not _budget():
        return None, "cap"
    out = deepseek(system, user, max_tokens)
    if out:
        return out, "deepseek"
    out = gemini(system, user, max_tokens)
    return (out, "gemini") if out else (None, "none")


def reason_json(system: str, user: str, max_tokens: int = 2000) -> tuple[dict | None, str]:
    txt, via = reason(system + "\nReturn ONLY one valid JSON object, no prose, no code fences.", user, max_tokens)
    if not txt:
        return None, via
    m = re.search(r"\{.*\}", txt, re.S)
    try:
        return json.loads(m.group(0)) if m else None, via
    except Exception:
        return None, via + ":badjson"


def providers() -> dict:
    return {"deepseek": bool(os.environ.get("DEEPSEEK_API_KEY")), "gemini": bool(os.environ.get("GEMINI_API_KEY")), "used_today": _day["n"], "cap": DAILY_CAP, "last": dict(_last)}
