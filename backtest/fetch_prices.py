"""
fetch_prices.py — 1-minute OHLCV around every collected wallet buy, plus
static token facts, for the replay harness. Free public sources only:

  - GeckoTerminal (no key, ~30 req/min): pool lookup + minute candles
  - Solana RPC (ALCHEMY_RPC_URL or the public endpoint): mint/freeze
    authority + Token-2022 extensions (current state — once revoked these
    can't be re-enabled, so "revoked now" implies "revoked then"; a live
    authority now was live then too)

Usage:
    python -m backtest.fetch_prices --buys backtest/data/wallet_buys.json --out backtest/data/prices.json
"""
import argparse
import json
import os
import time
from pathlib import Path

import requests

GT = "https://api.geckoterminal.com/api/v2"
RPC_URL = os.environ.get("ALCHEMY_RPC_URL") or "https://api.mainnet-beta.solana.com"
_last_gt = [0.0]


def gt_get(path, params=None, tries=6):
    delay = 5
    for _ in range(tries):
        wait = _last_gt[0] + 2.1 - time.time()  # stay under ~30/min
        if wait > 0:
            time.sleep(wait)
        _last_gt[0] = time.time()
        try:
            r = requests.get(GT + path, params=params, headers={"accept": "application/json"}, timeout=30)
        except requests.RequestException:
            time.sleep(delay); delay *= 2; continue
        if r.status_code == 429:
            time.sleep(delay); delay = min(delay * 2, 60); continue
        if r.status_code == 404:
            return None
        if r.ok:
            return r.json()
        time.sleep(delay)
    return None


def mint_facts(mint):
    try:
        r = requests.post(RPC_URL, json={"jsonrpc": "2.0", "id": 1, "method": "getAccountInfo",
                                         "params": [mint, {"encoding": "jsonParsed"}]}, timeout=20).json()
        info = (((r.get("result") or {}).get("value") or {}).get("data") or {}).get("parsed", {}).get("info", {})
        if not info:
            return {"mint_authority": "unknown", "freeze_authority": "unknown", "extensions": [], "supply": None}
        dec = info.get("decimals") or 0
        supply = int(info.get("supply") or 0) / (10 ** dec) if info.get("supply") else None
        return {
            "mint_authority": info.get("mintAuthority"),
            "freeze_authority": info.get("freezeAuthority"),
            "extensions": [e.get("extension") for e in info.get("extensions", [])],
            "supply": supply,
        }
    except Exception:
        return {"mint_authority": "unknown", "freeze_authority": "unknown", "extensions": [], "supply": None}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--buys", default="backtest/data/wallet_buys.json")
    ap.add_argument("--out", default="backtest/data/prices.json")
    ap.add_argument("--hours-after", type=float, default=24)
    args = ap.parse_args()

    buys = json.loads(Path(args.buys).read_text())["buys"]
    out_path = Path(args.out)
    data = json.loads(out_path.read_text()) if out_path.exists() else {}
    windows = {}
    for b in buys:
        if not b.get("block_time"):
            continue
        lo, hi = windows.get(b["mint"], (b["block_time"], b["block_time"]))
        windows[b["mint"]] = (min(lo, b["block_time"]), max(hi, b["block_time"]))

    for i, (mint, (t0, t1)) in enumerate(sorted(windows.items(), key=lambda kv: kv[1][0])):
        if mint in data:
            continue
        rec = {"pool": None, "candles": [], **mint_facts(mint)}
        pools = gt_get(f"/networks/solana/tokens/{mint}/pools", {"page": 1})
        best = None
        for p in (pools or {}).get("data", []):
            base = ((p.get("relationships") or {}).get("base_token") or {}).get("data", {}).get("id", "")
            if base != f"solana_{mint}":
                continue
            reserve = float(p["attributes"].get("reserve_in_usd") or 0)
            if best is None or reserve > best[0]:
                best = (reserve, p)
        if best:
            attrs = best[1]["attributes"]
            rec.update(pool=best[1]["id"].split("_", 1)[1], reserve_usd_now=best[0],
                       fdv_usd_now=float(attrs.get("fdv_usd") or 0) or None,
                       price_usd_now=float(attrs.get("base_token_price_usd") or 0) or None,
                       pool_created_at=attrs.get("pool_created_at"), dex=(best[1].get("relationships") or {}).get("dex", {}).get("data", {}).get("id"))
            end = int(t1 + args.hours_after * 3600)
            candles = {}
            before = end
            while before > t0 - 600:
                resp = gt_get(f"/networks/solana/pools/{rec['pool']}/ohlcv/minute",
                              {"aggregate": 1, "before_timestamp": before, "limit": 1000, "currency": "usd", "token": "base"})
                rows = (((resp or {}).get("data") or {}).get("attributes") or {}).get("ohlcv_list") or []
                if not rows:
                    break
                for ts, o, h, l, c, v in rows:
                    candles[int(ts)] = [int(ts), o, h, l, c, v]
                oldest = min(r[0] for r in rows)
                if oldest >= before:
                    break
                before = oldest
            rec["candles"] = [candles[k] for k in sorted(candles) if t0 - 600 <= k <= end]
        data[mint] = rec
        print(f"[{i + 1}/{len(windows)}] {mint[:8]} pool={rec['pool'] and rec['pool'][:8]} candles={len(rec['candles'])}", flush=True)
        if (i + 1) % 5 == 0:
            out_path.write_text(json.dumps(data))
    out_path.write_text(json.dumps(data))
    print(f"done: {len(data)} mints -> {out_path}")


if __name__ == "__main__":
    main()
