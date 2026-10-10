"""Ledger Market Thoughts — a super-trader market read that runs inside Ledger.

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
    r = requests.get(url, params=params, headers=UA, timeout=timeout)
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


def next_boom(today: str | None = None):
    """Rotates one researched theme per day from data/next_boom.json (no generated content)."""
    try:
        themes = json.loads(BOOM_FILE.read_text()).get("themes", [])
    except Exception:
        return None
    if not themes: return None
    d = dt.date.fromisoformat(today) if today else dt.date.today()
    t = themes[d.toordinal() % len(themes)]
    keep = ("theme", "emoji", "thesis", "boom_window", "why_now", "crypto", "stocks", "invalidation", "researched")
    return {k: t.get(k) for k in keep if t.get(k) is not None}


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
    reads = [asset_read(assets[n], derivs.get(n), ((hl or {}).get("coins") or {}).get(n)) for n, _ in ASSETS if n in assets]
    reg = regime(assets, glob, fg) if assets else {"label": "Unknown", "key": "chop", "score": 0, "notes": []}
    summary, stance = outlook(reads, reg, fg, cbp, dex)
    return {"ts": time.time(), "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(), "regime": reg,
            "assets": reads, "fng": fg, "global": glob, "coinbase_premium": cbp, "trending": trend,
            "hyperliquid": hl, "dex_flows": dex, "trade_ideas": trade_ideas(assets, hl) if assets else [],
            "summary": summary, "stance": stance, "next_boom": next_boom(), "mid_caps": mids, "low_caps": lows,
            "trenches": safe("trenches", trenches, dex, lows, reads, fg, next_boom()),
            "size_scale": regime_scale_for(reg["key"]), "warnings": LOG[-12:]}


# ---------------- mid caps / low caps / trenches ----------------
STABLES = {"USDT", "USDC", "DAI", "FDUSD", "TUSD", "USDE", "USDS", "PYUSD", "USD1", "BUSD", "WBTC", "WETH", "STETH", "WSTETH", "WEETH",
           "CBBTC", "BTCB", "LEO", "XAUT", "PAXG", "BSC-USD", "SUSDE", "USDD", "FRAX", "RLUSD"}


def midcap_ideas(n=3):
    """$100M-$3B coins showing quiet accumulation: up modestly over 7d, not chasing on the day, healthy
    volume/mcap, uptrend on the daily and close to the 20D (not extended). Levels from Binance candles."""
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
        w["obs"].append({"ts": now, "bs": b / (b + s_), "px": px, "mc": mc, "fdv": fdv, "liq": liq, "vol": vol})
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
        if now - o["ts"] > 3 * 3600 or not (1e6 <= o["mc"] <= 1e8) or o["px"] > 20 or not o["liq"]:
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


def trenches(dex, lows, reads, fg, boom):
    """Ledger's opinion on the memecoin trenches, from live DEX data (rule-based, honest)."""
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
                if not c or time.time() - c.get("ts", 0) > EVERY_H * 3600:
                    refresh()
            except Exception as e:
                print(f"[MARKET] refresh error: {str(e)[:160]}")
            time.sleep(300)
    threading.Thread(target=loop, daemon=True, name="market-thoughts").start()
    print(f"[MARKET] Ledger Market Thoughts on — refresh every {EVERY_H}h")
