"""
real_trading.py — real on-chain execution via Jupiter's Ultra Swap API,
isolated from ledger_bot.py's paper-trading logic on purpose: this is the
only module that ever touches SOLANA_PRIVATE_KEY, so keeping it small and
self-contained makes it easy to audit end-to-end.

Trading currency is USDC, not SOL — the wallet holds USDC for the trades
themselves (buys quote USDC->token, sells quote token->USDC). SOL is still
required, unconditionally, for every Solana transaction's network fee
regardless of what the trade itself is denominated in — see
_check_gas_reserve() below, which is checked before every real order.

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
    live USDC balance; every sell checks the live token balance and
    shrinks to whatever's actually there before selling a single unit
    more than truly exists on-chain.

Safety contract (non-negotiable, per the person running this bot):
  - The private key lives ONLY in the SOLANA_PRIVATE_KEY env var. It is
    never written to a file, never printed, never included in an
    exception message, never logged.
  - REAL_TRADING_ENABLED defaults to False and is controlled ONLY by the
    REAL_TRADING_ENABLED env var — flipping it needs no code change and
    no deploy, just a Railway env var edit.
  - Position sizing is dynamic, not a fixed dollar figure: every buy is
    capped at MAX_REAL_POSITION_PCT (default 30%) of the CURRENT live
    on-chain USDC balance, recomputed fresh on every call via
    get_max_real_position_usdc() — never cached, never a stored number.
    Enforced inside execute_real_trade() regardless of what paper-trading
    position sizing calculated. It is never bypassed.
  - MAX_TOTAL_EXPOSURE_PCT (default 85%) is a second, independent ceiling
    on total USDC value committed across ALL open real positions combined
    (existing cost basis, confirmed against the chain) plus the new buy —
    this exists because Sniper Mode can open several positions in quick
    succession, and the per-position cap alone doesn't stop that sequence
    from eventually committing nearly the whole wallet. A minimum reserve
    (100% - MAX_TOTAL_EXPOSURE_PCT) always stays liquid.
  - MAX_REAL_DAILY_USDC is a third, independent ceiling on total real
    USDC spent across ALL positions in a rolling UTC day — a fixed dollar
    figure (not a percentage), since resetting daily doesn't scale with
    balance the way per-position/total-exposure sizing should.
  - MIN_SOL_FOR_GAS is checked before every real order, buy or sell, and
    is unconditional — trading in USDC doesn't make SOL optional, since
    there is no such thing as a gas-free Solana transaction. Falling
    below it refuses the trade outright with a clear reason, logged as
    a "refused" journal entry, instead of letting a transaction fail
    midway for lack of fees.
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

from journal_store import log_journal

REAL_TRADING_ENABLED = os.environ.get("REAL_TRADING_ENABLED", "false").strip().lower() == "true"

# Per-position sizing is a PERCENTAGE of the current live USDC balance,
# not a fixed dollar figure — see get_max_real_position_usdc() below.
# 30% means a sequence of buys tapers geometrically (each is 30% of
# whatever's left), which alone approaches full exposure only in the
# limit; MAX_TOTAL_EXPOSURE_PCT below is the explicit hard backstop.
MAX_REAL_POSITION_PCT = float(os.environ.get("MAX_REAL_POSITION_PCT", "0.30"))
# 85% of total USDC value (live liquid balance + already-committed real
# positions, confirmed against the chain) — see _compute_real_exposure_usdc().
# Leaves a 15% floor always liquid: a buffer for exit slippage and so the
# wallet is never fully committed to open positions at once.
MAX_TOTAL_EXPOSURE_PCT = float(os.environ.get("MAX_TOTAL_EXPOSURE_PCT", "0.85"))
MAX_REAL_DAILY_USDC = float(os.environ.get("MAX_REAL_DAILY_USDC", "6.00"))  # fixed $, independent of the % caps above
# $1 minimum rather than a strict pro-rata conversion of the old SOL
# minimum — Jupiter's platform fee plus network fee eats a large
# fraction of anything much smaller than this, so a sub-$1 "real" fill
# would mostly just be fees.
MIN_REAL_TICKET_USDC = float(os.environ.get("MIN_REAL_TICKET_USDC", "1.00"))
MAX_ACCEPTABLE_PRICE_IMPACT_PCT = 5.0  # skip the trade if Jupiter's quote implies more slippage than this

# Every Solana transaction costs SOL for network fees, full stop — moving
# the trading currency to USDC does not change that. Checked before every
# real order; falling below this refuses the trade outright rather than
# letting it fail midway for lack of gas.
MIN_SOL_FOR_GAS = float(os.environ.get("MIN_SOL_FOR_GAS", "0.01"))

SOLANA_PRIVATE_KEY = os.environ.get("SOLANA_PRIVATE_KEY", "")  # never printed, never logged
SOLANA_WALLET_ADDRESS = os.environ.get("SOLANA_WALLET_ADDRESS", "")  # optional pin — see load_keypair()
JUPITER_API_KEY = os.environ.get("JUPITER_API_KEY", "")
ALCHEMY_RPC_URL = os.environ.get("ALCHEMY_RPC_URL", "")

JUPITER_ULTRA_BASE = "https://api.jup.ag/ultra/v1"
USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
USDC_DECIMALS = 6
USDC_UNITS_PER_USDC = 10 ** USDC_DECIMALS
LAMPORTS_PER_SOL = 1_000_000_000

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


def _check_gas_reserve() -> tuple:
    """
    Checked before every real order, buy or sell, regardless of the
    trade's own currency. Trading in USDC doesn't make SOL optional —
    every Solana transaction needs it for network fees, no exceptions.
    Returns (ok: bool, sol_balance: float). On failure, logs a
    kind="refused" journal entry (the same kind used elsewhere in this
    codebase for a pre-flight refusal, e.g. is_known_stablecoin) rather
    than folding it into the generic "did_real" reporting the caller
    does for other blocked/failed outcomes — a drained gas tank is
    structurally different from "a guard rail didn't like this trade"
    and deserves its own clear record.
    """
    sol_balance = _check_wallet_balance_sol()
    if sol_balance < MIN_SOL_FOR_GAS:
        log_journal(
            kind="refused",
            text=f"Refused real trade — wallet SOL balance ({sol_balance:.4f}) is below the gas reserve floor (MIN_SOL_FOR_GAS={MIN_SOL_FOR_GAS})",
            meta={"sol_balance": sol_balance, "min_sol_for_gas": MIN_SOL_FOR_GAS},
        )
        return False, sol_balance
    return True, sol_balance


def _check_onchain_token_balance_raw(mint: str) -> int:
    """
    The real, on-chain source of truth for how much of `mint` this
    wallet actually holds right now, in raw (smallest-unit) terms —
    summed across every token account for this mint (a mint belongs to
    exactly one token program, so filtering by mint alone already
    covers both the classic and Token-2022 cases; no need to loop over
    program IDs). Used to reconcile real_positions.json before every
    sell (and, for USDC, before every buy) so a stale/drifted local
    record can never cause an attempt to spend or sell more than
    genuinely exists.

    Solana's getTokenAccountsByOwner filter takes EITHER "mint" OR
    "programId", never both in the same filter object — combining them
    silently returns zero accounts rather than erroring, which is
    exactly the shape of bug this function used to have (found live:
    a wallet holding real USDC read back as a $0.00 balance).
    """
    if not ALCHEMY_RPC_URL:
        raise RuntimeError("Set ALCHEMY_RPC_URL env var first.")
    wallet = _wallet_pubkey_str()
    payload = {
        "jsonrpc": "2.0", "id": 1, "method": "getTokenAccountsByOwner",
        "params": [wallet, {"mint": mint}, {"encoding": "jsonParsed"}],
    }
    resp = _request_with_backoff("POST", ALCHEMY_RPC_URL, json=payload, timeout=15)
    resp.raise_for_status()
    accounts = resp.json().get("result", {}).get("value", [])
    total_raw = 0
    for acc in accounts:
        info = acc.get("account", {}).get("data", {}).get("parsed", {}).get("info", {})
        amount_str = info.get("tokenAmount", {}).get("amount")
        if amount_str is not None:
            total_raw += int(amount_str)
    return total_raw


def _check_wallet_balance_usdc() -> float:
    """Live on-chain USDC balance, in dollar terms (not raw units)."""
    return _check_onchain_token_balance_raw(USDC_MINT) / USDC_UNITS_PER_USDC


def get_max_real_position_usdc(live_usdc_balance: float = None) -> float:
    """
    MAX_REAL_POSITION_PCT (default 30%) of the CURRENT live on-chain USDC
    balance — recomputed fresh every time this is called, never cached
    or stored. As the wallet's liquid balance changes (spent into
    positions, topped up, drawn down by a sell), the cap moves with it
    automatically instead of drifting stale relative to a fixed dollar
    figure.

    Pass `live_usdc_balance` when the caller already fetched it this
    same call (as _execute_buy does) to avoid a redundant RPC round
    trip; otherwise this fetches it itself.
    """
    if live_usdc_balance is None:
        live_usdc_balance = _check_wallet_balance_usdc()
    return live_usdc_balance * MAX_REAL_POSITION_PCT


def _compute_real_exposure_usdc(positions: dict) -> float:
    """
    Sums cost_basis_usdc across every tracked real position — but only
    counts a position whose on-chain token balance is confirmed to
    still be > 0 right now. A position closed out through some other
    path (a manual sell, a bug, a previous run's incomplete cleanup)
    shouldn't keep inflating exposure just because real_positions.json
    hasn't caught up. This is the "idealmente confirmado contra a
    chain" half of the total-exposure cap — one getTokenAccountsByOwner
    call per tracked position, which is fine at the position counts a
    bot this size ever realistically holds at once.
    """
    total = 0.0
    for mint, pos in positions.items():
        if mint == "_meta":
            continue
        if pos.get("raw_amount", 0) <= 0:
            continue
        if _check_onchain_token_balance_raw(mint) > 0:
            total += pos.get("cost_basis_usdc", 0.0)
    return total


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


def _spent_today_usdc(positions: dict) -> float:
    meta = positions.get("_meta", {})
    if meta.get("date") != _today_utc():
        return 0.0
    return meta.get("spent_usdc", 0.0)


def _record_spend(positions: dict, usdc_spent: float):
    today = _today_utc()
    meta = positions.get("_meta", {})
    if meta.get("date") != today:
        meta = {"date": today, "spent_usdc": 0.0}
    meta["spent_usdc"] = meta.get("spent_usdc", 0.0) + usdc_spent
    positions["_meta"] = meta


def _record_realized_pnl(positions: dict, delta_usdc: float):
    """
    All-time running total, unlike _record_spend's daily reset — a
    realized gain/loss doesn't expire at UTC midnight the way a daily
    spend cap does.
    """
    meta = positions.get("_meta", {})
    meta["realized_pnl_usdc"] = meta.get("realized_pnl_usdc", 0.0) + delta_usdc
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


def dry_run_quote(token_mint: str, amount_usdc: float, side: str) -> dict:
    """
    SAFE TEST MODE — only calls GET /order, never touches
    SOLANA_PRIVATE_KEY (no `taker` is sent), never signs, never calls
    /execute. Impossible to spend money through this function by
    construction. Use this to sanity-check quotes before ever enabling
    REAL_TRADING_ENABLED.

    amount_usdc is a dollar amount (e.g. 1.5 for $1.50), for both sides —
    on "sell" there's no real holdings tracked in dry-run mode, so this
    just quotes a nominal token-leg amount computed the same way, to
    show the shape of a sell quote too; the number itself is
    illustrative, not a real balance.
    """
    if side == "buy":
        input_mint, output_mint = USDC_MINT, token_mint
        amount_raw = round(amount_usdc * USDC_UNITS_PER_USDC)
    elif side == "sell":
        input_mint, output_mint = token_mint, USDC_MINT
        amount_raw = round(amount_usdc * USDC_UNITS_PER_USDC)
    else:
        raise ValueError("side must be 'buy' or 'sell'")

    order = _get_order(input_mint, output_mint, amount_raw)
    return {
        "side": side,
        "token_mint": token_mint,
        "amount_usdc_requested": amount_usdc,
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


def execute_real_trade(token: str, amount_usdc: float, side: str) -> dict:
    """
    Real execution, with a typed outcome instead of a bare pass/fail —
    adapted from omo's OrderResult (execute.server.ts), which
    distinguishes "no key loaded" from "a guard rail refused this" from
    "attempted and failed" from "filled". That distinction is the whole
    point: an unarmed bot and a broken one look identical from a plain
    boolean, and conflating them either hides a real problem behind a
    routine "not trading today" state, or cries wolf about the reverse.

    amount_usdc is always a dollar amount, for both sides: on "buy" it's
    how much USDC to spend; on "sell" it's the USDC-equivalent slice of
    the position's original cost basis to sell (converted internally to
    a fraction of the real, on-chain-reconciled token balance).

    Statuses:
      "unarmed"  REAL_TRADING_ENABLED is false. Normal, expected, not an
                 error — the caller should report it plainly (once,
                 without alarm) and keep going on paper.
      "blocked"  A guard rail intentionally refused: price impact too
                 high, a cap reached, insufficient/drifted balance,
                 insufficient SOL for gas, a misconfigured wallet pin,
                 nothing to sell. The unwilling-but-correct outcome.
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
        gas_ok, sol_balance = _check_gas_reserve()
        if not gas_ok:
            return _result("blocked", reason=f"wallet SOL balance ({sol_balance:.4f}) is below MIN_SOL_FOR_GAS ({MIN_SOL_FOR_GAS}) — refusing rather than risk a mid-transaction failure for lack of gas")

        if side == "buy":
            return _execute_buy(token, amount_usdc)
        return _execute_sell(token, amount_usdc)
    except RuntimeError as e:
        # Configuration problems (missing key, wallet mismatch, missing
        # RPC/API key) — the guard-rail-style outcome, not a crash.
        return _result("blocked", reason=str(e))
    except Exception as e:
        print(f"[REAL TRADE ERROR] {token} {side} {amount_usdc}: {e}")
        return _result("failed", reason=str(e))


def _execute_buy(token: str, amount_usdc: float) -> dict:
    positions = _load_real_positions()
    existing = positions.get(token, {"raw_amount": 0, "cost_basis_usdc": 0.0, "buy_signatures": [], "sell_signatures": []})

    # Re-derive from the chain rather than trusting only what this file
    # last recorded — the wallet may have been spent from elsewhere, or a
    # prior run may have crashed after signing but before saving state.
    # Fetched once here and reused below for both the position-size cap
    # and the final balance-sufficiency check, instead of two RPC calls.
    live_usdc_balance = _check_wallet_balance_usdc()
    max_position_usdc = get_max_real_position_usdc(live_usdc_balance)

    already_committed = existing["cost_basis_usdc"]
    remaining_position_budget = max_position_usdc - already_committed
    if remaining_position_budget <= 0:
        return _result("blocked", reason=f"MAX_REAL_POSITION_PCT ({MAX_REAL_POSITION_PCT:.0%} of current ${live_usdc_balance:.2f} balance = ${max_position_usdc:.2f}) already committed to {token}")

    spent_today = _spent_today_usdc(positions)
    remaining_daily_budget = MAX_REAL_DAILY_USDC - spent_today
    if remaining_daily_budget <= 0:
        return _result("blocked", reason=f"MAX_REAL_DAILY_USDC (${MAX_REAL_DAILY_USDC:.2f}) already spent today (${spent_today:.2f})")

    # Sniper Mode can open several positions in quick succession — the
    # per-position cap alone doesn't stop that sequence from eventually
    # committing nearly the whole wallet, so this is an independent,
    # explicit ceiling on total USDC value across every open real
    # position combined (existing exposure confirmed against the chain,
    # not just trusted from the file).
    existing_exposure = _compute_real_exposure_usdc(positions)
    total_balance = live_usdc_balance + existing_exposure
    max_total_exposure = total_balance * MAX_TOTAL_EXPOSURE_PCT
    remaining_exposure_headroom = max_total_exposure - existing_exposure
    if remaining_exposure_headroom <= 0:
        return _result("blocked", reason=f"MAX_TOTAL_EXPOSURE_PCT ({MAX_TOTAL_EXPOSURE_PCT:.0%} of ${total_balance:.2f} total = ${max_total_exposure:.2f}) already committed across open real positions")

    clamped_amount_usdc = min(amount_usdc, remaining_position_budget, remaining_daily_budget, remaining_exposure_headroom)
    if clamped_amount_usdc < MIN_REAL_TICKET_USDC:
        return _result("blocked", reason=f"clamped size ${clamped_amount_usdc:.2f} is below MIN_REAL_TICKET_USDC (${MIN_REAL_TICKET_USDC:.2f})")

    if live_usdc_balance < clamped_amount_usdc:
        return _result("blocked", reason=f"live wallet USDC balance (${live_usdc_balance:.2f}) is insufficient for a ${clamped_amount_usdc:.2f} buy")

    taker = _wallet_pubkey_str()
    amount_raw = round(clamped_amount_usdc * USDC_UNITS_PER_USDC)
    order = _get_order(USDC_MINT, token, amount_raw, taker=taker)

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
    existing["cost_basis_usdc"] += clamped_amount_usdc
    existing["buy_signatures"].append(exec_result["signature"])
    positions[token] = existing
    _record_spend(positions, clamped_amount_usdc)
    _save_real_positions(positions)

    return _result(
        "success", signature=exec_result["signature"], usdc_spent=clamped_amount_usdc,
        tokens_received=tokens_received, price_impact_pct=price_impact,
    )


def _execute_sell(token: str, amount_usdc: float) -> dict:
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

    cost_basis = existing["cost_basis_usdc"] or 1e-9
    fraction = max(0.0, min(1.0, amount_usdc / cost_basis))
    raw_to_sell = round(fraction * existing["raw_amount"])
    if raw_to_sell <= 0:
        return _result("blocked", reason="computed sell amount rounds to zero")

    taker = _wallet_pubkey_str()
    order = _get_order(token, USDC_MINT, raw_to_sell, taker=taker)

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

    usdc_received_raw = int(order.get("outAmount") or 0)
    usdc_received = usdc_received_raw / USDC_UNITS_PER_USDC
    # cost_basis (captured above, before the reduction below) times the
    # fraction just sold is what was originally paid for exactly this
    # slice — proceeds minus that is this sell's realized gain/loss,
    # accumulated into positions["_meta"]["realized_pnl_usdc"] so the
    # dashboard has real, not paper, P&L to show.
    _record_realized_pnl(positions, usdc_received - cost_basis * fraction)
    existing["raw_amount"] -= raw_to_sell
    existing["cost_basis_usdc"] = max(0.0, existing["cost_basis_usdc"] * (1 - fraction))
    existing["sell_signatures"].append(exec_result["signature"])
    if existing["raw_amount"] <= 0:
        positions.pop(token, None)
    else:
        positions[token] = existing
    _save_real_positions(positions)

    return _result(
        "success", signature=exec_result["signature"], tokens_sold=raw_to_sell,
        usdc_received=usdc_received, price_impact_pct=price_impact,
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


# ── Read-only accessors for external callers (e.g. api_server.py's ──
# ── dashboard endpoint) — public (no leading underscore) since these ──
# ── are meant to be called from outside this module. ─────────────────

def get_realized_pnl_usdc() -> float:
    """All-time realized P&L across every real sell, ever."""
    positions = _load_real_positions()
    return positions.get("_meta", {}).get("realized_pnl_usdc", 0.0)


def get_open_real_positions_summary() -> list:
    """
    Open real positions for DISPLAY — trusts real_positions.json
    directly, no live on-chain reconciliation per position. Deliberately
    cheap (zero RPC calls) so a dashboard can poll this every few
    seconds without hammering the RPC provider or Jupiter. The actual
    trading path (_execute_sell, _compute_real_exposure_usdc) always
    re-derives from the chain before acting on a position; this is
    read-only display data, not something a trading decision is made
    from, so the file's own bookkeeping is close enough.
    """
    positions = _load_real_positions()
    return [
        {"mint": mint, "cost_basis_usdc": pos.get("cost_basis_usdc", 0.0), "raw_amount": pos.get("raw_amount", 0)}
        for mint, pos in positions.items()
        if mint != "_meta" and pos.get("raw_amount", 0) > 0
    ]


def get_wallet_balances() -> dict:
    """
    Live on-chain USDC and SOL balances, for display. Unlike the
    position summary above, this DOES hit the RPC (both balances are
    single cheap calls, not one-per-position) — a balance figure is
    exactly the kind of thing that's misleading if stale, so it's worth
    the round trip on every dashboard poll. Raises if SOLANA_PRIVATE_KEY
    or ALCHEMY_RPC_URL aren't configured; callers should catch that.
    """
    return {"usdc": _check_wallet_balance_usdc(), "sol": _check_wallet_balance_sol()}
