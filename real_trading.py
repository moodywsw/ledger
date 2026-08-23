"""
real_trading.py — real on-chain execution via Jupiter's Ultra Swap API,
isolated from ledger_bot.py's paper-trading logic on purpose: this is the
only module that ever touches SOLANA_PRIVATE_KEY, so keeping it small and
self-contained makes it easy to audit end-to-end.

Two patterns below are adapted from omo (github.com/omotrades/omo, MIT
license) — see the README's Acknowledgements section:

  - "unarmed" is a normal, expected state, not a failure. Every function
    here keeps working (reads chain state, reasons, journals) whether or
    not SOLANA_PRIVATE_KEY is set; only the actual signing step is
    gated. execute_real_trade() never raises for this — it returns
    {"status": "unarmed", ...} so callers can report it plainly instead
    of it looking like a crash or, worse, going unreported.

  - Real balances are re-derived from the chain immediately before every
    order, never trusted from real_positions.json alone. That local file
    is a cache/journal of what this bot believes it holds — it can drift
    from what's actually in the wallet (a prior run crashed mid-update,
    something else touched the wallet, a bug). Every buy checks the
    live SOL balance; every sell checks the live token balance and
    shrinks to whatever's actually there before selling a single unit
    more than truly exists on-chain.

Safety contract (non-negotiable, per the person running this bot):
  - The private key lives ONLY in the SOLANA_PRIVATE_KEY env var. It is
    never written to a file, never printed, never included in an
    exception message, never logged.
  - REAL_TRADING_ENABLED defaults to False and is controlled ONLY by the
    REAL_TRADING_ENABLED env var — flipping it needs no code change and
    no deploy, just a Railway env var edit.
  - MAX_REAL_POSITION_SOL is a hard ceiling on total SOL committed to any
    single real position (initial buy + any top-ups combined), enforced
    inside execute_real_trade() regardless of what paper-trading position
    sizing calculated. It is never bypassed.
  - MAX_REAL_DAILY_SOL is a second, independent ceiling on total real SOL
    spent across ALL positions in a rolling UTC day — a single overly
    conviction-scaled position can't drain the wallet even if each
    individual buy respects MAX_REAL_POSITION_SOL.
  - Every real-trading outcome — including "disabled", "guard rail
    tripped", and "attempted but failed" — is caught here and returned
    as a typed status rather than raised, so a Jupiter outage, a bad
    quote, or a signing error can never crash or block the paper-trading
    loop that's driving this.

Jupiter Ultra Swap API (https://api.jup.ag/ultra/v1) — the flow is:
  1. GET /order  -> quote + an unsigned, base64-encoded transaction
  2. sign it locally with solders
  3. POST /execute -> broadcasts the signed transaction, returns the
     on-chain signature once landed (Ultra's "managed landing" already
     polls for confirmation server-side — a non-"Success" status here is
     treated as no fill, never journaled as one)

This replaces the old /quote + /swap v6-style API this stub used to
reference — Ultra is Jupiter's current recommended path for anyone who
doesn't need custom instruction building (RPC-less, gasless, automatic
slippage optimization), which fits a small bot like this one well.
"""
import base64
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

REAL_TRADING_ENABLED = os.environ.get("REAL_TRADING_ENABLED", "false").strip().lower() == "true"
MAX_REAL_POSITION_SOL = float(os.environ.get("MAX_REAL_POSITION_SOL", "0.02"))
MAX_REAL_DAILY_SOL = float(os.environ.get("MAX_REAL_DAILY_SOL", "0.06"))  # independent of the per-position cap
MIN_REAL_TICKET_SOL = float(os.environ.get("MIN_REAL_TICKET_SOL", "0.002"))  # below this, a real fill is mostly fees
MAX_ACCEPTABLE_PRICE_IMPACT_PCT = 5.0  # skip the trade if Jupiter's quote implies more slippage than this
MIN_SOL_FEE_BUFFER = 0.02  # kept unspent for network/priority fees on top of any buy amount

SOLANA_PRIVATE_KEY = os.environ.get("SOLANA_PRIVATE_KEY", "")  # never printed, never logged
SOLANA_WALLET_ADDRESS = os.environ.get("SOLANA_WALLET_ADDRESS", "")  # optional pin — see load_keypair()
JUPITER_API_KEY = os.environ.get("JUPITER_API_KEY", "")
ALCHEMY_RPC_URL = os.environ.get("ALCHEMY_RPC_URL", "")

JUPITER_ULTRA_BASE = "https://api.jup.ag/ultra/v1"
WSOL_MINT = "So11111111111111111111111111111111111111112"
LAMPORTS_PER_SOL = 1_000_000_000
TOKEN_PROGRAM_ID = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
TOKEN_2022_PROGRAM_ID = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"

REAL_POSITIONS_FILE = Path(os.environ.get("DATA_DIR", ".")) / "real_positions.json"

_keypair_cache = None

if not REAL_TRADING_ENABLED:
    print("[real_trading] UNARMED — REAL_TRADING_ENABLED is not 'true'. "
          "Sniper/priority-copy decisions keep running on paper; no real order will be placed.")


def _request_with_backoff(method, url, max_retries=4, **kwargs):
    """Small local retry-on-429 wrapper, same spirit as ledger_bot's
    request_with_backoff — duplicated rather than imported to keep this
    module import-independent from ledger_bot.py (no circular import,
    and this file stays auditable on its own)."""
    delay = 2
    resp = None
    for attempt in range(max_retries):
        resp = requests.request(method, url, **kwargs)
        if resp.status_code != 429:
            return resp
        time.sleep(delay)
        delay *= 2
    return resp


def load_keypair():
    """
    Loads the signing keypair from SOLANA_PRIVATE_KEY. Cached after first
    call. Never includes the key value in any error message.

    If SOLANA_WALLET_ADDRESS is set, the derived public key must match it
    exactly or this raises — adapted from omo's keys.server.ts, which
    verifies its trading key against its published wallet on every load
    and "fails loudly instead of quietly trading from some other
    account." Setting SOLANA_WALLET_ADDRESS is optional but recommended:
    it turns a wrong/rotated/pasted-into-the-wrong-env-var key into an
    immediate, obvious error instead of the bot silently signing from an
    unexpected wallet.
    """
    global _keypair_cache
    if _keypair_cache is not None:
        return _keypair_cache
    if not SOLANA_PRIVATE_KEY:
        raise RuntimeError("SOLANA_PRIVATE_KEY env var is not set.")
    from solders.keypair import Keypair
    try:
        keypair = Keypair.from_base58_string(SOLANA_PRIVATE_KEY)
    except Exception:
        raise RuntimeError("SOLANA_PRIVATE_KEY is set but could not be parsed as a base58 secret key.")
    if SOLANA_WALLET_ADDRESS and str(keypair.pubkey()) != SOLANA_WALLET_ADDRESS:
        raise RuntimeError(
            f"SOLANA_PRIVATE_KEY derives {keypair.pubkey()}, which does not match "
            f"SOLANA_WALLET_ADDRESS ({SOLANA_WALLET_ADDRESS}). Refusing to trade from "
            f"an unexpected wallet — check for a pasted-in-wrong key or a stale env var."
        )
    _keypair_cache = keypair
    return _keypair_cache


def _wallet_pubkey_str() -> str:
    return str(load_keypair().pubkey())


def _check_wallet_balance_sol() -> float:
    if not ALCHEMY_RPC_URL:
        raise RuntimeError("Set ALCHEMY_RPC_URL env var first.")
    payload = {"jsonrpc": "2.0", "id": 1, "method": "getBalance", "params": [_wallet_pubkey_str()]}
    resp = _request_with_backoff("POST", ALCHEMY_RPC_URL, json=payload, timeout=15)
    resp.raise_for_status()
    lamports = resp.json().get("result", {}).get("value", 0) or 0
    return lamports / LAMPORTS_PER_SOL


def _check_onchain_token_balance_raw(mint: str) -> int:
    """
    The real, on-chain source of truth for how much of `mint` this
    wallet actually holds right now, in raw (smallest-unit) terms —
    summed across every token account for this mint under either the
    classic or Token-2022 program. Used to reconcile real_positions.json
    before every sell, so a stale/drifted local record can never cause
    an attempt to sell more than genuinely exists.
    """
    if not ALCHEMY_RPC_URL:
        raise RuntimeError("Set ALCHEMY_RPC_URL env var first.")
    wallet = _wallet_pubkey_str()
    total_raw = 0
    for program_id in (TOKEN_PROGRAM_ID, TOKEN_2022_PROGRAM_ID):
        payload = {
            "jsonrpc": "2.0", "id": 1, "method": "getTokenAccountsByOwner",
            "params": [wallet, {"mint": mint, "programId": program_id}, {"encoding": "jsonParsed"}],
        }
        resp = _request_with_backoff("POST", ALCHEMY_RPC_URL, json=payload, timeout=15)
        resp.raise_for_status()
        accounts = resp.json().get("result", {}).get("value", [])
        for acc in accounts:
            info = acc.get("account", {}).get("data", {}).get("parsed", {}).get("info", {})
            amount_str = info.get("tokenAmount", {}).get("amount")
            if amount_str is not None:
                total_raw += int(amount_str)
    return total_raw


def _load_real_positions() -> dict:
    if not REAL_POSITIONS_FILE.exists():
        return {}
    try:
        with REAL_POSITIONS_FILE.open("r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _save_real_positions(positions: dict):
    try:
        REAL_POSITIONS_FILE.parent.mkdir(parents=True, exist_ok=True)
        with REAL_POSITIONS_FILE.open("w", encoding="utf-8") as f:
            json.dump(positions, f, indent=2)
    except Exception as e:
        print(f"[WARN] real_positions.json write failed: {e}")


def _today_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _spent_today_sol(positions: dict) -> float:
    meta = positions.get("_meta", {})
    if meta.get("date") != _today_utc():
        return 0.0
    return meta.get("spent_sol", 0.0)


def _record_spend(positions: dict, sol_spent: float):
    today = _today_utc()
    meta = positions.get("_meta", {})
    if meta.get("date") != today:
        meta = {"date": today, "spent_sol": 0.0}
    meta["spent_sol"] = meta.get("spent_sol", 0.0) + sol_spent
    positions["_meta"] = meta


def _get_order(input_mint: str, output_mint: str, amount_raw: int, taker: str = None) -> dict:
    if not JUPITER_API_KEY:
        raise RuntimeError("JUPITER_API_KEY env var is not set.")
    params = {"inputMint": input_mint, "outputMint": output_mint, "amount": str(amount_raw)}
    if taker:
        params["taker"] = taker
    resp = _request_with_backoff(
        "GET", f"{JUPITER_ULTRA_BASE}/order",
        headers={"x-api-key": JUPITER_API_KEY}, params=params, timeout=20,
    )
    resp.raise_for_status()
    return resp.json()


def _execute_order(signed_tx_b64: str, request_id: str) -> dict:
    resp = _request_with_backoff(
        "POST", f"{JUPITER_ULTRA_BASE}/execute",
        headers={"x-api-key": JUPITER_API_KEY, "content-type": "application/json"},
        json={"signedTransaction": signed_tx_b64, "requestId": request_id},
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()


def dry_run_quote(token_mint: str, amount_sol: float, side: str) -> dict:
    """
    SAFE TEST MODE — only calls GET /order, never touches
    SOLANA_PRIVATE_KEY (no `taker` is sent), never signs, never calls
    /execute. Impossible to spend money through this function by
    construction. Use this to sanity-check quotes before ever enabling
    REAL_TRADING_ENABLED.
    """
    if side == "buy":
        input_mint, output_mint = WSOL_MINT, token_mint
        amount_raw = round(amount_sol * LAMPORTS_PER_SOL)
    elif side == "sell":
        input_mint, output_mint = token_mint, WSOL_MINT
        # No real holdings tracked in dry-run — just quote a nominal
        # amount so you can see the shape of a sell quote too. Uses
        # lamports-equivalent as a stand-in raw amount for the token
        # leg; the number itself is illustrative, not a real balance.
        amount_raw = round(amount_sol * LAMPORTS_PER_SOL)
    else:
        raise ValueError("side must be 'buy' or 'sell'")

    order = _get_order(input_mint, output_mint, amount_raw)
    return {
        "side": side,
        "token_mint": token_mint,
        "amount_sol_requested": amount_sol,
        "out_amount": order.get("outAmount"),
        "price_impact_pct": order.get("priceImpact"),
        "router": order.get("router"),
        "gasless": order.get("gasless"),
        "raw_order_response": order,
    }


def _result(status: str, **fields) -> dict:
    """Builds a typed result dict. `success` is kept alongside `status`
    purely for the convenience of a truthy check at call sites."""
    return {"status": status, "success": status == "success", **fields}


def execute_real_trade(token: str, amount_sol: float, side: str) -> dict:
    """
    Real execution, with a typed outcome instead of a bare pass/fail —
    adapted from omo's OrderResult (execute.server.ts), which
    distinguishes "no key loaded" from "a guard rail refused this" from
    "attempted and failed" from "filled". That distinction is the whole
    point: an unarmed bot and a broken one look identical from a plain
    boolean, and conflating them either hides a real problem behind a
    routine "not trading today" state, or cries wolf about the reverse.

    Statuses:
      "unarmed"  REAL_TRADING_ENABLED is false. Normal, expected, not an
                 error — the caller should report it plainly (once,
                 without alarm) and keep going on paper.
      "blocked"  A guard rail intentionally refused: price impact too
                 high, a cap reached, insufficient/drifted balance, a
                 misconfigured wallet pin, nothing to sell. The
                 unwilling-but-correct outcome.
      "failed"   An order was actually attempted and something broke
                 (network, Jupiter, signing, an unconfirmed fill).
      "success"  Filled and confirmed.

    Never raises — every failure mode above is caught here and returned
    as one of these statuses, so a Jupiter outage, a bad quote, or a
    signing error can never crash or block the paper-trading loop that's
    driving this.
    """
    if not REAL_TRADING_ENABLED:
        return _result(
            "unarmed",
            reason="REAL_TRADING_ENABLED is not 'true' — decision stays paper-only, nothing signed or sent.",
        )
    if side not in ("buy", "sell"):
        return _result("failed", reason=f"invalid side {side!r}, must be 'buy' or 'sell'")

    try:
        if side == "buy":
            return _execute_buy(token, amount_sol)
        return _execute_sell(token, amount_sol)
    except RuntimeError as e:
        # Configuration problems (missing key, wallet mismatch, missing
        # RPC/API key) — the guard-rail-style outcome, not a crash.
        return _result("blocked", reason=str(e))
    except Exception as e:
        print(f"[REAL TRADE ERROR] {token} {side} {amount_sol}: {e}")
        return _result("failed", reason=str(e))


def _execute_buy(token: str, amount_sol: float) -> dict:
    positions = _load_real_positions()
    existing = positions.get(token, {"raw_amount": 0, "cost_basis_sol": 0.0, "buy_signatures": [], "sell_signatures": []})

    already_committed = existing["cost_basis_sol"]
    remaining_position_budget = MAX_REAL_POSITION_SOL - already_committed
    if remaining_position_budget <= 0:
        return _result("blocked", reason=f"MAX_REAL_POSITION_SOL ({MAX_REAL_POSITION_SOL} SOL) already committed to {token}")

    spent_today = _spent_today_sol(positions)
    remaining_daily_budget = MAX_REAL_DAILY_SOL - spent_today
    if remaining_daily_budget <= 0:
        return _result("blocked", reason=f"MAX_REAL_DAILY_SOL ({MAX_REAL_DAILY_SOL} SOL) already spent today ({spent_today:.4f} SOL)")

    clamped_amount_sol = min(amount_sol, remaining_position_budget, remaining_daily_budget)
    if clamped_amount_sol < MIN_REAL_TICKET_SOL:
        return _result("blocked", reason=f"clamped size {clamped_amount_sol:.5f} SOL is below MIN_REAL_TICKET_SOL ({MIN_REAL_TICKET_SOL} SOL)")

    # Re-derive from the chain rather than trusting only what this file
    # last recorded — the wallet may have been spent from elsewhere, or a
    # prior run may have crashed after signing but before saving state.
    wallet_balance = _check_wallet_balance_sol()
    if wallet_balance < clamped_amount_sol + MIN_SOL_FEE_BUFFER:
        return _result("blocked", reason=f"live wallet balance {wallet_balance:.4f} SOL is insufficient for {clamped_amount_sol:.4f} SOL + {MIN_SOL_FEE_BUFFER} SOL fee buffer")

    taker = _wallet_pubkey_str()
    amount_raw = round(clamped_amount_sol * LAMPORTS_PER_SOL)
    order = _get_order(WSOL_MINT, token, amount_raw, taker=taker)

    price_impact = float(order.get("priceImpact") or 0)
    if price_impact > MAX_ACCEPTABLE_PRICE_IMPACT_PCT:
        return _result("blocked", reason=f"price impact {price_impact:.2f}% exceeds MAX_ACCEPTABLE_PRICE_IMPACT_PCT ({MAX_ACCEPTABLE_PRICE_IMPACT_PCT}%)")

    tx_b64 = order.get("transaction")
    if not tx_b64:
        return _result("failed", reason="Jupiter returned no transaction to sign (quote may be unroutable)")

    signed_b64 = _sign_transaction(tx_b64)
    exec_result = _execute_order(signed_b64, order["requestId"])

    # Ultra's /execute already polls for landing server-side — anything
    # other than "Success" here means it never actually confirmed, and
    # must never be journaled or counted as a fill.
    if exec_result.get("status") != "Success":
        return _result("failed", reason=f"execute returned status={exec_result.get('status')!r}", raw_result=exec_result)

    tokens_received = int(order.get("outAmount") or 0)
    existing["raw_amount"] += tokens_received
    existing["cost_basis_sol"] += clamped_amount_sol
    existing["buy_signatures"].append(exec_result["signature"])
    positions[token] = existing
    _record_spend(positions, clamped_amount_sol)
    _save_real_positions(positions)

    return _result(
        "success", signature=exec_result["signature"], sol_spent=clamped_amount_sol,
        tokens_received=tokens_received, price_impact_pct=price_impact,
    )


def _execute_sell(token: str, amount_sol: float) -> dict:
    positions = _load_real_positions()
    existing = positions.get(token)
    if not existing or existing.get("raw_amount", 0) <= 0:
        return _result("blocked", reason=f"no tracked real position for {token} to sell")

    # Re-derive the actual held amount from the chain before selling
    # anything — real_positions.json is a cache of what this bot believes
    # it holds, and a crashed run or anything else touching the wallet
    # can make that drift from what's actually there. Never sell more
    # than genuinely exists on-chain, regardless of what the file says.
    onchain_raw = _check_onchain_token_balance_raw(token)
    if onchain_raw <= 0:
        # Tracked a position that no longer exists on-chain — clear the
        # stale entry so it doesn't keep tripping this on every future
        # exit attempt for this mint.
        positions.pop(token, None)
        _save_real_positions(positions)
        return _result("blocked", reason=f"on-chain balance for {token} is zero — local record was stale, position cleared")
    if onchain_raw < existing["raw_amount"]:
        print(f"[real_trading] {token}: real_positions.json said {existing['raw_amount']} raw, chain says {onchain_raw} — using the chain figure")
        existing["raw_amount"] = onchain_raw

    cost_basis = existing["cost_basis_sol"] or 1e-9
    fraction = max(0.0, min(1.0, amount_sol / cost_basis))
    raw_to_sell = round(fraction * existing["raw_amount"])
    if raw_to_sell <= 0:
        return _result("blocked", reason="computed sell amount rounds to zero")

    taker = _wallet_pubkey_str()
    order = _get_order(token, WSOL_MINT, raw_to_sell, taker=taker)

    price_impact = float(order.get("priceImpact") or 0)
    if price_impact > MAX_ACCEPTABLE_PRICE_IMPACT_PCT:
        return _result("blocked", reason=f"price impact {price_impact:.2f}% exceeds MAX_ACCEPTABLE_PRICE_IMPACT_PCT ({MAX_ACCEPTABLE_PRICE_IMPACT_PCT}%)")

    tx_b64 = order.get("transaction")
    if not tx_b64:
        return _result("failed", reason="Jupiter returned no transaction to sign (quote may be unroutable)")

    signed_b64 = _sign_transaction(tx_b64)
    exec_result = _execute_order(signed_b64, order["requestId"])

    if exec_result.get("status") != "Success":
        return _result("failed", reason=f"execute returned status={exec_result.get('status')!r}", raw_result=exec_result)

    sol_received_lamports = int(order.get("outAmount") or 0)
    existing["raw_amount"] -= raw_to_sell
    existing["cost_basis_sol"] = max(0.0, existing["cost_basis_sol"] * (1 - fraction))
    existing["sell_signatures"].append(exec_result["signature"])
    if existing["raw_amount"] <= 0:
        positions.pop(token, None)
    else:
        positions[token] = existing
    _save_real_positions(positions)

    return _result(
        "success", signature=exec_result["signature"], tokens_sold=raw_to_sell,
        sol_received=sol_received_lamports / LAMPORTS_PER_SOL, price_impact_pct=price_impact,
        fraction_sold=fraction,
    )


def _sign_transaction(tx_b64: str) -> str:
    from solders.transaction import VersionedTransaction
    keypair = load_keypair()
    raw = base64.b64decode(tx_b64)
    unsigned = VersionedTransaction.from_bytes(raw)
    signed = VersionedTransaction(unsigned.message, [keypair])
    return base64.b64encode(bytes(signed)).decode()


def has_real_position(token: str) -> bool:
    positions = _load_real_positions()
    return token in positions and positions[token].get("raw_amount", 0) > 0
