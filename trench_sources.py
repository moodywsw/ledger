"""Wider trench radar (all free; tokens.xyz only if TOKENS_XYZ_API_KEY is set).

pump.fun (new / graduating / recently graduated), other launchpads via GeckoTerminal new pools
(letsbonk/raydium-launchlab, Moonshot, Believe/meteora-dbc, bags, boop, Base: zora/clanker/virtuals,
BSC: four-meme), DexScreener boosts + fresh profiles, tokens.xyz trending + risk.
Returns a compact summary used by Market Thoughts' trenches block."""
from __future__ import annotations

import os, time
from concurrent.futures import ThreadPoolExecutor

import requests

UA = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}
PUMP = "https://frontend-api-v3.pump.fun/coins"
GT = "https://api.geckoterminal.com/api/v2/networks/{net}/dexes/{dex}/pools"
PADS = [("letsbonk", "solana", "raydium-launchlab"), ("Moonshot", "solana", "moonshot"), ("Believe", "solana", "meteora-dbc"),
        ("Bags", "solana", "bags-fm"), ("Boop", "solana", "boop-fun"), ("Zora (Base)", "base", "zora"), ("four.meme (BSC)", "bsc", "four-meme")]
TX = "https://api.tokens.xyz/v1"


def _get(url, params=None, headers=None):
    r = requests.get(url, params=params, headers={**UA, **(headers or {})}, timeout=10)
    r.raise_for_status()
    return r.json()


def _pump(kind: str) -> list:
    p = {"offset": 0, "limit": 40, "includeNsfw": "false"}
    if kind == "new":
        p.update(sort="created_timestamp", order="DESC")
    elif kind == "graduating":
        p.update(sort="market_cap", order="DESC", complete="false")
    else:
        p.update(sort="last_trade_timestamp", order="DESC", complete="true")
    xs = []
    for _ in range(3):
        try:
            xs = _get(PUMP, p); break
        except Exception:
            time.sleep(2)
    now = time.time()
    out = []
    for x in xs:
        mc = float(x.get("usd_market_cap") or 0)
        age_h = (now - (x.get("created_timestamp") or 0) / 1000) / 3600
        if kind == "graduated" and not (60_000 < mc < 50_000_000 and age_h < 24 * 14):
            continue   # skip stables/old giants
        out.append({"sym": x.get("symbol"), "mc": mc, "replies": x.get("reply_count") or 0, "age_h": round(age_h, 1), "mint": x.get("mint")})
    return out


def _pad(name, net, dex) -> dict:
    try:
        d = _get(GT.format(net=net, dex=dex), {"page": 1, "sort": "h24_volume_usd_desc"})["data"]
    except Exception:
        return {"pad": name, "n": 0}
    rows = []
    for p in d[:20]:
        a = p["attributes"]
        try:
            vol = float((a.get("volume_usd") or {}).get("h24") or 0); fdv = float(a.get("fdv_usd") or 0)
            tx = a.get("transactions", {}).get("h24", {}); b, s = tx.get("buys", 0), tx.get("sells", 0)
        except Exception:
            continue
        rows.append({"sym": a.get("name", "").split(" / ")[0], "vol": vol, "fdv": fdv, "buy_share": b / (b + s) if b + s else .5})
    hot = max(rows, key=lambda r: r["vol"]) if rows else None
    return {"pad": name, "n": len(rows), "vol": round(sum(r["vol"] for r in rows)), "hot": hot}


def _dexscreener() -> dict:
    out = {"boosted": [], "profiles": 0}
    try:
        b = _get("https://api.dexscreener.com/token-boosts/top/v1")
        out["boosted"] = [{"chain": x.get("chainId"), "addr": x.get("tokenAddress"), "amount": x.get("totalAmount")} for x in b[:8]]
        out["profiles"] = len(_get("https://api.dexscreener.com/token-profiles/latest/v1"))
    except Exception:
        pass
    return out


def _tokensxyz() -> dict:
    key = os.environ.get("TOKENS_XYZ_API_KEY")
    if not key:
        return {"enabled": False}
    h = {"x-api-key": key}
    try:
        d = _get(f"{TX}/assets/trending", {"limit": 15}, h)
        items = d.get("data") or d.get("assets") or d.get("items") or (d if isinstance(d, list) else [])
        rows = []
        for x in items[:15]:
            rows.append({"sym": x.get("symbol") or (x.get("asset") or {}).get("symbol"), "name": x.get("name"),
                         "chg24": x.get("priceChange24hPercent") or x.get("price_change_24h"), "id": x.get("assetId") or x.get("id"),
                         "mint": x.get("mint")})
        return {"enabled": True, "trending": [r for r in rows if r["sym"]]}
    except Exception as e:
        return {"enabled": True, "error": type(e).__name__}


def tx_risk(mint: str) -> dict | None:
    """tokens.xyz quick risk check for a Solana mint (None if no key/failed)."""
    key = os.environ.get("TOKENS_XYZ_API_KEY")
    if not key or not mint:
        return None
    try:
        return _get(f"{TX}/assets/risk-summary", {"mint": mint}, {"x-api-key": key})
    except Exception:
        return None


def scan() -> dict:
    res = {}
    for k in ("new", "graduating", "graduated"):   # pump.fun rate-limits bursts: go one by one
        res[k] = _pump(k); time.sleep(1.2)
    with ThreadPoolExecutor(4) as ex:
        ds, tx = ex.submit(_dexscreener), ex.submit(_tokensxyz)
        pads = [ex.submit(_pad, *p) for p in PADS]
        res["ds"], res["tx"] = ds.result(), tx.result()
        res["pads"] = [f.result() for f in pads]
    new, grad_ing, grad = res["new"], res["graduating"], res["graduated"]
    lines = []
    if new:
        lines.append(f"pump.fun: {len(new)} new launches in the last batch, median mcap ${sorted(x['mc'] for x in new)[len(new) // 2]:,.0f}")
    if grad_ing:
        g = grad_ing[0]; lines.append(f"closest to graduating: ${g['sym']} at ${g['mc']:,.0f}")
    if grad:
        top = max(grad, key=lambda x: x["mc"]); lines.append(f"{len(grad)} recently graduated still trading; strongest ${top['sym']} ${top['mc']:,.0f}")
    act = sorted([p for p in res["pads"] if p.get("n")], key=lambda p: -p.get("vol", 0))
    if act:
        lines.append("hottest launchpad: " + act[0]["pad"] + (f" (${act[0]['hot']['sym']})" if act[0].get("hot") else ""))
    return {"ts": time.time(), "pump": {"new": new[:10], "graduating": grad_ing[:8], "graduated": grad[:8]}, "pads": res["pads"],
            "dexscreener": res["ds"], "tokensxyz": res["tx"], "lines": lines}
