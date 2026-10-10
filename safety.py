"""Free rug/honeypot screens: RugCheck (Solana) + GoPlus (Solana & EVM). Fail closed: unknown = unsafe."""
import time, requests

_C: dict = {}
EVM_ID = {"ethereum": "1", "eth": "1", "bsc": "56", "base": "8453", "arbitrum": "42161"}
UA = {"User-Agent": "Mozilla/5.0 MirkoBot"}


def _j(url, params=None):
    r = requests.get(url, params=params, headers=UA, timeout=12)
    r.raise_for_status()
    return r.json()


def check(chain: str, addr: str) -> dict:
    """{'ok': bool, 'flags': [...], 'lp_locked': pct|None, 'top10': pct|None}"""
    k = f"{chain}:{addr}"
    hit = _C.get(k)
    if hit and time.time() - hit["ts"] < 6 * 3600:
        return hit
    flags, lp, top10 = [], None, None
    try:
        if chain == "solana":
            rc = _j(f"https://api.rugcheck.xyz/v1/tokens/{addr}/report/summary")
            for r in rc.get("risks") or []:
                if r.get("level") == "danger":
                    flags.append(r.get("name", "danger"))
            lp = rc.get("lpLockedPct")
            if (rc.get("score_normalised") or 0) > 60:
                flags.append(f"RugCheck score {rc.get('score_normalised')}")
            g = (_j("https://api.gopluslabs.io/api/v1/solana/token_security", {"contract_addresses": addr}).get("result") or {}).get(addr) or {}
            if (g.get("mintable") or {}).get("status") == "1": flags.append("mint authority live")
            if (g.get("freezable") or {}).get("status") == "1": flags.append("freeze authority live")
            if (g.get("non_transferable") == "1"): flags.append("non-transferable")
        elif chain in EVM_ID:
            g = (_j(f"https://api.gopluslabs.io/api/v1/token_security/{EVM_ID[chain]}", {"contract_addresses": addr}).get("result") or {}).get(addr.lower())
            if not g:
                flags.append("no security data")
            else:
                for f, lab in (("is_honeypot", "honeypot"), ("cannot_sell_all", "can't sell all"), ("is_mintable", "mintable"), ("transfer_pausable", "pausable"),
                               ("is_blacklisted", "blacklist"), ("hidden_owner", "hidden owner"), ("can_take_back_ownership", "ownership reclaim"), ("owner_change_balance", "owner can edit balances")):
                    if g.get(f) == "1": flags.append(lab)
                for f in ("buy_tax", "sell_tax"):
                    try:
                        if float(g.get(f) or 0) > 0.05: flags.append(f"{f.replace('_', ' ')} {float(g[f]):.0%}")
                    except ValueError:
                        pass
                lps = g.get("lp_holders") or []
                lp = sum(float(h.get("percent") or 0) for h in lps if h.get("is_locked") == 1 or str(h.get("address", "")).lower().startswith(("0x000000000000000000000000000000000000dead", "0x0000000000000000000000000000000000000000"))) * 100
                hs = g.get("holders") or []
                top10 = sum(float(h.get("percent") or 0) for h in hs[:10] if not h.get("is_contract")) * 100 if hs else None
        else:
            flags.append("unsupported chain")
    except Exception as e:
        flags.append(f"screen unavailable ({type(e).__name__})")
    out = {"ok": not flags, "flags": flags, "lp_locked": lp, "top10": top10, "ts": time.time()}
    _C[k] = out
    return out
