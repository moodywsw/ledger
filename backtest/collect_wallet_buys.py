"""
collect_wallet_buys.py — builds a historical "wallet feed" dataset for the
replay harness: every real buy each tracked wallet (wallets.json) made in
the last N days, parsed with the SAME extract_new_buys() logic the live
bot uses, so the replay sees exactly the signals the bot would have seen.

Read-only: only getSignaturesForAddress / getTransaction calls. Uses
ALCHEMY_RPC_URL if set, otherwise Solana's public RPC (slow, rate
limited, but needs no key). Never touches any private key.

Usage:
    python -m backtest.collect_wallet_buys --days 14 --out backtest/data/wallet_buys.json
"""
import argparse
import json
import os
import sys
import time
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

RPC_URL = os.environ.get("ALCHEMY_RPC_URL") or "https://api.mainnet-beta.solana.com"


def rpc(method, params, retries=6):
    delay, last = 1.0, None
    for _ in range(retries):
        try:
            r = requests.post(RPC_URL, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params}, timeout=30)
            if r.status_code == 429:
                time.sleep(delay)
                delay = min(delay * 2, 20)
                continue
            r.raise_for_status()
            body = r.json()
            if "error" in body:
                raise RuntimeError(body["error"])
            return body.get("result")
        except (requests.RequestException, ValueError) as e:
            time.sleep(delay)
            delay = min(delay * 2, 20)
            last = e
    raise RuntimeError(f"{method} failed after retries: {last!r}")


def main():
    # Imported lazily so --help works without the bot's deps.
    from ledger_bot import extract_new_buys

    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=float, default=14)
    ap.add_argument("--max-sigs", type=int, default=400, help="per wallet")
    ap.add_argument("--wallets", default="wallets.json")
    ap.add_argument("--out", default="backtest/data/wallet_buys.json")
    ap.add_argument("--workers", type=int, default=6, help="parallel getTransaction calls (public RPC allows ~10 rps)")
    args = ap.parse_args()

    wallets = json.loads(Path(args.wallets).read_text())["wallets"]
    cutoff = time.time() - args.days * 86400
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    existing = json.loads(out_path.read_text()) if out_path.exists() else {"buys": [], "done_wallets": []}
    buys = existing["buys"]
    done = set(existing.get("done_wallets", []))

    for w in wallets:
        addr, handle = w["address"], w["handle"]
        if addr in done:
            continue
        sigs, before = [], None
        while len(sigs) < args.max_sigs:
            opts = {"limit": 100}
            if before:
                opts["before"] = before
            page = rpc("getSignaturesForAddress", [addr, opts]) or []
            if not page:
                break
            for s in page:
                if (s.get("blockTime") or 0) < cutoff:
                    page = []
                    break
                if s.get("err") is None:
                    sigs.append(s)
            if not page:
                break
            before = page[-1]["signature"]
        print(f"[{handle}] {len(sigs)} successful signatures in window", flush=True)
        n_buys = 0

        def fetch(sig):
            try:
                return rpc("getTransaction", [sig, {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 1}])
            except Exception as e:
                print(f"  skip {sig[:10]}: {e}", flush=True)
                return None

        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            txs = list(pool.map(fetch, [s["signature"] for s in sigs[: args.max_sigs]]))
        for tx in txs:
            if not tx:
                continue
            for b in extract_new_buys([tx], addr):
                b.update({"wallet": addr, "handle": handle, "block_time": tx.get("blockTime"), "tx_version": tx.get("version")})
                buys.append(b)
                n_buys += 1
        print(f"[{handle}] {n_buys} buys", flush=True)
        done.add(addr)
        out_path.write_text(json.dumps({"buys": buys, "done_wallets": sorted(done), "days": args.days}, indent=1))

    print(f"total buys: {len(buys)} -> {out_path}")


if __name__ == "__main__":
    main()
