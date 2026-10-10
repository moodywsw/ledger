"""Trending bubbles data: CoinGecko free markets (top 50) + trending, cached 10 min server-side."""
import threading, time
import requests

_c = {"ts": 0, "d": None}
_lock = threading.Lock()
CG = "https://api.coingecko.com/api/v3"


def get() -> dict:
    with _lock:
        if _c["d"] and time.time() - _c["ts"] < 600:
            return _c["d"]
        try:
            h = {"accept": "application/json", "user-agent": "mirko/1.0"}
            m = requests.get(f"{CG}/coins/markets", headers=h, timeout=15, params={
                "vs_currency": "usd", "order": "market_cap_desc", "per_page": 60, "page": 1, "price_change_percentage": "1h,24h,7d"}).json()
            try:
                tr = {c["item"]["id"] for c in requests.get(f"{CG}/search/trending", headers=h, timeout=15).json().get("coins", [])}
            except Exception:
                tr = set()
            stable = {"usdt", "usdc", "dai", "usde", "fdusd", "usds", "pyusd", "usd1", "tusd", "busd", "susde", "steth", "wsteth", "wbtc", "weth", "wbeth", "cbbtc", "weeth", "bsc-usd", "usdtb", "rseth", "lbtc", "jitosol", "susds"}
            items = [{"id": x["id"], "s": x["symbol"].upper()[:8], "mc": x.get("market_cap") or 0,
                      "h1": x.get("price_change_percentage_1h_in_currency"), "d1": x.get("price_change_percentage_24h_in_currency"),
                      "w1": x.get("price_change_percentage_7d_in_currency"), "t": x["id"] in tr}
                     for x in m if isinstance(x, dict) and x.get("symbol", "").lower() not in stable and "usd" not in x.get("symbol", "").lower() and x.get("symbol", "").lower() not in ("xaut", "paxg")][:50]
            if items:
                _c.update(ts=time.time(), d={"ts": int(time.time()), "items": items})
        except Exception as e:
            print(f"[BUBBLES] {type(e).__name__}")
        return _c["d"] or {"ts": 0, "items": []}


_logos = {"ts": 0, "m": {}}


def logos() -> dict:
    """symbol -> CoinGecko image URL for the top 250 coins (refreshed daily)."""
    if _logos["m"] and time.time() - _logos["ts"] < 86400:
        return _logos["m"]
    try:
        m = {}
        for page in (1, 2):
            for x in requests.get(f"{CG}/coins/markets", timeout=15, headers={"accept": "application/json"},
                                  params={"vs_currency": "usd", "order": "market_cap_desc", "per_page": 250, "page": page}).json():
                s = x.get("symbol", "").upper()
                if s and s not in m and str(x.get("image", "")).startswith("https://coin-images.coingecko.com/"):
                    m[s] = x["image"].replace("/large/", "/small/")
        if m:
            _logos.update(ts=time.time(), m=m)
    except Exception as e:
        print(f"[LOGOS] {type(e).__name__}")
    return _logos["m"]


_dx = {"ts": 0, "d": None}


def dex() -> dict:
    """DEX flows: trending GeckoTerminal pools on Solana/Base/BSC/Ethereum, cached 5 min. Opens on DexScreener."""
    with _lock:
        if _dx["d"] and time.time() - _dx["ts"] < 300:
            return _dx["d"]
        items, seen = [], set()
        for net in ("solana", "base", "bsc", "eth"):
            try:
                d = requests.get(f"https://api.geckoterminal.com/api/v2/networks/{net}/trending_pools", timeout=12, headers={"accept": "application/json"}).json().get("data") or []
            except Exception:
                continue
            for p in d[:15]:
                a = p.get("attributes") or {}
                sym = (a.get("name") or "?").split(" / ")[0].upper()[:8]
                if sym in seen or "USD" in sym or sym in ("SOL", "WETH", "WBNB", "ETH"):
                    continue
                pc = a.get("price_change_percentage") or {}
                tx = (a.get("transactions") or {}).get("h24") or {}
                def f(x):
                    try: return float(x)
                    except (TypeError, ValueError): return None
                seen.add(sym)
                dsnet = {"eth": "ethereum"}.get(net, net)
                items.append({"id": f"https://dexscreener.com/{dsnet}/{a.get('address')}", "s": sym, "mc": f(a.get("market_cap_usd") or a.get("fdv_usd")) or 0,
                              "h1": f(pc.get("h1")), "d1": f(pc.get("h24")), "w1": f(pc.get("h6")), "vol": f((a.get("volume_usd") or {}).get("h24")) or 0,
                              "bs": (tx.get("buys") or 0) / max(1, (tx.get("buys") or 0) + (tx.get("sells") or 0)), "t": False, "net": net})
        items.sort(key=lambda x: -x["vol"])
        if items:
            _dx.update(ts=time.time(), d={"ts": int(time.time()), "items": items[:45], "src": "dex"})
        return _dx["d"] or {"ts": 0, "items": []}
