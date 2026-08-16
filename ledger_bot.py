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

Optional:
  ANTHROPIC_API_KEY - enables periodic market/trend research (see
                       run_market_research below). Without it, that
                       part is silently skipped — everything else
                       still works.
  BIRDEYE_API_KEY   - enables real chart/market-structure analysis
                       (higher-highs/higher-lows, break of structure)
                       fed into conviction analysis. Free tier at
                       birdeye.so. Without it, conviction analysis
                       just skips the chart-structure input.

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

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
ANTHROPIC_API_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_MODEL = "claude-sonnet-5"

BIRDEYE_API_KEY = os.environ.get("BIRDEYE_API_KEY", "")
BIRDEYE_OHLCV_URL = "https://public-api.birdeye.so/defi/v3/ohlcv"

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

# Maps Helius's internal source codes to readable platform names —
# without this, the "Source" field would show raw codes like
# "PUMP_AMM" or worse, unrecognized values, instead of "Pump.fun".
SOURCE_DISPLAY_NAMES = {
    "PUMP_AMM": "Pump.fun",
    "PUMPFUN": "Pump.fun",
    "RAYDIUM": "Raydium",
    "ORCA": "Orca",
    "JUPITER": "Jupiter",
    "METEORA": "Meteora",
    "PHOENIX": "Phoenix",
    "OPENBOOK": "OpenBook",
}


def get_source_display_name(source_code: str) -> str:
    """Turns a raw Helius source code into a readable platform name."""
    if not source_code:
        return "Unknown Platform"
    return SOURCE_DISPLAY_NAMES.get(
        source_code.upper(),
        source_code.replace("_", " ").title(),  # graceful fallback for unmapped codes
    )

# Wallets to watch now live in wallets.json, not here — edit that file
# to add/remove/swap tracked traders without touching this code.
WALLETS_CONFIG_FILE = Path("wallets.json")


# Consistent color palette for Ledger's Discord embeds
COLOR_BUY = 0x3b82f6       # blue — new position opened
COLOR_PROFIT = 0x22c55e    # green — profit taken/locked in
COLOR_LOSS = 0xef4444      # red — stop-loss / losing close
COLOR_NEUTRAL = 0x64748b   # slate — informational
COLOR_STRONG_SIGNAL = 0xf59e0b  # amber — whale-backed thesis


def speak(title: str, description: str, color: int = COLOR_NEUTRAL, fields: list = None):
    """
    Ledger's public voice. Always prints a plain-text line to the
    console (for logs), and — if DISCORD_WEBHOOK_URL is configured —
    posts a normal Discord message (not an embed/card) built from
    markdown, in the requested layout: bold title, then each field
    as its own bold-labeled section. `color` is accepted but unused
    for Discord now (plain messages have no color) — kept so callers
    don't need changes.
    """
    print(f"{title} — {description}")

    lines = [f"**{title}**"]
    if description:
        lines.append(description)
    if fields:
        for f in fields:
            if "\n" in f["value"]:
                # Multi-line value (e.g. a bulleted notes block) — label
                # on its own line, content below it.
                lines.append(f"**{f['name']}**\n{f['value']}")
            else:
                # Single-line value — label and value together, matching
                # the requested "**CA:** address" / "**Thesis** text" style.
                lines.append(f"**{f['name']}** {f['value']}")
    text = "\n\n".join(lines)

    if not DISCORD_WEBHOOK_URL:
        return

    # Discord's message content cap is 2000 chars — trim defensively
    # so an unusually long thesis can't silently fail to send.
    if len(text) > 1990:
        text = text[:1987] + "..."

    payload = {
        "username": LEDGER_DISCORD_NAME,
        "content": text,
    }
    if LEDGER_DISCORD_AVATAR_URL:
        payload["avatar_url"] = LEDGER_DISCORD_AVATAR_URL
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
    Returns (watched_wallets, wallet_handles, priority_wallets):
      - watched_wallets: list[str]
      - wallet_handles: dict[str, str]
      - priority_wallets: set[str] — wallets marked "priority": true in
        the config get treated as maximum-conviction signals always,
        regardless of portfolio value. Use this for traders whose
        track record/influence is trusted independent of net worth
        (e.g. a well-known figure whose calls move markets).
    """
    if not WALLETS_CONFIG_FILE.exists():
        print(f"[WARN] {WALLETS_CONFIG_FILE} not found — no wallets loaded.")
        return [], {}, set()

    data = json.loads(WALLETS_CONFIG_FILE.read_text())
    entries = data.get("wallets", [])
    watched = [e["address"] for e in entries]
    handles = {e["address"]: e["handle"] for e in entries}
    priority = {e["address"] for e in entries if e.get("priority")}
    return watched, handles, priority


WATCHED_WALLETS, WALLET_HANDLES, PRIORITY_WALLETS = load_wallets()

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

# Position size scales continuously with conviction (the risk_score
# from analyze_conviction), between these two bounds — a risk_score
# of 0 (safest) gets MAX_CONVICTION_SIZE_SOL, a risk_score of 10
# (riskiest that still passed) gets MIN_SCOUT_SIZE_SOL.
MIN_SCOUT_SIZE_SOL = 0.02
MAX_CONVICTION_SIZE_SOL = 0.25

# ── State ────────────────────────────────────────────────────────────

STATE_FILE = Path("ledger_state.json")


@dataclass
class PaperPosition:
    token: str
    entry_price: float
    size_sol: float
    opened_at: str
    opened_by: str = ""  # wallet address that triggered this position
    symbol: str = ""  # resolved ticker/name, e.g. "BONK" (no $ prefix) — may be empty if unresolved
    risk_level: str = "🟡 High"  # 🟢 Lower (whale-backed) or 🟡 High (scout/unconfirmed)
    initial_recovered: bool = False   # has the original capital been sold back out?
    peak_price: float = 0.0  # highest price seen since initial capital was recovered — powers the trailing stop
    is_narrative: bool = False  # matched an active viral narrative (see NARRATIVES_FILE) — gets a wider trailing stop


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
    Resolves a mint address to its real symbol/name/description via
    Helius's DAS API (getAsset) — this is what turns an unreadable
    address like 'H3mqq7...' into something like 'MOONCAT', and pulls
    the coin's "lore" (its description, when the launch included one)
    for conviction analysis to reference. Returns {"symbol": ...,
    "name": ..., "description": ...}, with empty strings if metadata
    isn't available (very new/unlisted tokens sometimes have none yet).
    """
    if not HELIUS_API_KEY:
        return {"symbol": "", "name": "", "description": ""}
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
            "description": metadata.get("description", ""),
        }
    except Exception as e:
        print(f"[WARN] metadata lookup failed for {mint}: {e}")
        return {"symbol": "", "name": "", "description": ""}


# ── Market research (merged from the standalone market_intel.py) ─────
#
# This used to run as a separate Railway service. It's folded in here
# instead because Railway doesn't share files between services — a
# separate service writing market_intel.json couldn't be read by this
# one anyway. Running it in the same process as the trading loop means
# narrative detection below actually has real data to check against,
# with no extra infrastructure. (If you still have a standalone
# ledger-market-intel service running, it's now redundant — safe to
# pause/delete it, since this replaces what it did.)

INTEL_FILE = Path("market_intel.json")
MAX_INTEL_ENTRIES_KEPT = 30
MARKET_RESEARCH_EVERY_N_CYCLES = 120  # ~every 4 hours at 120s/cycle

MARKET_RESEARCH_PROMPT = """You're researching the current state of the \
Solana memecoin market for a trader persona named Ledger. Search for \
what's happening RIGHT NOW — the last 24-48 hours — and summarize in \
a tight, practical way:

1. What's the overall market mood in the Solana trenches (risk-on / \
risk-off / choppy)?
2. Any narrative or "meta" that's clearly rotating in or out right now?
3. Any major single events worth knowing (a big rug, a major KOL call, \
a notable launch)?
4. Any viral internet trend, meme, sound, phrase, or challenge right \
now — on TikTok or elsewhere — that Solana memecoins are being named \
after or riding. These often drive much larger and longer moves than \
typical trenches narratives, since the audience comes from outside \
crypto entirely — flag these as potential "gems" worth watching \
closely rather than exiting early.

Keep the whole summary under 200 words, dense factual notes, no \
pleasantries. This feeds another AI as context, not a person.

End with one extra line, exactly in this format (empty if nothing \
qualifies): TREND_KEYWORDS: word1, word2, word3
Short keywords/phrases tied to CURRENTLY viral trends that a token \
name or symbol might reference — these are what mark a token as a
potential narrative "gem" instead of a normal solo pump."""


def extract_trend_keywords(summary: str) -> list:
    for line in summary.splitlines():
        if line.strip().upper().startswith("TREND_KEYWORDS:"):
            raw = line.split(":", 1)[1].strip()
            return [kw.strip() for kw in raw.split(",") if kw.strip()]
    return []


def run_market_research() -> str:
    if not ANTHROPIC_API_KEY:
        raise RuntimeError("ANTHROPIC_API_KEY not set — skipping market research.")
    headers = {
        "x-api-key": ANTHROPIC_API_KEY,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }
    payload = {
        "model": ANTHROPIC_MODEL,
        "max_tokens": 600,
        "messages": [{"role": "user", "content": MARKET_RESEARCH_PROMPT}],
        "tools": [{"type": "web_search_20250305", "name": "web_search"}],
    }
    resp = requests.post(ANTHROPIC_API_URL, headers=headers, json=payload, timeout=60)
    resp.raise_for_status()
    data = resp.json()
    text_parts = [b["text"] for b in data.get("content", []) if b.get("type") == "text"]
    return "\n".join(text_parts).strip()


def save_market_intel(summary: str):
    entries = []
    if INTEL_FILE.exists():
        try:
            entries = json.loads(INTEL_FILE.read_text())
        except json.JSONDecodeError:
            entries = []
    entries.append({
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "summary": summary,
        "tiktok_keywords": extract_trend_keywords(summary),  # kept as tiktok_keywords for compatibility
    })
    entries = entries[-MAX_INTEL_ENTRIES_KEPT:]
    INTEL_FILE.write_text(json.dumps(entries, indent=2))
    print(f"Saved market research. Log now has {len(entries)} entries.")


def do_market_research_pass():
    """Runs one research pass and saves it — called periodically from main()."""
    print("Ledger is researching the market for trend/gem signals...")
    try:
        summary = run_market_research()
        print(f"\n{summary}\n")
        save_market_intel(summary)
    except Exception as e:
        print(f"[ERROR] market research pass failed: {e}")


# ── Narrative awareness ──────────────────────────────────────────────

# Reads the same file market_intel.py writes to — its research pass
# extracts TikTok-viral keywords and stores them in the latest entry.
# One source of truth for narrative data, instead of a second file
# that would need to stay in sync with it.
NARRATIVES_FILE = Path("market_intel.json")

# Narrative-driven tokens (a viral trend spreading across many launches
# at once, especially TikTok-driven ones) behave very differently from
# a normal solo pump — they can pull back hard and keep climbing,
# since the whole crowd is still rotating in. A tight 20% trailing
# stop would cut these far too early. This wider stop gives a genuine
# narrative room to breathe.
TRAILING_STOP_PCT_NARRATIVE = 0.40  # vs the normal 20% (TRAILING_STOP_PCT below)


def check_is_narrative_token(name: str, symbol: str) -> bool:
    """
    Checks whether a token's name/symbol matches a TikTok-viral
    keyword from market_intel.py's most recent research pass. Returns
    False gracefully if that file doesn't exist yet (e.g.
    market_intel.py hasn't run yet) or holds no matching keyword —
    narrative detection is a bonus signal, never a requirement for
    normal operation.
    """
    if not NARRATIVES_FILE.exists():
        return False
    try:
        entries = json.loads(NARRATIVES_FILE.read_text())
    except json.JSONDecodeError:
        return False
    if not entries:
        return False

    keywords = entries[-1].get("tiktok_keywords", [])
    if not keywords:
        return False

    combined = f"{name} {symbol}".lower()
    for keyword in keywords:
        if keyword.lower() in combined:
            return True
    return False


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

# ── Chart / market structure analysis ─────────────────────────────────
#
# Implements the guide's core chart-reading concept: a chart is either
# printing higher highs + higher lows (uptrend), lower highs + lower
# lows (downtrend), or neither (choppy/sideways) — and a "break of
# structure" (price failing to hold a level that previously held)
# signals a real change in who's in control. This feeds real price
# history into conviction analysis instead of just current spot price.

SWING_PIVOT_WINDOW = 2  # candles on each side used to confirm a swing high/low
MIN_CANDLES_FOR_STRUCTURE = 8  # below this, there's not enough history to read


def get_ohlcv_candles(mint: str, interval: str = "5m", hours_back: int = 4) -> list:
    """
    Fetches recent candlestick data for a token via Birdeye's OHLCV V3
    API. Returns a list of {time, open, high, low, close, volume}
    dicts in chronological order, or an empty list if unavailable
    (no API key, brand-new token with no history yet, API error) —
    callers must handle the empty case gracefully, never assume data.
    """
    if not BIRDEYE_API_KEY:
        return []

    now = int(time.time())
    time_from = now - hours_back * 3600

    headers = {
        "X-API-KEY": BIRDEYE_API_KEY,
        "x-chain": "solana",
        "accept": "application/json",
    }
    params = {
        "address": mint,
        "type": interval,
        "time_from": time_from,
        "time_to": now,
        "mode": "range",
        "currency": "usd",
    }
    try:
        resp = requests.get(BIRDEYE_OHLCV_URL, headers=headers, params=params, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        items = data.get("data", {}).get("items", [])
        candles = [
            {
                "time": c.get("unix_time", c.get("unixTime")),
                "open": c.get("o"),
                "high": c.get("h"),
                "low": c.get("l"),
                "close": c.get("c"),
                "volume": c.get("v"),
            }
            for c in items
        ]
        candles.sort(key=lambda c: c["time"] or 0)
        return candles
    except Exception as e:
        print(f"[WARN] OHLCV fetch failed for {mint}: {e}")
        return []


def detect_market_structure(candles: list) -> dict:
    """
    Reads market structure from candle data: finds swing highs/lows
    (local peaks/troughs confirmed by neighboring candles on each
    side), then compares the two most recent of each to classify the
    trend as uptrend (higher highs + higher lows), downtrend (lower
    highs + lower lows), or choppy (neither). Also flags a "break of
    structure" — current price violating the most recent swing
    low in an uptrend, or swing high in a downtrend — since that's
    the single clearest signal something has changed.

    Returns {"trend": "uptrend"|"downtrend"|"choppy"|"insufficient_data",
    "break_of_structure": str or None, "note": str} — always returns
    a usable dict, never raises, so a thin/missing chart history never
    blocks a trading decision.
    """
    if len(candles) < MIN_CANDLES_FOR_STRUCTURE:
        return {
            "trend": "insufficient_data",
            "break_of_structure": None,
            "note": "Not enough price history yet to read market structure — token is likely very new.",
        }

    w = SWING_PIVOT_WINDOW
    highs = [c["high"] for c in candles]
    lows = [c["low"] for c in candles]

    swing_highs = []
    swing_lows = []
    for i in range(w, len(candles) - w):
        window_highs = highs[i - w:i + w + 1]
        window_lows = lows[i - w:i + w + 1]
        if highs[i] == max(window_highs):
            swing_highs.append((i, highs[i]))
        if lows[i] == min(window_lows):
            swing_lows.append((i, lows[i]))

    if len(swing_highs) < 2 or len(swing_lows) < 2:
        return {
            "trend": "insufficient_data",
            "break_of_structure": None,
            "note": "No clear swing points formed yet — price action too choppy or too short to read.",
        }

    prev_high, last_high = swing_highs[-2][1], swing_highs[-1][1]
    prev_low, last_low = swing_lows[-2][1], swing_lows[-1][1]

    higher_highs = last_high > prev_high
    higher_lows = last_low > prev_low
    lower_highs = last_high < prev_high
    lower_lows = last_low < prev_low

    if higher_highs and higher_lows:
        trend = "uptrend"
    elif lower_highs and lower_lows:
        trend = "downtrend"
    else:
        trend = "choppy"

    current_price = candles[-1]["close"]
    bos = None
    if trend == "uptrend" and current_price < last_low:
        bos = "bearish break — price fell below the most recent higher low"
    elif trend == "downtrend" and current_price > last_high:
        bos = "bullish break — price pushed above the most recent lower high"

    note = f"Market structure: {trend}."
    if bos:
        note += f" {bos}."

    return {"trend": trend, "break_of_structure": bos, "note": note}


def analyze_conviction(
    token: str,
    metadata: dict,
    trigger_wallet_handle: str,
    trigger_platform: str,
    is_priority_wallet: bool,
    is_whale_wallet: bool,
) -> dict:
    """
    Ledger's actual judgment call — replaces the old static templates.
    Uses Claude to independently evaluate whether a detected buy is
    genuinely worth entering, instead of automatically mirroring every
    wallet buy. Considers the coin's own lore/theme (not just "a
    wallet bought it"), recent market/narrative context, and how much
    weight the triggering wallet's track record deserves.

    Returns:
        {
          "conviction": "buy" or "pass",
          "risk_score": int 0-10 (0 = safest, 10 = most reckless),
          "thesis": str — 2-3 sentences, professional, mentions the
                     coin's lore/theme when known,
          "independent": bool — True if the reasoning stands on the
                     coin's own merits, False if primarily following
                     the triggering wallet's lead
        }

    Falls back to a simple always-pass-through rule (old behavior) if
    ANTHROPIC_API_KEY isn't configured, so the bot still runs without
    it — just without independent judgment.
    """
    name = metadata.get("name", "") or token
    symbol = metadata.get("symbol", "") or "UNKNOWN"
    description = metadata.get("description", "") or "no description available"

    if not ANTHROPIC_API_KEY:
        # No independent judgment available — fall back to treating
        # every detected buy as a pass-through scout position, same
        # as the original design, clearly marked as wallet-following.
        return {
            "conviction": "buy",
            "risk_score": 3 if (is_priority_wallet or is_whale_wallet) else 7,
            "thesis": f"{trigger_wallet_handle} has taken a position here. No independent analysis available (ANTHROPIC_API_KEY not set) — sizing based on wallet trust alone.",
            "independent": False,
        }

    wallet_trust = (
        "a priority trader whose calls are trusted at maximum conviction"
        if is_priority_wallet else
        "a whale-tier wallet (6-figure+ portfolio)"
        if is_whale_wallet else
        "a standard tracked wallet, no special trust level"
    )

    recent_intel = ""
    if INTEL_FILE.exists():
        try:
            entries = json.loads(INTEL_FILE.read_text())
            if entries:
                recent_intel = entries[-1].get("summary", "")
        except json.JSONDecodeError:
            pass

    candles = get_ohlcv_candles(token)
    structure = detect_market_structure(candles)

    prompt = f"""You are Ledger, a professional, disciplined Solana memecoin trader. A wallet you track just bought a token. Evaluate independently whether YOU would enter this position — do not simply mirror the wallet's action.

Core principles you trade by:
- Never borrow conviction. A wallet buying something is one input, not a reason on its own. Ask: if I found this token myself with no wallet attached, would I still buy it?
- Watch for "vamping" — when a narrative or trend goes viral, multiple competing tokens often launch around the same theme, and the crowd frequently buys the wrong (non-canonical) one before the real one is confirmed. If this token's appeal rests on a trend/narrative match, weigh how likely it is to be the token the community actually rallies around, versus a copycat that gets abandoned once the "real" one is identified. Treat unclear canonical status as a reason to raise the risk score, not to pass outright — being early on the right one is valuable, but so is being honest about the uncertainty.
- Read the market regime from your own recent research before sizing conviction — the same setup deserves more caution in quiet/risk-off conditions than in active/risk-on ones.
- A thesis should be something you could defend in two sentences. If you can't articulate a concrete reason beyond "the wallet bought it," that's a signal to pass or mark the risk high.

Token name: {name}
Ticker: {symbol}
Lore/description: {description}
Mint: {token}

Triggering wallet: {trigger_wallet_handle} ({wallet_trust})
Detected on: {trigger_platform}

Recent market context (your own research, may be empty if none yet — use this to judge the current market regime and whether this token matches a live narrative you're already tracking, including vamping risk if multiple tokens could be riding the same trend):
{recent_intel or "none available yet"}

Chart / market structure for this specific token ({structure['trend']}):
{structure['note']}
Treat "insufficient_data" as neutral — don't penalize a token just for being too new to have chart history yet. But an actual downtrend or a bearish break of structure is a real reason for caution, and an uptrend or bullish break supports the thesis. The chart confirms or challenges a thesis, it never replaces one.

Decide independently: does this token's own merit (its theme, timing, narrative fit, canonical-vs-copycat likelihood, and current chart structure) plus the wallet signal add up to a real position — or is this just noise not worth capital?

Respond with ONLY valid JSON, no other text, no markdown code fences:
{{"conviction": "buy" or "pass", "risk_score": <integer 0-10, 0=safest 10=most reckless>, "thesis": "<2-3 sentences, professional trader voice, correct grammar, reference the coin's lore/theme if known, no slang, never use a dollar sign character>", "independent": <true if your reasoning stands on the coin's own merit beyond just following the wallet, false if you are primarily following the wallet's lead>}}"""

    headers = {
        "x-api-key": ANTHROPIC_API_KEY,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }
    payload = {
        "model": ANTHROPIC_MODEL,
        "max_tokens": 400,
        "messages": [{"role": "user", "content": prompt}],
    }

    try:
        resp = requests.post(ANTHROPIC_API_URL, headers=headers, json=payload, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        text = "".join(b["text"] for b in data.get("content", []) if b.get("type") == "text").strip()
        # Strip accidental code fences, just in case
        if text.startswith("```"):
            text = text.strip("`").lstrip("json").strip()
        result = json.loads(text)

        result["conviction"] = result.get("conviction", "pass")
        result["risk_score"] = max(0, min(10, int(result.get("risk_score", 10))))
        result["thesis"] = result.get("thesis", "").replace("$", "")  # extra safety net
        result["independent"] = bool(result.get("independent", False))
        return result
    except Exception as e:
        print(f"[WARN] conviction analysis failed for {token}: {e} — defaulting to pass (skip, don't guess)")
        return {"conviction": "pass", "risk_score": 10, "thesis": "", "independent": False}



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


def open_paper_position(state: LedgerState, token: str, price: float, size_sol: float, opened_by: str = "", strength: str = "weak"):
    ok, reason = can_open_position(state, size_sol)
    if not ok:
        print(f"[BLOCKED] {token}: {reason}")
        return

    metadata = get_token_metadata(token)
    symbol = metadata.get("symbol", "")  # no "$" prefix — avoids triggering another bot's ticker auto-detection
    risk_level = "🟢 Lower Risk (whale-backed)" if strength == "strong" else "🟡 High Risk (scout)"
    is_narrative = check_is_narrative_token(metadata.get("name", ""), metadata.get("symbol", ""))

    state.balance_sol -= size_sol
    state.open_positions[token] = PaperPosition(
        token=token,
        entry_price=price,
        size_sol=size_sol,
        opened_at=datetime.now(timezone.utc).isoformat(),
        opened_by=opened_by,
        symbol=symbol,
        risk_level=risk_level,
        is_narrative=is_narrative,
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
    narrative_tag = " [Narrative Play — wider trailing stop applied]" if is_narrative else ""
    # No separate Discord post here — the caller (main loop) sends one
    # single combined message per buy, including this position's size
    # and the resulting balance, instead of a second message.
    print(f"{display_name} — Position Opened — Entry: {price:.6g} USD  •  Size: {size_sol} SOL{narrative_tag}")
    state.save()


def partial_close_paper_position(state: LedgerState, token: str, exit_price: float, fraction: float, reason: str = "✂️ Scaled Out"):
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
        "risk_level": pos.get("risk_level", ""),
        "at": datetime.now(timezone.utc).isoformat(),
    })
    display_name = pos.get("symbol") or token
    notes = (
        f"• **Sold:** {fraction:.0%} of position at {exit_price:.6g} USD\n"
        f"• **PnL:** {pnl:+.4f} SOL\n"
        f"• **Amount Sold:** {sell_size:.4f} SOL  •  **Remaining Position:** {pos['size_sol']:.4f} SOL\n"
        f"• **Current Balance:** {state.balance_sol:.4f} SOL"
    )
    speak(
        title=f"📊 {display_name} — {reason}",
        description="",
        color=COLOR_PROFIT if pnl >= 0 else COLOR_LOSS,
        fields=[
            {"name": "CA:", "value": token, "inline": False},
            {"name": "ℹ️ Additional Notes", "value": notes, "inline": False},
        ],
    )

    if pos["size_sol"] < 0.001:  # fully drained — close it out entirely
        del state.open_positions[token]
    else:
        state.open_positions[token] = pos
    state.save()


def close_paper_position(state: LedgerState, token: str, exit_price: float, reason: str = None):
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
        "risk_level": pos.get("risk_level", ""),
        "at": datetime.now(timezone.utc).isoformat(),
    })
    display_name = pos.get("symbol") or token
    status = reason or ("✅ Position Closed" if pnl >= 0 else "❌ Position Closed")
    notes = (
        f"• **Exit:** {exit_price:.6g} USD\n"
        f"• **PnL:** {pnl:+.4f} SOL\n"
        f"• **Amount Sold:** {pos['size_sol']:.4f} SOL (full position)\n"
        f"• **Current Balance:** {state.balance_sol:.4f} SOL"
    )
    speak(
        title=f"📊 {display_name} — {status}",
        description="",
        color=COLOR_PROFIT if pnl >= 0 else COLOR_LOSS,
        fields=[
            {"name": "CA:", "value": token, "inline": False},
            {"name": "ℹ️ Additional Notes", "value": notes, "inline": False},
        ],
    )
    state.save()


# Exit rules — coherent with Ledger's "balanced" persona: cuts losers
# fast, recovers initial capital early, then lets the rest ride with a
# TRAILING stop instead of a fixed profit target. There's no hard
# ceiling on the upside — a trailing stop locks in gains only once
# momentum genuinely reverses, rather than capping wins at an
# arbitrary "sell at 2x/4x" ladder. This is the "profit-taker, not
# moonbag-holder" behavior: it never sells purely because a price
# level was hit, only because the position gave back real ground.
STOP_LOSS_PCT = -0.25            # close everything if down 25% from entry
INITIAL_RECOVERY_PCT = 0.40      # at +40%, sell enough to recoup the original capital
TRAILING_STOP_PCT = 0.20         # after that, close if price pulls back 20% from its peak


def check_open_positions(state: LedgerState):
    """
    Checks every open paper position against current price and applies
    the staged exit strategy:
      1. Stop-loss: down 25% from entry -> close the whole thing.
      2. At +40%: sell just enough to recover the original capital
         (position keeps running "on house money" after this).
      3. From there: track the highest price seen (the "peak"). If
         price pulls back 20% from that peak, close what's left. No
         fixed take-profit level — a position at 3x can keep running
         to 10x as long as it doesn't give back 20% off its high; it
         only exits when momentum actually breaks.
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
            close_paper_position(state, mint, current_price, reason="🛑 Stop Loss")
            continue

        if not pos["initial_recovered"]:
            if change_pct >= INITIAL_RECOVERY_PCT - EPSILON:
                # Sell exactly the fraction whose proceeds equal the
                # original capital — e.g. at 1.4x, selling 1/1.4 of
                # the position returns exactly the initial size_sol.
                current_multiple = 1 + change_pct
                fraction_to_recover_capital = 1 / current_multiple
                partial_close_paper_position(
                    state, mint, current_price, fraction_to_recover_capital,
                    reason="💰 Capital Recovered"
                )
                if mint in state.open_positions:
                    state.open_positions[mint]["initial_recovered"] = True
                    state.open_positions[mint]["peak_price"] = current_price
                    state.save()
        else:
            peak_price = max(pos.get("peak_price", 0) or 0, current_price)
            if peak_price != pos.get("peak_price"):
                state.open_positions[mint]["peak_price"] = peak_price
                state.save()

            applicable_trailing_pct = TRAILING_STOP_PCT_NARRATIVE if pos.get("is_narrative") else TRAILING_STOP_PCT
            pullback_from_peak = (peak_price - current_price) / peak_price
            if pullback_from_peak >= applicable_trailing_pct - EPSILON:
                reason = "📉 Trailing Stop — Profit Locked"
                if pos.get("is_narrative"):
                    reason = "📉 Narrative Trailing Stop — Profit Locked"
                close_paper_position(state, mint, current_price, reason=reason)


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


# ── Performance analysis — "learning from losses" ────────────────────

MONTHLY_PROFIT_GOAL_USD = 500  # paper-trading target — see note in recap message
PERFORMANCE_RECAP_EVERY_N_CYCLES = 720  # ~once/day at 120s/cycle


def compute_performance_stats(state: LedgerState) -> dict:
    """
    Analyzes closed/partial-closed trades to answer the question that
    actually matters: is this working, and for which kind of signal?
    This is what "learning from losses" means in practice here — not
    a black-box that rewires itself, but real numbers Ledger (and you)
    can look at and use to judge whether whale-backed signals are
    actually outperforming scout plays, or whether the strategy needs
    to change.
    """
    exits = [t for t in state.trade_log if t["action"] in ("close", "partial_close")]
    if not exits:
        return {"total_exits": 0}

    def win_rate(entries):
        if not entries:
            return None
        wins = sum(1 for t in entries if t["pnl_sol"] > 0)
        return wins / len(entries) * 100

    whale_exits = [t for t in exits if "Lower Risk" in t.get("risk_level", "")]
    scout_exits = [t for t in exits if "Lower Risk" not in t.get("risk_level", "")]

    return {
        "total_exits": len(exits),
        "win_rate_pct": win_rate(exits),
        "whale_win_rate_pct": win_rate(whale_exits),
        "whale_exit_count": len(whale_exits),
        "scout_win_rate_pct": win_rate(scout_exits),
        "scout_exit_count": len(scout_exits),
        "total_pnl_sol": sum(t["pnl_sol"] for t in exits),
    }


def get_month_start_iso() -> str:
    now = datetime.now(timezone.utc)
    return datetime(now.year, now.month, 1, tzinfo=timezone.utc).isoformat()


def compute_monthly_pnl_usd(state: LedgerState, sol_price_usd: float) -> float:
    """
    Sums realized PnL from trade_log entries dated within the current
    calendar month, converted to USD — this is what gets compared
    against MONTHLY_PROFIT_GOAL_USD. Paper trading only for now; this
    tracks whether the STRATEGY would be on pace for the goal, not
    real money.
    """
    month_start = get_month_start_iso()
    monthly_pnl_sol = sum(
        t["pnl_sol"] for t in state.trade_log
        if t["action"] in ("close", "partial_close") and t.get("at", "") >= month_start
    )
    return monthly_pnl_sol * sol_price_usd


def post_performance_recap(state: LedgerState):
    """
    Posts a periodic honest report card to Discord: win rate overall
    and by signal type, plus progress toward the $500/month paper
    goal. This is the surfacing half of "learning from losses" — the
    other half is you (or a future automated step) actually adjusting
    strategy based on what this shows, e.g. sizing down scout plays
    further if their win rate stays well below whale-backed ones.
    """
    stats = compute_performance_stats(state)
    if stats["total_exits"] == 0:
        return  # nothing closed yet — nothing to report

    sol_prices = get_token_prices_usd([SOL_MINT])
    sol_price_usd = sol_prices.get(SOL_MINT, 0)
    monthly_pnl_usd = compute_monthly_pnl_usd(state, sol_price_usd) if sol_price_usd else None

    lines = [
        f"**Overall win rate:** {stats['win_rate_pct']:.0f}% ({stats['total_exits']} exits)",
    ]
    if stats["whale_win_rate_pct"] is not None:
        lines.append(f"**Whale-backed win rate:** {stats['whale_win_rate_pct']:.0f}% ({stats['whale_exit_count']} exits)")
    if stats["scout_win_rate_pct"] is not None:
        lines.append(f"**Scout win rate:** {stats['scout_win_rate_pct']:.0f}% ({stats['scout_exit_count']} exits)")
    lines.append(f"**Total realized PnL:** {stats['total_pnl_sol']:+.4f} SOL")

    if monthly_pnl_usd is not None:
        progress_pct = max(0, monthly_pnl_usd) / MONTHLY_PROFIT_GOAL_USD * 100
        lines.append(
            f"\n**Monthly goal progress:** {monthly_pnl_usd:+.2f} / {MONTHLY_PROFIT_GOAL_USD} USD "
            f"({progress_pct:.0f}%) — paper trading, not real funds yet"
        )

    speak(
        title="📊 Performance Recap",
        description="\n".join(lines),
        color=COLOR_PROFIT if stats["total_pnl_sol"] >= 0 else COLOR_LOSS,
    )


# ── Main loop ─────────────────────────────────────────────────────────

def main():
    state = LedgerState.load()
    print(f"Ledger booting up. Paper balance: {state.balance_sol} SOL")

    whale_wallets = set()
    cycle_count = 0

    while True:
        cycle_count += 1

        # Reload every cycle — edit wallets.json anytime, no restart needed
        watched_wallets, wallet_handles, priority_wallets = load_wallets()
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

        # Performance recap — the "learning from losses" surfacing step
        if cycle_count % PERFORMANCE_RECAP_EVERY_N_CYCLES == 0:
            try:
                post_performance_recap(state)
            except Exception as e:
                print(f"[ERROR] performance recap failed: {e}")

        # Market/trend research — powers narrative ("gem") detection above.
        # Runs on cycle 1 too, so there's data available from the start
        # instead of waiting 4 hours for the first pass.
        if ANTHROPIC_API_KEY and (cycle_count == 1 or cycle_count % MARKET_RESEARCH_EVERY_N_CYCLES == 0):
            do_market_research_pass()

        for wallet in watched_wallets:
            try:
                txs = get_wallet_transactions(wallet)
                buys = extract_new_buys(txs, wallet)
                for buy in buys:
                    if buy["signature"] in state.seen_signatures:
                        continue  # already processed this exact transaction
                    state.seen_signatures.append(buy["signature"])

                    token = buy["mint"]
                    is_priority = wallet in priority_wallets
                    is_whale = wallet in whale_wallets
                    trader_name = WALLET_HANDLES.get(wallet, wallet[:6] + "...")  # "..." kept for unknown handles
                    platform_name = get_source_display_name(buy["source"])

                    metadata = get_token_metadata(token)
                    display_symbol = metadata.get("symbol") or token[:6] + "..."  # no "$" — avoids triggering another bot

                    analysis = analyze_conviction(
                        token, metadata, trader_name, platform_name, is_priority, is_whale
                    )

                    if analysis["conviction"] != "buy":
                        print(f"  [PASS] {display_symbol} — no independent conviction, skipping (not posted).")
                        continue

                    # Fetch price BEFORE posting, so the single message can
                    # include the actual size bought and resulting balance
                    # — no second "position opened" message needed.
                    prices = get_token_prices_usd([token])
                    entry_price = prices.get(token)
                    if entry_price is None:
                        print(f"  [SKIP] no price data for {token} yet — can't size a position.")
                        continue

                    risk_score = analysis["risk_score"]
                    risk_bucket = "🟢" if risk_score <= 3 else "🟡" if risk_score <= 6 else "🔴"

                    # Size scales continuously with conviction (risk_score),
                    # not just two fixed tiers — concentrated conviction on
                    # your best-scoring ideas beats spreading evenly thin.
                    # risk_score 0 (safest) -> MAX_CONVICTION_SIZE_SOL;
                    # risk_score 10 (riskiest) -> MIN_SCOUT_SIZE_SOL.
                    size_sol = MIN_SCOUT_SIZE_SOL + (MAX_CONVICTION_SIZE_SOL - MIN_SCOUT_SIZE_SOL) * (1 - risk_score / 10)
                    size_sol = min(size_sol, MAX_POSITION_SOL)
                    strength = "strong" if (is_priority or is_whale) else "weak"

                    ok, block_reason = can_open_position(state, size_sol)
                    if not ok:
                        print(f"  [BLOCKED] {display_symbol}: {block_reason}")
                        continue

                    balance_after = state.balance_sol - size_sol
                    notes_lines = [
                        f"• **Risk Assessment:** {risk_bucket} {risk_score}/10",
                        f"• **Amount Bought:** {size_sol:.4f} SOL",
                        f"• **Current Balance:** {balance_after:.4f} SOL",
                    ]
                    if not analysis["independent"]:
                        # Only credit/mention the wallet when the call actually
                        # followed its lead — an independent call stands on
                        # its own, with no wallet name attached.
                        notes_lines.append(f"• **Trader:** {trader_name}  •  **Platform:** {platform_name}")
                    additional_message = "\n".join(notes_lines)

                    speak(
                        title=f"📊 {display_symbol}",
                        description="",
                        color=COLOR_STRONG_SIGNAL if is_priority or is_whale else COLOR_NEUTRAL,
                        fields=[
                            {"name": "CA:", "value": token, "inline": False},
                            {"name": "🧠 Thesis", "value": analysis["thesis"], "inline": False},
                            {"name": "ℹ️ Additional Notes", "value": additional_message, "inline": False},
                        ],
                    )

                    open_paper_position(state, token, entry_price, size_sol, opened_by=wallet, strength=strength)
            except Exception as e:
                print(f"[ERROR] wallet {wallet}: {e}")
            time.sleep(0.3)  # spread requests out across the cycle

        check_open_positions(state)
        state.save()
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    main()
