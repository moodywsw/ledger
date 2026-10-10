"""Mirko's eyes: free flow & sentiment feeds (ETF flows, stablecoin supply, liquidations, Google News) -> cached hourly.
Reddit + X live in social.py; Fear&Greed / funding / OI / taker flow / Coinbase premium live in market_thoughts.py."""
from __future__ import annotations

import json, os, re, threading, time
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import requests

DATA = Path(os.environ.get("DATA_DIR", "."))
UA = {"User-Agent": "Mozilla/5.0 (Mirko research bot)"}
_mem = {"ts": 0, "d": None}


def _num(s: str):
    s = s.replace(",", "").strip()
    neg = s.startswith("(")
    try:
        v = float(s.strip("()"))
    except ValueError:
        return None
    return -v if neg else v


def etf_flows(asset: str = "btc") -> dict | None:
    """Farside daily US spot ETF net flows ($m). Last 5 trading days + total."""
    h = requests.get(f"https://farside.co.uk/{asset}/", headers=UA, timeout=20).text
    rows = []
    for r in re.findall(r"<tr[^>]*>(.*?)</tr>", h, re.S):
        c = [re.sub(r"<[^>]+>", "", x).strip() for x in re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", r, re.S)]
        if c and re.match(r"\d{2} \w{3} \d{4}$", c[0]) and len(c) > 2:
            v = _num(c[-1])
            if v is not None:
                rows.append({"date": c[0], "net": v})
    if not rows:
        return None
    last5 = rows[-5:]
    return {"asset": asset.upper(), "last": last5[-1], "sum5": round(sum(x["net"] for x in last5), 1), "days": last5}


def stablecoins() -> dict | None:
    d = requests.get("https://stablecoins.llama.fi/stablecoincharts/all", timeout=20).json()
    def tot(x): return sum((x.get("totalCirculatingUSD") or {}).values())
    now, d7, d30 = tot(d[-1]), tot(d[-8]), tot(d[-31])
    return {"supply_bn": round(now / 1e9, 1), "chg7_bn": round((now - d7) / 1e9, 2), "chg30_bn": round((now - d30) / 1e9, 2)}


def liquidations() -> dict | None:
    """OKX public liquidation orders (recent filled) for BTC/ETH/SOL: long vs short liquidated notional."""
    out = {}
    for c in ("BTC", "ETH", "SOL"):
        j = requests.get("https://www.okx.com/api/v5/public/liquidation-orders", headers=UA, timeout=15,
                         params={"instType": "SWAP", "uly": f"{c}-USDT", "state": "filled"}).json()
        lo = sh = 0.0
        for blk in j.get("data", []):
            for x in blk.get("details", []):
                n = float(x.get("sz", 0)) * float(x.get("bkPx", 0)) * {"BTC": .01, "ETH": .1, "SOL": 1}[c]
                if x.get("posSide") == "long" or (x.get("posSide") == "net" and x.get("side") == "sell"):
                    lo += n
                else:
                    sh += n
        out[c] = {"longs_liq_usd": round(lo), "shorts_liq_usd": round(sh)}
    return out


def gnews(q: str, n: int = 8) -> list:
    root = ET.fromstring(requests.get("https://news.google.com/rss/search", headers=UA, timeout=15,
                                      params={"q": q, "hl": "en-US", "gl": "US", "ceid": "US:en"}).content)
    return [{"title": i.findtext("title", "")[:180], "src": (i.find("source").text if i.find("source") is not None else "")}
            for i in root.iter("item")][:n]


def build() -> dict:
    jobs = {"etf_btc": (etf_flows, "btc"), "etf_eth": (etf_flows, "eth"), "etf_sol": (etf_flows, "sol"),
            "stables": (stablecoins,), "liqs": (liquidations,),
            "news_crypto": (gnews, "bitcoin OR crypto when:1d"), "news_markets": (gnews, "stock market OR Fed when:1d"),
            "news_politics": (gnews, "election OR geopolitics OR tariffs when:1d")}
    out = {}
    with ThreadPoolExecutor(6) as ex:
        futs = {k: ex.submit(*v) for k, v in jobs.items()}
        for k, f in futs.items():
            try:
                out[k] = f.result()
            except Exception as e:
                print(f"[EYES] {k}: {type(e).__name__}")
                out[k] = None
    out["ts"] = time.time()
    return out


def cached(max_age_s: int = 3600) -> dict:
    if _mem["d"] and time.time() - _mem["ts"] < max_age_s:
        return _mem["d"]
    p = DATA / "eyes.json"
    try:
        d = json.loads(p.read_text())
        if time.time() - d.get("ts", 0) < max_age_s:
            _mem.update(ts=d["ts"], d=d); return d
    except Exception:
        pass
    d = build()
    try:
        p.write_text(json.dumps(d))
    except Exception:
        pass
    _mem.update(ts=d["ts"], d=d)
    return d


def summary_lines(d: dict | None = None) -> list:
    d = d or cached()
    L = []
    for k in ("etf_btc", "etf_eth", "etf_sol"):
        e = d.get(k)
        if e: L.append(f"{e['asset']} spot ETFs: last day {e['last']['net']:+.0f}m ({e['last']['date']}), 5-day {e['sum5']:+.0f}m")
    s = d.get("stables")
    if s: L.append(f"Stablecoin supply ${s['supply_bn']}bn, 7d {s['chg7_bn']:+.2f}bn, 30d {s['chg30_bn']:+.2f}bn")
    lq = d.get("liqs")
    if lq: L.append("Recent OKX liquidations: " + ", ".join(f"{c} longs ${v['longs_liq_usd']/1e3:.0f}k / shorts ${v['shorts_liq_usd']/1e3:.0f}k" for c, v in lq.items()))
    return L


def headlines(n: int = 12) -> list:
    d = cached()
    return [x["title"] for k in ("news_crypto", "news_markets", "news_politics") for x in (d.get(k) or [])[: n // 3]]


def start():
    def loop():
        while True:
            try:
                cached(3300)
            except Exception as e:
                print(f"[EYES] {type(e).__name__}")
            time.sleep(1800)
    threading.Thread(target=loop, daemon=True, name="eyes").start()
