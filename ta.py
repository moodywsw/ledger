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


# ---------- extra intel ----------
def volume_profile(bars, bins=24):
    lo, hi = min(b["l"] for b in bars), max(b["h"] for b in bars)
    if hi <= lo:
        return None
    w = (hi - lo) / bins
    vol = [0.0] * bins
    for b in bars:
        a, z = int((b["l"] - lo) / w), int((b["h"] - lo) / w)
        a, z = max(0, min(bins - 1, a)), max(0, min(bins - 1, z))
        share = b["v"] / (z - a + 1)
        for i in range(a, z + 1):
            vol[i] += share
    poc = max(range(bins), key=lambda i: vol[i])
    tot, inc, lo_i, hi_i = sum(vol), vol[poc], poc, poc
    while inc < 0.7 * tot and (lo_i > 0 or hi_i < bins - 1):
        nxt_lo = vol[lo_i - 1] if lo_i > 0 else -1
        nxt_hi = vol[hi_i + 1] if hi_i < bins - 1 else -1
        if nxt_hi >= nxt_lo: hi_i += 1; inc += nxt_hi
        else: lo_i -= 1; inc += nxt_lo
    mx = max(vol) or 1
    return {"poc": lo + (poc + .5) * w, "vah": lo + (hi_i + 1) * w, "val": lo + lo_i * w,
            "bins": [{"p": lo + (i + .5) * w, "v": round(vol[i] / mx, 3)} for i in range(bins)]}


def hl_ctx(sym: str):
    try:
        m, c = requests.post("https://api.hyperliquid.xyz/info", json={"type": "metaAndAssetCtxs"}, timeout=12).json()
        names = [u["name"] for u in m["universe"]]
        if sym not in names:
            return None
        x = c[names.index(sym)]
        px = float(x["markPx"])
        return {"funding_8h_pct": float(x["funding"]) * 8 * 100, "oi_usd": float(x["openInterest"]) * px, "vol24_usd": float(x["dayNtlVlm"]),
                "premium_pct": float(x.get("premium") or 0) * 100}
    except Exception:
        return None


def hl_top(sym: str):
    try:
        import market_thoughts as mt
        return (((mt.cached() or {}).get("hyperliquid") or {}).get("coins") or {}).get(sym)
    except Exception:
        return None


def gt_holders(chain: str, addr: str):
    try:
        d = _get(f"https://api.geckoterminal.com/api/v2/networks/{GT_NET[chain]}/tokens/{addr}/info")["data"]["attributes"]
    except Exception:
        return None
    h = d.get("holders") or {}
    dist = h.get("distribution_percentage") or {}
    out = {"count": h.get("count"), "top10_pct": float(dist["top_10"]) if dist.get("top_10") else None,
           "top11_20_pct": float(dist["11_20"]) if dist.get("11_20") else None, "gt_score": d.get("gt_score"), "twitter": d.get("twitter_handle")}
    try:   # holder growth vs the last time anyone asked Mirko about this token
        from pathlib import Path
        import json as _j
        f = Path(os.environ.get("DATA_DIR", ".")) / "ta_holders.json"
        hist = _j.loads(f.read_text()) if f.exists() else {}
        prev = hist.get(addr)
        if prev and out["count"] and prev["count"]:
            out["growth"] = {"delta": out["count"] - prev["count"], "hours": round((time.time() - prev["ts"]) / 3600, 1)}
        if out["count"] and (not prev or time.time() - prev["ts"] > 3600):
            hist[addr] = {"count": out["count"], "ts": time.time()}
            f.write_text(_j.dumps(dict(list(hist.items())[-2000:])))
    except Exception:
        pass
    return out


def whale_trades(chain: str, pool: str, min_usd: float = 5000):
    try:
        d = _get(f"https://api.geckoterminal.com/api/v2/networks/{GT_NET[chain]}/pools/{pool}/trades", {"trade_volume_in_usd_greater_than": min_usd})["data"]
    except Exception:
        return None
    cut = time.time() - 86400
    buys = sells = 0.0; nb = ns = 0; biggest = []
    import datetime as _dt
    for t in d:
        a = t["attributes"]
        try:
            ts = _dt.datetime.fromisoformat(a["block_timestamp"].replace("Z", "+00:00")).timestamp()
        except Exception:
            continue
        if ts < cut:
            continue
        v = float(a.get("volume_in_usd") or 0)
        if a.get("kind") == "buy": buys += v; nb += 1
        else: sells += v; ns += 1
        biggest.append({"kind": a.get("kind"), "usd": round(v), "ago_h": round((time.time() - ts) / 3600, 1)})
    biggest.sort(key=lambda x: -x["usd"])
    return {"min_usd": min_usd, "buys_usd": round(buys), "sells_usd": round(sells), "n_buys": nb, "n_sells": ns, "largest": biggest[:5]}


BULL = re.compile(r"\b(moon|bull|pump|breakout|undervalued|accumulat|long|buy(ing)?|send it|gem|ath|rally)\b", re.I)
BEAR = re.compile(r"\b(dump|bear|rug|scam|short|sell(ing)?|dead|crash|overvalued|exit|rekt|down bad)\b", re.I)


def social(sym: str, name: str | None):
    import xml.etree.ElementTree as ET
    subs = " OR ".join(f"subreddit:{x}" for x in ("CryptoCurrency", "CryptoMarkets", "solana", "SatoshiStreetBets", "altcoin", "memecoins", "ethtrader", "Bitcoin", "CryptoMoonShots"))
    q = f'"{sym if len(sym) > 2 else (name or sym)}" ({subs})'
    posts = []
    SPAM = re.compile(r"referral|exchange|mexc|bydfi|zoomex|signal|airdrop|giveaway|promo|bingx|bitget|bybit|kucoin", re.I)
    for qq in (q, f'"{sym if len(sym) > 2 else (name or sym)}" crypto'):
        try:
            root = ET.fromstring(requests.get("https://www.reddit.com/search.rss", params={"q": qq, "sort": "new", "t": "week"}, headers=UA, timeout=12).content)
        except Exception:
            continue
        ns = {"a": "http://www.w3.org/2005/Atom"}
        for e in root.findall("a:entry", ns)[:50]:
            t = (e.findtext("a:title", "", ns) or "").strip()
            link = (e.find("a:link", ns).get("href") if e.find("a:link", ns) is not None else "")
            sub = (e.find("a:category", ns).get("label") if e.find("a:category", ns) is not None else "")
            if "/comments/" not in link or SPAM.search(sub) or any(p["url"] == link for p in posts):
                continue
            tone = "bull" if BULL.search(t) and not BEAR.search(t) else "bear" if BEAR.search(t) and not BULL.search(t) else "neutral"
            posts.append({"src": sub or "Reddit", "text": t[:160], "url": link if link.startswith("https://www.reddit.com/") else "", "tone": tone})
        if len(posts) >= 5:
            break
    news = []
    try:
        root = ET.fromstring(requests.get("https://news.google.com/rss/search", params={"q": f"{name or sym} crypto when:7d", "hl": "en-US", "gl": "US", "ceid": "US:en"}, headers=UA, timeout=12).content)
        for i in list(root.iter("item"))[:6]:
            news.append({"title": i.findtext("title", "")[:160], "url": i.findtext("link", ""), "src": i.find("source").text if i.find("source") is not None else ""})
    except Exception:
        pass
    nb, nr = sum(p["tone"] == "bull" for p in posts), sum(p["tone"] == "bear" for p in posts)
    return {"mentions_7d": len(posts), "bull": nb, "bear": nr,
            "quotes": [p for p in posts if p["tone"] != "neutral"][:4] or posts[:3], "news": news}


TA_SYS = ("You are Mirko, a professional crypto technical analyst and trader. Use ONLY the numbers given. Concise trader language, digits, no filler. "
          "Be decisive: if there's no edge, say 'No trade' and why. Conviction 1-10 must reflect R:R and confluence.")
TA_SCHEMA = ('{"summary": "2 sentences, the verdict", "structure": "1-2 sentences", "levels": "key S/R with numbers", "indicators": "EMAs, RSI, volume read", '
             '"volume_profile": "POC/value area read", "smart_money": "holders concentration, holder growth, whale flows, top-trader/HL positioning, funding/OI — whatever data exists", '
             '"liquidity": "on-chain liquidity/LP read or exchange liquidity", "sentiment": "social + news read", '
             '"prediction": {"up": 0-100, "sideways": 0-100, "down": 0-100, "horizon": "e.g. 3-7 days", "next_move": "one sentence"}, '
             '"action": {"type": "Long|Short|No trade", "entry": "price or zone", "tp": "targets", "sl": "stop", "rr": "R:R"}, '
             '"scenarios": {"bull": "trigger -> target", "base": "...", "bear": "trigger -> target"}, "conviction": 1-10, '
             '"thesis": "2-3 sentence thesis if warranted, else empty"}')


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


def _cg_search(q: str):
    try:
        cs = _get("https://api.coingecko.com/api/v3/search", {"query": q}).get("coins") or []
    except Exception:
        return None
    return cs[0] if cs else None


def _cg_platforms(cid: str):
    try:
        d = _get(f"https://api.coingecko.com/api/v3/coins/{cid}", {"localization": "false", "tickers": "false", "market_data": "false", "community_data": "false", "developer_data": "false"})
        return d.get("platforms") or {}, (d.get("image") or {}).get("small")
    except Exception:
        return {}, None


CG_CHAIN = {"solana": "solana", "ethereum": "ethereum", "base": "base", "binance-smart-chain": "bsc", "arbitrum-one": "arbitrum"}


def run(q: str) -> dict:
    q = q.strip()[:64]
    if not q or not re.match(r"^[A-Za-z0-9.$_ -]+$", q):
        return {"error": "Enter a ticker (SOL), a name (dogwifhat) or a contract address."}
    q = q.lstrip("$").strip()
    is_ca = bool(SOL_RX.match(q) or EVM_RX.match(q))
    key = q if is_ca else q.upper()
    with _lock:
        hit = _cache.get(key)
        if hit and time.time() - hit["ts"] < TTL:
            return hit
    meta, bars, tf, pair = {}, None, "4h", None
    sym = None if is_ca else q.upper().replace(" ", "")
    if sym and len(sym) <= 12:
        bars = _binance(sym)
    if not bars and not is_ca:   # name search -> CoinGecko -> Binance symbol or on-chain contract
        c = _cg_search(q)
        if c:
            sym = c["symbol"].upper()
            meta["logo"] = c.get("large") or c.get("thumb")
            bars = _binance(sym)
            if not bars:
                plats, logo = _cg_platforms(c["id"])
                meta["logo"] = logo or meta.get("logo")
                for k, v in plats.items():
                    if k in CG_CHAIN and v:
                        q, is_ca = v, True
                        break
    if bars:
        meta.update({"name": meta.get("name") or sym, "symbol": sym, "source": "Binance spot", "chain": "CEX", "tv": f"BINANCE:{sym}USDT"})
        if sym not in ("BTC", "ETH", "SOL", "BNB", "XRP", "USDC", "USDT"):
            try:   # canonical on-chain contract via CoinGecko (e.g. WIF on Solana) for holders / whale flows
                c = _cg_search(sym)
                plats = _cg_platforms(c["id"])[0] if c and c.get("symbol", "").upper() == sym else {}
                ca = next((v for k, v in plats.items() if k in CG_CHAIN and v), None)
                pp = _dex_pair(ca) if ca else None
                if pp and ((pp.get("liquidity") or {}).get("usd") or 0) > 2e5:
                    pair = pp
                    meta.update({"address": pp["baseToken"]["address"], "onchain": pp["chainId"], "liq": (pp.get("liquidity") or {}).get("usd")})
            except Exception:
                pass
    else:
        try:
            pair = _dex_pair(q)
        except Exception:
            pair = None
        if not pair:
            return {"error": "Couldn't find that token on Binance, CoinGecko or any DEX."}
        ch = pair["chainId"]
        young = (time.time() - (pair.get("pairCreatedAt") or 0) / 1000) < 86400 * 10
        bars = _gt(ch, pair["pairAddress"], "hour", 1 if young else 4, 300)
        tf = "1h" if young else "4h"
        bt = pair.get("baseToken") or {}
        sym = (bt.get("symbol") or "?").upper()
        meta.update({"name": bt.get("name"), "symbol": sym, "address": bt.get("address"), "chain": ch, "source": f"{pair.get('dexId')} via GeckoTerminal",
                     "logo": (pair.get("info") or {}).get("imageUrl") or meta.get("logo"), "mcap": pair.get("marketCap") or pair.get("fdv"),
                     "liq": (pair.get("liquidity") or {}).get("usd"), "url": pair.get("url"), "pair_age_d": round((time.time() - (pair.get("pairCreatedAt") or 0) / 1000) / 86400, 1),
                     "txns24": (pair.get("txns") or {}).get("h24"), "vol24": (pair.get("volume") or {}).get("h24"), "chg24": (pair.get("priceChange") or {}).get("h24")})
    if not bars or len(bars) < 30:
        return {"error": "Not enough price history yet for TA."}
    a = analyse(bars)
    vp = volume_profile(bars[-200:])
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(5) as ex:
        f_soc = ex.submit(social, sym, meta.get("name"))
        f_hl = ex.submit(hl_ctx, sym) if meta["chain"] == "CEX" else None
        och = meta.get("onchain") or meta["chain"]
        f_hold = ex.submit(gt_holders, och, meta["address"]) if meta.get("address") and och in GT_NET else None
        f_wh = ex.submit(whale_trades, och, pair["pairAddress"], 5000 if (meta.get("liq") or 0) > 2e5 else 1000) if pair and och in GT_NET else None
        intel = {"social": f_soc.result(), "hl": f_hl.result() if f_hl else None, "hl_top": hl_top(sym) if meta["chain"] == "CEX" else None,
                 "holders": f_hold.result() if f_hold else None, "whales": f_wh.result() if f_wh else None, "vp": vp}
    so, ho, wh, hl, ht = intel["social"], intel["holders"], intel["whales"], intel["hl"], intel["hl_top"]
    facts = (f"Token {sym} ({meta.get('name')}, {meta.get('chain')}), timeframe {tf}, {len(bars)} bars. Price {_fp(a['price'])}, change over the window {a['chg']:+.1f}%. "
             f"Structure: {a['structure']}. EMA20 {_fp(a['ema20'])} EMA50 {_fp(a['ema50'])} EMA200 {_fp(a['ema200'])}. RSI14 {a['rsi'] and round(a['rsi'], 1)}. "
             f"Volume last 20 bars vs prior 20: {a['vol_trend']:.2f}x. Supports {list(map(_fp, a['support']))}. Resistances {list(map(_fp, a['resistance']))}. "
             f"Trendline slopes upper {a['tl_high'] and round(a['tl_high']['slope_pct_bar'], 3)}%/bar, lower {a['tl_low'] and round(a['tl_low']['slope_pct_bar'], 3)}%/bar. "
             + (f"Volume profile: POC {_fp(vp['poc'])}, value area {_fp(vp['val'])}-{_fp(vp['vah'])}. " if vp else "")
             + (f"Mcap ${meta['mcap']:,.0f}, DEX liquidity ${meta['liq']:,.0f}, pair age {meta.get('pair_age_d')}d, 24h txns {meta.get('txns24')}, 24h vol ${meta.get('vol24') or 0:,.0f}. " if meta.get("mcap") and meta.get("liq") else "")
             + (f"Holders {ho['count']}, top10 hold {ho['top10_pct']}%" + (f", holder change {ho['growth']['delta']:+} in {ho['growth']['hours']}h" if ho.get("growth") else "") + ". " if ho and ho.get("count") else "")
             + (f"Whale trades >${wh['min_usd']} last 24h: buys ${wh['buys_usd']:,} ({wh['n_buys']}) vs sells ${wh['sells_usd']:,} ({wh['n_sells']}). " if wh else "")
             + (f"Hyperliquid perp: funding {hl['funding_8h_pct']:.4f}%/8h, OI ${hl['oi_usd']/1e6:.1f}m, 24h vol ${hl['vol24_usd']/1e6:.0f}m. " if hl else "")
             + (f"Hyperliquid top traders: {ht['longs']} long / {ht['shorts']} short. " if ht else "")
             + f"Reddit mentions 7d: {so['mentions_7d']} ({so['bull']} bullish / {so['bear']} bearish titles). "
             + ("News: " + " | ".join(n["title"] for n in so["news"][:5]) if so["news"] else "No recent news."))
    read, via = None, "rules"
    if _llm_ok():
        try:
            import llm
            read, via = llm.reason_json(TA_SYS, facts + "\nWrite the full read as JSON: " + TA_SCHEMA, 2500)
        except Exception as e:
            print(f"[TA] llm {type(e).__name__}")
    if not read or not read.get("action"):
        read, via = _rule_read(a, sym), "rules"
    try:
        read["conviction"] = max(1, min(10, int(read.get("conviction") or 5)))
    except (TypeError, ValueError):
        read["conviction"] = 5
    out = {"ts": time.time(), "q": key, "meta": meta, "tf": tf, "bars": [{"time": b["t"], "open": b["o"], "high": b["h"], "low": b["l"], "close": b["c"], "value": b["v"]} for b in bars],
           "ta": {k: v for k, v in a.items() if k != "series"}, "series": a["series"], "read": read, "via": via, "intel": intel}
    with _lock:
        _cache[key] = out
        if len(_cache) > 300:
            _cache.pop(next(iter(_cache)))
    return out
