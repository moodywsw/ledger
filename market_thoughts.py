"""Mirko Market Thoughts — a super-trader market read that runs inside Mirko.

Ported from the standalone market-reads generator (read.py): same free, keyless
sources (Binance/Bybit/OKX/Coinbase, CoinGecko, alternative.me, Hyperliquid,
GeckoTerminal). Refreshed in a background thread every MARKET_THOUGHTS_EVERY_H
hours (default 2) and cached to $DATA_DIR/market_thoughts.json. Every source
fails soft: a section whose data is missing is simply omitted.

Consumers:
  * /api/market_thoughts (website tab)
  * persona (musings / beliefs / mood regime)
  * ledger_bot sizing: regime_size_scale() -> risk-off shrinks new entries
"""
from __future__ import annotations

import json, os, threading, time, datetime as dt
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import requests

UA = {"User-Agent": "ledger-market-thoughts/1.0"}
ASSETS = [("BTC", "BTCUSDT"), ("ETH", "ETHUSDT"), ("SOL", "SOLUSDT")]
SPOT_HOSTS = ["https://data-api.binance.vision", "https://api.binance.com"]
EVERY_H = float(os.environ.get("MARKET_THOUGHTS_EVERY_H", "2"))
HL_ENABLED = os.environ.get("MARKET_THOUGHTS_HL", "true").lower() not in ("0", "false", "no")
RISK_OFF_SCALE = float(os.environ.get("REGIME_RISK_OFF_SCALE", "0.6"))
REGIME_FILTER = os.environ.get("MARKET_REGIME_FILTER", "true").lower() not in ("0", "false", "no")
BOOM_FILE = Path(__file__).resolve().parent / "data" / "next_boom.json"
LOG: list = []


def cache_path() -> Path:
    return Path(os.environ.get("DATA_DIR", ".")) / "market_thoughts.json"


def log(m):
    LOG.append(m)
    print(f"[MARKET] {m}")


def get(url, params=None, timeout=20):
    for i in range(3):   # free APIs (GeckoTerminal: ~30 req/min) answer 429 under load: back off and retry
        r = requests.get(url, params=params, headers=UA, timeout=timeout)
        if r.status_code == 429 and i < 2:
            time.sleep(float(r.headers.get("Retry-After") or 0) or 4 * (i + 1))
            continue
        r.raise_for_status()
        return r.json()


def spot(path, params):
    last = None
    for h in SPOT_HOSTS:
        try:
            return get(h + path, params)
        except Exception as e:
            last = e
    raise last


def safe(name, fn, *a, **k):
    try:
        return fn(*a, **k)
    except Exception as e:
        log(f"{name} failed: {str(e)[:120]}")
        return None


def pct(a, b):
    return (a / b - 1) * 100 if b else None


def sma(v, n):
    return sum(v[-n:]) / n if len(v) >= n else None


def rsi(closes, n=14):
    if len(closes) < n + 1:
        return None
    g = [max(closes[i] - closes[i - 1], 0) for i in range(1, len(closes))]
    l = [max(closes[i - 1] - closes[i], 0) for i in range(1, len(closes))]
    ag, al = sum(g[:n]) / n, sum(l[:n]) / n
    for i in range(n, len(g)):
        ag, al = (ag * (n - 1) + g[i]) / n, (al * (n - 1) + l[i]) / n
    return 100.0 if al == 0 else 100 - 100 / (1 + ag / al)


def pivots(h, l, w=2):
    ph = [h[i] for i in range(w, len(h) - w) if h[i] == max(h[i - w:i + w + 1])]
    pl = [l[i] for i in range(w, len(l) - w) if l[i] == min(l[i - w:i + w + 1])]
    return ph, pl


def cluster(levels, tol=0.02):
    out = []
    for lv in sorted(levels):
        if out and abs(lv / out[-1][0] - 1) < tol:
            out[-1].append(lv)
        else:
            out.append([lv])
    return [sum(c) / len(c) for c in out]


def klines(sym, interval, limit):
    k = spot("/api/v3/klines", {"symbol": sym, "interval": interval, "limit": limit})
    return [{"t": r[0], "o": float(r[1]), "h": float(r[2]), "l": float(r[3]), "c": float(r[4])} for r in k]


def asset_data(name, sym):
    t = spot("/api/v3/ticker/24hr", {"symbol": sym})
    price = float(t["lastPrice"])
    d, h4 = klines(sym, "1d", 120), klines(sym, "4h", 120)
    closes = [x["c"] for x in d]
    closes[-1] = price
    a = {"name": name, "price": price, "chg24": float(t["priceChangePercent"]), "vol24": float(t["quoteVolume"]),
         "chg7": pct(price, d[-8]["c"]) if len(d) >= 8 else None,
         "chg30": pct(price, d[-31]["c"]) if len(d) >= 31 else None,
         "hi30": max(x["h"] for x in d[-30:]), "lo30": min(x["l"] for x in d[-30:]),
         "ma20": sma(closes, 20), "ma50": sma(closes, 50), "ma20_prev": sma(closes[:-5], 20),
         "rsi_d": rsi(closes), "rsi_4h": rsi([x["c"] for x in h4[:-1]] + [price]),
         "hi7": max(x["h"] for x in h4[-42:]), "lo7": min(x["l"] for x in h4[-42:]),
         "atr": sum(max(d[i]["h"], d[i - 1]["c"]) - min(d[i]["l"], d[i - 1]["c"]) for i in range(len(d) - 14, len(d))) / 14}
    ph, pl = pivots([x["h"] for x in d[-90:]], [x["l"] for x in d[-90:]], 3)
    ph4, pl4 = pivots([x["h"] for x in h4[-42:]], [x["l"] for x in h4[-42:]], 3)
    rc = [x for x in ph + ph4 + [a["hi7"], a["hi30"]] if x > price * 1.003]
    sc = [x for x in pl + pl4 + [a["lo7"], a["lo30"]] if x < price * 0.997]
    for m in (a["ma20"], a["ma50"]):
        if m:
            (rc if m > price * 1.003 else sc if m < price * 0.997 else []).append(m)
    a["res"], a["sup"] = sorted(cluster(rc))[:2], sorted(cluster(sc), reverse=True)[:2]
    return a


def derivatives(name, sym, price):
    """Funding / OI / positioning across Binance, Bybit, OKX. Each piece optional."""
    out = {"funding": {}, "oi": {}}
    F = "https://fapi.binance.com/futures/data/"

    def ratio(path, key="longAccount"):
        r = get(F + path, {"symbol": sym, "period": "4h", "limit": 7})
        return float(r[-1][key]), float(r[0][key])
    x = safe(f"{name} bn top", ratio, "topLongShortPositionRatio")
    if x: out["top_long"], out["top_long_prev"] = x
    x = safe(f"{name} bn retail", ratio, "globalLongShortAccountRatio")
    if x: out["retail_long"] = x[0]

    def taker():
        r = get(F + "takerlongshortRatio", {"symbol": sym, "period": "4h", "limit": 6})
        b, s = sum(float(i["buyVol"]) for i in r), sum(float(i["sellVol"]) for i in r)
        return b / s if s else None
    x = safe(f"{name} taker", taker)
    if x: out["taker"] = x
    oih = safe(f"{name} oi hist", get, F + "openInterestHist", {"symbol": sym, "period": "4h", "limit": 7})
    if oih and len(oih) >= 7:
        out["oi_chg24"] = pct(float(oih[-1]["sumOpenInterestValue"]), float(oih[0]["sumOpenInterestValue"]))
    pi = safe(f"{name} bn funding", get, "https://fapi.binance.com/fapi/v1/premiumIndex", {"symbol": sym})
    if pi: out["funding"]["Binance"] = float(pi["lastFundingRate"]) * 100
    oi = safe(f"{name} bn oi", get, "https://fapi.binance.com/fapi/v1/openInterest", {"symbol": sym})
    if oi: out["oi"]["Binance"] = float(oi["openInterest"]) * price
    bt = safe(f"{name} bybit", get, "https://api.bybit.com/v5/market/tickers", {"category": "linear", "symbol": sym})
    if bt and bt.get("retCode") == 0 and bt["result"]["list"]:
        t = bt["result"]["list"][0]
        out["funding"]["Bybit"] = float(t["fundingRate"]) * 100
        out["oi"]["Bybit"] = float(t["openInterestValue"])
    inst = f"{name}-USDT-SWAP"
    of = safe(f"{name} okx funding", get, "https://www.okx.com/api/v5/public/funding-rate", {"instId": inst})
    if of and of.get("code") == "0" and of["data"]:
        out["funding"]["OKX"] = float(of["data"][0]["fundingRate"]) * 100
    oo = safe(f"{name} okx oi", get, "https://www.okx.com/api/v5/public/open-interest", {"instType": "SWAP", "instId": inst})
    if oo and oo.get("code") == "0" and oo["data"]:
        out["oi"]["OKX"] = float(oo["data"][0]["oiUsd"])
    tot = sum(out["oi"].values())
    if out["funding"]:
        w = [(out["oi"].get(k, 0), v) for k, v in out["funding"].items()]
        ws = sum(a for a, _ in w)
        out["funding_agg"] = sum(a * v for a, v in w) / ws if ws else sum(v for _, v in w) / len(w)
    out["oi_total"] = tot or None
    return out


def global_data():
    g = get("https://api.coingecko.com/api/v3/global")["data"]
    return {"mcap": g["total_market_cap"]["usd"], "mcap_chg": g.get("market_cap_change_percentage_24h_usd"),
            "btc_dom": g["market_cap_percentage"]["btc"]}


def fng():
    d = get("https://api.alternative.me/fng/", {"limit": 8})["data"]
    return {"v": int(d[0]["value"]), "cls": d[0]["value_classification"], "prev": int(d[1]["value"]),
            "wk": int(d[7]["value"]) if len(d) > 7 else None}


def coinbase_premium():
    cb = float(get("https://api.exchange.coinbase.com/products/BTC-USD/ticker")["price"])
    bn = float(spot("/api/v3/ticker/price", {"symbol": "BTCUSDT"})["price"])
    return (cb / bn - 1) * 100


def trending():
    cs = get("https://api.coingecko.com/api/v3/search/trending")["coins"]
    return [{"symbol": c["item"]["symbol"].upper(), "name": c["item"]["name"],
             "rank": c["item"].get("market_cap_rank"),
             "chg24": ((c["item"].get("data") or {}).get("price_change_percentage_24h") or {}).get("usd")} for c in cs[:8]]


def hl_smart_money(top=20):
    """Hyperliquid: top accounts (>$1M, positive month AND all-time PnL) -> BTC/ETH/SOL net positioning."""
    lb = get("https://stats-data.hyperliquid.xyz/Mainnet/leaderboard", timeout=120)["leaderboardRows"]
    elig = []
    for r in lb:
        try:
            wp = {k: v for k, v in r["windowPerformances"]}
            av, m, at = float(r["accountValue"]), float(wp["month"]["pnl"]), float(wp["allTime"]["pnl"])
            if av > 1e6 and m > 0 and at > 0:
                elig.append({"addr": r["ethAddress"].lower(), "month": m, "all": at})
        except Exception:
            continue
    uni = {x["addr"]: x for x in sorted(elig, key=lambda x: -x["month"])[:top] + sorted(elig, key=lambda x: -x["all"])[:top]}

    def ch(addr):
        for i in range(3):
            try:
                r = requests.post("https://api.hyperliquid.xyz/info", json={"type": "clearinghouseState", "user": addr},
                                  headers=UA, timeout=20)
                r.raise_for_status()
                return r.json()
            except Exception:
                time.sleep(1 + i)
        return None
    with ThreadPoolExecutor(6) as ex:
        states = [s for s in ex.map(ch, list(uni)) if s]
    if len(states) < 8:
        raise RuntimeError(f"only {len(states)} HL states")
    coins = {}
    for c in ("BTC", "ETH", "SOL"):
        pos = []
        for s in states:
            for ap in s.get("assetPositions", []):
                p = ap.get("position", {})
                if p.get("coin") == c and float(p.get("szi") or 0) != 0:
                    pos.append({"long": float(p["szi"]) > 0, "ntl": abs(float(p["positionValue"])), "entry": float(p["entryPx"])})
        L, S = [p for p in pos if p["long"]], [p for p in pos if not p["long"]]
        ln, sn = sum(p["ntl"] for p in L), sum(p["ntl"] for p in S)
        wavg = lambda ps: (sum(p["ntl"] * p["entry"] for p in ps) / sum(p["ntl"] for p in ps)) if ps else None
        coins[c] = {"longs": len(L), "shorts": len(S), "long_ntl": ln, "short_ntl": sn,
                    "long_share": ln / (ln + sn) if ln + sn else None, "long_entry": wavg(L), "short_entry": wavg(S)}
    return {"accounts": len(states), "coins": coins}


_POOLS: list = []   # raw trending pools from the last dex_flows() call (reused for low caps / trenches)


def dex_flows():
    """Net buy pressure on trending DEX pools (GeckoTerminal), Solana/Base/BSC."""
    rows = []
    _POOLS.clear()
    for net in ("solana", "base", "bsc"):
        d = safe(f"gt {net}", get, f"https://api.geckoterminal.com/api/v2/networks/{net}/trending_pools", {"page": 1})
        for p in ((d or {}).get("data") or []):
            _POOLS.append((net, p))
        for p in ((d or {}).get("data") or [])[:10]:
            a = p.get("attributes") or {}
            tx = (a.get("transactions") or {}).get("h24") or {}
            b, s = tx.get("buyers") or tx.get("buys") or 0, tx.get("sellers") or tx.get("sells") or 0
            vol = float((a.get("volume_usd") or {}).get("h24") or 0)
            if vol < 250_000 or not (b + s):
                continue
            rows.append({"pair": (a.get("name") or "").split(" / ")[0][:16], "chain": net, "vol24": vol,
                         "buy_share": b / (b + s), "chg24": float((a.get("price_change_percentage") or {}).get("h24") or 0),
                         "liq": float(a.get("reserve_in_usd") or 0)})
        time.sleep(1.2)
    rows.sort(key=lambda r: -(r["buy_share"] - 0.5) * min(r["vol24"], 5e6))
    return rows[:8]


# ---------------- analysis ----------------
def trend_score(a):
    s, p = 0, a["price"]
    if a["ma20"]: s += 1 if p > a["ma20"] else -1
    if a["ma50"]: s += 1 if p > a["ma50"] else -1
    if a["ma20"] and a["ma50"]: s += 1 if a["ma20"] > a["ma50"] else -1
    if a["ma20"] and a["ma20_prev"]: s += 1 if a["ma20"] > a["ma20_prev"] else -1
    if a["rsi_d"] is not None: s += 1 if a["rsi_d"] > 55 else -1 if a["rsi_d"] < 45 else 0
    if a.get("chg7") is not None: s += 1 if a["chg7"] > 3 else -1 if a["chg7"] < -3 else 0
    return s


def asset_bias(a, score):
    if a["ma20"] and a["price"] < a["ma20"] and score >= 1:
        return "neutral", "Neutral · pullback in uptrend"
    if a["ma20"] and a["price"] > a["ma20"] and score <= -1:
        return "neutral", "Neutral · bounce in downtrend"
    if score >= 3: return "bull", "Bullish"
    if score >= 1: return "bull", "Lean bullish"
    if score <= -3: return "bear", "Bearish"
    if score <= -1: return "bear", "Lean bearish"
    return "neutral", "Neutral"


def momentum_text(a):
    r, r4 = a["rsi_d"], a["rsi_4h"]
    if r is None: return ""
    t = ("daily RSI overbought, chasing here is late" if r >= 70 else "momentum healthy on the daily" if r >= 58 else
         "momentum flat, no edge either way" if r >= 45 else "momentum weak on the daily" if r >= 32 else
         "daily RSI oversold, bounce risk for shorts")
    if r4 is not None:
        if r4 >= 70: t += ", 4h stretched"
        elif r4 <= 30: t += ", 4h oversold"
        elif r < 45 and r4 > 55: t += ", but 4h is trying to turn up"
        elif r > 55 and r4 < 45: t += ", 4h cooling off"
    return t


def fp(x):
    if x is None: return "n/a"
    return f"${x:,.0f}" if x >= 1000 else f"${x:,.2f}" if x >= 10 else f"${x:,.4f}"


def asset_read(a, dv, hl):
    sc = trend_score(a)
    tone, word = asset_bias(a, sc)
    p = a["price"]
    r1 = a["res"][0] if a["res"] else a["hi30"]
    s1 = a["sup"][0] if a["sup"] else a["lo30"]
    r2 = a["res"][1] if len(a["res"]) > 1 else None
    s2 = a["sup"][1] if len(a["sup"]) > 1 else None
    pos20 = "above" if a["ma20"] and p > a["ma20"] else "below"
    pos50 = "above" if a["ma50"] and p > a["ma50"] else "below"
    notes = []
    f = (dv or {}).get("funding_agg")
    oc = (dv or {}).get("oi_chg24")
    if oc is not None and abs(oc) >= 3:
        notes.append("OI rising into weakness, fresh shorts" if oc > 0 and a["chg24"] < 0 else
                     "OI rising with price, real demand" if oc > 0 else
                     "OI falling on the bounce, short covering" if a["chg24"] > 0 else "OI flushing out, deleveraging")
    if hl and hl["longs"] + hl["shorts"] >= 4:
        n = hl["longs"] + hl["shorts"]
        if hl["longs"] >= 0.7 * n: notes.append(f"Hyperliquid top traders {hl['longs']}/{n} long")
        elif hl["shorts"] >= 0.7 * n: notes.append(f"Hyperliquid top traders {hl['shorts']}/{n} short")
    if tone == "bull":
        plan = f"Buy dips toward {fp(s1)} while it holds; invalid on a daily close below {fp(s2 or a['lo30'])}."
    elif tone == "bear":
        plan = f"Sell rips into {fp(r1)} until reclaimed; invalid on a daily close above {fp(r2 or r1)}."
    else:
        top = r1 if r1 >= p * 1.015 or not r2 else r2
        bot = s1 if s1 <= p * 0.985 or not s2 else s2
        plan = f"Range {fp(bot)} to {fp(top)}. Trade the edges, not the middle; a daily close outside picks the side."
    return {"name": a["name"], "price": p, "chg24": a["chg24"], "chg7": a["chg7"], "score": sc, "tone": tone, "bias": word,
            "support": a["sup"], "resistance": a["res"], "ma20": a["ma20"], "ma50": a["ma50"], "rsi_d": a["rsi_d"],
            "rsi_4h": a["rsi_4h"], "structure": f"Price is {pos20} the 20D ({fp(a['ma20'])}) and {pos50} the 50D ({fp(a['ma50'])}); {momentum_text(a)}.",
            "bull_case": f"Daily close above {fp(r1)}" + (f" opens {fp(r2)}." if r2 else " opens a retest of the 30D high."),
            "bear_case": f"Losing {fp(s1)}" + (f" exposes {fp(s2)}." if s2 else " exposes the 30D low."),
            "notes": notes, "plan": plan,
            "funding": f, "oi_total": (dv or {}).get("oi_total"), "oi_chg24": oc, "top_long": (dv or {}).get("top_long"),
            "retail_long": (dv or {}).get("retail_long"), "taker": (dv or {}).get("taker"), "hl": hl}


def regime(assets, glob, fg):
    scores = [trend_score(a) for a in assets.values()]
    avg = sum(scores) / len(scores) if scores else 0
    if avg >= 3: reg, key = "Risk-on uptrend", "risk_on"
    elif avg >= 1.5: reg, key = "Constructive, choppy", "risk_on"
    elif avg > -1.5: reg, key = "Range / indecision", "chop"
    elif avg > -3: reg, key = "Corrective, risk-off tilt", "risk_off"
    else: reg, key = "Downtrend, risk-off", "risk_off"
    notes = []
    if fg:
        if fg["v"] >= 70 and avg < 0: notes.append("sentiment greedy while price is weak, a bearish divergence")
        elif fg["v"] <= 30 and avg > -1: notes.append("fear with price holding up, contrarian bullish")
        elif fg["v"] <= 25: notes.append("extreme fear, usually near local lows")
        elif fg["v"] >= 75: notes.append("extreme greed, upside getting crowded")
    if glob and glob.get("mcap_chg") is not None and abs(glob["mcap_chg"]) >= 3:
        notes.append(f"total mcap {glob['mcap_chg']:+.1f}% in 24h, " + ("strong risk appetite" if glob["mcap_chg"] > 0 else "broad selling"))
    btc = assets.get("BTC")
    if btc and btc.get("chg7") is not None and len(assets) > 1:
        weak = all(assets[c]["chg7"] is not None and assets[c]["chg7"] < btc["chg7"] for c in assets if c != "BTC")
        notes.append("alts lagging BTC, not alt season" if weak else "alts keeping pace with BTC")
    return {"label": reg, "key": key, "score": round(avg, 2), "notes": notes}


def make_idea(a, side, why):
    p, atr = a["price"], a.get("atr")
    if not atr: return None
    sup = [x for x in a["sup"] if x < p] or [a["lo30"]]
    res = [x for x in a["res"] if x > p] or [a["hi30"]]
    if side == "Long":
        s1 = sup[0]; lo, hi = s1, min(s1 + 0.25 * atr, p)
        if p - s1 > 2.5 * atr: lo, hi = p - 0.8 * atr, p - 0.4 * atr
        stop = min(lo, s1) - 0.5 * atr
        tg = sorted(set(x for x in res + [a["hi30"], a["hi7"]] if x > hi * 1.01))
        entry = (lo + hi) / 2; risk = entry - stop
        tg = [x for x in tg if (x - entry) >= 1.2 * risk]
        if not tg or risk <= 0: return None
        t1 = tg[0] if tg[0] - entry <= 4 * risk else entry + 2 * risk
        t2 = next((x for x in tg if x > t1 * 1.01 and x - entry <= 6 * risk), None)
        rr = (t1 - entry) / risk
    else:
        r1 = res[0]; lo, hi = max(r1 - 0.25 * atr, p), r1
        if r1 - p > 2.5 * atr: lo, hi = p + 0.4 * atr, p + 0.8 * atr
        stop = max(hi, r1) + 0.5 * atr
        tg = sorted(set(x for x in sup + [a["lo30"], a["lo7"]] if x < lo * 0.99), reverse=True)
        entry = (lo + hi) / 2; risk = stop - entry
        tg = [x for x in tg if (entry - x) >= 1.2 * risk]
        if not tg or risk <= 0: return None
        t1 = tg[0] if entry - tg[0] <= 4 * risk else entry - 2 * risk
        t2 = next((x for x in tg if x < t1 * 0.99 and entry - x <= 6 * risk), None)
        rr = (entry - t1) / risk
    if rr < 1.0: return None
    return {"name": a["name"], "side": side, "venue": "Spot" if side == "Long" else "Perp", "entry_lo": lo, "entry_hi": hi,
            "stop": stop, "t1": t1, "t2": t2, "rr": round(rr, 2), "why": why}


def trade_ideas(assets, hl):
    out = []
    for name, a in assets.items():
        tone, word = asset_bias(a, trend_score(a))
        h = (hl or {}).get("coins", {}).get(name) or {}
        if tone == "bull": side, why = "Long", f"{word.lower()} trend, buy the dip into support"
        elif tone == "bear": side, why = "Short", f"{word.lower()} trend, sell the bounce into resistance"
        else:
            mid = ((a["sup"][0] if a["sup"] else a["lo30"]) + (a["res"][0] if a["res"] else a["hi30"])) / 2
            side = "Long" if a["price"] <= mid else "Short"
            why = "range trade: " + ("buy the bottom of the range" if side == "Long" else "fade the top of the range")
        ls = h.get("long_share")
        if ls is not None and h.get("longs", 0) + h.get("shorts", 0) >= 4:
            if side == "Short" and ls <= 0.4: why += "; top traders short too"
            if side == "Long" and ls >= 0.6: why += "; top traders long too"
        i = make_idea(a, side, why) or make_idea(a, "Short" if side == "Long" else "Long", "counter-range setup at the next level")
        if i: out.append(i)
    return out


SCHEMA = 11
BOOM_DEFAULT = {"theme": "AI infrastructure", "emoji": "🤖", "thesis": "Compute, power and data centres keep absorbing capital; the picks-and-shovels trade is the patient one.",
                "boom_window": "Nov 2026 – Dec 2027", "why_now": ["Hyperscaler capex guidance keeps rising", "Power and cooling are the new bottleneck"],
                "crypto": ["TAO", "RENDER", "FET"], "stocks": ["NVDA", "AVGO", "VRT", "CEG"], "invalidation": "Capex cuts from two or more hyperscalers."}
BOOM_PROXIES = {"space": ["SOL", "LINK"], "ai": ["TAO", "RENDER", "FET"], "energy": ["BTC"], "nuclear": ["BTC"], "robot": ["TAO", "FET"],
                "quantum": ["BTC"], "defense": ["LINK"], "stable": ["ETH", "SOL"], "rwa": ["ONDO", "LINK"], "gaming": ["IMX"], "bitcoin": ["BTC"]}


def _insights():
    try:
        import insights
        d = insights.cached(max_age_h=36)
        return {k: d[k] for k in ("beliefs", "risk_tilt", "items", "date")} if d else None
    except Exception:
        return None


def _boom_hist_path():
    return Path(os.environ.get("DATA_DIR", ".")) / "boom_history.json"


def boom_with_history(cur: dict) -> dict:
    """Archive: when the current theme changes, the previous one moves to history (with its dates). Always kept."""
    try:
        h = json.loads(_boom_hist_path().read_text())
    except Exception:
        h = {"current": None, "since": None, "previous": []}
    today = dt.date.today().isoformat()
    if not h.get("current") or h["current"].get("theme") != cur.get("theme"):
        if h.get("current"):
            h["previous"].insert(0, {**h["current"], "shown_from": h.get("since"), "shown_to": today})
            h["previous"] = h["previous"][:30]
        h["current"], h["since"] = cur, today
        try:
            _boom_hist_path().write_text(json.dumps(h))
        except Exception:
            pass
    if len(h.get("previous", [])) < 4:   # seed the archive with the themes that ran on earlier days
        seen = {cur.get("theme")} | {x.get("theme") for x in h.get("previous", [])}
        base = dt.date.fromisoformat(h.get("since") or today)
        k = 1
        while len(h["previous"]) < 4 and k < 30:
            day = base - dt.timedelta(days=k)
            t = next_boom(day.isoformat())
            if t and t.get("theme") not in seen:
                seen.add(t["theme"]); h["previous"].append({**t, "shown_from": day.isoformat(), "shown_to": (day + dt.timedelta(days=1)).isoformat()})
            k += 1
        try:
            _boom_hist_path().write_text(json.dumps(h))
        except Exception:
            pass
    src = {}
    try:
        src = {t.get("theme"): t.get("best_source") for t in json.loads(BOOM_FILE.read_text()).get("themes", [])}
    except Exception:
        pass
    return {**cur, "since": h.get("since"), "previous": [{**{k: x.get(k) for k in ("theme", "emoji", "thesis", "boom_window", "crypto", "stocks", "shown_from", "shown_to")}, "source": x.get("best_source") or src.get(x.get("theme"))}
                                                         for x in h.get("previous", [])]}


def next_boom(today: str | None = None):
    """Rotates one researched theme per day from data/next_boom.json (no generated content)."""
    try:
        themes = json.loads(BOOM_FILE.read_text()).get("themes", [])
    except Exception:
        return None
    if not themes: return None
    d = dt.date.fromisoformat(today) if today else dt.date.today()
    pinned = [x for x in themes if x.get("featured_on") == d.isoformat()]
    rot = [x for x in themes if not x.get("featured_on") or x.get("featured_on") != d.isoformat()]
    t = pinned[0] if pinned else rot[d.toordinal() % len(rot)]
    keep = ("theme", "emoji", "thesis", "boom_window", "why_now", "crypto", "stocks", "invalidation", "researched", "best_source")
    out = {k: t.get(k) for k in keep if t.get(k) is not None}
    if not out.get("crypto"):   # never leave the crypto side empty: closest liquid proxies, labelled as such
        th = (out.get("theme", "") + " " + out.get("thesis", "")).lower()
        px = next((v for k, v in BOOM_PROXIES.items() if k in th), ["BTC", "ETH"])
        out["crypto"] = px; out["crypto_proxy"] = True
    return out


def outlook(reads, reg, fg, cbp, dex):
    bulls = [r["name"] for r in reads if r["tone"] == "bull"]
    bears = [r["name"] for r in reads if r["tone"] == "bear"]
    parts = [f"Regime: {reg['label'].lower()}."]
    if bulls and not bears: parts.append(f"{', '.join(bulls)} lead with constructive structure.")
    elif bears and not bulls: parts.append(f"{', '.join(bears)} under pressure; rallies are for selling until levels are reclaimed.")
    elif bulls and bears: parts.append(f"Split tape: {', '.join(bulls)} firm, {', '.join(bears)} weak.")
    else: parts.append("Majors are range-bound; patience beats prediction here.")
    if fg: parts.append(f"Fear & Greed {fg['v']} ({fg['cls'].lower()}).")
    if cbp is not None and abs(cbp) >= 0.05:
        parts.append("Coinbase premium positive, US spot bid present." if cbp > 0 else "Coinbase discount, US spot demand soft.")
    parts += [n[0].upper() + n[1:] + "." for n in reg["notes"][:2]]
    hot = [d for d in (dex or []) if d["buy_share"] >= 0.55]
    if hot: parts.append("On-chain, buyers dominate " + ", ".join(f"{d['pair']} ({d['chain']})" for d in hot[:3]) + ".")
    if reg["key"] == "risk_off": stance = "Defensive: smaller memecoin size, faster profit-taking, no chasing."
    elif reg["key"] == "risk_on": stance = "Offensive but disciplined: full size on clean signals, let winners breathe."
    else: stance = "Neutral: normal size, take profits into strength, respect stops."
    return " ".join(parts), stance


def _fmtp(v):
    if v is None: return "—"
    return f"${v:,.0f}" if v >= 100 else f"${v:,.2f}" if v >= 1 else f"${v:.4g}"


def idea_thesis(i: dict) -> dict:
    """Short organized thesis for every idea: why / trigger / target / invalidation."""
    why = (i.get("why") or "").split(":")[-1].strip() or "structure + flows line up"
    lo, hi = i.get("entry_lo"), i.get("entry_hi")
    long_ = str(i.get("side", "Long")).lower().startswith("l")
    trig = (f"buy the {_fmtp(lo)}–{_fmtp(hi)} zone on a hold" if long_ else f"short a rejection of {_fmtp(lo)}–{_fmtp(hi)}") if lo and hi else "on a clean reclaim"
    tgt = _fmtp(i.get("t1")) + (f" then {_fmtp(i['t2'])}" if i.get("t2") else "")
    inv = f"{'daily close below' if long_ else 'close above'} {_fmtp(i.get('stop'))}" if i.get("stop") else "structure breaks"
    if i.get("tier") in ("low", "micro"):
        inv += "; or buyers flip to net sellers"
    return {"why": why[0].upper() + why[1:], "trigger": trig[0].upper() + trig[1:], "target": tgt, "invalid": inv[0].upper() + inv[1:]}


def radar_micros(tr: dict) -> list:
    """Fallback micro caps from pump.fun graduates: >=2 days old, $80k-$700k mcap, liquidity >= $20k,
    buyers ahead on 6h AND 24h, price up on 6h (a base turning up). Checked live on DexScreener."""
    out = []
    cands = [x for x in ((tr.get("radar") or {}).get("pump") or {}).get("graduated", []) if x.get("mint") and (x.get("age_h") or 0) >= 48]
    for pg in range(1, 11):   # graduated pump.fun coins trade on PumpSwap: busiest pools that are 2+ days old and still micro
        try:
            d = requests.get("https://api.geckoterminal.com/api/v2/networks/solana/dexes/pumpswap/pools",
                             params={"page": pg, "sort": "h24_volume_usd_desc"}, timeout=10, headers={"Accept": "application/json"}).json().get("data") or []
        except Exception:
            break
        time.sleep(2.2)   # GeckoTerminal free tier: 30 calls/min
        for q in d:
            a = q.get("attributes") or {}
            try:
                age_h = (time.time() - dt.datetime.fromisoformat(a["pool_created_at"].replace("Z", "+00:00")).timestamp()) / 3600
                fdv = float(a.get("fdv_usd") or 0)
            except Exception:
                continue
            mint = (((q.get("relationships") or {}).get("base_token") or {}).get("data") or {}).get("id", "").split("_", 1)[-1]
            if age_h >= 48 and 8e4 <= fdv <= 7e5 and mint and not mint.startswith("So1111"):
                cands.append({"sym": a.get("name", "").split(" / ")[0], "mint": mint, "mc": fdv, "age_h": age_h})
    seen = set(); cands = [c for c in cands if not (c["mint"] in seen or seen.add(c["mint"]))]
    for c in cands[:14]:
        try:
            ps = requests.get(f"https://api.dexscreener.com/latest/dex/tokens/{c['mint']}", timeout=8).json().get("pairs") or []
        except Exception:
            continue
        if not ps: continue
        p = max(ps, key=lambda x: (x.get("liquidity") or {}).get("usd") or 0)
        liq = (p.get("liquidity") or {}).get("usd") or 0; mc = p.get("marketCap") or p.get("fdv") or 0
        tx = p.get("txns") or {}; h6, h24 = tx.get("h6") or {}, tx.get("h24") or {}
        px = float(p.get("priceUsd") or 0); ch6 = (p.get("priceChange") or {}).get("h6") or 0
        if not (8e4 <= mc <= 7e5 and liq >= 2e4 and px > 0 and h6.get("buys", 0) > h6.get("sells", 0) and h24.get("buys", 0) > h24.get("sells", 0) and ch6 > 0):
            continue
        bs = h24["buys"] / max(1, h24["buys"] + h24["sells"])
        out.append({"name": p.get("baseToken", {}).get("symbol") or c["sym"], "chain": "solana", "address": c["mint"], "tier": "micro", "side": "Long", "venue": "Spot", "spec": True,
                    "mcap": mc, "entry_lo": px * 0.95, "entry_hi": px * 1.02, "stop": px * 0.72, "t1": px * 1.8, "t2": px * 3,
                    "why": f"pump.fun graduate, {int(c['age_h'] // 24)}d old: buyers {bs:.0%} of 24h flow, up {ch6:.0f}% in 6h, ${liq/1000:.0f}k liquidity"})
        if len(out) >= 3: break
    for i in out:
        i["risk"] = risk_score(i); i["thesis"] = idea_thesis(i)
    return out


def brief(reads, reg, fg, cbp, dex, ins, radar_lines=None) -> dict:
    """Scannable headline card: headline, 3-4 icon bullets, chips, what I'm doing."""
    bulls = [r["name"] for r in reads if r["tone"] == "bull"]; bears = [r["name"] for r in reads if r["tone"] == "bear"]
    if reg["key"] == "risk_on": head = "Risk-on: buyers are in control"
    elif reg["key"] == "risk_off": head = "Risk-off: protect capital first"
    elif bears and not bulls: head = f"Chop with a weak {bears[0]}: sell rips, buy only the edges"
    elif bulls and not bears: head = f"Range, but {bulls[0]} is leaning up"
    else: head = "Range market: trade the edges, skip the middle"
    b = []
    if bulls or bears:
        b.append({"i": "📈" if bulls and not bears else "📉" if bears and not bulls else "⚖️",
                  "t": (f"{', '.join(bulls)} firm" if bulls else "") + (" · " if bulls and bears else "") + (f"{', '.join(bears)} weak" if bears else "")})
    else:
        b.append({"i": "⚖️", "t": "BTC, ETH, SOL all stuck in their ranges"})
    if cbp is not None:
        b.append({"i": "🇺🇸", "t": "US spot bid is present (Coinbase premium)" if cbp > 0.05 else "US spot demand is soft (Coinbase discount)" if cbp < -0.05 else "US spot flows are neutral"})
    hot = [d for d in (dex or []) if d.get("buy_share", 0) >= 0.55]
    if hot:
        b.append({"i": "🔥", "t": "On-chain buyers lead " + ", ".join(d["pair"] for d in hot[:3])})
    elif radar_lines:
        b.append({"i": "🎰", "t": radar_lines[0]})
    if ins and ins.get("beliefs"):
        b.append({"i": "🌍", "t": ins["beliefs"][0][:120]})
    doing = {"risk_on": "Full size on clean breakouts, letting winners run.", "risk_off": "Small size, quick profits, no chasing."}.get(
        reg["key"], "Normal size at range edges, taking profits into strength.")
    chips = [{"k": "Regime", "v": reg["label"], "tone": "bull" if reg["key"] == "risk_on" else "bear" if reg["key"] == "risk_off" else "neutral"}]
    if fg:
        chips.append({"k": "Fear & Greed", "v": f"{fg['v']} · {fg['cls']}", "tone": "bull" if fg["v"] >= 55 else "bear" if fg["v"] <= 40 else "neutral"})
    if cbp is not None:
        chips.append({"k": "CB premium", "v": f"{cbp:+.2f}%", "tone": "bull" if cbp > 0 else "bear"})
    return {"headline": head, "bullets": b[:4], "chips": chips, "doing": doing}


DESK_SYS = ("You are Mirko, a senior crypto macro/derivatives trader writing the morning desk note for a professional trading desk. "
            "Use ONLY the data given. Trader language, dense, concrete numbers, no filler, no platitudes (never 'the chart decides', "
            "'trade the edges' without levels, 'stay safe'). Every claim must cite a number from the data. Numbers as digits.")
DESK_SCHEMA = ('{"bias": "Bullish|Bearish|Neutral, with lean e.g. \'Neutral, leaning short\'", "confidence": 0-100, '
               '"headline": "<=12 words, sharp", "takeaway": "one sharp sentence", '
               '"changed": ["2-4 bullets: what changed since the previous read, with numbers"], '
               '"levels": [{"asset": "BTC", "support": "$x / $y", "resistance": "$x / $y", "pivot": "$x"}], '
               '"flows": ["3-5 bullets on positioning/flows: funding, OI, taker/CVD, top-trader & retail L/S, ETF flows, Coinbase premium, stablecoin supply, liquidations"], '
               '"plan": [{"if": "trigger with level", "then": "action with entry/target/stop"}], '
               '"risks": ["2-3 concrete risks/catalysts"], '
               '"coins": [{"asset": "BTC", "trend": "Up|Down|Range (timeframe)", "bias": "Bullish|Bearish|Neutral", "support": "$x", "resistance": "$x", '
               '"flip": "what would flip the view (level + condition)", "call": "one-line call"}] (BTC, ETH, SOL), '
               '"smart_money": {"accumulating": [{"name": "asset or group", "why": "evidence with number"}], "distributing": [{"name": "...", "why": "..."}]}, '
               '"degen_picks": [{"name": "TICKER from the candidate list", "chain": "...", "thesis": "visionary 1-2 sentence bull case", "risk": "what kills it"}] (0-3, only from candidates; bold but reasoned)}')


def smart_money_rules(reads, dex, lows=None) -> dict:
    """Group real flow evidence per asset; score > 0 = accumulating, < 0 = distributing."""
    ev = {}
    def add(name, score, txt):
        e = ev.setdefault(name, {"score": 0.0, "ev": []}); e["score"] += score; e["ev"].append(txt)
    m = lambda x: f"${x/1e6:.0f}m" if abs(x) >= 1e6 else f"${x/1e3:.0f}k"
    for r in reads:
        hl = r.get("hl") or {}
        if hl.get("longs") is not None:
            net = (hl.get("long_ntl") or 0) - (hl.get("short_ntl") or 0)
            if abs(net) >= 5e6:
                add(r["name"], 1 if net > 0 else -1, f"HL top traders net {'long' if net > 0 else 'short'} {m(abs(net))} ({hl['longs']}L/{hl['shorts']}S)")
        tl, rl = r.get("top_long"), r.get("retail_long")
        if tl and rl and abs(tl - rl) >= 0.04:
            add(r["name"], 1 if tl > rl else -1, f"Binance top traders {tl:.0%} long vs retail {rl:.0%}")
    try:
        import eyes
        e = eyes.cached(12 * 3600) or {}
        for k in ("etf_btc", "etf_eth", "etf_sol"):
            x = e.get(k)
            if x and abs(x.get("sum5") or 0) >= 10:
                add(x["asset"], 1 if x["sum5"] > 0 else -1, f"spot ETFs {'+' if x['sum5'] > 0 else '-'}${abs(x['sum5']):.0f}m net over 5 days")
    except Exception:
        pass
    for d in (dex or [])[:10]:
        bs, v = d.get("buy_share", 0.5), d.get("vol24") or 0
        if v >= 2e5 and (bs >= 0.58 or bs <= 0.42):
            add(d["pair"], 1 if bs >= 0.58 else -1, f"on-chain {bs:.0%} buys on {m(v)} 24h volume ({d.get('chain')})")
    acc = sorted(((k, v) for k, v in ev.items() if v["score"] > 0), key=lambda kv: -kv[1]["score"])
    dis = sorted(((k, v) for k, v in ev.items() if v["score"] < 0), key=lambda kv: kv[1]["score"])
    mixed = [(k, v) for k, v in ev.items() if v["score"] == 0]
    f = lambda xs: [{"name": k, "why": "; ".join(v["ev"][:3])} for k, v in xs[:5]]
    return {"accumulating": f(acc), "distributing": f(dis), "mixed": f(mixed)}


def _desk_facts(reads, reg, fg, cbp, glob, dex, prev) -> str:
    L = [f"Regime model: {reg.get('label')} (score {reg.get('score')})"]
    if fg: L.append(f"Fear&Greed {fg['v']} ({fg['cls']}), yesterday {fg.get('prev')}, week ago {fg.get('wk')}")
    if glob: L.append(f"Total mcap ${glob['mcap']/1e12:.2f}T ({(glob.get('mcap_chg') or 0):+.1f}% 24h), BTC dominance {glob['btc_dom']:.1f}%")
    if cbp is not None: L.append(f"Coinbase premium {cbp:+.3f}%")
    for r in reads:
        hl = r.get("hl") or {}
        L.append(f"{r['name']}: ${r['price']:,.4g} 24h {r['chg24']:+.1f}% 7d {r['chg7']:+.1f}%; support {[round(x, 2) for x in r['support'][:2]]} resistance {[round(x, 2) for x in r['resistance'][:2]]}; "
                 f"20D {r['ma20'] and round(r['ma20'], 2)} 50D {r['ma50'] and round(r['ma50'], 2)}; RSI d {r['rsi_d'] and round(r['rsi_d'])} 4h {r['rsi_4h'] and round(r['rsi_4h'])}; "
                 f"funding {r['funding'] if r['funding'] is None else round(r['funding'], 4)}%/8h; OI ${(r['oi_total'] or 0)/1e9:.2f}bn {r['oi_chg24'] if r['oi_chg24'] is None else round(r['oi_chg24'], 1)}% 24h; "
                 f"taker buy/sell {r['taker'] and round(r['taker'], 3)}; Binance top-trader long share {r['top_long']}; retail long share {r['retail_long']}; "
                 f"Hyperliquid top traders {hl.get('longs', '?')} long / {hl.get('shorts', '?')} short")
    try:
        import eyes
        L += eyes.summary_lines()
        L.append("Headlines: " + " | ".join(eyes.headlines(9)))
    except Exception:
        pass
    hot = [f"{d['pair']} buy share {d.get('buy_share', 0):.0%}" for d in (dex or [])[:4]]
    if hot: L.append("DEX flows: " + ", ".join(hot))
    try:
        c = cached() or {}
        cands = [f"{i['name']} ({i.get('chain', '')}, mcap {i.get('mcap')}, {i.get('why', '')[:90]})" for i in (c.get("micro_caps") or []) + (c.get("low_caps") or [])][:8]
        tr = c.get("trenches") or {}
        cands += [f"{x.get('sym') or x.get('name')} ({x.get('chain', '')}: {str(x.get('why') or x.get('note') or '')[:80]})" for x in (tr.get("could_pump") or tr.get("pump") or [])[:6] if isinstance(x, dict)]
        if cands: L.append("DEGEN CANDIDATES (micro/low caps passing sanity filters): " + " | ".join(cands))
    except Exception:
        pass
    if prev:
        L.append("PREVIOUS READ: " + json.dumps({k: prev.get(k) for k in ("bias", "confidence", "headline", "levels", "ts_h")})[:900])
    return "\n".join(L)


def desk_note(reads, reg, fg, cbp, glob, dex) -> dict | None:
    prev = (cached() or {}).get("desk")
    if prev and prev.get("coins") and time.time() - prev.get("ts", 0) < 3600 * 1.5:
        return prev
    try:
        import llm
        facts = _desk_facts(reads, reg, fg, cbp, glob, dex, prev)
        d, via = llm.reason_json(DESK_SYS, "DATA:\n" + facts + "\n\nWrite the desk note as JSON with this schema: " + DESK_SCHEMA, 2500)
    except Exception as e:
        log(f"desk note: {type(e).__name__}")
        return prev
    if not d or not d.get("headline"):
        log(f"desk note: llm {via}")
        return prev
    d["ts"], d["via"] = time.time(), via
    d["ts_h"] = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%MZ")
    try:
        d["confidence"] = max(0, min(100, int(d.get("confidence") or 50)))
    except (TypeError, ValueError):
        d["confidence"] = 50
    for k in ("changed", "flows", "risks", "plan", "levels", "coins", "degen_picks"):
        d[k] = [x for x in (d.get(k) or []) if x][:6]
    sm = d.get("smart_money") if isinstance(d.get("smart_money"), dict) else {}
    d["smart_money"] = {k: [x for x in (sm.get(k) or []) if isinstance(x, dict) and x.get("name")][:6] for k in ("accumulating", "distributing")}
    return d


def build():
    LOG.clear()
    assets, derivs = {}, {}
    with ThreadPoolExecutor(3) as ex:
        res = list(ex.map(lambda s: (s[0], safe(f"{s[0]} spot", asset_data, *s)), ASSETS))
    for n, a in res:
        if a: assets[n] = a
    for n, sym in ASSETS:
        if n in assets:
            derivs[n] = safe(f"{n} derivs", derivatives, n, sym, assets[n]["price"])
    glob, fg, cbp = safe("global", global_data), safe("fng", fng), safe("cb premium", coinbase_premium)
    trend = safe("trending", trending) or []
    hl = safe("hyperliquid", hl_smart_money) if HL_ENABLED else None
    dex = safe("dex flows", dex_flows) or []
    mids = safe("mid caps", midcap_ideas) or []
    lows = safe("low caps", lowcap_ideas) or []
    micros = safe("micro caps", microcap_ideas) or []
    reads = [asset_read(assets[n], derivs.get(n), ((hl or {}).get("coins") or {}).get(n)) for n, _ in ASSETS if n in assets]
    reg = regime(assets, glob, fg) if assets else {"label": "Unknown", "key": "chop", "score": 0, "notes": []}
    summary, stance = outlook(reads, reg, fg, cbp, dex)
    stocks_conv = safe("stock conviction", stock_convictions) or []
    for lst in (mids, lows, micros):
        for i in lst:
            i["risk"] = risk_score(i)
            i["thesis"] = idea_thesis(i)
    tr = _with_radar(safe("trenches", trenches, dex, lows, reads, fg, next_boom()) or trenches_fallback(reads, fg, next_boom(), trend))
    taken = {i["name"] for i in mids + lows} | {n for n, _ in ASSETS}
    micros = [i for i in micros if i["name"] not in taken]
    if len(micros) < 3:
        micros += safe("degen micros", degen_micros, taken | {i["name"] for i in micros}, 3 - len(micros)) or []
    for i in micros:
        i["risk"] = risk_score(i); i["thesis"] = idea_thesis(i)
    tr["frontrun"] = safe("frontrun", frontrun) or []
    ins = _insights()
    if ins and ins.get("beliefs"):
        summary += " Daily reading: " + ins["beliefs"][0]
    return {"ts": time.time(), "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(), "regime": reg,
            "assets": reads, "fng": fg, "global": glob, "coinbase_premium": cbp, "trending": trend,
            "hyperliquid": hl, "dex_flows": dex, "trade_ideas": [{**i, "risk": risk_score(i), "thesis": idea_thesis(i)} for i in (trade_ideas(assets, hl) if assets else [])],
            "brief": brief(reads, reg, fg, cbp, dex, ins, (tr.get("radar") or {}).get("lines")),
            "desk": safe("desk note", desk_note, reads, reg, fg, cbp, glob, dex),
            "smart_money": safe("smart money", smart_money_rules, reads, dex) or {"accumulating": [], "distributing": []},
            "summary": summary, "stance": stance, "next_boom": boom_with_history(next_boom() or BOOM_DEFAULT), "mid_caps": mids, "low_caps": lows, "micro_caps": micros, "stocks_conv": stocks_conv,
            "trenches": tr,
            "insights": ins, "schema": SCHEMA,
            "size_scale": regime_scale_for(reg["key"]), "warnings": LOG[-12:]}


# ---------------- mid caps / low caps / trenches ----------------
STABLES = {"USDT", "USDC", "DAI", "FDUSD", "TUSD", "USDE", "USDS", "PYUSD", "USD1", "BUSD", "WBTC", "WETH", "STETH", "WSTETH", "WEETH",
           "CBBTC", "BTCB", "LEO", "XAUT", "PAXG", "BSC-USD", "SUSDE", "USDD", "FRAX", "RLUSD"}


BINANCE_SYMS: set = set()


def _load_binance_syms():
    global BINANCE_SYMS
    if BINANCE_SYMS:
        return
    try:
        BINANCE_SYMS = {x["symbol"] for x in spot("/api/v3/ticker/price", {})}
    except Exception:
        pass


def midcap_ideas(n=3):
    """$100M-$3B coins showing quiet accumulation: up modestly over 7d, not chasing on the day, healthy
    volume/mcap, uptrend on the daily and close to the 20D (not extended). Levels from Binance candles."""
    _load_binance_syms()
    rows = get("https://api.coingecko.com/api/v3/coins/markets", {"vs_currency": "usd", "order": "market_cap_desc", "per_page": 250,
               "page": 1, "price_change_percentage": "7d"})
    cands = []
    for r in rows:
        sym, mc, vol = (r.get("symbol") or "").upper(), r.get("market_cap") or 0, r.get("total_volume") or 0
        c7, c24 = r.get("price_change_percentage_7d_in_currency"), r.get("price_change_percentage_24h")
        if sym in STABLES or sym in ("BTC", "ETH", "SOL") or not (1e8 <= mc <= 3e9) or c7 is None or c24 is None:
            continue
        if 0 <= c7 <= 25 and -4 <= c24 <= 8 and 0.04 <= vol / mc <= 1.0:
            cands.append((vol / mc + c7 / 50, sym, r))
    out = []
    for _, sym, r in sorted(cands, reverse=True)[:10]:
        if len(out) >= n:
            break
        if BINANCE_SYMS and sym + "USDT" not in BINANCE_SYMS:
            continue   # not on Binance spot: no candles for levels, skip quietly
        a = safe(f"{sym} mid", asset_data, sym, sym + "USDT")
        if not a or trend_score(a) < 1 or not a["ma20"] or a["price"] > a["ma20"] * 1.10:
            continue
        i = make_idea(a, "Long", f"quiet accumulation: {r['price_change_percentage_7d_in_currency']:+.0f}% 7d, "
                      f"vol/mcap {r['total_volume'] / r['market_cap']:.0%}, holding above the 20D")
        if i:
            i.update(tier="mid", mcap=r["market_cap"], name=sym)
            out.append(i)
    return out


def _watch_path():
    return Path(os.environ.get("DATA_DIR", ".")) / "lowcap_watch.json"


def lowcap_ideas(n=3):
    """Low caps ($1M-$100M) from trending DEX pools, tracked across refreshes in lowcap_watch.json.
    Qualified = buyers > sellers on 2+ refreshes >= 4h apart with higher lows; else labelled speculative.
    Sanity: mcap/FDV consistent, liquidity <= 0.5x mcap, volume/liquidity < 50, price < $20 (no wrapped/stocks)."""
    try:
        watch = json.loads(_watch_path().read_text())
    except Exception:
        watch = {}
    now = time.time()
    for net, p in _POOLS:
        a = p.get("attributes") or {}
        tid = ((p.get("relationships") or {}).get("base_token") or {}).get("data", {}).get("id", "")
        addr = tid.split("_", 1)[1] if "_" in tid else None
        tx = (a.get("transactions") or {}).get("h24") or {}
        b, s_ = tx.get("buyers") or 0, tx.get("sellers") or 0
        try:
            px, liq = float(a.get("base_token_price_usd") or 0), float(a.get("reserve_in_usd") or 0)
            mc = float(a.get("market_cap_usd") or a.get("fdv_usd") or 0); fdv = float(a.get("fdv_usd") or 0)
            vol = float((a.get("volume_usd") or {}).get("h24") or 0)
        except Exception:
            continue
        if not addr or not (b + s_) or not px:
            continue
        k = f"{net}:{addr}"
        w = watch.setdefault(k, {"sym": (a.get("name") or "").split(" / ")[0][:14], "chain": net, "pool": a.get("address"), "obs": []})
        w["obs"].append({"ts": now, "bs": b / (b + s_), "px": px, "mc": mc, "fdv": fdv, "liq": liq, "vol": vol, "buyers": b})
        w["created"] = a.get("pool_created_at") or w.get("created")
        w["obs"] = w["obs"][-12:]
    for k in [k for k, w in watch.items() if now - w["obs"][-1]["ts"] > 4 * 86400]:
        del watch[k]
    try:
        _watch_path().write_text(json.dumps(watch))
    except Exception:
        pass
    scored = []
    for k, w in watch.items():
        o = w["obs"][-1]
        if now - o["ts"] > 3 * 3600 or not (1e6 < o["mc"] <= 1e8) or o["px"] > 20 or not o["liq"]:
            continue
        if o["liq"] > 0.5 * o["mc"] or o["vol"] / o["liq"] > 50 or (o["fdv"] and o["mc"] > o["fdv"] * 1.05) or o["bs"] < 0.52:
            continue
        acc = [x for x in w["obs"] if x["bs"] > 0.52]
        span = (acc[-1]["ts"] - acc[0]["ts"]) if len(acc) > 1 else 0
        lows = [x["px"] for x in w["obs"][-4:]]
        base = len(lows) >= 2 and min(lows[-2:]) >= 0.9 * min(lows)
        qual = len(acc) >= 2 and span >= 4 * 3600 and base
        scored.append((qual, len(acc) + o["bs"] * 2, k, w, o, base))
    scored.sort(key=lambda x: (-x[0], -x[1]))
    out = []
    for qual, _, k, w, o, base in scored:
        if len(out) >= n:
            break
        net, addr = k.split(":", 1)
        try:
            time.sleep(2.1)
            ohl = sorted(get(f"https://api.geckoterminal.com/api/v2/networks/{net}/pools/{w['pool']}/ohlcv/hour",
                             {"limit": 72, "aggregate": 4, "currency": "usd"})["data"]["attributes"]["ohlcv_list"])
            ds = max(get(f"https://api.dexscreener.com/tokens/v1/{net}/{addr}"), key=lambda q: (q.get("liquidity") or {}).get("usd") or 0)
        except Exception as e:
            log(f"lowcap {w['sym']}: {str(e)[:60]}")
            continue
        pxl = float(ds.get("priceUsd") or 0)
        if len(ohl) < 6 or not pxl or abs(ohl[-1][4] / pxl - 1) > 0.10 or not ds.get("marketCap"):
            continue
        p = pxl
        lo5 = min(r[3] for r in ohl[-6:])
        lo, hi_e = max(lo5, p * 0.90), p
        entry = (lo + hi_e) / 2
        stop = max(lo5 * 0.95, entry * 0.80)
        if stop >= lo * 0.97:
            stop = lo * 0.88
        risk = entry - stop
        hi = max(r[2] for r in ohl)
        t1 = max(hi, entry + 2 * risk) if hi > entry * 1.15 else entry + 2 * risk
        out.append({"name": w["sym"], "chain": net, "address": addr, "side": "Long", "venue": "Spot", "tier": "low",
                    "entry_lo": lo, "entry_hi": hi_e, "stop": stop, "t1": t1, "t2": entry + 3 * risk if t1 < entry + 3 * risk else None,
                    "rr": round((t1 - entry) / risk, 2) if risk > 0 else None, "mcap": ds.get("marketCap"), "spec": not qual,
                    "why": (f"buyers > sellers on {len([x for x in w['obs'] if x['bs'] > .52])} scans" if qual else "net buying on the latest scan")
                           + (", higher lows" if base else ", no clear base yet")})
    return out


def microcap_ideas(n=2):
    """Micro caps (<= ~$600k mcap), ONLY with real conviction: a launch wrecked by snipers in its first day
    that is now base-building with sustained accumulation. All must hold:
    pool age >= 2 days; first-day high -> low drawdown >= 50%; price now >= 1.3x that low (a base, not a knife);
    buyers > sellers on >= 3 scans spanning >= 8h; unique buyers and liquidity rising across scans;
    liquidity >= $25k and <= 0.6x mcap; volume/liquidity < 30. If none qualify: empty list (site says so)."""
    try:
        watch = json.loads(_watch_path().read_text())
    except Exception:
        return []
    now, out = time.time(), []
    for k, w in watch.items():
        obs = w["obs"]; o = obs[-1]
        if now - o["ts"] > 3 * 3600 or not (5e4 <= o["mc"] <= 6e5) or not o["liq"] or o["liq"] < 25_000 or o["liq"] > 0.6 * o["mc"] or o["vol"] / o["liq"] > 30:
            continue
        try:
            age_d = (now - dt.datetime.fromisoformat(str(w.get("created")).replace("Z", "+00:00")).timestamp()) / 86400
        except Exception:
            continue
        acc = [x for x in obs if x["bs"] > 0.52]
        if age_d < 2 or len(acc) < 3 or acc[-1]["ts"] - acc[0]["ts"] < 8 * 3600:
            continue
        if not (obs[-1]["liq"] > obs[0]["liq"] * 1.05 and (obs[-1].get("buyers") or 0) >= (obs[0].get("buyers") or 0)):
            continue
        net, addr = k.split(":", 1)
        try:
            time.sleep(2.1)
            ohl = sorted(get(f"https://api.geckoterminal.com/api/v2/networks/{net}/pools/{w['pool']}/ohlcv/hour",
                             {"limit": 200, "aggregate": 4, "currency": "usd"})["data"]["attributes"]["ohlcv_list"])
        except Exception as e:
            log(f"micro {w['sym']}: {str(e)[:60]}")
            continue
        if len(ohl) < 12:
            continue
        first = ohl[:6]; hi1 = max(r[2] for r in first); low_after = min(r[3] for r in ohl[3:])
        px = ohl[-1][4]
        if hi1 <= 0 or low_after / hi1 > 0.5 or px < low_after * 1.3:
            continue
        lows = [r[3] for r in ohl[-12:]]
        if min(lows[-6:]) < min(lows[:6]) * 0.95:
            continue   # still making lower lows: no base
        stop = min(lows[-6:]) * 0.9; risk = px - stop
        if risk <= 0:
            continue
        t1 = min(hi1, px + 3 * risk)
        out.append({"name": w["sym"], "chain": net, "address": addr, "side": "Long", "venue": "Spot", "tier": "micro",
                    "entry_lo": min(lows[-6:]), "entry_hi": px, "stop": stop, "t1": t1, "rr": round((t1 - px) / risk, 2),
                    "mcap": o["mc"], "spec": True,
                    "why": f"{age_d:.0f}d old, first day sniped -{(1 - low_after / hi1):.0%}, now a base +{(px / low_after - 1):.0%} off the low; "
                           f"buyers > sellers on {len(acc)} scans, liquidity growing"})
        if len(out) >= n:
            break
    return out


FR_Q = ["launch date announced", "set to launch next week", "release date confirmed tech", "keynote event date", "election date vote next week",
        "final match date", "token unlock next week", "binance will list", "film premiere date", "SpaceX launch date", "AI model release next week", "Fed meeting next week"]


def _fr_path():
    return cache_path().with_name("frontrun.json")


def frontrun(max_age_h: float = 6) -> list:
    """Upcoming catalysts (any domain) -> narrative tokens that could front-run them.
    Events: Google News RSS + Mirko's eyes -> one LLM pass picks dated upcoming events with search keywords.
    Tokens: DexScreener search per keyword; the OG (first-launched) token of a narrative ranks first, then volume and holders."""
    try:
        c = json.loads(_fr_path().read_text())
        if time.time() - c["ts"] < max_age_h * 3600:
            return c["items"]
    except Exception:
        pass
    import xml.etree.ElementTree as ET, llm, ta
    heads = []
    for q in FR_Q:
        try:
            root = ET.fromstring(requests.get("https://news.google.com/rss/search", params={"q": q + " when:7d", "hl": "en-US", "gl": "US", "ceid": "US:en"}, timeout=10).content)
            heads += [i.findtext("title", "")[:150] for i in list(root.iter("item"))[:6]]
        except Exception:
            continue
    if not heads:
        return []
    today = dt.date.today().isoformat()
    d, _ = llm.reason_json("You are Mirko, a degen narrative trader who front-runs events with memecoins/narrative tokens.",
                           f"Today is {today}. From these headlines pick the 5 best UPCOMING events (next 3-45 days, any domain: tech launches, politics, sports, culture, crypto unlocks/listings, macro) "
                           "that crypto traders could front-run with narrative tokens. For each give 1-2 short DexScreener search keywords for tokens likely named after it (e.g. 'GTA6', 'grok', 'starship').\n"
                           + "\n".join(heads[:70]) + '\nJSON: {"events": [{"event": "...", "date": "YYYY-MM-DD or month", "domain": "tech|politics|sports|culture|crypto|macro", "why": "1 sentence why tokens could run", "keywords": ["..."]}]}', 6000)
    if not d:
        log(f"frontrun: llm gave nothing ({_}), {len(heads)} headlines")
    items = []
    for ev in ((d or {}).get("events") or [])[:5]:
        toks, seen = [], set()
        for kw in (ev.get("keywords") or [])[:2]:
            try:
                ps = requests.get("https://api.dexscreener.com/latest/dex/search", params={"q": kw}, timeout=10).json().get("pairs") or []
            except Exception:
                continue
            for p in ps:
                bt = p.get("baseToken") or {}
                a = bt.get("address")
                if not a or a in seen or p.get("chainId") not in ("solana", "base", "ethereum", "bsc"):
                    continue
                if kw.lower().replace(" ", "") not in (bt.get("symbol", "") + bt.get("name", "")).lower().replace(" ", ""):
                    continue
                vol = (p.get("volume") or {}).get("h24") or 0; mc = p.get("marketCap") or p.get("fdv") or 0
                if mc < 2500 or ((p.get("liquidity") or {}).get("usd") or 0) < 1500:
                    continue
                seen.add(a)
                toks.append({"sym": bt.get("symbol"), "name": bt.get("name"), "chain": p["chainId"], "ca": a, "mc": mc, "vol24": vol,
                             "created": (p.get("pairCreatedAt") or 0) / 1000, "url": p.get("url")})
        toks.sort(key=lambda t: t["created"] or 9e12)
        if toks:
            toks[0]["og"] = True
        rest = sorted(toks[1:], key=lambda t: -t["vol24"])
        pick = toks[:1] + rest[:2]
        for t in pick:
            t["age_d"] = round((time.time() - t["created"]) / 86400, 1) if t["created"] else None
            try:
                time.sleep(2.1)
                h = ta.gt_holders(t["chain"], t["ca"]) or {}
                t["holders"] = h.get("count")
            except Exception:
                t["holders"] = None
        items.append({"event": ev.get("event"), "date": ev.get("date"), "domain": ev.get("domain"), "why": ev.get("why"), "tokens": pick})
    try:
        if items:
            _fr_path().write_text(json.dumps({"ts": time.time(), "items": items}))
    except Exception:
        pass
    return items


LAUNCH_DEXES = [("solana", "pumpswap"), ("solana", "stonkfun"), ("solana", "letsbonk-fun"), ("solana", "raydium-launchlab"), ("solana", "pump-fun")]


def degen_micros(exclude: set, n=3) -> list:
    """Degen micro caps ($30k-$1.5M) that SURVIVED: age >= 24h, sustained volume, buyers ahead on 6h and 24h,
    hard safety screens (RugCheck/GoPlus: no mint/freeze authority, no honeypot/tax, LP not pulled),
    holder spread (>= 250 holders, top10 <= 35%), and on-chain whale buys >= whale sells over 24h.
    Sources: pump.fun/PumpSwap, stonk.fun, letsbonk, Raydium LaunchLab pools via GeckoTerminal + DexScreener boosts."""
    import safety, ta
    cands, seen = [], set(x.upper() for x in exclude)
    for net, dex, pg in [(n_, d_, p_) for n_, d_ in LAUNCH_DEXES for p_ in ((1, 2, 3, 4) if d_ in ("pumpswap", "stonkfun") else (1, 2))]:
        try:
            d = get(f"https://api.geckoterminal.com/api/v2/networks/{net}/dexes/{dex}/pools", {"sort": "h24_volume_usd_desc", "page": pg}).get("data") or []
        except Exception as e:
            log(f"degen {dex}: {str(e)[:60]}"); continue
        time.sleep(2.1)
        for q in d[:20]:
            a = q.get("attributes") or {}
            mint = (((q.get("relationships") or {}).get("base_token") or {}).get("data") or {}).get("id", "").split("_", 1)[-1]
            try:
                age_h = (time.time() - dt.datetime.fromisoformat(a["pool_created_at"].replace("Z", "+00:00")).timestamp()) / 3600
                mc = float(a.get("market_cap_usd") or a.get("fdv_usd") or 0)
                vol = float((a.get("volume_usd") or {}).get("h24") or 0)
            except Exception:
                continue
            sym = (a.get("name") or "").split(" / ")[0][:14]
            if mint and age_h >= 24 and 3e4 <= mc <= 1.5e6 and vol >= 3e4 and sym.upper() not in seen and not mint.startswith("So1111"):
                cands.append({"sym": sym, "mint": mint, "net": net, "src": dex, "age_h": age_h, "mc": mc, "vol": vol, "pool": a.get("address")})
                seen.add(sym.upper())
    cands.sort(key=lambda c: -c["vol"] / max(c["mc"], 1))
    out = []
    for c in cands[:14]:
        try:
            ps = requests.get(f"https://api.dexscreener.com/latest/dex/tokens/{c['mint']}", timeout=8).json().get("pairs") or []
        except Exception:
            continue
        if not ps:
            continue
        p = max(ps, key=lambda x: (x.get("liquidity") or {}).get("usd") or 0)
        liq = (p.get("liquidity") or {}).get("usd") or 0
        tx = p.get("txns") or {}; h6, h24 = tx.get("h6") or {}, tx.get("h24") or {}
        if liq < 1.5e4 or h6.get("buys", 0) <= h6.get("sells", 0) or h24.get("buys", 0) <= h24.get("sells", 0):
            continue
        sf = safety.check("solana", c["mint"])
        if not sf["ok"]:
            log(f"degen {c['sym']} rejected: {', '.join(sf['flags'])[:80]}"); continue
        time.sleep(2.1)
        ho = ta.gt_holders("solana", c["mint"]) or {}
        if not ho.get("count") or ho["count"] < 250 or (ho.get("top10_pct") or 100) > 35:
            continue
        time.sleep(2.1)
        wh = ta.whale_trades("solana", p["pairAddress"], 500) or {}
        if not wh or wh.get("buys_usd", 0) < wh.get("sells_usd", 0) or wh.get("n_buys", 0) < 2:
            continue
        px = float(p.get("priceUsd") or 0)
        if not px:
            continue
        mc = p.get("marketCap") or p.get("fdv") or c["mc"]
        src = {"pumpswap": "pump.fun grad", "pump-fun": "pump.fun", "stonkfun": "stonk.fun", "letsbonk-fun": "letsbonk", "raydium-launchlab": "LaunchLab"}.get(c["src"], c["src"])
        out.append({"name": p.get("baseToken", {}).get("symbol") or c["sym"], "chain": "solana", "address": c["mint"], "tier": "micro", "side": "Long", "venue": "Spot", "spec": True,
                    "mcap": mc, "entry_lo": px * 0.92, "entry_hi": px * 1.02, "stop": px * 0.7, "t1": px * 2, "t2": px * 4, "rr": round((2 - 1) / 0.3, 2),
                    "holders": ho["count"], "top10": ho.get("top10_pct"), "age_d": round(c["age_h"] / 24, 1), "lp_locked": sf.get("lp_locked"),
                    "why": f"{src}, survived {c['age_h'] / 24:.0f}d · {ho['count']:,} holders, top10 {ho.get('top10_pct', 0):.0f}% · whales bought ${wh['buys_usd']:,} vs sold ${wh['sells_usd']:,} (24h) · "
                           f"{h24['buys']}/{h24['sells']} buys/sells · ${liq / 1000:.0f}k liq · RugCheck/GoPlus clean. Degen size only."})
        if len(out) >= n:
            break
    return out


def _with_radar(t: dict) -> dict:
    try:
        import trench_sources
        r = trench_sources.scan()
    except Exception:
        return t
    t = dict(t or {})
    t["radar"] = {"lines": r["lines"], "pads": [{"pad": p["pad"], "n": p.get("n", 0), "vol": p.get("vol", 0), "hot": (p.get("hot") or {}).get("sym")} for p in r["pads"]],
                  "pump": {k: [{"sym": x["sym"], "mc": round(x["mc"]), "mint": x.get("mint"), "age_h": x.get("age_h")} for x in v[:8]] for k, v in r["pump"].items()},
                  "tokensxyz": [x["sym"] for x in (r["tokensxyz"].get("trending") or [])][:8]}
    if r["lines"]:
        hot = next((l for l in r["lines"] if l.startswith("hottest launchpad")), None)
        if hot:
            t["take"] = (t.get("take") or "").rstrip() + " " + hot[0].upper() + hot[1:] + "."
    grads = [g for g in (r["pump"].get("graduated") or []) if g["mc"] < 5e6 and not str(g["sym"]).upper().startswith("USD")]
    if grads:
        g = max(grads, key=lambda x: x["mc"])
        t.setdefault("could_pump", [])
        if isinstance(t["could_pump"], list) and len(t["could_pump"]) < 6:
            t["could_pump"].append({"sym": g["sym"], "chain": "pump.fun", "why": f"graduated and holding ${g['mc']:,.0f} mcap"})
    return t


def trenches_fallback(reads, fg, boom, trend) -> dict:
    """Never empty: when DEX pools are unavailable, an honest read from majors + trending."""
    sol = next((r for r in reads if r["name"] == "SOL"), None)
    tone = sol["tone"] if sol else "neutral"
    mood = {"bull": "Trenches have a tailwind", "bear": "Trenches are bleeding", "neutral": "Trenches are choppy"}[tone]
    take = (f"{mood}. I couldn't pull fresh on-chain pool data this round, so this is a read from SOL's structure"
            + (f" (SOL is {sol['bias'].split(' ·')[0].lower()})" if sol else "") + " and what's trending. "
            + ("Memecoin beta follows SOL; size up only into strength." if tone == "bull" else "Small size, fast exits, no bag-holding." ))
    hot = [t for t in (trend or []) if (t.get("chg24") or 0) > 20][:3]
    cold = [t for t in (trend or []) if (t.get("chg24") or 0) < -10][:3]
    lt = [r["name"] for r in reads if r["tone"] == "bull"] + [str(c).lstrip("$") for c in ((boom or {}).get("crypto") or [])][:3]
    return {"mood": mood, "take": take,
            "could_pump": [{"sym": t["symbol"], "chain": "trending", "why": f"+{t['chg24']:.0f}% day, attention is on it"} for t in hot],
            "could_dump": [{"sym": t["symbol"], "chain": "trending", "why": f"{t['chg24']:.0f}% day, momentum broke"} for t in cold],
            "accumulate": [], "long_term": lt or ["BTC", "SOL"],
            "rules": "Trenches rules: size for zero, take first profits early, never chase a green candle you didn't see build."}


def trenches(dex, lows, reads, fg, boom):
    """Mirko's opinion on the memecoin trenches, from live DEX data (rule-based, honest)."""
    pools = []
    for net, p in _POOLS:
        a = p.get("attributes") or {}
        tx = (a.get("transactions") or {}).get("h1") or {}
        try:
            pools.append({"sym": (a.get("name") or "").split(" / ")[0][:14], "chain": net,
                          "h1": float((a.get("price_change_percentage") or {}).get("h1") or 0),
                          "h24": float((a.get("price_change_percentage") or {}).get("h24") or 0),
                          "bs1": (tx.get("buyers") or 0) / max(1, (tx.get("buyers") or 0) + (tx.get("sellers") or 0)),
                          "vol": float((a.get("volume_usd") or {}).get("h24") or 0), "liq": float(a.get("reserve_in_usd") or 0)})
        except Exception:
            continue
    if not pools:
        return None
    hot = sum(1 for p in pools if p["h24"] > 50) / len(pools)
    sol = next((r for r in reads if r["name"] == "SOL"), None)
    mood = ("Trenches are on fire" if hot > .4 else "Trenches are selective" if hot > .15 else "Trenches are cold")
    take = (f"{mood}: {hot:.0%} of trending pools are up 50%+ on the day. "
            + (f"SOL itself is {sol['bias'].split(' ·')[0].lower()}, " if sol else "")
            + ("so memecoin beta has a tailwind." if sol and sol["tone"] == "bull" else
               "so rotations are fast and exits matter more than entries." if not sol or sol["tone"] == "neutral" else
               "so most pumps get sold into. Smaller size, faster exits."))
    if fg and fg["v"] >= 75:
        take += " Greed is high; the late money is here."
    pump = [p for p in pools if p["bs1"] >= .6 and 0 < p["h1"] < 40 and p["liq"] > 30_000 and p["vol"] / max(p["liq"], 1) < 40]
    dump = [p for p in pools if (p["h24"] > 150 and p["bs1"] < .45) or p["vol"] / max(p["liq"], 1) > 60]
    pump.sort(key=lambda p: -p["bs1"]); dump.sort(key=lambda p: -p["h24"])
    acc = [l for l in lows if not l.get("spec")] or lows
    lt = [r["name"] for r in reads if r["tone"] == "bull"] + [str(c).lstrip("$") for c in ((boom or {}).get("crypto") or [])][:3]
    return {"mood": mood, "take": take,
            "could_pump": [{"sym": p["sym"], "chain": p["chain"], "why": f"{p['bs1']:.0%} buyers last hour, +{p['h1']:.0f}% 1h, liquidity holds"} for p in pump[:4]],
            "could_dump": [{"sym": p["sym"], "chain": p["chain"], "why": (f"+{p['h24']:.0f}% day but sellers took over" if p["h24"] > 150 else "volume dwarfs liquidity — exit-liquidity risk")} for p in dump[:4]],
            "accumulate": [{"sym": l["name"], "chain": l["chain"], "why": l["why"]} for l in acc[:3]],
            "long_term": lt or ["BTC"],
            "rules": "Trenches rules: size for zero, take first profits early, never chase a green candle you didn't see build."}


CONV_STOCKS = ["NVDA", "TSLA", "COIN", "MSTR"]


def stock_convictions() -> list:
    """Per-stock conviction 0-100 from Yahoo daily candles: trend vs 20D/50D and 1M momentum."""
    out = []
    for sym in CONV_STOCKS:
        try:
            r = get(f"https://query1.finance.yahoo.com/v8/finance/chart/{sym}", {"range": "6mo", "interval": "1d"})["chart"]["result"][0]
            cl = [c for c in r["indicators"]["quote"][0]["close"] if c]
        except Exception:
            continue
        if len(cl) < 60:
            continue
        px, s20, s50 = cl[-1], sum(cl[-20:]) / 20, sum(cl[-50:]) / 50
        m1 = (px / cl[-21] - 1) * 100
        score = (1 if px > s20 else -1) + (1 if px > s50 else -1) + (1 if s20 > s50 else -1) + max(-2, min(2, m1 / 8))
        tone = "bull" if score >= 1.5 else "bear" if score <= -1.5 else "neutral"
        out.append({"name": sym, "price": round(px, 2), "chg1m": round(m1, 1), "score": round(score, 2), "tone": tone,
                    "conv": int(max(5, min(95, 50 + score * 9))),
                    "note": {"bull": "uptrend, buying dips", "bear": "downtrend, selling rips", "neutral": "range, waiting"}[tone]})
        time.sleep(0.3)
    return out


def risk_score(i: dict) -> int:
    base = {"mid": 4, "low": 7, "micro": 8}.get(i.get("tier"), 2)
    base += (1 if i.get("venue") == "Perp" else 0) + (1 if (i.get("rr") or 0) < 1.5 else 0) + (1 if i.get("spec") else 0)
    return int(max(0, min(10, base)))


def regime_scale_for(key: str) -> float:
    if not REGIME_FILTER: return 1.0
    return max(0.25, min(1.0, RISK_OFF_SCALE)) if key == "risk_off" else 1.0


_lock = threading.Lock()


def cached(max_age_h: float | None = None) -> dict | None:
    try:
        d = json.loads(cache_path().read_text())
    except Exception:
        return None
    if max_age_h is not None and time.time() - d.get("ts", 0) > max_age_h * 3600:
        return None
    return d


def refresh() -> dict | None:
    if not _lock.acquire(blocking=False):
        return None
    try:
        d = build()
        if not d["assets"]:
            log("no asset data; keeping previous read")
            return None
        p = cache_path(); p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".tmp"); tmp.write_text(json.dumps(d, default=str)); tmp.replace(p)
        print(f"[MARKET] read refreshed: {d['regime']['label']} · scale {d['size_scale']}")
        return d
    finally:
        _lock.release()


def regime_size_scale() -> tuple[float, str]:
    """Multiplier for NEW entry size from the latest read (stale > 6h = no effect). Only ever shrinks."""
    d = cached(max_age_h=6)
    if not d: return 1.0, ""
    s = regime_scale_for((d.get("regime") or {}).get("key", "chop"))
    return s, (f"regime {d['regime']['label']} → size x{s:.2f}" if s < 1 else "")


_started = False


def start():
    global _started
    if _started or os.environ.get("MARKET_THOUGHTS_ENABLED", "true").lower() in ("0", "false", "no"):
        return
    _started = True

    def loop():
        while True:
            try:
                c = cached()
                if not c or c.get("schema", 0) < SCHEMA or time.time() - c.get("ts", 0) > EVERY_H * 3600:
                    refresh()
            except Exception as e:
                print(f"[MARKET] refresh error: {str(e)[:160]}")
            time.sleep(300)
    threading.Thread(target=loop, daemon=True, name="market-thoughts").start()
    print(f"[MARKET] Mirko Market Thoughts on — refresh every {EVERY_H}h")
