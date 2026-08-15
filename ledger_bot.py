"""
Ledger — Solana memecoin trench agent (paper trading scaffold)

What this does right now:
  - Watches a list of wallet addresses for new buys (via Helius)
  - Checks each watched wallet's TOTAL portfolio value (SOL + every
    token held, priced in USD via Jupiter) and flags whales — accounts
    at 6-figure+ total value — weighting their buys as stronger signals
  - Turns each detected buy into a "thesis" in Ledger's voice
  - Speaks publicly: every thesis, trade, and exit posts live to a
    Discord channel via webhook (see DISCORD_WEBHOOK_URL) under
    Ledger's name — this is optional; leave it unset to run silently
  - Simulates a trade against a paper balance (NO real funds move)
  - Enforces hard risk limits so the logic is proven safe before
    it ever touches a real wallet

What this does NOT do yet:
  - Execute real trades (that's a deliberate later step — see
    execute_real_trade() stub at the bottom, disabled by default)
  - Read FOMO app callout text (no public API for that — see notes)

Env vars required:
  HELIUS_API_KEY   - from https://helius.dev (free tier is fine to start)

Install:
  pip install requests --break-system-packages
"""

import os
import json
import time
import requests
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

# ── Config ────────────────────────────────────────────────────────────

HELIUS_API_KEY = os.environ.get("HELIUS_API_KEY", "")
HELIUS_BASE_URL = "https://api.helius.xyz/v0"
HELIUS_RPC_URL = f"https://mainnet.helius-rpc.com/?api-key={HELIUS_API_KEY}"

# Discord webhook URL — this is Ledger's public voice. Get one from
# a Discord channel: Edit Channel -> Integrations -> Webhooks -> New
# Webhook -> Copy URL. Leave blank to run silently (console only).
DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL", "")
LEDGER_DISCORD_NAME = "Ledger"
LEDGER_DISCORD_AVATAR_URL = os.environ.get("LEDGER_AVATAR_URL", "")  # optional

# Wallets with a TOTAL portfolio value (SOL + all tokens, in USD) at
# or above this are treated as "whale" wallets — their buys get
# weighted as stronger signals in the thesis. Targeting 6-figure+
# accounts, not just wallets sitting on raw SOL.
WHALE_VALUE_USD_THRESHOLD = 100_000

JUPITER_PRICE_API = "https://lite-api.jup.ag/price/v3"

# Wallets to watch now live in wallets.json, not here — edit that file
# to add/remove/swap tracked traders without touching this code.
WALLETS_CONFIG_FILE = Path("wallets.json")


def speak(message: str, embed_extra: dict = None):
    """
    Ledger's public voice. Prints to console always, and — if
    DISCORD_WEBHOOK_URL is configured — also posts to Discord under
    his name/avatar. Silently skips Discord if not configured, so this
    is safe to call everywhere without breaking anything.
    """
    print(message)
    if not DISCORD_WEBHOOK_URL:
        return
    payload = {
        "username": LEDGER_DISCORD_NAME,
        "content": message,
    }
    if LEDGER_DISCORD_AVATAR_URL:
        payload["avatar_url"] = LEDGER_DISCORD_AVATAR_URL
    if embed_extra:
        payload["embeds"] = [embed_extra]
    try:
        requests.post(DISCORD_WEBHOOK_URL, json=payload, timeout=10)
    except Exception as e:
        # Never let a Discord hiccup take down the trading loop — this
        # is called from critical paths (stop-loss, position opens),
        # so it must fail silently and let the bot keep running.
        print(f"[WARN] Discord post failed: {e}")


def load_wallets():
    """
    Loads the watchlist from wallets.json.
    Returns (watched_wallets: list[str], wallet_handles: dict[str, str]).
    """
    if not WALLETS_CONFIG_FILE.exists():
        print(f"[WARN] {WALLETS_CONFIG_FILE} not found — no wallets loaded.")
        return [], {}

    data = json.loads(WALLETS_CONFIG_FILE.read_text())
    entries = data.get("wallets", [])
    watched = [e["address"] for e in entries]
    handles = {e["address"]: e["handle"] for e in entries}
    return watched, handles


WATCHED_WALLETS, WALLET_HANDLES = load_wallets()

# Poll interval — how often to check each wallet for new buys.
# Kept conservative (2 min) to stay comfortably inside Helius's free
# tier limits with 30+ wallets tracked. Free tier is enough for this
# — no need to pay, just don't hammer it.
POLL_SECONDS = 120

# Balance checks (for whale ranking) are expensive relative to how
# often they actually change — no need to check every cycle. This
# checks balances once every N cycles instead of every single one.
# NOTE: Helius's Wallet API balances endpoint costs 100 credits PER
# CALL (their pricing) — with 32 wallets, that's 3,200 credits every
# time this runs. Checking once a day (rather than every 20 min) keeps
# this comfortably inside free-tier monthly credit limits.
BALANCE_RECHECK_EVERY_N_CYCLES = 720  # ~once/day at 120s/cycle

# ── Risk limits (hard-coded, not suggestions) ───────────────────────────

MAX_POSITION_SOL = 0.5        # max size of any single paper position
MAX_DAILY_LOSS_SOL = 2.0      # bot stops opening new positions past this
MAX_TRADES_PER_HOUR = 10      # circuit breaker against runaway logic
STARTING_PAPER_BALANCE_SOL = 20.0

# ── State ────────────────────────────────────────────────────────────

STATE_FILE = Path("ledger_state.json")


@dataclass
class PaperPosition:
    token: str
    entry_price: float
    size_sol: float
    opened_at: str
    opened_by: str = ""  # wallet address that triggered this position
    symbol: str = ""  # resolved ticker/name, e.g. "$BONK" — may be empty if unresolved
    initial_recovered: bool = False   # has the original capital been sold back out?
    next_scaleout_multiple: float = 2.0  # entry-price multiple for the next 25% trim


@dataclass
class LedgerState:
    balance_sol: float = STARTING_PAPER_BALANCE_SOL
    realized_pnl_sol: float = 0.0
    open_positions: dict = field(default_factory=dict)
    trade_log: list = field(default_factory=list)
    trades_this_hour: list = field(default_factory=list)  # timestamps
    seen_signatures: list = field(default_factory=list)   # avoid re-processing the same tx

    def save(self):
        # Cap seen_signatures so this doesn't grow forever
        self.seen_signatures = self.seen_signatures[-2000:]
        STATE_FILE.write_text(json.dumps(self.__dict__, indent=2, default=str))

    @classmethod
    def load(cls):
        if STATE_FILE.exists():
            data = json.loads(STATE_FILE.read_text())
            return cls(**data)
        return cls()


def get_wallet_total_value_usd(wallet_address: str) -> float:
    """
    Total portfolio value in USD via Helius's Wallet API (v1) —
    replaces the old v0 balances endpoint, which was retired and now
    returns 404. The v1 endpoint conveniently returns totalUsdValue
    directly (already summed across SOL + every token), sourced from
    Helius's own DAS pricing — no separate price lookup needed for
    this. NOTE: this endpoint costs 100 credits per call (Helius's
    pricing, not ours) — that's why balance checks are deliberately
    infrequent (see BALANCE_RECHECK_EVERY_N_CYCLES) rather than every
    poll cycle, to stay well within free-tier monthly credits.
    """
    if not HELIUS_API_KEY:
        raise RuntimeError("Set HELIUS_API_KEY env var first.")
    url = f"https://api.helius.xyz/v1/wallet/{wallet_address}/balances"
    headers = {"X-Api-Key": HELIUS_API_KEY}
    resp = request_with_backoff("GET", url, headers=headers, timeout=15)
    data = resp.json()
    return data.get("totalUsdValue", 0.0)


def get_token_prices_usd(mints: list) -> dict:
    """
    Batch price lookup via Jupiter's Price API V3 (free tier, no key
    needed via lite-api.jup.ag). Replaces the old V2/V6 endpoints,
    which were fully deprecated and now refuse connections. Returns
    {mint: price_usd}. Missing/unlisted tokens (a lot of brand-new
    memecoins won't have a price here) are just omitted — treated as
    $0 contribution, a reasonable simplification for illiquid tokens.
    """
    if not mints:
        return {}
    prices = {}
    CHUNK = 50  # V3 docs example batches are smaller; staying conservative
    for i in range(0, len(mints), CHUNK):
        chunk = mints[i:i + CHUNK]
        try:
            resp = requests.get(JUPITER_PRICE_API, params={"ids": ",".join(chunk)}, timeout=15)
            resp.raise_for_status()
            data = resp.json()  # V3 returns {mint: {usdPrice, ...}} directly, no "data" wrapper
            for mint, info in data.items():
                if info and "usdPrice" in info:
                    prices[mint] = float(info["usdPrice"])
        except requests.exceptions.RequestException as e:
            print(f"[WARN] price lookup failed for a chunk: {e}")
    return prices


SOL_MINT = "So11111111111111111111111111111111111111112"


def rank_wallets_by_balance(wallets: list) -> list:
    """
    Returns [(wallet_address, handle, total_value_usd), ...] sorted
    richest first, based on TOTAL portfolio value (SOL + all tokens),
    not just raw SOL. Small delay between each wallet to spread out
    requests and stay well within free-tier rate limits.
    """
    ranked = []
    for wallet in wallets:
        try:
            value = get_wallet_total_value_usd(wallet)
        except Exception as e:
            print(f"[ERROR] value check {wallet}: {e}")
            value = 0.0
        ranked.append((wallet, WALLET_HANDLES.get(wallet, wallet[:6] + "..."), value))
        time.sleep(0.3)  # spread requests out instead of firing all at once
    ranked.sort(key=lambda x: x[2], reverse=True)
    return ranked


def request_with_backoff(method, url, max_retries=5, **kwargs):
    """
    Wraps a requests call with exponential backoff on rate-limit (429)
    responses — instead of hammering Helius and getting blocked
    harder, it waits progressively longer (2s, 4s, 8s...) and retries.
    This is what keeps a free-tier setup usable without paying.
    """
    delay = 2
    for attempt in range(max_retries):
        resp = requests.request(method, url, **kwargs)
        if resp.status_code != 429:
            resp.raise_for_status()
            return resp
        print(f"[RATE LIMIT] hit 429, waiting {delay}s before retry ({attempt + 1}/{max_retries})...")
        time.sleep(delay)
        delay *= 2
    resp.raise_for_status()  # give up, raise whatever the last response was
    return resp


def get_token_metadata(mint: str) -> dict:
    """
    Resolves a mint address to its real symbol/name via Helius's DAS
    API (getAsset) — this is what turns an unreadable address like
    'H3mqq7...' into something like '$MOONCAT'. Returns {"symbol": ...,
    "name": ...}, with empty strings if metadata isn't available (very
    new/unlisted tokens sometimes have no metadata yet).
    """
    if not HELIUS_API_KEY:
        return {"symbol": "", "name": ""}
    payload = {
        "jsonrpc": "2.0",
        "id": "ledger",
        "method": "getAsset",
        "params": {"id": mint},
    }
    try:
        resp = request_with_backoff("POST", HELIUS_RPC_URL, json=payload, timeout=15)
        data = resp.json()
        content = data.get("result", {}).get("content", {})
        metadata = content.get("metadata", {})
        return {
            "symbol": metadata.get("symbol", ""),
            "name": metadata.get("name", ""),
        }
    except Exception as e:
        print(f"[WARN] metadata lookup failed for {mint}: {e}")
        return {"symbol": "", "name": ""}


# ── Wallet watching ──────────────────────────────────────────────────

def get_wallet_transactions(wallet_address: str, limit: int = 10):
    """Pull recent transactions for a wallet via Helius."""
    if not HELIUS_API_KEY:
        raise RuntimeError("Set HELIUS_API_KEY env var first.")
    url = f"{HELIUS_BASE_URL}/addresses/{wallet_address}/transactions"
    params = {"api-key": HELIUS_API_KEY, "limit": limit}
    resp = request_with_backoff("GET", url, params=params, timeout=15)
    return resp.json()


def extract_new_buys(transactions: list, wallet_address: str) -> list:
    """
    Filter transactions down to real token buys, based on the actual
    Helius tokenTransfers structure (confirmed against live data):

        tokenTransfers: [
            { fromUserAccount, toUserAccount, mint, tokenAmount, ... },
            ...
        ]

    A "buy" = the watched wallet receives (toUserAccount) a token that
    isn't SOL/USDC/USDT — i.e. it swapped something stable/SOL for a
    new token. Only looks at type == "SWAP" transactions; TRANSFER and
    UNKNOWN types are skipped (confirmed via live testing these are
    mostly plain transfers, not trades).
    """
    STABLE_OR_SOL_MINTS = {
        "So11111111111111111111111111111111111111112",  # wrapped SOL
        "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",  # USDC
        "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB",  # USDT
    }

    buys = []
    for tx in transactions:
        if tx.get("type") != "SWAP":
            continue

        for transfer in tx.get("tokenTransfers", []):
            if (
                transfer.get("toUserAccount") == wallet_address
                and transfer.get("mint") not in STABLE_OR_SOL_MINTS
            ):
                buys.append({
                    "signature": tx.get("signature"),
                    "mint": transfer.get("mint"),
                    "amount": transfer.get("tokenAmount"),
                    "source": tx.get("source"),
                })

    return buys


# ── Ledger's voice ───────────────────────────────────────────────────

def generate_thesis(token: str, wallet: str, signal_strength: str) -> str:
    """
    Placeholder thesis generator. Swap this for a real call to an LLM
    (e.g. the Anthropic API) once you want genuinely reasoned theses
    instead of templated ones — this keeps the scaffold runnable
    without an extra API dependency for now.
    """
    name = WALLET_HANDLES.get(wallet, wallet[:6] + "...")
    templates = {
        "strong": (
            f"{name} just aped into ${token} and this wallet's "
            f"been right more than wrong. Volume's confirming, not just "
            f"one guy's bag. Sizing in — scout position, not full send."
        ),
        "weak": (
            f"{name} bought ${token} but it's a thin signal on "
            f"its own. Watching, not touching yet — need volume or a "
            f"second wallet to confirm before I'm in."
        ),
    }
    return templates.get(signal_strength, templates["weak"])


# ── Paper trading engine ─────────────────────────────────────────────

def can_open_position(state: LedgerState, size_sol: float) -> tuple[bool, str]:
    if size_sol > MAX_POSITION_SOL:
        return False, f"size {size_sol} exceeds MAX_POSITION_SOL ({MAX_POSITION_SOL})"
    if state.realized_pnl_sol <= -MAX_DAILY_LOSS_SOL:
        return False, "daily loss limit hit, no new positions"
    if size_sol > state.balance_sol:
        return False, "insufficient paper balance"

    now = time.time()
    state.trades_this_hour = [t for t in state.trades_this_hour if now - t < 3600]
    if len(state.trades_this_hour) >= MAX_TRADES_PER_HOUR:
        return False, "hourly trade limit hit"

    return True, "ok"


def open_paper_position(state: LedgerState, token: str, price: float, size_sol: float, opened_by: str = ""):
    ok, reason = can_open_position(state, size_sol)
    if not ok:
        print(f"[BLOCKED] {token}: {reason}")
        return

    metadata = get_token_metadata(token)
    symbol = f"${metadata['symbol']}" if metadata.get("symbol") else ""

    state.balance_sol -= size_sol
    state.open_positions[token] = PaperPosition(
        token=token,
        entry_price=price,
        size_sol=size_sol,
        opened_at=datetime.now(timezone.utc).isoformat(),
        opened_by=opened_by,
        symbol=symbol,
    ).__dict__
    state.trades_this_hour.append(time.time())
    state.trade_log.append({
        "action": "open",
        "token": token,
        "symbol": symbol,
        "price": price,
        "size_sol": size_sol,
        "opened_by": opened_by,
        "at": datetime.now(timezone.utc).isoformat(),
    })
    display_name = symbol if symbol else token
    speak(f"[PAPER OPEN] {display_name} @ {price} size {size_sol} SOL")
    state.save()


def partial_close_paper_position(state: LedgerState, token: str, exit_price: float, fraction: float):
    """
    Sells a FRACTION of the current remaining position, not the whole
    thing — used for scaling out (recover initial capital, then trim
    on the way up) instead of an all-or-nothing exit.
    """
    pos = state.open_positions.get(token)
    if not pos:
        return

    sell_size = pos["size_sol"] * fraction
    pnl = (exit_price - pos["entry_price"]) / pos["entry_price"] * sell_size
    state.balance_sol += sell_size + pnl
    state.realized_pnl_sol += pnl
    pos["size_sol"] -= sell_size

    state.trade_log.append({
        "action": "partial_close",
        "token": token,
        "exit_price": exit_price,
        "fraction_sold": fraction,
        "pnl_sol": pnl,
        "at": datetime.now(timezone.utc).isoformat(),
    })
    speak(f"[SCALE OUT] {token} @ {exit_price} sold {fraction:.0%} of position, pnl {pnl:+.4f} SOL, {pos['size_sol']:.4f} SOL remaining")

    if pos["size_sol"] < 0.001:  # fully drained — close it out entirely
        del state.open_positions[token]
    else:
        state.open_positions[token] = pos
    state.save()


def close_paper_position(state: LedgerState, token: str, exit_price: float):
    pos = state.open_positions.pop(token, None)
    if not pos:
        return
    pnl = (exit_price - pos["entry_price"]) / pos["entry_price"] * pos["size_sol"]
    state.balance_sol += pos["size_sol"] + pnl
    state.realized_pnl_sol += pnl
    state.trade_log.append({
        "action": "close",
        "token": token,
        "exit_price": exit_price,
        "pnl_sol": pnl,
        "at": datetime.now(timezone.utc).isoformat(),
    })
    speak(f"[PAPER CLOSE] {token} @ {exit_price} pnl {pnl:+.4f} SOL")
    state.save()


# Exit rules — coherent with Ledger's "balanced" persona: cuts losers
# fast, recovers initial capital early, then lets the rest ride and
# trims into strength instead of an all-or-nothing exit.
STOP_LOSS_PCT = -0.25            # close everything if down 25%
INITIAL_RECOVERY_PCT = 0.40      # at +40%, sell enough to recoup the original capital
SCALEOUT_FRACTION = 0.25         # after that, trim 25% of what's left at each further 2x


def check_open_positions(state: LedgerState):
    """
    Checks every open paper position against current price and applies
    the staged exit strategy:
      1. Stop-loss: down 25% -> close the whole thing, no hesitation.
      2. At +40%: sell just enough to recover the original capital
         (position keeps running "on house money" after this).
      3. From there, every further 2x (from entry price) -> trim 25%
         of whatever's left, letting a trimmed core keep riding.
    Run this every cycle so positions aren't left unmonitored.
    """
    if not state.open_positions:
        return

    mints = list(state.open_positions.keys())
    prices = get_token_prices_usd(mints)

    for mint in mints:
        pos = state.open_positions.get(mint)
        if not pos:
            continue  # may have been fully closed already this pass
        current_price = prices.get(mint)
        if current_price is None:
            continue  # no price data this cycle, check again next cycle

        change_pct = (current_price - pos["entry_price"]) / pos["entry_price"]
        EPSILON = 1e-9  # avoids floating-point precision misses at exact thresholds

        if change_pct <= STOP_LOSS_PCT + EPSILON:
            speak(f"[STOP LOSS] {mint} down {change_pct:.1%}, cutting it — no hesitation.")
            close_paper_position(state, mint, current_price)
            continue

        if not pos["initial_recovered"]:
            if change_pct >= INITIAL_RECOVERY_PCT - EPSILON:
                # Sell exactly the fraction whose proceeds equal the
                # original capital — e.g. at 1.4x, selling 1/1.4 of
                # the position returns exactly the initial size_sol.
                current_multiple = 1 + change_pct
                fraction_to_recover_capital = 1 / current_multiple
                speak(f"[INITIAL OUT] {mint} up {change_pct:.1%} — pulling the initial capital back out.")
                partial_close_paper_position(state, mint, current_price, fraction_to_recover_capital)
                if mint in state.open_positions:
                    state.open_positions[mint]["initial_recovered"] = True
                    state.open_positions[mint]["next_scaleout_multiple"] = 2.0
                    state.save()
        else:
            next_multiple = pos["next_scaleout_multiple"]
            if current_price >= pos["entry_price"] * next_multiple - EPSILON:
                speak(f"[SCALEOUT TRIGGER] {mint} hit {next_multiple}x entry — trimming 25% of what's left.")
                partial_close_paper_position(state, mint, current_price, SCALEOUT_FRACTION)
                if mint in state.open_positions:
                    state.open_positions[mint]["next_scaleout_multiple"] = next_multiple * 2
                    state.save()


# ── Real execution (disabled — future step, not wired up yet) ───────

REAL_TRADING_ENABLED = False  # flip only after paper track record + your own review

def execute_real_trade(*args, **kwargs):
    if not REAL_TRADING_ENABLED:
        raise RuntimeError(
            "Real trading is disabled. This is intentional — flip "
            "REAL_TRADING_ENABLED only after you've reviewed a paper "
            "trading track record and wired up wallet signing (e.g. "
            "via solders + Jupiter swap API) yourself, with the same "
            "risk limits enforced on the real path too."
        )
    # Real implementation would go here: build swap tx via Jupiter API,
    # sign with the bot's dedicated wallet keypair, send via RPC.


# ── Main loop ─────────────────────────────────────────────────────────

def main():
    state = LedgerState.load()
    print(f"Ledger booting up. Paper balance: {state.balance_sol} SOL")

    whale_wallets = set()
    cycle_count = 0

    while True:
        cycle_count += 1

        # Reload every cycle — edit wallets.json anytime, no restart needed
        watched_wallets, wallet_handles = load_wallets()
        globals()["WALLET_HANDLES"] = wallet_handles  # generate_thesis() reads this

        if not watched_wallets:
            print(f"No wallets in {WALLETS_CONFIG_FILE} — add some and it'll pick them up next cycle.")
            time.sleep(POLL_SECONDS)
            continue

        # Balance/whale check is expensive — only do it occasionally,
        # not every single cycle, to keep well within free-tier limits
        if cycle_count == 1 or cycle_count % BALANCE_RECHECK_EVERY_N_CYCLES == 0:
            print(f"Checking total portfolio value for {len(watched_wallets)} wallets to flag whales...")
            ranked = rank_wallets_by_balance(watched_wallets)
            whale_wallets = {w for w, _, val in ranked if val >= WHALE_VALUE_USD_THRESHOLD}
            for wallet, handle, val in ranked[:5]:
                tag = " [WHALE]" if wallet in whale_wallets else ""
                print(f"  {handle}: ${val:,.0f}{tag}")

        for wallet in watched_wallets:
            try:
                txs = get_wallet_transactions(wallet)
                buys = extract_new_buys(txs, wallet)
                for buy in buys:
                    if buy["signature"] in state.seen_signatures:
                        continue  # already processed this exact transaction
                    state.seen_signatures.append(buy["signature"])

                    token = buy["mint"]
                    strength = "strong" if wallet in whale_wallets else "weak"
                    thesis = generate_thesis(token, wallet, strength)

                    embed = {
                        "title": f"New thesis {'💎' if strength == 'strong' else '👀'}",
                        "description": thesis,
                        "color": 0x22c55e if strength == "strong" else 0x94a3b8,
                        "fields": [
                            {"name": "Mint", "value": token, "inline": False},
                            {"name": "Signal", "value": strength, "inline": True},
                        ],
                    }
                    speak(f"[THESIS] {thesis}\n  mint: {token} | amount: {buy['amount']} | source: {buy['source']}", embed_extra=embed)

                    prices = get_token_prices_usd([token])
                    entry_price = prices.get(token)
                    if entry_price is None:
                        print(f"  [SKIP] no price data for {token} yet — can't size a position.")
                        continue

                    # Whale-backed signals get sized bigger; everything
                    # else is a small scout position, capped either way.
                    size_sol = min(0.2 if strength == "strong" else 0.05, MAX_POSITION_SOL)
                    open_paper_position(state, token, entry_price, size_sol, opened_by=wallet)
            except Exception as e:
                print(f"[ERROR] wallet {wallet}: {e}")
            time.sleep(0.3)  # spread requests out across the cycle

        check_open_positions(state)
        state.save()
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    main()
