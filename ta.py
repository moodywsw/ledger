"""Mirko TA: resolve a ticker / contract address, pull OHLCV (Binance for CEX coins, GeckoTerminal for on-chain pools),
compute EMAs / RSI / S-R / trendlines / scenario paths, and get Mirko's read (DeepSeek -> Gemini, capped). Cached 10 min per token."""
from __future__ import annotations

import os, re, threading, time
import requests

UA = {"User-Agent": "Mozilla/5.0 (Mirko TA)", "accept": "application/json"}
_cache: dict = {}
_lock = threading.Lock()
TTL = 600
LLM_DAY_CAP = int(os.environ.get("TA_LLM_DAY_CAP", "150"))
_llm_day = {"d": "", "n": 0}
SOL_RX = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{32,44}$")
EVM_RX = re.compile(r"^0x[0-9a-fA-F]{40}$")
GT_NET = {"solana": "solana", "ethereum": "eth", "base": "base", "bsc": "bsc", "arbitrum": "arbitrum", "polygon": "polygon_pos",
          "avalanche": "avax", "optimism": "optimism", "sui": "sui-network", "ton": "ton", "hyperevm": "hyperevm"}
BN_SPOT = ["https://api.binance.com", "https://data-api.binance.vision"]


def _get(url, params=None, t=15):
    r = requests.get(url, params=params, headers=UA, timeout=t)
    r.raise_for_status()
    return r.json()


def _binance(sym: str, interval="4h", limit=300):
    for base in BN_SPOT:
        try:
            k = _get(f"{base}/api/v3/klines", {"symbol": f"{sym}USDT", "interval": interval, "limit": limit})
            if k:
                return [{"t": int(x[0] / 1000), "o": float(x[1]), "h": float(x[2]), "l": float(x[3]), "c": float(x[4]), "v": float(x[5]) * float(x[4])} for x in k]
        except Exception:
            continue
    return None


def _dex_pair(q: str):
    if SOL_RX.match(q) or EVM_RX.match(q):
        d = _get(f"https://api.dexscreener.com/latest/dex/tokens/{q}")
    else:
        d = _get("https://api.dexscreener.com/latest/dex/search", {"q": q})
    ps = [p for p in (d.get("pairs") or []) if p.get("chainId") in GT_NET]
    if not SOL_RX.match(q) and not EVM_RX.match(q):
        ps = [p for p in ps if (p.get("baseToken") or {}).get("symbol", "").upper() == q.upper()] or ps
    ps.sort(key=lambda p: (p.get("liquidity") or {}).get("usd") or 0, reverse=True)
    return ps[0] if ps else None


def _gt(chain: str, pool: str, tf="hour", agg=1, limit=300):
    d = _get(f"https://api.geckoterminal.com/api/v2/networks/{GT_NET[chain]}/pools/{pool}/ohlcv/{tf}", {"aggregate": agg, "limit": limit, "currency": "usd"})
    rows = d["data"]["attributes"]["ohlcv_list"]
    return [{"t": int(r[0]), "o": float(r[1]), "h": float(r[2]), "l": float(r[3]), "c": float(r[4]), "v": float(r[5])} for r in sorted(rows)]


def ema(v, n):
    if not v:
        return []
    k, out, e = 2 / (n + 1), [], v[0]
    for x in v:
        e = x * k + e * (1 - k)
        out.append(e)
    return out


def rsi_series(c, n=14):
    out = [None] * len(c)
    if len(c) <= n:
        return out
    g = [max(c[i] - c[i - 1], 0) for i in range(1, len(c))]
    l = [max(c[i - 1] - c[i], 0) for i in range(1, len(c))]
    ag, al = sum(g[:n]) / n, sum(l[:n]) / n
    for i in range(n, len(g) + 1):
        if i > n:
            ag, al = (ag * (n - 1) + g[i - 1]) / n, (al * (n - 1) + l[i - 1]) / n
        out[i] = 100.0 if al == 0 else 100 - 100 / (1 + ag / al)
    return out


def swings(bars, w=4):
    hi = [i for i in range(w, len(bars) - w) if bars[i]["h"] == max(b["h"] for b in bars[i - w:i + w + 1])]
    lo = [i for i in range(w, len(bars) - w) if bars[i]["l"] == min(b["l"] for b in bars[i - w:i + w + 1])]
    return hi, lo


def levels(bars, hi, lo, price):
    pts = [bars[i]["h"] for i in hi] + [bars[i]["l"] for i in lo]
    cl = []
    for p in sorted(pts):
        if cl and abs(p / cl[-1][0] - 1) < 0.015:
            cl[-1].append(p)
        else:
            cl.append([p])
    lv = [(sum(c) / len(c), len(c)) for c in cl]
    sup = sorted([x for x in lv if x[0] < price], key=lambda x: -x[0])[:3]
    res = sorted([x for x in lv if x[0] > price], key=lambda x: x[0])[:3]
    return [round(x[0], 10) for x in sup], [round(x[0], 10) for x in res]


def trendline(bars, idx, key):
    if len(idx) < 2:
        return None
    a, b = idx[-2], idx[-1]
    ya, yb = bars[a][key], bars[b][key]
    slope = (yb - ya) / (b - a)
    end = len(bars) - 1
    return {"from": {"t": bars[a]["t"], "v": ya}, "to": {"t": bars[end]["t"], "v": yb + slope * (end - b)}, "slope_pct_bar": slope / ya * 100}


def analyse(bars):
    c = [b["c"] for b in bars]
    price = c[-1]
    e20, e50, e200 = ema(c, 20), ema(c, 50), ema(c, 200)
    r = rsi_series(c)
    hi, lo = swings(bars)
    sup, res = levels(bars, hi, lo, price)
    tl_hi, tl_lo = trendline(bars, hi, "h"), trendline(bars, lo, "l")
    step = bars[-1]["t"] - bars[-2]["t"]
    rng = (max(b["h"] for b in bars[-50:]) - min(b["l"] for b in bars[-50:])) or price * 0.05
    up = res[0] if res else price + rng * 0.5
    dn = sup[0] if sup else price - rng * 0.5
    T = lambda k: bars[-1]["t"] + step * k
    paths = {"bull": [{"t": bars[-1]["t"], "v": price}, {"t": T(8), "v": up}, {"t": T(18), "v": res[1] if len(res) > 1 else up + (up - price)}],
             "base": [{"t": bars[-1]["t"], "v": price}, {"t": T(8), "v": (price + dn) / 2 if price > (up + dn) / 2 else (price + up) / 2}, {"t": T(18), "v": price}],
             "bear": [{"t": bars[-1]["t"], "v": price}, {"t": T(8), "v": dn}, {"t": T(18), "v": sup[1] if len(sup) > 1 else dn - (price - dn)}]}
    vol20 = sum(b["v"] for b in bars[-20:]) / 20
    vol_prev = sum(b["v"] for b in bars[-40:-20]) / 20 or 1
    struct = ("higher highs & higher lows" if len(hi) > 1 and len(lo) > 1 and bars[hi[-1]]["h"] > bars[hi[-2]]["h"] and bars[lo[-1]]["l"] > bars[lo[-2]]["l"] else
              "lower highs & lower lows" if len(hi) > 1 and len(lo) > 1 and bars[hi[-1]]["h"] < bars[hi[-2]]["h"] and bars[lo[-1]]["l"] < bars[lo[-2]]["l"] else "mixed / range")
    return {"price": price, "ema20": e20[-1], "ema50": e50[-1], "ema200": e200[-1] if len(c) >= 200 else None, "rsi": r[-1],
            "support": sup, "resistance": res, "tl_high": tl_hi, "tl_low": tl_lo, "paths": paths, "structure": struct,
            "vol_trend": vol20 / vol_prev, "chg": (price / c[-min(len(c), 42)] - 1) * 100,
            "series": {"ema20": [{"time": b["t"], "value": v} for b, v in zip(bars, e20)][20:],
                       "ema50": [{"time": b["t"], "value": v} for b, v in zip(bars, e50)][50:],
                       "ema200": [{"time": b["t"], "value": v} for b, v in zip(bars, e200)][200:],
                       "rsi": [{"time": b["t"], "value": v} for b, v in zip(bars, r) if v is not None]}}


TA_SYS = ("You are Mirko, a professional crypto technical analyst and trader. Use ONLY the numbers given. Concise trader language, digits, no filler. "
          "Be decisive: if there's no edge, say 'No trade' and why. Conviction 1-10 must reflect R:R and confluence.")
TA_SCHEMA = ('{"summary": "2 sentences", "structure": "1-2 sentences", "levels": "key S/R with numbers", "indicators": "EMAs, RSI, volume read", '
             '"action": {"type": "Long|Short|No trade", "entry": "price or zone", "tp": "targets", "sl": "stop", "rr": "R:R"}, '
             '"scenarios": {"bull": "trigger -> target", "base": "...", "bear": "trigger -> target"}, "conviction": 1-10}')


def _fp(x):
    return "n/a" if x is None else f"{x:,.2f}" if x >= 100 else f"{x:.4f}" if x >= 0.01 else f"{x:.3g}"


def _rule_read(a, name):
    p, e20, e50 = a["price"], a["ema20"], a["ema50"]
    up = p > e20 > e50
    dn = p < e20 < e50
    s, r = (a["support"] or [None])[0], (a["resistance"] or [None])[0]
    if up and s:
        act = {"type": "Long", "entry": f"{_fp(min(s, e20))}–{_fp(max(s, e20))}", "tp": _fp(r) if r else "trail", "sl": _fp(s * 0.97), "rr": ""}
    elif dn and r:
        act = {"type": "Short", "entry": f"{_fp(min(e20, r))}–{_fp(max(e20, r))}", "tp": _fp(s) if s else "trail", "sl": _fp(r * 1.03), "rr": ""}
    else:
        act = {"type": "No trade", "entry": "", "tp": "", "sl": "", "rr": ""}
    conv = 6 if act["type"] != "No trade" and (a["rsi"] or 50) < 70 and (a["rsi"] or 50) > 30 else 3
    return {"summary": f"{name}: {a['structure']}, price {'above' if p > e50 else 'below'} the 50 EMA.", "structure": a["structure"],
            "levels": f"Support {', '.join(map(_fp, a['support'])) or 'n/a'} · Resistance {', '.join(map(_fp, a['resistance'])) or 'n/a'}",
            "indicators": f"EMA20 {_fp(e20)}, EMA50 {_fp(e50)}, RSI {a['rsi'] and round(a['rsi'])}, volume {a['vol_trend']:.2f}x vs prior.",
            "action": act, "scenarios": {}, "conviction": conv}


def _llm_ok():
    d = time.strftime("%Y-%m-%d")
    with _lock:
        if _llm_day["d"] != d:
            _llm_day.update(d=d, n=0)
        if _llm_day["n"] >= LLM_DAY_CAP:
            return False
        _llm_day["n"] += 1
        return True


def run(q: str) -> dict:
    q = q.strip()[:64]
    if not q or not re.match(r"^[A-Za-z0-9.$_-]+$", q):
        return {"error": "Enter a ticker (e.g. SOL) or a contract address."}
    q = q.lstrip("$")
    key = q if (SOL_RX.match(q) or EVM_RX.match(q)) else q.upper()
    with _lock:
        hit = _cache.get(key)
        if hit and time.time() - hit["ts"] < TTL:
            return hit
    meta, bars, tf = {}, None, "4h"
    if not (SOL_RX.match(q) or EVM_RX.match(q)) and len(q) <= 12:
        bars = _binance(q.upper())
        if bars:
            meta = {"name": q.upper(), "symbol": q.upper(), "source": "Binance spot", "chain": "CEX"}
    if not bars:
        p = _dex_pair(q)
        if not p:
            return {"error": "Couldn't find that token on Binance or any DEX."}
        ch = p["chainId"]
        bars = _gt(ch, p["pairAddress"], "hour", 1 if (time.time() - (p.get("pairCreatedAt") or 0) / 1000) < 86400 * 10 else 4, 300)
        tf = "1h" if (time.time() - (p.get("pairCreatedAt") or 0) / 1000) < 86400 * 10 else "4h"
        bt = p.get("baseToken") or {}
        meta = {"name": bt.get("name"), "symbol": bt.get("symbol"), "address": bt.get("address"), "chain": ch, "source": f"{p.get('dexId')} via GeckoTerminal",
                "logo": (p.get("info") or {}).get("imageUrl"), "mcap": p.get("marketCap") or p.get("fdv"), "liq": (p.get("liquidity") or {}).get("usd"),
                "url": p.get("url")}
    if not bars or len(bars) < 30:
        return {"error": "Not enough price history yet for TA."}
    a = analyse(bars)
    facts = (f"Token {meta.get('symbol')} ({meta.get('chain')}), timeframe {tf}, {len(bars)} bars. Price {_fp(a['price'])}, change over ~7d {a['chg']:+.1f}%. "
             f"Structure: {a['structure']}. EMA20 {_fp(a['ema20'])} EMA50 {_fp(a['ema50'])} EMA200 {_fp(a['ema200'])}. RSI14 {a['rsi'] and round(a['rsi'], 1)}. "
             f"Volume last 20 bars vs prior 20: {a['vol_trend']:.2f}x. Supports {list(map(_fp, a['support']))}. Resistances {list(map(_fp, a['resistance']))}. "
             f"Upper trendline slope {a['tl_high'] and round(a['tl_high']['slope_pct_bar'], 3)}%/bar, lower {a['tl_low'] and round(a['tl_low']['slope_pct_bar'], 3)}%/bar. "
             + (f"Mcap ${meta['mcap']:,.0f}, liquidity ${meta['liq']:,.0f}." if meta.get("mcap") and meta.get("liq") else ""))
    read, via = None, "rules"
    if _llm_ok():
        try:
            import llm
            read, via = llm.reason_json(TA_SYS, facts + "\nWrite the TA read as JSON: " + TA_SCHEMA, 1500)
        except Exception as e:
            print(f"[TA] llm {type(e).__name__}")
    if not read or not read.get("action"):
        read, via = _rule_read(a, meta.get("symbol") or q), "rules"
    try:
        read["conviction"] = max(1, min(10, int(read.get("conviction") or 5)))
    except (TypeError, ValueError):
        read["conviction"] = 5
    out = {"ts": time.time(), "q": key, "meta": meta, "tf": tf, "bars": [{"time": b["t"], "open": b["o"], "high": b["h"], "low": b["l"], "close": b["c"], "value": b["v"]} for b in bars],
           "ta": {k: v for k, v in a.items() if k != "series"}, "series": a["series"], "read": read, "via": via}
    with _lock:
        _cache[key] = out
        if len(_cache) > 300:
            _cache.pop(next(iter(_cache)))
    return out
