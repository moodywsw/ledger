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
