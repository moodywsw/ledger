"""Mirko scans social media (free, light): Reddit hot posts via public Atom RSS and the latest
posts of a few key X accounts via the free fxtwitter API. Refreshed every ~2h, cached on disk.
Output feeds persona musings/beliefs and the brain's 'social' and 'politics' galaxies.
Only post titles/text snippets and public handles of well-known public accounts are kept."""
from __future__ import annotations

import json, os, re, threading, time
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import requests

EVERY_H = float(os.environ.get("SOCIAL_EVERY_H", "2"))
UA = {"User-Agent": "Mozilla/5.0 MirkoBot/1.0 (+public research)"}
SUBS = {"crypto": ["CryptoCurrency", "solana", "Bitcoin"], "stocks": ["wallstreetbets", "stocks"], "politics": ["politics", "worldnews"]}
X_ACCOUNTS = [a.strip() for a in os.environ.get("SOCIAL_X_ACCOUNTS",
              "elonmusk,saylor,VitalikButerin,cz_binance,aeyakovenko,federalreserve,WhiteHouse,zerohedge,unusual_whales,DeItaone").split(",") if a.strip()]
ATOM = "{http://www.w3.org/2005/Atom}"
_BULL = re.compile(r"\b(moon|pump|bull|ath|breakout|buy(ing)?|rally|green|calls|squeeze|send it)\b", re.I)
_BEAR = re.compile(r"\b(dump|crash|bear|rug|sell(ing)?|red|puts|scam|rekt|recession|liquidat)\w*", re.I)


def _path() -> Path:
    return Path(os.environ.get("DATA_DIR", "data")) / "social.json"


def reddit(sub: str, n=12) -> list:
    try:
        root = ET.fromstring(requests.get(f"https://www.reddit.com/r/{sub}/hot/.rss", headers=UA, timeout=10).content)
    except Exception:
        return []
    out = []
    for e in root.iter(f"{ATOM}entry"):
        t = re.sub(r"\s+", " ", e.findtext(f"{ATOM}title") or "").strip()
        if t and not t.lower().startswith(("daily discussion", "weekly", "what are your moves")):
            out.append(t[:160])
        if len(out) >= n:
            break
    return out


def x_latest(user: str, n=3) -> list:
    try:
        r = requests.get(f"https://api.fxtwitter.com/2/profile/{user}/statuses", headers=UA, timeout=10).json()
    except Exception:
        return []
    out = []
    for s in (r.get("results") or [])[:n * 2]:
        a = ((s.get("author") or {}).get("screen_name") or "").lower()
        if a != user.lower():
            continue   # skip reposts of other accounts
        t = re.sub(r"https?://\S+", "", s.get("text") or "").strip()
        if len(t) > 15:
            out.append({"user": user, "text": re.sub(r"\s+", " ", t)[:200], "ts": s.get("created_timestamp")})
        if len(out) >= n:
            break
    return out


def _tone(texts: list) -> float:
    b = sum(len(_BULL.findall(t)) for t in texts); s = sum(len(_BEAR.findall(t)) for t in texts)
    return round((b - s) / max(1, b + s), 2)


def build() -> dict:
    with ThreadPoolExecutor(6) as ex:
        rd = {g: sum(ex.map(reddit, subs), []) for g, subs in SUBS.items()}
        xs = sum(ex.map(x_latest, X_ACCOUNTS), [])
    words = {}
    for t in rd["crypto"] + rd["stocks"] + [x["text"] for x in xs]:
        for tk in re.findall(r"\$[A-Za-z]{2,6}\b|\b[A-Z]{3,5}\b", t):
            tk = tk.lstrip("$").upper()
            if tk not in ("THE", "AND", "FOR", "USA", "CEO", "ETF", "SEC", "AI", "IPO", "USD", "GDP", "FED", "NEW", "WSB", "DD", "YOLO", "IM", "US", "THIS", "WHAT", "WHY", "HOW", "JUST", "NOT", "ALL", "BUT", "ARE", "WAS", "HAS", "NOW", "GOP", "DNC", "NATO", "TIL", "AMA", "PSA", "LOL", "OMG", "WTF", "IMO", "FUD", "MEV"):
                words[tk] = words.get(tk, 0) + 1
    hot = [k for k, v in sorted(words.items(), key=lambda kv: -kv[1]) if v >= 2][:8]
    tone = {"crypto": _tone(rd["crypto"]), "stocks": _tone(rd["stocks"]), "x": _tone([x["text"] for x in xs])}
    beliefs = []
    lab = lambda v: "euphoric" if v > .4 else "upbeat" if v > .1 else "fearful" if v < -.4 else "nervous" if v < -.1 else "mixed"
    if rd["crypto"]: beliefs.append(f"Crypto Reddit feels {lab(tone['crypto'])} today. Retail mood is a contrarian signal at the extremes, noise in between.")
    if rd["stocks"]: beliefs.append(f"WSB/stocks crowd is {lab(tone['stocks'])}" + (f"; most-mentioned tickers: {', '.join(hot[:4])}." if hot else "."))
    if xs: beliefs.append(f"Big accounts on X sound {lab(tone['x'])}. I read them for what moves attention, not for truth.")
    return {"ts": time.time(), "reddit": rd, "x": xs, "hot_tickers": hot, "tone": tone, "beliefs": beliefs,
            "sources": {**{g: len(v) for g, v in rd.items()}, "x": len(xs)}}


def cached(max_age_h: float | None = 12) -> dict | None:
    try:
        d = json.loads(_path().read_text())
    except Exception:
        return None
    if max_age_h is not None and time.time() - d.get("ts", 0) > max_age_h * 3600:
        return None
    return d


def public_view() -> dict:
    d = cached() or {}
    return {"ts": d.get("ts"), "tone": d.get("tone"), "hot_tickers": d.get("hot_tickers", []), "beliefs": d.get("beliefs", []),
            "reddit": {g: v[:5] for g, v in (d.get("reddit") or {}).items()}, "x": (d.get("x") or [])[:8]}


def refresh():
    d = build()
    if not any(d["sources"].values()):
        return None
    p = _path(); p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp"); tmp.write_text(json.dumps(d)); tmp.replace(p)
    print(f"[SOCIAL] scanned: {d['sources']}")
    return d


_started = False


def start():
    global _started
    if _started or os.environ.get("SOCIAL_ENABLED", "true").lower() in ("0", "false", "no"):
        return
    _started = True

    def loop():
        time.sleep(45)
        while True:
            try:
                c = cached(None)
                if not c or time.time() - c.get("ts", 0) > EVERY_H * 3600:
                    refresh()
            except Exception as e:
                print(f"[SOCIAL] error: {type(e).__name__}")
            time.sleep(900)
    threading.Thread(target=loop, daemon=True, name="social").start()
