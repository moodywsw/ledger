"""
market_data.py — free, keyless market data that DexScreener can't give us.

GeckoTerminal (api.geckoterminal.com, public, ~30 req/min) indexes
pump.fun bonding-curve pools from the first trade, whereas DexScreener
returns `pairs: null` for them. That gap is why the sniper never had a
price, liquidity or market cap and therefore could never trade. Every
call here is throttled (one request per GT_MIN_INTERVAL_SECONDS, shared
across threads), cached, and never raises: missing data comes back as
None / [] and the risk gate keeps treating it as "unknown", not "safe".

Also: pump.fun metadata (socials) from the launch event's IPFS `uri`,
and off-curve (PDA) detection used to drop bonding-curve / AMM vault
accounts from top-10 holder concentration.
"""
from __future__ import annotations

import os
import threading
import time
from datetime import datetime

import requests

GT_BASE = "https://api.geckoterminal.com/api/v2"
GT_MIN_INTERVAL_SECONDS = float(os.environ.get("GT_MIN_INTERVAL_SECONDS", "2.1"))  # ≈28 req/min
GT_CACHE_SECONDS = float(os.environ.get("GT_CACHE_SECONDS", "20"))
GT_TIMEOUT = 12
SOL_MINT = "So11111111111111111111111111111111111111112"

# pump.fun bonding curve + the AMMs it graduates to; anything with a
# pool on these counts as "launch / migration" territory.
LAUNCH_DEX_IDS = {"pump-fun"}
MIGRATION_DEX_IDS = {"pumpswap", "raydium", "raydium-cpmm", "meteora-damm-v2", "meteora"}

_lock = threading.Lock()
_last_call = [0.0]
_cache: dict = {}


def _f(x):
    try:
        return float(x) if x is not None else None
    except (TypeError, ValueError):
        return None


def _iso_ts(s):
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def gt_get(path: str, params: dict | None = None, cache_seconds: float | None = None):
    """Throttled + cached GET. Returns parsed JSON or None. Never raises."""
    key = (path, tuple(sorted((params or {}).items())))
    ttl = GT_CACHE_SECONDS if cache_seconds is None else cache_seconds
    now = time.time()
    hit = _cache.get(key)
    if hit and now - hit[0] < ttl:
        return hit[1]
    with _lock:
        wait = _last_call[0] + GT_MIN_INTERVAL_SECONDS - time.time()
        if wait > 0:
            time.sleep(wait)
        _last_call[0] = time.time()
    try:
        r = requests.get(GT_BASE + path, params=params, timeout=GT_TIMEOUT,
                         headers={"accept": "application/json;version=20230302"})
        if r.status_code == 429:
            print(f"[GT] rate limited on {path}")
            return None
        if r.status_code == 404:
            data = None
        else:
            r.raise_for_status()
            data = r.json()
    except Exception as e:
        print(f"[GT] {path} failed: {type(e).__name__}: {e}")
        return None
    _cache[key] = (time.time(), data)
    if len(_cache) > 3000:
        for k in list(_cache)[:1500]:
            _cache.pop(k, None)
    return data


def _pool_to_pair(pool: dict, base_mint: str | None = None) -> dict:
    """GeckoTerminal pool -> the subset of DexScreener's pair shape the bot reads."""
    a = pool.get("attributes") or {}
    rel = pool.get("relationships") or {}
    dex = ((rel.get("dex") or {}).get("data") or {}).get("id")
    base_id = ((rel.get("base_token") or {}).get("data") or {}).get("id", "")
    base = base_id.split("_", 1)[-1] if base_id else base_mint
    tx = (a.get("transactions") or {})
    h1, m5 = tx.get("h1") or {}, tx.get("m5") or {}
    vol = a.get("volume_usd") or {}
    chg = a.get("price_change_percentage") or {}
    fdv = _f(a.get("fdv_usd"))
    mcap = _f(a.get("market_cap_usd"))
    return {
        "chainId": "solana",
        "dexId": dex,
        "pairAddress": a.get("address"),
        "baseToken": {"address": base, "name": (a.get("name") or "").split(" / ")[0]},
        "priceUsd": a.get("base_token_price_usd"),
        "liquidity": {"usd": _f(a.get("reserve_in_usd"))},
        "fdv": fdv,
        "marketCap": mcap or fdv,
        "txns": {"h1": {"buys": h1.get("buys"), "sells": h1.get("sells")},
                 "m5": {"buys": m5.get("buys"), "sells": m5.get("sells")}},
        "volume": {"h1": _f(vol.get("h1")), "m5": _f(vol.get("m5")), "h24": _f(vol.get("h24"))},
        "priceChange": {"m5": _f(chg.get("m5")), "h1": _f(chg.get("h1")), "h24": _f(chg.get("h24"))},
        "pairCreatedAt": (_iso_ts(a.get("pool_created_at")) or 0) * 1000,
        "buyers_h1": h1.get("buyers"),
        "source": "geckoterminal",
    }


def gt_best_pair(mint: str) -> dict | None:
    """Deepest GT pool for a mint, in DexScreener pair shape, or None."""
    data = gt_get(f"/networks/solana/tokens/{mint}/pools", {"page": 1}, cache_seconds=10)
    pools = (data or {}).get("data") or []
    pairs = [_pool_to_pair(p) for p in pools]
    pairs = [p for p in pairs if (p.get("baseToken") or {}).get("address") == mint] or pairs
    if not pairs:
        return None
    return max(pairs, key=lambda p: (p.get("liquidity") or {}).get("usd") or 0)


def gt_new_pools(pages: int = 1) -> list:
    """Newest Solana pools (pairs shape) — used to catch pump.fun graduations
    (pumpswap/raydium pools of a ...pump mint), which pumpdev's WS doesn't emit."""
    out = []
    for page in range(1, pages + 1):
        data = gt_get("/networks/solana/new_pools", {"include": "base_token,dex", "page": page}, cache_seconds=10)
        out += [_pool_to_pair(p) for p in (data or {}).get("data") or []]
    return out


def gt_trending_pools(duration: str = "1h") -> list:
    data = gt_get("/networks/solana/trending_pools", {"include": "base_token", "duration": duration}, cache_seconds=300)
    syms = {i.get("id"): (i.get("attributes") or {}).get("symbol") for i in (data or {}).get("included") or []}
    out = []
    for p in (data or {}).get("data") or []:
        pair = _pool_to_pair(p)
        bid = (((p.get("relationships") or {}).get("base_token") or {}).get("data") or {}).get("id")
        pair["baseToken"]["symbol"] = syms.get(bid)
        out.append(pair)
    return out


def gt_pool_trades(pool_address: str, min_usd: float = 0) -> list:
    """Last ≤300 trades on a pool: [{wallet, kind, usd, ts, token_amount}]."""
    data = gt_get(f"/networks/solana/pools/{pool_address}/trades",
                  {"trade_volume_in_usd_greater_than": min_usd} if min_usd else None, cache_seconds=120)
    out = []
    for t in (data or {}).get("data") or []:
        a = t.get("attributes") or {}
        kind = a.get("kind")
        out.append({
            "wallet": a.get("tx_from_address"), "kind": kind, "usd": _f(a.get("volume_in_usd")) or 0.0,
            "ts": _iso_ts(a.get("block_timestamp")), "tx": a.get("tx_hash"),
            "token_amount": _f(a.get("to_token_amount") if kind == "buy" else a.get("from_token_amount")),
        })
    return out


def gt_ohlcv_minutes(pool_address: str, aggregate: int = 1, limit: int = 1000, before_ts: int | None = None) -> list:
    """[(ts, o, h, l, c, vol_usd)] oldest first."""
    params = {"aggregate": aggregate, "limit": limit, "currency": "usd"}
    if before_ts:
        params["before_timestamp"] = int(before_ts)
    data = gt_get(f"/networks/solana/pools/{pool_address}/ohlcv/minute", params, cache_seconds=60)
    rows = (((data or {}).get("data") or {}).get("attributes") or {}).get("ohlcv_list") or []
    return sorted((tuple(float(x) for x in r) for r in rows), key=lambda r: r[0])


# ── pump.fun metadata (socials) ──────────────────────────────────────

_META_CACHE: dict = {}
IPFS_GATEWAYS = ("https://ipfs.io/ipfs/", "https://cf-ipfs.com/ipfs/", "https://gateway.pinata.cloud/ipfs/")


def fetch_launch_metadata(uri: str | None, timeout: float = 6.0) -> dict:
    """pump.fun token metadata JSON (twitter / telegram / website / description)
    from the launch event's `uri`. pumpdev's WS event carries the uri but not
    the socials, which is why `require_socials` rejected every launch."""
    if not uri:
        return {}
    if uri in _META_CACHE:
        return _META_CACHE[uri]
    urls = [uri]
    if "/ipfs/" in uri:
        cid = uri.split("/ipfs/", 1)[1]
        urls += [g + cid for g in IPFS_GATEWAYS if not uri.startswith(g)]
    for u in urls:
        try:
            r = requests.get(u, timeout=timeout)
            if r.ok:
                j = r.json()
                meta = {k: (j.get(k) or None) for k in ("twitter", "telegram", "website", "description")}
                _META_CACHE[uri] = meta
                if len(_META_CACHE) > 5000:
                    _META_CACHE.clear()
                return meta
        except Exception:
            continue
    return {}


def looks_like_real_social(url: str | None) -> bool:
    """A bare x.com/<status> link to someone else's tweet or an empty string
    isn't a project social; require something that at least parses as a link."""
    if not url:
        return False
    u = url.strip().lower()
    return u.startswith(("http://", "https://", "t.me/", "x.com/", "twitter.com/")) and len(u) > 12


# ── off-curve owners (bonding curves, AMM vaults, PDAs) ───────────────

def is_program_owned(owner: str) -> bool:
    """True if `owner` is off the ed25519 curve (a PDA: bonding curve,
    pool vault, ...), i.e. not a person's wallet. Unknown -> False."""
    try:
        from solders.pubkey import Pubkey
        return not Pubkey.from_string(owner).is_on_curve()
    except Exception:
        return False
