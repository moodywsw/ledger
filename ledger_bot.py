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
  SNIPER_MODE_ENABLED - set to "true" to enable Sniper Mode: a
                       separate, fast, filter-based strategy that
                       enters new Pump.fun launches directly (dev buy
                       size + holder concentration filters, ~50% of
                       launches that pass), instead of only trading on
                       wallet-detected buys. Off by default. Requires
                       the "websockets" package (see Install below).

Install:
  pip install requests websockets --break-system-packages
"""

import os
import json
import time
import re
import random
import threading
import queue
import asyncio
import requests
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from pathlib import Path

from journal_store import log_journal, get_token_history
from theses_store import upsert_thesis
from api_server import start_api_server

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
# Supports posting to multiple channels: set this env var to several
# webhook URLs separated by commas (e.g. "https://.../1,https://.../2")
# to post every message to all of them.
DISCORD_WEBHOOK_URLS = [u.strip() for u in DISCORD_WEBHOOK_URL.split(",") if u.strip()]
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



def speak(
    title: str, description: str, color: int = COLOR_NEUTRAL, fields: list = None,
    journal_kind: str = None, token_ticker: str = None, journal_meta: dict = None,
):
    """
    Ledger's public voice. Always prints a plain-text line to the
    console (for logs), and — if DISCORD_WEBHOOK_URL is configured —
    posts a normal Discord message (not an embed/card) built from
    markdown, in the requested layout: bold title, then each field
    as its own bold-labeled section. `color` is accepted but unused
    for Discord now (plain messages have no color) — kept so callers
    don't need changes.

    If `journal_kind` is passed, the exact same "{title} — {description}"
    text already being printed/posted also gets appended to the
    persistent journal (journal_store.log_journal) — this is the one
    choke point where everything Ledger already says out loud becomes
    durable, without generating any new text or duplicating the
    decision logic that produced title/description in the first place.
    """
    console_line = f"{title} — {description}"
    print(console_line)

    if journal_kind:
        log_journal(kind=journal_kind, text=console_line, token_ticker=token_ticker, meta=journal_meta)

    # Keep a leading emoji outside the bold wrap (matches "💰 **TRADE
    # CLOSED — X**" style) instead of bolding the emoji along with the
    # text — split off the first "word" only if it looks like an emoji
    # (non-ASCII), leave plain-text titles untouched.
    title_parts = title.split(" ", 1)
    if len(title_parts) == 2 and not title_parts[0].isascii():
        lines = [f"{title_parts[0]} **{title_parts[1]}**"]
    else:
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

    if not DISCORD_WEBHOOK_URLS:
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

    for webhook_url in DISCORD_WEBHOOK_URLS:
        try:
            requests.post(webhook_url, json=payload, timeout=10)
        except Exception as e:
            # Never let a Discord hiccup take down the trading loop — this
            # is called from critical paths (stop-loss, position opens),
            # so it must fail silently and let the bot keep running. One
            # channel failing doesn't stop the others from getting posted.
            print(f"[WARN] Discord post failed for one webhook: {e}")


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

# ── Risk limits (hard-coded, not suggestions) ───────────────────────────

MAX_POSITION_SOL = 0.5        # max size of any single paper position
MAX_DAILY_LOSS_SOL = 2.0      # bot stops opening new positions past this
MAX_TRADES_PER_HOUR = 10      # circuit breaker against runaway logic
STARTING_PAPER_BALANCE_SOL = 10.0  # degen-mode: aim to compound this fast toward the 100 SOL goal, reset if wiped out

# Position size scales continuously with conviction (the risk_score
# from analyze_conviction), between these two bounds — a risk_score
# of 0 (safest) gets MAX_CONVICTION_SIZE_SOL, a risk_score of 10
# (riskiest that still passed) gets MIN_SCOUT_SIZE_SOL.
MIN_SCOUT_SIZE_SOL = 0.02
MAX_CONVICTION_SIZE_SOL = 0.25

# Conviction-mode pacing: the value computed from MIN_SCOUT_SIZE_SOL/
# MAX_CONVICTION_SIZE_SOL above is the TARGET size, not what actually
# gets bought on day one. Only CONVICTION_INITIAL_ENTRY_FRACTION of it
# goes in on the initial entry; the rest scales in later (via
# top_up_conviction_position, called from check_open_positions) only
# once the position shows sustained price growth — never full size on
# the first buy.
CONVICTION_INITIAL_ENTRY_FRACTION = 0.40
CONVICTION_TOPUP_STAGE1_GROWTH_PCT = 0.15   # +15% from entry -> top up to CONVICTION_TOPUP_STAGE1_TARGET_FRACTION of target
CONVICTION_TOPUP_STAGE1_TARGET_FRACTION = 0.70
CONVICTION_TOPUP_STAGE2_GROWTH_PCT = 0.30   # +30% from entry -> top up to the full target
CONVICTION_TOPUP_STAGE2_TARGET_FRACTION = 1.00

# ── Sniper Mode ──────────────────────────────────────────────────────
#
# A separate, faster, more mechanical strategy from the main wallet-
# tracking one: enters new Pump.fun launches directly, before there's
# any wallet signal or chart history — pure speed. Off by default —
# set SNIPER_MODE_ENABLED=true to turn it on.
SNIPER_MODE_ENABLED = os.environ.get("SNIPER_MODE_ENABLED", "true").lower() == "true"  # on by default now
SNIPER_WS_URL = "wss://pumpdev.io/ws"  # same free, unofficial feed as pumpfun_listener.py
SNIPER_ACTIVE_PRESET = os.environ.get("SNIPER_PRESET", "hyper_early_scalp")

SNIPER_MIN_CONFIDENCE_TO_ENTER = 2.0  # minimum confidence multiplier required to actually buy —
                                       # replaces the old 50% coin-flip with a real conviction bar
SNIPER_MIN_DEV_BUY_SOL = 0.5      # below this, strongly correlates with instant rugs
SNIPER_POSITION_SIZE_PCT = 0.08   # 8% of current bankroll per trade at 1.0x confidence — scales
                                   # automatically as the bankroll grows toward 10 SOL or resets to 1
SNIPER_MIN_POSITION_SOL = 0.005   # floor, so sizing doesn't round down to something meaningless

# Position size scales with per-trade confidence instead of a single
# fixed multiplier — 1.0x is baseline, up to these ceilings for the
# most convicted setups. Trusted-wallet copies get a higher ceiling
# than blind Sniper Mode launches, matching how much independent
# judgment actually goes into each.
PRIORITY_MAX_SIZE_MULTIPLIER = 5.0
SNIPER_MAX_SIZE_MULTIPLIER = 4.0

SNIPER_MIN_LIQUIDITY_USD = 1_000     # below this, a launch is too thin to trade safely
SNIPER_MAX_ENTRY_MARKET_CAP_USD = 250_000  # above this, it's no longer an "early" entry

# Priority-copy gets far more room than the sniper ceiling above — it's
# following a trusted trader, not blind speed-sniping a fresh launch —
# but $5M+ is already well past "memecoin play" territory. Confirmed
# live: USD1 at $157.84M MC and TRUMP at $1.4B MC both get blocked by
# this, as they should — those aren't memecoin plays, copying them was
# never the point of this mechanism.
PRIORITY_COPY_MAX_ENTRY_MARKET_CAP_USD = 5_000_000

# Known stablecoin mints — verified addresses only, nothing guessed.
# If a priority-copy target is one of these, there's no trading thesis
# in copying it regardless of market cap: it's pegged to ~$1 and isn't
# going anywhere.
KNOWN_STABLECOIN_MINTS = {
    "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",  # USDC
    "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB",  # USDT
    "USD1ttGY1N17NEEHLmELoaybftRBUSErhqYiQzvEmuB",   # USD1 (World Liberty Financial) — verified live against the $157.84M MC example
}
# Symbol backup — catches stablecoins whose exact mint isn't hardcoded
# above, and copycat/impostor tokens riding a stablecoin's ticker
# (equally pointless to copy: a real peg goes nowhere, and a fake one
# trading on a stablecoin's name isn't a memecoin thesis either).
KNOWN_STABLECOIN_SYMBOLS = {"USDC", "USDT", "USD1", "DAI", "BUSD", "TUSD", "FDUSD", "PYUSD", "USDE", "FRAX", "USDD"}


def is_known_stablecoin(mint: str, symbol: str) -> bool:
    if mint in KNOWN_STABLECOIN_MINTS:
        return True
    return (symbol or "").strip().upper() in KNOWN_STABLECOIN_SYMBOLS
SNIPER_MAX_QUEUE_DRAIN_PER_CYCLE = 20   # cap how many freshly-launched tokens enter the pending list per cycle
SNIPER_MAX_PENDING_EVAL_PER_CYCLE = 20  # cap how many pending (aging-in) candidates get evaluated per cycle
SNIPER_CHECK_INTERVAL_SECONDS = 20      # sniper positions get checked this often, not the full 2-min main cycle
MAX_CONCURRENT_SNIPER_POSITIONS = 8    # hard cap on simultaneous sniper/priority-copy positions

# Daily loss pause — a percentage-based circuit breaker separate from
# the fixed MAX_DAILY_LOSS_SOL below: if the balance drawdown from the
# start of the current run exceeds this, pause ALL new buys for 12
# hours rather than continuing to trade through a bad stretch.
DAILY_LOSS_PAUSE_PCT = -0.12       # -12% from the run's starting balance
DAILY_LOSS_PAUSE_HOURS = 4

# Total exposure and reserve rules — new position-level checks, on top
# of the per-trade sizing above.
MAX_TOTAL_EXPOSURE_PCT = 0.25      # never have more than 25% of total equity tied up in open positions at once
MIN_RESERVE_PCT = 0.15             # always keep at least 15% of equity as liquid SOL, not committed to trades

# Peak-drawdown circuit breaker — separate from the run-start-based
# daily loss pause above. Tracks the highest balance ever seen this
# run; if the balance falls 25% below that peak, switch to "ultra
# conservative mode" (halved position sizing) until it recovers.
PEAK_DRAWDOWN_ULTRA_CONSERVATIVE_PCT = -0.25
ULTRA_CONSERVATIVE_SIZE_MULTIPLIER = 0.5

# 3-day goal tracking — a hard deadline, not just an aspirational target.
GOAL_DEADLINE_HOURS = 72

# Filter presets, using the exact thresholds shared — only the fields
# marked below are actually enforced. Several fields from the original
# preset (snipers %, insiders %, bundle %, pro traders %, audit score,
# Dex Paid) are NOT enforced — there's no free/available data source
# for them (they require proprietary wallet-clustering and bundling
# analysis that specialized paid tools like Axiom provide). Treat
# those as "not implemented," not as silently passing — being honest
# about this gap matters more than faking a filter.
SNIPER_PRESETS = {
    "hyper_early_scalp": {
        "bonding_curve_min_pct": 0, "bonding_curve_max_pct": 15,
        "age_min_minutes": 2, "age_max_minutes": 45,
        "top10_holders_max_pct": 35,
        "dev_holding_max_pct": 10,
        "min_holders": 50,
        "require_socials": True,
        "require_ca_ends_pump": True,
    },
    "early_momentum_swing": {
        "bonding_curve_min_pct": 15, "bonding_curve_max_pct": 45,
        "age_min_minutes": 10, "age_max_minutes": 180,
        "top10_holders_max_pct": 30,
        "dev_holding_max_pct": 12,
        "min_holders": None,
        "require_socials": False,
        "require_ca_ends_pump": False,
    },
    "narrative_filter_hunt": {
        "bonding_curve_min_pct": 10, "bonding_curve_max_pct": 60,
        "age_min_minutes": 30, "age_max_minutes": 720,
        "top10_holders_max_pct": 28,
        "dev_holding_max_pct": 10,
        "min_holders": 300,
        "require_socials": True,
        "require_ca_ends_pump": False,
        "keywords_include": ["ai", "agent", "game", "tool", "analytics", "points", "infra", "depin", "studio"],
        "keywords_exclude": ["anti-sell", "reflection", "transfer tax", "rebase"],
    },
}

# Cupsey-style exit rules, applied specifically to Sniper Mode positions —
# recalibrated to a staged take-profit ladder instead of one small fixed
# target, per the more detailed breakdown: sell half at 2x, another chunk
# at 3-5x, let a small trimmed remainder ride under the main patient
# trailing-stop logic once both rungs have fired. Stop-loss widened to
# match the realistic -30% to -40% range instead of the earlier -10%
# guess. Max hold is now a backstop safety net, not the primary exit —
# the TP ladder and SL do most of the work.
CUPSEY_TP1_MULTIPLE = 1.20         # sell CUPSEY_TP1_FRACTION of the position at +20% — a target
                                    # actually reachable within the 1-minute hold cap, unlike the
                                    # old 2x (+100%) which most trades never got to in time
CUPSEY_TP1_FRACTION = 0.50
CUPSEY_TP2_MULTIPLE = 4.0          # sell CUPSEY_TP2_FRACTION (of the ORIGINAL size) at 4x (middle of 3-5x)
CUPSEY_TP2_FRACTION = 0.30
CUPSEY_STOP_LOSS_PCT = -0.35       # middle of the realistic -30% to -40% range
CUPSEY_MAX_HOLD_SECONDS = 60       # hard cap at 1 minute for ALL sniper-style plays, including priority copies
CUPSEY_DEV_SELL_EXIT_THRESHOLD_PCT = 0.5  # if the dev's holding drops to ≤50% of what it was at entry, exit — a real sell-off signal

# ── State ────────────────────────────────────────────────────────────

# DATA_DIR points at a mounted persistent volume in production (Railway:
# DATA_DIR=/data) so this file survives deploys/restarts instead of
# living in the working directory, which gets wiped every time. Defaults
# to "." for local dev, where no volume is mounted.
STATE_FILE = Path(os.environ.get("DATA_DIR", ".")) / "ledger_state.json"

# Set RESET_STATE_ON_BOOT=true (Railway Variables) to wipe the saved
# paper trading state on the next restart — useful for a clean start
# (e.g. switching to a new starting balance) without needing shell
# access to the container. Remember to unset it afterward, or it'll
# wipe progress on every future restart too.
RESET_STATE_ON_BOOT = os.environ.get("RESET_STATE_ON_BOOT", "false").lower() == "true"


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
    original_size_sol: float = 0.0  # sniper TP2 sizes off the ORIGINAL position, not what's left after TP1
    tp1_hit: bool = False  # has the 2x staged take-profit already fired?
    tp2_hit: bool = False  # has the 4x staged take-profit already fired?
    entry_dev_holding_pct: float = None  # dev's % holding at entry — used to detect a dev sell-off
    thesis: str = ""  # original reasoning at entry — gives later commentary/decisions real context
    commented_at_checkpoint: bool = False  # has the mid-hold live commentary already been posted?
    dip_buys: int = 0  # how many times this position has been averaged into on a drawdown
    entry_market_cap_usd: float = None  # market cap at entry — powers the clean "Entry MC -> Exit MC" close format
    target_size_sol: float = 0.0  # conviction-mode only: the FULL size calculated from risk_score — 0 for non-conviction positions (priority-copy, sniper), which never scale in
    topup_stage: int = 0  # conviction-mode pacing: 0 = only the initial fractional entry, 1 = first top-up done, 2 = fully topped up to target_size_sol


@dataclass
class LedgerState:
    balance_sol: float = STARTING_PAPER_BALANCE_SOL
    realized_pnl_sol: float = 0.0
    open_positions: dict = field(default_factory=dict)
    trade_log: list = field(default_factory=list)
    trades_this_hour: list = field(default_factory=list)  # timestamps
    seen_signatures: list = field(default_factory=list)   # avoid re-processing the same tx
    total_resets: int = 0             # how many times the bankroll has been wiped out and restarted
    run_start_balance: float = STARTING_PAPER_BALANCE_SOL  # baseline for the daily loss pause, reset each run
    run_start_time: str = None         # ISO timestamp — baseline for the 72h goal deadline, reset each run
    peak_balance: float = STARTING_PAPER_BALANCE_SOL  # highest balance ever seen this run — powers the -25% ultra-conservative trigger
    ultra_conservative_mode: bool = False  # halves position sizing while active
    goal_deadline_announced: bool = False  # so the 72h pass/fail announcement only fires once per run
    trading_paused_until: str = None  # ISO timestamp — no new buys accepted while set and in the future
    daily_target_hit_this_run: bool = False  # so the 10-SOL milestone only announces once per run

    def save(self):
        # Cap seen_signatures so this doesn't grow forever
        self.seen_signatures = self.seen_signatures[-2000:]
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        STATE_FILE.write_text(json.dumps(self.__dict__, indent=2, default=str))

    @classmethod
    def load(cls):
        if STATE_FILE.exists():
            data = json.loads(STATE_FILE.read_text())
            state = cls(**data)
        else:
            state = cls()
        if not state.run_start_time:
            state.run_start_time = datetime.now(timezone.utc).isoformat()
        return state


def get_top10_holder_pct(mint: str) -> float:
    """
    Returns the percentage of total token supply held by the top 10
    accounts, using standard (free) Solana RPC methods — no paid data
    service needed. This is one of the most commonly cited sniper
    safety filters: heavy concentration in a handful of wallets means
    those wallets can crash the price by selling at will.

    Returns None if unavailable (RPC error, token too new to have
    indexed supply data yet) — callers must treat that as "unknown,"
    never as "safe."
    """
    if not HELIUS_API_KEY:
        return None
    try:
        supply_payload = {"jsonrpc": "2.0", "id": 1, "method": "getTokenSupply", "params": [mint]}
        supply_resp = request_with_backoff("POST", HELIUS_RPC_URL, json=supply_payload, timeout=15)
        supply_data = supply_resp.json()
        total_supply = float(supply_data.get("result", {}).get("value", {}).get("uiAmount") or 0)
        if total_supply <= 0:
            return None

        largest_payload = {"jsonrpc": "2.0", "id": 1, "method": "getTokenLargestAccounts", "params": [mint]}
        largest_resp = request_with_backoff("POST", HELIUS_RPC_URL, json=largest_payload, timeout=15)
        largest_data = largest_resp.json()
        accounts = largest_data.get("result", {}).get("value", [])[:10]
        top10_amount = sum(float(a.get("uiAmount") or 0) for a in accounts)

        return (top10_amount / total_supply) * 100
    except Exception as e:
        print(f"[WARN] holder concentration check failed for {mint}: {e}")
        return None


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
        "max_tokens": 1500,  # Sonnet 5 reserves budget for adaptive thinking by default — low values can 400
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
        # Only journal entry point not wrapped in speak() — market
        # research never posts to Discord, but "read" is explicitly one
        # of the four journal kinds, and this is the only place in the
        # codebase where Ledger actually "reads" something (vs. acting
        # or commenting), so it gets logged directly here.
        log_journal(kind="read", text=summary)
    except requests.exceptions.HTTPError as e:
        body = e.response.text[:500] if e.response is not None else "no response body"
        print(f"[ERROR] market research pass HTTP error: {e} — body: {body}")
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

        transfers = tx.get("tokenTransfers", [])

        # A genuine buy means the wallet BOTH pays something out (native
        # SOL, or a stablecoin/wrapped-SOL token) AND receives the new
        # token, in the SAME transaction. Checking only the inflow isn't
        # enough — a token sent directly to the wallet by someone else,
        # or the wallet showing up as an incidental hop in someone
        # else's swap, would otherwise look identical to a real
        # purchase. Requiring a matching outflow confirms the wallet is
        # the one actually executing and paying for the trade. Checks
        # BOTH tokenTransfers (wrapped SOL/USDC/USDT) and nativeTransfers
        # (plain SOL) — Pump.fun buys typically pay in native SOL, which
        # Helius records separately from SPL token transfers.
        wallet_paid_out = any(
            transfer.get("fromUserAccount") == wallet_address
            and transfer.get("mint") in STABLE_OR_SOL_MINTS
            for transfer in transfers
        ) or any(
            native.get("fromUserAccount") == wallet_address
            and (native.get("amount") or 0) > 0
            for native in tx.get("nativeTransfers", [])
        )
        if not wallet_paid_out:
            continue

        for transfer in transfers:
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


def copy_priority_wallet_entry(
    token: str, wallet: str, trader_name: str, platform_name: str, metadata: dict, state: "LedgerState"
):
    """
    Directly mirrors a trusted wallet's buy — no independent-conviction
    gate, no "pass" possible, since it's trusted enough to copy
    outright. Uses the SAME sniper-style execution as Sniper Mode
    (Cupsey exit ladder via check_sniper_positions, same fast 1-minute
    hold cap), with confidence-based sizing up to
    PRIORITY_MAX_SIZE_MULTIPLIER instead of a fixed multiplier applied
    identically to every copy.

    Exceptions to "no pass possible" — none of these are conviction
    judgments, they're mechanical safety/sanity checks that apply
    regardless of how trusted the wallet is:
      - the wash-trading flag (get_wash_trading_flag) — the last
        hour's activity doesn't look like real trading;
      - PRIORITY_COPY_MAX_ENTRY_MARKET_CAP_USD — the token is well
        past memecoin-play territory (a trusted trader buying a $150M+
        or $1B+ MC asset isn't the "follow a degen wallet into a fresh
        play" scenario this mechanism exists for);
      - is_known_stablecoin() — the token is a stablecoin (or a
        copycat riding a stablecoin's ticker), which has no trading
        thesis at any market cap.
    """
    display_symbol = metadata.get("symbol") or token[:6] + "..."

    if token in state.open_positions:
        print(f"  [SKIP] {display_symbol}: already holding a position, not copying this buy.")
        return

    if is_known_stablecoin(token, metadata.get("symbol", "")):
        print(f"  [SKIP] {display_symbol}: known stablecoin — no trading thesis in copying a ~$1.00 peg.")
        log_journal(
            kind="refused",
            text=f"Skipped copying {trader_name}'s buy of {display_symbol} — known stablecoin, nothing to trade.",
            token_ticker=display_symbol,
            meta={"wallet": trader_name, "mint": token},
        )
        return

    _, entry_mc = get_liquidity_and_market_cap(token)
    if entry_mc is not None and entry_mc > PRIORITY_COPY_MAX_ENTRY_MARKET_CAP_USD:
        print(f"  [SKIP] {display_symbol}: market cap {format_market_cap(entry_mc)} exceeds priority-copy ceiling ({format_market_cap(PRIORITY_COPY_MAX_ENTRY_MARKET_CAP_USD)})")
        log_journal(
            kind="refused",
            text=f"Skipped copying {trader_name}'s buy of {display_symbol} — market cap {format_market_cap(entry_mc)} is well past a memecoin-play ceiling.",
            token_ticker=display_symbol,
            meta={"wallet": trader_name, "market_cap_usd": entry_mc},
        )
        return

    entry_price = get_sniper_entry_price(token)
    if entry_price is None:
        print(f"  [SKIP] {display_symbol}: no price data yet for this copy.")
        return

    wash_flag = get_wash_trading_flag(token)
    if wash_flag["suspicious"]:
        print(f"  [SKIP] {display_symbol}: wash-trading flag — {wash_flag['reason']}")
        log_journal(
            kind="refused",
            text=f"Skipped copying {trader_name}'s buy of {display_symbol} — {wash_flag['reason']}",
            token_ticker=display_symbol,
            meta={"wallet": trader_name, "h1_buys": wash_flag["h1_buys"], "h1_sells": wash_flag["h1_sells"]},
        )
        return

    dev_pct = get_dev_holding_pct(token, wallet)
    top10_pct = get_top10_holder_pct(token)
    # entry_mc already fetched above for the market-cap ceiling check — reused here, not re-fetched

    prior_entries = get_token_history(display_symbol, limit=5)
    judgment = get_entry_opinion(
        display_symbol, metadata.get("name", ""), trader_name, platform_name,
        top10_pct, dev_pct, max_multiplier=PRIORITY_MAX_SIZE_MULTIPLIER,
        history_context=summarize_token_history(prior_entries),
    )
    entry_opinion = judgment["opinion"]
    confidence_multiplier = judgment["confidence_multiplier"]

    ultra_conservative_multiplier = ULTRA_CONSERVATIVE_SIZE_MULTIPLIER if state.ultra_conservative_mode else 1.0
    size_sol = max(
        SNIPER_MIN_POSITION_SOL,
        state.balance_sol * SNIPER_POSITION_SIZE_PCT * confidence_multiplier * ultra_conservative_multiplier,
    )
    size_sol = min(size_sol, MAX_POSITION_SOL)

    ok, block_reason = can_open_position(state, size_sol)
    if not ok:
        print(f"  [BLOCKED] {display_symbol}: {block_reason}")
        return

    mc_display = format_market_cap(entry_mc)
    speak(
        title=f"⭐ TRADE OPENED — {display_symbol}",
        description=(
            f"Entry: `{mc_display}` · Size: `{size_sol:.4f} SOL` (confidence {confidence_multiplier:.1f}x)\n"
            f"Copying **{trader_name}** on {platform_name}\n"
            f"**{entry_opinion}**"
        ),
        color=COLOR_STRONG_SIGNAL,
        fields=[{"name": "CA:", "value": token, "inline": False}],
        journal_kind="did", token_ticker=display_symbol,
        journal_meta={"wallet": trader_name, "platform": platform_name, "size_sol": size_sol, "confidence_multiplier": confidence_multiplier, "prior_encounters": len(prior_entries)},
    )

    open_paper_position(
        state, token, entry_price, size_sol, opened_by=wallet, strength="strong",
        thesis=entry_opinion, entry_market_cap_usd=entry_mc,
    )
    if token in state.open_positions:
        # Tagged to contain "Sniper" so it correctly routes through
        # check_sniper_positions (the Cupsey ladder), not the main
        # trailing-stop logic — see the substring check in both.
        state.open_positions[token]["risk_level"] = "⭐ Priority Copy (Sniper)"
        state.open_positions[token]["original_size_sol"] = size_sol
        state.open_positions[token]["entry_dev_holding_pct"] = dev_pct
        state.save()


def analyze_conviction(
    token: str,
    metadata: dict,
    trigger_wallet_handle: str,
    trigger_platform: str,
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
            "risk_score": 7,
            "thesis": f"{trigger_wallet_handle} has taken a position here. No independent analysis available (ANTHROPIC_API_KEY not set) — sizing based on wallet trust alone.",
            "independent": False,
            "entry_condition": None,
            "invalidation": None,
            "criteria": {},
        }

    wallet_trust = "a standard tracked wallet, no special trust level"  # priority wallets never reach this function — see copy_priority_wallet_entry

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

    prompt = f"""You are Ledger, a moderate-risk Solana memecoin trader — degen enough to actually play the trenches, disciplined enough not to blow up. A wallet you track just bought a token. Evaluate independently whether YOU would enter this position — do not simply mirror the wallet's action.

Your risk tolerance: you are NOT an ultra-conservative institutional trader. Passing on every setup because it isn't perfect defeats the entire point of being in the trenches — decent, coherent setups deserve a scout position, not a pass. Reserve "pass" for genuine red flags: no coherent theme or narrative at all, a clear downtrend with a confirmed bearish break of structure and nothing offsetting it, or a token that's obviously a low-effort copy of an already-established "real" version of a trend. A setup that's merely uncertain, early, or thin on information is exactly what a small scout-sized "buy" with a higher risk_score is for — that's the tool for uncertainty, not passing.

Core principles you trade by:
- Never borrow conviction. A wallet buying something is one input, not a reason on its own. Ask: if I found this token myself with no wallet attached, would I still buy it — even as a small speculative scout position?
- Watch for "vamping" — when a narrative or trend goes viral, multiple competing tokens often launch around the same theme, and the crowd frequently buys the wrong (non-canonical) one before the real one is confirmed. If this token's appeal rests on a trend/narrative match, weigh how likely it is to be the token the community actually rallies around, versus a copycat that gets abandoned once the "real" one is identified. Treat unclear canonical status as a reason to raise the risk score, not to pass outright — being early on the right one is valuable, but so is being honest about the uncertainty.
- Read the market regime from your own recent research before sizing conviction — the same setup deserves more caution in quiet/risk-off conditions than in active/risk-on ones, but "quiet market" alone is not a reason to sit out entirely.
- A thesis should be something you could defend in two sentences. If you can't articulate a concrete reason beyond "the wallet bought it," that's a signal to pass or mark the risk high.
- Liquidity or volume alone is never a sufficient reason to enter — there must be a real reason behind the numbers, not just activity for its own sake.
- Loyalty to a dying position is a slow way to lose money — cut it once the thesis has broken, don't stay in just because you're already in.

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
Treat "insufficient_data" as neutral — don't penalize a token just for being too new to have chart history yet. A downtrend or bearish break of structure is a real reason for extra caution (higher risk_score, smaller size), but only a confirmed downtrend with no offsetting narrative strength should push you all the way to "pass." The chart confirms or challenges a thesis, it never replaces one.

Before writing your thesis, work through these six checks explicitly — a "holds" or "fails" verdict plus a one-sentence note for each. You have web search available: use it for check 3 specifically, to look up this token's actual recent trading activity instead of guessing.

1. recent_buy_sell_pressure — buyers vs sellers in the last hour: does real demand currently outweigh supply, or is it the other way around?
2. age_and_socials — is this project old enough / does it have a visible social presence and a site, or is it a blind, brand-new, anonymous launch?
3. volume_6h_substance — use web search to check this specific token's real 6-hour trading volume and activity level. Is it genuine, sustained volume, or is it thin, unverifiable, or inconsistent with the hype?
4. narrative_real_vs_fomo — is the narrative this token is riding grounded in something real (an actual event, product, launch, or trend), or is it just hype with nothing substantive behind it?
5. late_buyers_trapped — is there a large cohort of recent buyers sitting deep underwater who are likely to dump into any strength? "holds" = no meaningful trapped-buyer overhang; "fails" = yes, there's a real overhang.
6. counter_case — write the single strongest argument AGAINST entering this position. Then judge it honestly: does that counter-case actually hold up as a real reason to stay out ("holds" — it's a real problem), or is it weak and doesn't change your read ("fails" — the setup still stands)?

Decide independently, using the six checks above alongside the token's own merit (theme, timing, narrative fit, canonical-vs-copycat likelihood, chart structure) and the wallet signal: does this add up to at least a small speculative position, or are there genuine red flags that make this not worth even a scout-sized bet? Remember: uncertainty alone calls for a smaller size and a higher risk_score, not a pass. You should expect to say "buy" noticeably more often than "pass" for setups that have a coherent theme and no real red flags.

Only after working through all six checks do you write thesis_text, entry_condition (what you're watching for that would justify adding more size later), and invalidation (what would prove this thesis wrong and mean it's time to exit — tie this back to the "cut a dying position" principle above).

Respond with ONLY valid JSON, no other text, no markdown code fences:
{{"criteria": {{"recent_buy_sell_pressure": {{"verdict": "holds" or "fails", "note": "<one sentence>"}}, "age_and_socials": {{"verdict": "holds" or "fails", "note": "<one sentence>"}}, "volume_6h_substance": {{"verdict": "holds" or "fails", "note": "<one sentence>"}}, "narrative_real_vs_fomo": {{"verdict": "holds" or "fails", "note": "<one sentence>"}}, "late_buyers_trapped": {{"verdict": "holds" or "fails", "note": "<one sentence>"}}, "counter_case": {{"verdict": "holds" or "fails", "note": "<the strongest argument against entering, one sentence>"}}}}, "conviction": "buy" or "pass", "risk_score": <integer 0-10, 0=safest 10=most reckless>, "thesis": "<2-3 sentences, professional trader voice, correct grammar, reference the coin's lore/theme if known, no slang, never use a dollar sign character>", "entry_condition": "<one sentence: what you're watching for that would justify adding more size later>", "invalidation": "<one sentence: what would prove this thesis wrong and mean it's time to exit>", "independent": <true if your reasoning stands on the coin's own merit beyond just following the wallet, false if you are primarily following the wallet's lead>}}"""

    headers = {
        "x-api-key": ANTHROPIC_API_KEY,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }
    payload = {
        "model": ANTHROPIC_MODEL,
        "max_tokens": 2500,  # Sonnet 5 reserves budget for adaptive thinking by default — low values can 400;
                              # bumped further here since the six-point checklist plus web search results
                              # plus the final thesis/entry_condition/invalidation is a lot more output than before
        "messages": [{"role": "user", "content": prompt}],
        "tools": [{"type": "web_search_20250305", "name": "web_search"}],  # needed for check 3 (real 6h volume/activity)
    }

    try:
        resp = requests.post(ANTHROPIC_API_URL, headers=headers, json=payload, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        text = "".join(b["text"] for b in data.get("content", []) if b.get("type") == "text").strip()

        # Robust JSON extraction — handles accidental code fences or any
        # stray text around the JSON object, instead of failing (and
        # defaulting to "pass") on anything that isn't a perfectly
        # clean response.
        if text.startswith("```"):
            text = text.strip("`").lstrip("json").strip()
        json_match = re.search(r"\{.*\}", text, re.DOTALL)
        if json_match:
            text = json_match.group(0)
        result = json.loads(text)

        result["conviction"] = result.get("conviction", "pass")
        result["risk_score"] = max(0, min(10, int(result.get("risk_score", 10))))
        result["thesis"] = result.get("thesis", "").replace("$", "")  # extra safety net
        result["independent"] = bool(result.get("independent", False))
        result["entry_condition"] = (result.get("entry_condition") or "").replace("$", "") or None
        result["invalidation"] = (result.get("invalidation") or "").replace("$", "") or None
        result["criteria"] = result.get("criteria", {})
        return result
    except requests.exceptions.HTTPError as e:
        # Log the actual response body — the exception message alone
        # (e.g. "400 Client Error") doesn't say WHY, and Anthropic's
        # error body usually does (e.g. a specific invalid_request_error).
        body = e.response.text[:500] if e.response is not None else "no response body"
        print(f"[WARN] conviction analysis HTTP error for {token}: {e} — body: {body}")
        return {"conviction": "pass", "risk_score": 10, "thesis": "", "independent": False, "entry_condition": None, "invalidation": None, "criteria": {}}
    except Exception as e:
        print(f"[WARN] conviction analysis failed for {token}: {e} — defaulting to pass (skip, don't guess)")
        return {"conviction": "pass", "risk_score": 10, "thesis": "", "independent": False, "entry_condition": None, "invalidation": None, "criteria": {}}



# ── Paper trading engine ─────────────────────────────────────────────

def compute_total_equity(state: LedgerState) -> float:
    """Liquid balance + capital currently committed to open positions (approximate, ignores unrealized P&L)."""
    committed = sum(pos.get("size_sol", 0) for pos in state.open_positions.values())
    return state.balance_sol + committed


def can_open_position(state: LedgerState, size_sol: float) -> tuple[bool, str]:
    if size_sol > MAX_POSITION_SOL:
        return False, f"size {size_sol} exceeds MAX_POSITION_SOL ({MAX_POSITION_SOL})"
    if state.realized_pnl_sol <= -MAX_DAILY_LOSS_SOL:
        return False, "daily loss limit hit, no new positions"
    if size_sol > state.balance_sol:
        return False, "insufficient paper balance"
    if len(state.open_positions) >= MAX_CONCURRENT_SNIPER_POSITIONS:
        return False, f"max concurrent positions ({MAX_CONCURRENT_SNIPER_POSITIONS}) reached"
    if is_trading_paused(state):
        return False, f"trading paused until {state.trading_paused_until} (daily loss circuit breaker)"

    total_equity = compute_total_equity(state)
    if total_equity > 0:
        committed = sum(pos.get("size_sol", 0) for pos in state.open_positions.values())
        exposure_after = (committed + size_sol) / total_equity
        if exposure_after > MAX_TOTAL_EXPOSURE_PCT:
            return False, f"total exposure would exceed {MAX_TOTAL_EXPOSURE_PCT:.0%} of equity"

        balance_after = state.balance_sol - size_sol
        if balance_after < total_equity * MIN_RESERVE_PCT:
            return False, f"would drop reserve below {MIN_RESERVE_PCT:.0%} of equity"

    now = time.time()
    state.trades_this_hour = [t for t in state.trades_this_hour if now - t < 3600]
    if len(state.trades_this_hour) >= MAX_TRADES_PER_HOUR:
        return False, "hourly trade limit hit"

    return True, "ok"


def is_trading_paused(state: LedgerState) -> bool:
    if not state.trading_paused_until:
        return False
    return datetime.now(timezone.utc) < datetime.fromisoformat(state.trading_paused_until)


def check_daily_loss_pause(state: LedgerState):
    """
    A percentage-based circuit breaker, separate from the fixed
    MAX_DAILY_LOSS_SOL limit: if EQUITY (cash + capital committed to
    open positions — see compute_total_equity) drops 12% below where
    this run started, pause ALL new buys for 4 hours instead of
    continuing to trade through a bad stretch. Clears itself once the
    pause window elapses, starting a fresh baseline for the next
    stretch.

    Deliberately equity, not state.balance_sol: opening a position
    moves capital from cash into a position, it doesn't lose it —
    comparing raw cash against a cash baseline would read normal
    position-opening as a loss and could trip the pause on a perfectly
    healthy run (confirmed: 3x 0.5 SOL positions opened against a 10
    SOL run_start_balance — 15% of cash committed, zero SOL actually
    lost — used to trip this exact breaker before any of them closed).
    run_start_balance still stores an equity value despite the name;
    not renamed here to avoid an unrelated state-schema change.
    """
    if is_trading_paused(state):
        return  # still within an active pause — nothing to do yet

    if state.trading_paused_until:
        # Pause window has elapsed — clear it and start a fresh baseline
        state.trading_paused_until = None
        state.run_start_balance = compute_total_equity(state)
        state.save()
        return

    if state.run_start_balance <= 0:
        return
    drawdown_pct = (compute_total_equity(state) - state.run_start_balance) / state.run_start_balance
    if drawdown_pct <= DAILY_LOSS_PAUSE_PCT:
        pause_until = datetime.now(timezone.utc) + timedelta(hours=DAILY_LOSS_PAUSE_HOURS)
        state.trading_paused_until = pause_until.isoformat()
        speak(
            title="⏸️ Trading Paused — Daily Loss Limit",
            description=(
                f"Balance down {drawdown_pct:.1%} from the run's start ({state.run_start_balance:.4f} SOL). "
                f"Pausing all new buys for {DAILY_LOSS_PAUSE_HOURS}h — back at {pause_until.strftime('%H:%M UTC')}."
            ),
            color=COLOR_LOSS,
            journal_kind="commentary", journal_meta={"drawdown_pct": drawdown_pct},
        )
        state.save()


def check_ultra_conservative_mode(state: LedgerState):
    """
    Tracks the highest EQUITY ever seen this run (the "peak" — cash +
    capital committed to open positions, see compute_total_equity). If
    current equity falls 25% below that peak, switches on ultra-
    conservative mode (halved position sizing everywhere) until it
    recovers back above the trigger line. This is a DIFFERENT signal
    from the daily-loss pause above — that one resets its baseline
    each stretch, this one always measures from the best the run has
    ever done.

    Equity, not state.balance_sol, for the same reason as
    check_daily_loss_pause: opening positions moves cash into
    positions, it doesn't destroy it, and comparing raw cash against a
    cash peak would read normal position-opening as a fresh drawdown.
    peak_balance still stores an equity value despite the name; not
    renamed here to avoid an unrelated state-schema change.
    """
    current_equity = compute_total_equity(state)
    if current_equity > state.peak_balance:
        state.peak_balance = current_equity

    if state.peak_balance <= 0:
        return

    drawdown_from_peak = (current_equity - state.peak_balance) / state.peak_balance

    if not state.ultra_conservative_mode and drawdown_from_peak <= PEAK_DRAWDOWN_ULTRA_CONSERVATIVE_PCT:
        state.ultra_conservative_mode = True
        speak(
            title="🛡️ Ultra-Conservative Mode ON",
            description=(
                f"Balance down {drawdown_from_peak:.1%} from this run's peak ({state.peak_balance:.4f} SOL). "
                f"Halving position sizing until it recovers."
            ),
            color=COLOR_LOSS,
            journal_kind="commentary", journal_meta={"drawdown_from_peak": drawdown_from_peak},
        )
        state.save()
    elif state.ultra_conservative_mode and drawdown_from_peak > PEAK_DRAWDOWN_ULTRA_CONSERVATIVE_PCT:
        state.ultra_conservative_mode = False
        speak(
            title="🛡️ Ultra-Conservative Mode OFF",
            description="Balance has recovered — back to normal position sizing.",
            color=COLOR_PROFIT,
            journal_kind="commentary",
        )
        state.save()
    else:
        state.save()


def check_goal_deadline(state: LedgerState):
    """
    The 100 SOL / 72h goal is a hard deadline, not just an aspiration —
    announces pass or fail once the window elapses (doesn't force a
    reset on failure; check_for_blowup_reset already handles the case
    where the bankroll is actually gone).
    """
    if state.goal_deadline_announced or not state.run_start_time:
        return

    started = datetime.fromisoformat(state.run_start_time)
    hours_elapsed = (datetime.now(timezone.utc) - started).total_seconds() / 3600
    if hours_elapsed < GOAL_DEADLINE_HOURS:
        return

    state.goal_deadline_announced = True
    hit_goal = state.balance_sol >= DAILY_TARGET_SOL
    speak(
        title="⏰ 72-Hour Goal Deadline Reached",
        description=(
            f"{'🏆 Goal hit' if hit_goal else '📉 Goal not reached'} — "
            f"balance is {state.balance_sol:.4f} SOL vs the {DAILY_TARGET_SOL} SOL target "
            f"({(state.balance_sol / DAILY_TARGET_SOL * 100):.0f}% of the way there)."
        ),
        color=COLOR_PROFIT if hit_goal else COLOR_NEUTRAL,
        journal_kind="commentary", journal_meta={"hit_goal": hit_goal, "balance_sol": state.balance_sol},
    )
    state.save()


def open_paper_position(
    state: LedgerState, token: str, price: float, size_sol: float, opened_by: str = "", strength: str = "weak",
    thesis: str = "", entry_market_cap_usd: float = None, risk_score: int = None, target_size_sol: float = 0.0,
    entry_condition: str = None, invalidation: str = None,
):
    if token in state.open_positions:
        # Never silently overwrite an existing position — that would
        # deduct capital again while losing track of the first entry's
        # price, TP-ladder progress, and cost basis, corrupting the
        # paper trading numbers. Two different wallets buying the same
        # token is a real, useful signal, but it doesn't mean "buy
        # twice" — the existing position already has us in.
        print(f"[SKIP] {token}: already holding a position in this token, not opening a second one.")
        return

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
        thesis=thesis,
        entry_market_cap_usd=entry_market_cap_usd,
        target_size_sol=target_size_sol,
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
    upsert_thesis(
        ticker=display_name, token_mint=token, status="holding",
        thesis_text=thesis, risk_score=risk_score,
        entry_condition=entry_condition, invalidation=invalidation,
    )
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
    is_win = pnl >= 0
    change_pct = (exit_price - pos["entry_price"]) / pos["entry_price"]

    sol_price = get_sol_price_usd()
    pnl_usd = pnl * sol_price if sol_price else None
    balance_usd = state.balance_sol * sol_price if sol_price else None

    entry_mc = pos.get("entry_market_cap_usd")
    _, exit_mc = get_liquidity_and_market_cap(token)

    result_emoji = "🟢" if is_win else "❌"
    balance_str = f"`${balance_usd:,.0f} USDC`" if balance_usd is not None else f"`{state.balance_sol:.4f} SOL`"
    pnl_usd_bold = f" · **{'+${:,.0f}'.format(pnl_usd) if is_win else '-${:,.0f}'.format(abs(pnl_usd))} USDC**" if pnl_usd is not None else ""

    lines = [
        f"**Entry:** `{format_market_cap(entry_mc)}` → **Exit:** `{format_market_cap(exit_mc)}`",
        f"{result_emoji} **{change_pct:+.2%}**{pnl_usd_bold}  ({fraction:.0%} of position)",
        "",
        f"💵 **Balance:** {balance_str}",
    ]
    exit_opinion = get_exit_opinion(display_name, reason, pnl, change_pct)
    if exit_opinion:
        lines.append("")
        lines.append(f"**{exit_opinion}**")

    speak(
        title=f"💰 TRADE CLOSED — {display_name} ({reason})",
        description="\n".join(lines),
        color=COLOR_PROFIT if is_win else COLOR_LOSS,
        fields=[{"name": "CA:", "value": token, "inline": False}],
        journal_kind="did", token_ticker=display_name,
        journal_meta={"reason": reason, "pnl_sol": pnl, "fraction_sold": fraction, "change_pct": change_pct},
    )

    if pos["size_sol"] < 0.001:  # fully drained — close it out entirely
        del state.open_positions[token]
        # Position is genuinely gone now (not just trimmed) — this is
        # the point where the thesis should flip to "closed" too,
        # same as a full close_paper_position() would.
        upsert_thesis(ticker=display_name, status="closed")
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
    is_win = pnl >= 0
    change_pct = (exit_price - pos["entry_price"]) / pos["entry_price"]

    sol_price = get_sol_price_usd()
    pnl_usd = pnl * sol_price if sol_price else None
    balance_usd = state.balance_sol * sol_price if sol_price else None

    entry_mc = pos.get("entry_market_cap_usd")
    _, exit_mc = get_liquidity_and_market_cap(token)

    result_emoji = "🟢" if is_win else "❌"
    pnl_usd_bold = f" · **{'+${:,.0f}'.format(pnl_usd) if is_win else '-${:,.0f}'.format(abs(pnl_usd))} USDC**" if pnl_usd is not None else ""
    balance_str = f"`${balance_usd:,.0f} USDC`" if balance_usd is not None else f"`{state.balance_sol:.4f} SOL`"

    lines = [
        f"**Entry:** `{format_market_cap(entry_mc)}` → **Exit:** `{format_market_cap(exit_mc)}`",
        f"{result_emoji} **{change_pct:+.2%}**{pnl_usd_bold}",
        "",
        f"💵 **Balance:** {balance_str}",
    ]
    exit_opinion = get_exit_opinion(display_name, reason or "Position Closed", pnl, change_pct)
    if exit_opinion:
        lines.append("")
        lines.append(f"**{exit_opinion}**")

    speak(
        title=f"💰 TRADE CLOSED — {display_name}",
        description="\n".join(lines),
        color=COLOR_PROFIT if is_win else COLOR_LOSS,
        fields=[{"name": "CA:", "value": token, "inline": False}],
        journal_kind="did", token_ticker=display_name,
        journal_meta={"reason": reason, "pnl_sol": pnl, "change_pct": change_pct},
    )
    upsert_thesis(ticker=display_name, status="closed")
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

    Skips positions tagged "🎯 Sniper Play" — those use the separate,
    much faster Cupsey-style exit logic in check_sniper_positions()
    instead, since sniped positions are a different strategy entirely
    (fast scalp, never held long) from the patient trailing-stop
    approach here.

    Run this every cycle so positions aren't left unmonitored.
    """
    if not state.open_positions:
        return

    mints = [
        m for m, pos in state.open_positions.items()
        if "Sniper" not in pos.get("risk_level", "")
    ]
    if not mints:
        return
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

        # Conviction-mode pacing: top up toward target_size_sol on
        # confirmed growth, before any of the stop-loss/recovery/
        # trailing-stop logic below runs. Only ever ADDS capital to a
        # healthy position below its target — never touches the exit
        # paths, and non-conviction positions (target_size_sol == 0)
        # never enter this branch at all.
        if pos.get("target_size_sol", 0) > pos["size_sol"] + EPSILON:
            top_up_conviction_position(state, mint, current_price, change_pct)
            pos = state.open_positions.get(mint)
            if not pos:
                continue  # shouldn't happen from a top-up, but stay defensive

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


def top_up_conviction_position(state: LedgerState, token: str, current_price: float, change_pct: float):
    """
    Scales a conviction-mode position toward its target_size_sol in
    two fixed stages, gated on real price growth since entry — this is
    the "rest of the sizing" that CONVICTION_INITIAL_ENTRY_FRACTION
    deliberately left out of the initial buy. Uses price growth as the
    "sustained growth" signal rather than volume/holder-count deltas:
    those aren't tracked over time anywhere in this codebase today
    (get_approx_holder_count only ever sees a single snapshot, and
    per-token volume history isn't stored), while price is already
    fetched every cycle for every open position, so it's the one
    growth signal actually available without adding new state or
    extra API calls just for this.
    """
    pos = state.open_positions.get(token)
    if not pos:
        return

    target = pos.get("target_size_sol", 0)
    stage = pos.get("topup_stage", 0)

    if stage < 1 and change_pct >= CONVICTION_TOPUP_STAGE1_GROWTH_PCT:
        new_stage, target_fraction = 1, CONVICTION_TOPUP_STAGE1_TARGET_FRACTION
    elif stage < 2 and change_pct >= CONVICTION_TOPUP_STAGE2_GROWTH_PCT:
        new_stage, target_fraction = 2, CONVICTION_TOPUP_STAGE2_TARGET_FRACTION
    else:
        return  # no growth milestone crossed yet — nothing to do this cycle

    desired_size = target * target_fraction
    add_size = desired_size - pos["size_sol"]
    if add_size <= 0:
        return

    ok, reason = can_open_position(state, add_size)
    if not ok:
        print(f"[TOPUP BLOCKED] {token}: {reason}")
        return

    old_size, old_entry = pos["size_sol"], pos["entry_price"]
    new_size = old_size + add_size
    new_entry = (old_entry * old_size + current_price * add_size) / new_size

    state.balance_sol -= add_size
    pos["entry_price"] = new_entry
    pos["size_sol"] = new_size
    pos["topup_stage"] = new_stage
    state.open_positions[token] = pos
    state.save()

    display_name = pos.get("symbol") or token
    speak(
        title=f"➕ TOPPED UP — {display_name}",
        description=(
            f"Growth confirmed (+{change_pct:.1%} from entry) — added {add_size:.4f} SOL, "
            f"now {new_size:.4f} of {target:.4f} SOL target."
        ),
        color=COLOR_BUY,
        fields=[{"name": "CA:", "value": token, "inline": False}],
        journal_kind="did", token_ticker=display_name,
        journal_meta={"add_size_sol": add_size, "new_size_sol": new_size, "target_size_sol": target, "stage": new_stage},
    )


DIP_BUY_ADD_FRACTION = 0.5  # adds 50% of the original position size when buying a dip


def _format_journal_timestamp(ts: str) -> str:
    try:
        return datetime.fromisoformat(ts).strftime("%b %d %H:%M UTC")
    except Exception:
        return ts


def summarize_token_history(entries: list) -> str:
    """
    Turns raw journal_store entries for one token into a compact,
    human-readable line per past encounter, newest first — this is
    what gets dropped into the entry-opinion prompt as real memory of
    what Ledger already did with this exact token, instead of every
    repeat encounter reading as if it were the first. Informational
    only — never used to gate or block a copy decision.
    """
    if not entries:
        return ""

    lines = []
    for entry in entries:
        when = _format_journal_timestamp(entry.get("timestamp", ""))
        meta = entry.get("meta") or {}
        if "pnl_sol" in meta:
            result = "profit" if meta["pnl_sol"] >= 0 else "loss"
            lines.append(f"{when}: closed for a {result} ({meta['pnl_sol']:+.4f} SOL) — {meta.get('reason', 'exit')}")
        elif "confidence_multiplier" in meta and "size_sol" in meta:
            lines.append(f"{when}: opened at {meta['confidence_multiplier']:.1f}x confidence")
        elif "add_size_sol" in meta:
            lines.append(f"{when}: topped up on a confirmed dip")
        else:
            lines.append(f"{when}: {entry.get('text', '')}")

    return "; ".join(lines)


def get_entry_opinion(symbol: str, name: str, trader_name: str, platform_name: str, top10_pct: float, dev_pct: float, max_multiplier: float, history_context: str = None) -> dict:
    """
    Ledger's actual take on a token being copied from a trusted
    trader, plus a genuine confidence-based sizing decision — not a
    fixed multiplier applied identically every time. Returns
    {"opinion": str, "confidence_multiplier": float}, where the
    multiplier ranges from 1.0 (baseline size) up to max_multiplier
    (maximum conviction). No mention of "priority" in the wording —
    the trader is simply someone Ledger is copying, full stop.
    """
    if not ANTHROPIC_API_KEY:
        return {"opinion": f"{trader_name} entered.", "confidence_multiplier": 1.0}

    context_bits = [f"Trader: {trader_name} (a trusted trader you copy)", f"Platform: {platform_name}"]
    if top10_pct is not None:
        context_bits.append(f"Top 10 holders: {top10_pct:.1f}%")
    if dev_pct is not None:
        context_bits.append(f"Dev holding: {dev_pct:.1f}%")
    if history_context:
        context_bits.append(f"Ledger's history with this token: {history_context}")

    prompt = f"""You are Ledger. A trader you copy just bought a token, and you're mirroring the entry. Give your own brief, genuine take on THIS specific token — not a generic template line — and decide how much conviction this specific setup deserves.

Token: {symbol} ({name})
{chr(10).join(context_bits)}

Respond with ONLY valid JSON, no other text: {{"opinion": "<one short sentence, professional trader voice, correct grammar, no slang, no dollar signs — reference something specific about this token or situation, never mention 'priority' or 'trust' as the reason>", "confidence_multiplier": <number from 1.0 to {max_multiplier} — do not default to 1.0 out of habit; move up the scale whenever holder distribution and setup strength genuinely earn it, and down toward 1.0 only when the setup is truly just average>}}"""

    headers = {"x-api-key": ANTHROPIC_API_KEY, "anthropic-version": "2023-06-01", "content-type": "application/json"}
    payload = {"model": ANTHROPIC_MODEL, "max_tokens": 800, "messages": [{"role": "user", "content": prompt}]}  # Sonnet 5 reserves budget for adaptive thinking by default — low values can silently truncate the JSON and fall back to confidence_multiplier=1.0
    try:
        resp = requests.post(ANTHROPIC_API_URL, headers=headers, json=payload, timeout=20)
        resp.raise_for_status()
        data = resp.json()
        text = "".join(b["text"] for b in data.get("content", []) if b.get("type") == "text").strip()
        if text.startswith("```"):
            text = text.strip("`").lstrip("json").strip()
        json_match = re.search(r"\{.*\}", text, re.DOTALL)
        if json_match:
            text = json_match.group(0)
        result = json.loads(text)
        opinion = (result.get("opinion") or f"{trader_name} entered.").replace("$", "")
        multiplier = max(1.0, min(max_multiplier, float(result.get("confidence_multiplier", 1.0))))
        return {"opinion": opinion, "confidence_multiplier": multiplier}
    except requests.exceptions.HTTPError as e:
        body = e.response.text[:500] if e.response is not None else "no response body"
        print(f"[WARN] entry opinion HTTP error: {e} — body: {body}")
        return {"opinion": f"{trader_name} entered.", "confidence_multiplier": 1.0}
    except Exception as e:
        print(f"[WARN] entry opinion failed: {e}")
        return {"opinion": f"{trader_name} entered.", "confidence_multiplier": 1.0}


def get_snipe_confidence(symbol: str, name: str, top10_pct: float, dev_pct: float, max_multiplier: float, history_context: str = None) -> dict:
    """
    Same idea as get_entry_opinion, but for a Sniper Mode launch —
    no trader being copied, just Ledger's own read on the fresh
    launch itself, plus a confidence multiplier (1.0-max_multiplier)
    for sizing.
    """
    if not ANTHROPIC_API_KEY:
        return {"opinion": f"Sniped {symbol}.", "confidence_multiplier": 1.0}

    context_bits = []
    if top10_pct is not None:
        context_bits.append(f"Top 10 holders: {top10_pct:.1f}%")
    if dev_pct is not None:
        context_bits.append(f"Dev holding: {dev_pct:.1f}%")
    if history_context:
        context_bits.append(f"Ledger's history with this token: {history_context}")

    prompt = f"""You are Ledger, sniping a fresh Pump.fun launch that already passed your safety filters. Give a brief, genuine take on THIS specific token, and decide how much conviction it deserves.

Token: {symbol} ({name})
{chr(10).join(context_bits) if context_bits else "No holder data available yet."}

Respond with ONLY valid JSON, no other text: {{"opinion": "<one short sentence, professional trader voice, correct grammar, no slang, no dollar signs>", "confidence_multiplier": <number from 1.0 to {max_multiplier} — do not default to 1.0 out of habit; move up the scale whenever the theme/name and holder distribution genuinely earn it, and down toward 1.0 only when the setup is truly just average>}}"""

    headers = {"x-api-key": ANTHROPIC_API_KEY, "anthropic-version": "2023-06-01", "content-type": "application/json"}
    payload = {"model": ANTHROPIC_MODEL, "max_tokens": 800, "messages": [{"role": "user", "content": prompt}]}  # Sonnet 5 reserves budget for adaptive thinking by default — low values can silently truncate the JSON and fall back to confidence_multiplier=1.0
    try:
        resp = requests.post(ANTHROPIC_API_URL, headers=headers, json=payload, timeout=20)
        resp.raise_for_status()
        data = resp.json()
        text = "".join(b["text"] for b in data.get("content", []) if b.get("type") == "text").strip()
        if text.startswith("```"):
            text = text.strip("`").lstrip("json").strip()
        json_match = re.search(r"\{.*\}", text, re.DOTALL)
        if json_match:
            text = json_match.group(0)
        result = json.loads(text)
        opinion = (result.get("opinion") or f"Sniped {symbol}.").replace("$", "")
        multiplier = max(1.0, min(max_multiplier, float(result.get("confidence_multiplier", 1.0))))
        return {"opinion": opinion, "confidence_multiplier": multiplier}
    except requests.exceptions.HTTPError as e:
        body = e.response.text[:500] if e.response is not None else "no response body"
        print(f"[WARN] snipe confidence HTTP error: {e} — body: {body}")
        return {"opinion": f"Sniped {symbol}.", "confidence_multiplier": 1.0}
    except Exception as e:
        print(f"[WARN] snipe confidence failed: {e}")
        return {"opinion": f"Sniped {symbol}.", "confidence_multiplier": 1.0}


def get_exit_opinion(symbol: str, reason: str, pnl: float, change_pct: float) -> str:
    """
    A short reflective comment on how a trade actually went, attached
    to every exit — win or loss — instead of leaving the numbers to
    speak for themselves with no read on it.
    """
    if not ANTHROPIC_API_KEY:
        return ""

    prompt = f"""You are Ledger, reflecting briefly on a trade that just closed.

Token: {symbol}
Exit reason: {reason}
Result: {change_pct:+.1%} ({pnl:+.4f} SOL)

Write ONE short sentence, professional trader voice, correct grammar, no slang, no dollar signs — a genuine read on how this one played out. Respond with ONLY the sentence, no quotes, no JSON."""

    headers = {"x-api-key": ANTHROPIC_API_KEY, "anthropic-version": "2023-06-01", "content-type": "application/json"}
    payload = {"model": ANTHROPIC_MODEL, "max_tokens": 500, "messages": [{"role": "user", "content": prompt}]}  # Sonnet 5 reserves budget for adaptive thinking by default — low values can silently truncate the reflection to an empty string
    try:
        resp = requests.post(ANTHROPIC_API_URL, headers=headers, json=payload, timeout=20)
        resp.raise_for_status()
        data = resp.json()
        text = "".join(b["text"] for b in data.get("content", []) if b.get("type") == "text").strip()
        return text.replace("$", "")
    except requests.exceptions.HTTPError as e:
        body = e.response.text[:500] if e.response is not None else "no response body"
        print(f"[WARN] exit opinion HTTP error: {e} — body: {body}")
        return ""
    except Exception as e:
        print(f"[WARN] exit opinion failed: {e}")
        return ""


def get_live_trade_judgment(pos: dict, current_price: float, change_pct: float, held_seconds: float, situation: str) -> dict:
    """
    Asks Claude to treat THIS specific trade individually — a short,
    trader-voice live comment plus (only in a drawdown situation) a
    real decision between averaging into the dip or cutting losses,
    instead of a single fixed mechanical rule applied identically to
    every position. Falls back to a plain hold with no comment if
    ANTHROPIC_API_KEY isn't configured, or if anything about the call
    fails — never let commentary generation risk the actual position
    management.
    """
    if not ANTHROPIC_API_KEY:
        return {"action": "hold", "comment": ""}

    symbol = pos.get("symbol") or pos.get("token", "")[:6]
    thesis = pos.get("thesis") or "No stored thesis for this entry."

    prompt = f"""You are Ledger, checking in on a live sniper position — treat this trade on its own terms, not as a generic rule application.

Token: {symbol}
Original thesis at entry: {thesis}
Entry price: {pos['entry_price']:.10g}
Current price: {current_price:.10g} ({change_pct:+.1%})
Time held: {held_seconds:.0f}s (hard cap: {CUPSEY_MAX_HOLD_SECONDS}s)

Situation: {situation}

Respond with ONLY valid JSON, no other text: {{"action": "hold" or "buy_dip" or "stop_loss" or "take_profit", "comment": "<one short sentence, trader voice, no dollar signs, no slang>"}}

"buy_dip" only makes sense if the situation is a drawdown and you'd genuinely add to a position you still believe in. "stop_loss" means cut it now. "hold" means no action, just watching. "take_profit" only if the situation explicitly offers that choice."""

    headers = {
        "x-api-key": ANTHROPIC_API_KEY,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }
    payload = {
        "model": ANTHROPIC_MODEL,
        "max_tokens": 600,  # Sonnet 5 reserves budget for adaptive thinking by default — low values can 400
        "messages": [{"role": "user", "content": prompt}],
    }
    try:
        resp = requests.post(ANTHROPIC_API_URL, headers=headers, json=payload, timeout=20)
        resp.raise_for_status()
        data = resp.json()
        text = "".join(b["text"] for b in data.get("content", []) if b.get("type") == "text").strip()
        if text.startswith("```"):
            text = text.strip("`").lstrip("json").strip()
        json_match = re.search(r"\{.*\}", text, re.DOTALL)
        if json_match:
            text = json_match.group(0)
        result = json.loads(text)
        result["comment"] = (result.get("comment") or "").replace("$", "")
        result["action"] = result.get("action", "hold")
        return result
    except requests.exceptions.HTTPError as e:
        body = e.response.text[:500] if e.response is not None else "no response body"
        print(f"[WARN] live trade judgment HTTP error: {e} — body: {body}")
        return {"action": "hold", "comment": ""}
    except Exception as e:
        print(f"[WARN] live trade judgment failed: {e} — defaulting to hold, no comment")
        return {"action": "hold", "comment": ""}


def buy_the_dip(state: LedgerState, token: str, current_price: float):
    """
    Averages more capital into an existing position at a lower price,
    instead of cutting it — used when get_live_trade_judgment decides
    a drawdown is worth adding to rather than stopping out of. Adds
    DIP_BUY_ADD_FRACTION of the position's ORIGINAL size, capped by
    the normal risk limits like any other buy.
    """
    pos = state.open_positions.get(token)
    if not pos:
        return

    original_size = pos.get("original_size_sol") or pos["size_sol"]
    add_size = original_size * DIP_BUY_ADD_FRACTION

    ok, reason = can_open_position(state, add_size)
    if not ok:
        print(f"[DIP BUY BLOCKED] {token}: {reason}")
        return

    old_size = pos["size_sol"]
    old_entry = pos["entry_price"]
    new_size = old_size + add_size
    new_entry = (old_entry * old_size + current_price * add_size) / new_size

    state.balance_sol -= add_size
    pos["entry_price"] = new_entry
    pos["size_sol"] = new_size
    pos["dip_buys"] = pos.get("dip_buys", 0) + 1
    state.save()
    print(f"[DIP BUY] {token}: added {add_size:.4f} SOL at {current_price:.10g}, new avg entry {new_entry:.10g}")


def get_sniper_exit_price(mint: str) -> float:
    """
    Live exit price for Sniper Mode / priority-copy positions via
    DexScreener's priceUsd — used ONLY inside check_sniper_positions(),
    instead of the Jupiter Price API V3 (get_token_prices_usd) used
    everywhere else in this file.

    Why: live-tested on 2026-08-18. The Jupiter V3 price for BONK (one
    of the most liquid memecoins on Solana) returned the IDENTICAL
    blockId and usdPrice across a 25-second gap between two separate
    HTTP requests — its price snapshot simply hadn't refreshed, and
    the returned blockId was already ~56 Solana slots (~22s) behind
    the live chain tip. That's a snapshot, not a live quote, and it's
    too stale for a strategy with a 60-second hold. The same test
    against DexScreener — BONK plus two freshly-launched Pump.fun
    mints, three polls ~22s apart — showed BONK equally frozen (likely
    just no real trades on that pool right then), but BOTH Pump.fun
    mints showed real, moving priceUsd between polls. DexScreener is
    demonstrably fresher for the low-liquidity, minutes-old tokens
    Sniper Mode actually trades.

    Scout/conviction positions are held far longer than a minute, so
    they don't have the same urgency and keep using
    get_token_prices_usd() (Jupiter) unchanged.

    Note: entry prices for these same positions are still captured via
    Jupiter (evaluate_snipe_candidate / copy_priority_wallet_entry) —
    this function only changes the EXIT side, per the specific ask.
    That means change_pct is now computed from two different price
    sources (Jupiter entry vs DexScreener exit), which could itself
    introduce a small systematic offset if the two providers quote
    slightly different effective prices for the same pool — worth
    watching, not something this change addresses.

    Returns None if the mint has no Solana pair on DexScreener yet, or
    the request fails — same "missing data is never a green light"
    convention as every other price/market-data helper in this file.
    """
    try:
        resp = requests.get(
            "https://api.dexscreener.com/latest/dex/tokens/" + mint, timeout=15
        )
        resp.raise_for_status()
        pairs = resp.json().get("pairs") or []
        solana_pairs = [p for p in pairs if p.get("chainId") == "solana"]
        if not solana_pairs:
            return None
        pair = max(solana_pairs, key=lambda p: (p.get("liquidity") or {}).get("usd", 0) or 0)
        price_str = pair.get("priceUsd")
        return float(price_str) if price_str is not None else None
    except Exception as e:
        print(f"[WARN] sniper exit price fetch failed for {mint}: {e}")
        return None


def get_sniper_entry_price(mint: str) -> float:
    """
    Entry-side counterpart to get_sniper_exit_price() — same
    DexScreener priceUsd source, same staleness rationale, just
    delegating to it directly (the logic doesn't differ; this exists
    as its own name purely for readability at the entry call sites).

    Used ONLY at the two sniper/priority-copy entry points
    (evaluate_snipe_candidate, copy_priority_wallet_entry) so that
    entry_price and the exit price fetched later in
    check_sniper_positions come from the SAME provider. Mixing Jupiter
    (entry) with DexScreener (exit) was confirmed live to produce a
    change_pct that doesn't match the entry_mc -> exit_mc move shown
    in the same message — e.g. BOLLOCKS: Entry $298K MC -> Exit $283K
    MC (a real ~5% drop) displayed as +0.60%, because the % was being
    computed from two different providers' quotes for two different
    moments. This closes that gap for the sniper/priority-copy path.
    Scout/conviction entries (main loop) keep using
    get_token_prices_usd() (Jupiter) — they're held far longer than a
    minute, so this mismatch never mattered there.
    """
    return get_sniper_exit_price(mint)


def check_sniper_positions(state: LedgerState):
    """
    Cupsey-style exit for Sniper Mode positions — capped at a hard
    1-minute hold (positions were lingering far too long before), a
    staged take-profit ladder (sell half at 2x, another chunk at 4x),
    a dev-sell trigger (exit immediately if the creator wallet appears
    to be dumping), and — instead of a single fixed stop-loss rule
    applied identically to every trade — a live, per-trade judgment
    call: on a real drawdown, Ledger decides whether to average into
    the dip or cut losses, and posts a short comment either way. Also
    posts one brief live comment mid-hold on positions that haven't
    hit any trigger yet, so it's not silent between open and close.
    """
    sniper_mints = [
        m for m, pos in state.open_positions.items()
        if "Sniper" in pos.get("risk_level", "")
    ]
    if not sniper_mints:
        return

    now = datetime.now(timezone.utc)
    EPSILON = 1e-9

    for mint in sniper_mints:
        pos = state.open_positions.get(mint)
        if not pos:
            continue

        opened_at = datetime.fromisoformat(pos["opened_at"])
        held_seconds = (now - opened_at).total_seconds()
        # DexScreener, not Jupiter — see get_sniper_exit_price()'s
        # docstring for why. Fetched per-mint (no batch endpoint on
        # DexScreener), same pattern get_liquidity_and_market_cap()
        # already uses elsewhere in this file.
        current_price = get_sniper_exit_price(mint)

        # Dev-sell check — a real red flag, checked regardless of price data
        if pos.get("entry_dev_holding_pct"):
            current_dev_pct = get_dev_holding_pct(mint, pos.get("opened_by", ""))
            if current_dev_pct is not None:
                if current_dev_pct <= pos["entry_dev_holding_pct"] * CUPSEY_DEV_SELL_EXIT_THRESHOLD_PCT:
                    if current_price is not None:
                        close_paper_position(state, mint, current_price, reason="🚨 Dev Sell Detected — Exiting")
                    continue

        if held_seconds >= CUPSEY_MAX_HOLD_SECONDS:
            if current_price is not None:
                close_paper_position(state, mint, current_price, reason="⏱️ Sniper Time Exit")
            continue

        if current_price is None:
            continue  # no price yet this cycle — check again next cycle

        change_pct = (current_price - pos["entry_price"]) / pos["entry_price"]

        if change_pct <= CUPSEY_STOP_LOSS_PCT + EPSILON:
            # Drawdown territory — instead of always closing, ask for a
            # real per-trade judgment: buy the dip, or cut it here.
            judgment = get_live_trade_judgment(
                pos, current_price, change_pct, held_seconds,
                situation=f"Down {change_pct:.1%} from entry — decide whether this specific setup is worth averaging into, or whether to cut it now.",
            )
            if judgment["comment"]:
                speak(
                    title=f"💬 {pos.get('symbol') or mint[:6]}", description=judgment["comment"], color=COLOR_NEUTRAL,
                    journal_kind="commentary", token_ticker=pos.get("symbol") or mint[:6],
                )
            if judgment["action"] == "buy_dip":
                buy_the_dip(state, mint, current_price)
            else:
                close_paper_position(state, mint, current_price, reason="🎯 Sniper Stop Loss")
            continue

        current_multiple = 1 + change_pct

        if not pos["tp1_hit"] and current_multiple >= CUPSEY_TP1_MULTIPLE - EPSILON:
            partial_close_paper_position(state, mint, current_price, CUPSEY_TP1_FRACTION, reason=f"🎯 Sniper TP1 (+{(CUPSEY_TP1_MULTIPLE-1)*100:.0f}%)")
            if mint in state.open_positions:
                state.open_positions[mint]["tp1_hit"] = True
                state.save()
            continue

        if pos["tp1_hit"] and not pos["tp2_hit"] and current_multiple >= CUPSEY_TP2_MULTIPLE - EPSILON:
            # TP2 sizes off the ORIGINAL position, not what's left after TP1
            original_size = pos.get("original_size_sol") or pos["size_sol"]
            fraction_of_remaining = min(1.0, (CUPSEY_TP2_FRACTION * original_size) / pos["size_sol"])
            partial_close_paper_position(state, mint, current_price, fraction_of_remaining, reason="🎯 Sniper TP2 (4x)")
            if mint in state.open_positions:
                state.open_positions[mint]["tp2_hit"] = True
                # Both rungs fired — hand the trimmed remainder off to the
                # main patient trailing-stop logic instead of Cupsey's fast
                # exit, matching "let a runner ride only if distribution
                # improves" from the notes you shared. Deliberately does
                # NOT contain "Sniper" in the tag — that substring is what
                # both check_sniper_positions and check_open_positions use
                # to decide which function handles a given position, so
                # this is what actually moves it from one system to the
                # other, not just cosmetic labeling.
                state.open_positions[mint]["risk_level"] = "🏃 Trimmed Runner (trailing stop)"
                state.open_positions[mint]["initial_recovered"] = True
                state.open_positions[mint]["peak_price"] = current_price
                state.save()
            continue

        # No trigger fired — still holding. Post one brief live comment
        # partway through the hold (around the halfway mark) instead of
        # staying silent from open to close. Only ever fires once per
        # position, so it doesn't spam every ~20s check.
        halfway_point = CUPSEY_MAX_HOLD_SECONDS / 2
        if not pos.get("commented_at_checkpoint") and held_seconds >= halfway_point:
            judgment = get_live_trade_judgment(
                pos, current_price, change_pct, held_seconds,
                situation="Routine mid-hold check-in — no target or stop has been hit yet, just give a brief live read on how it's going.",
            )
            if judgment["comment"]:
                speak(
                    title=f"💬 {pos.get('symbol') or mint[:6]}", description=judgment["comment"], color=COLOR_NEUTRAL,
                    journal_kind="commentary", token_ticker=pos.get("symbol") or mint[:6],
                )
            if mint in state.open_positions:
                state.open_positions[mint]["commented_at_checkpoint"] = True
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


# ── Performance analysis — "learning from losses" ────────────────────

MONTHLY_PROFIT_GOAL_USD = 500  # paper-trading target — see note in recap message

# Degen-mode framing: small starting bankroll, aggressive daily target,
# auto-reset when it's wiped out. This is intentionally a much shorter,
# higher-variance goal than the monthly USD target above — the two
# coexist; this one is about the SOL balance itself, checked every cycle.
DAILY_TARGET_SOL = 100.0    # the 3-day (72h) goal from the 10 SOL start
BLOWUP_DUST_THRESHOLD_SOL = 0.01  # below this (and no open positions), treat the bankroll as wiped out
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
    sniper_exits = [t for t in exits if "Sniper" in t.get("risk_level", "")]
    scout_exits = [
        t for t in exits
        if "Lower Risk" not in t.get("risk_level", "") and "Sniper" not in t.get("risk_level", "")
    ]

    return {
        "total_exits": len(exits),
        "win_rate_pct": win_rate(exits),
        "whale_win_rate_pct": win_rate(whale_exits),
        "whale_exit_count": len(whale_exits),
        "sniper_win_rate_pct": win_rate(sniper_exits),
        "sniper_exit_count": len(sniper_exits),
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


def check_for_blowup_reset(state: LedgerState):
    """
    Degen-mode safety valve: if the bankroll is effectively wiped out
    (dust-level balance, nothing tied up in open positions either),
    reset back to the starting 1 SOL and keep going — "if he loses it
    all, restart" was the explicit instruction. Tracks how many times
    this has happened, so the recap can show whether the approach is
    actually working over many attempts or just repeatedly blowing up.
    """
    if state.open_positions:
        return  # capital is tied up, not gone — don't reset mid-position
    if state.balance_sol > BLOWUP_DUST_THRESHOLD_SOL:
        return

    state.total_resets += 1
    state.balance_sol = STARTING_PAPER_BALANCE_SOL
    state.daily_target_hit_this_run = False
    state.run_start_balance = STARTING_PAPER_BALANCE_SOL
    state.run_start_time = datetime.now(timezone.utc).isoformat()
    state.peak_balance = STARTING_PAPER_BALANCE_SOL
    state.ultra_conservative_mode = False
    state.goal_deadline_announced = False
    state.trading_paused_until = None
    speak(
        title="💀 Bankroll Wiped — Restarting",
        description=(
            f"Balance hit dust ({BLOWUP_DUST_THRESHOLD_SOL} SOL floor). "
            f"Resetting to {STARTING_PAPER_BALANCE_SOL} SOL and starting the next run.\n\n"
            f"**Total resets so far:** {state.total_resets}"
        ),
        color=COLOR_LOSS,
        journal_kind="commentary", journal_meta={"total_resets": state.total_resets},
    )
    state.save()


def check_for_daily_target_hit(state: LedgerState):
    """Announces once per run (i.e. once per reset cycle) when the balance reaches the 10 SOL target."""
    if state.daily_target_hit_this_run:
        return
    if state.balance_sol < DAILY_TARGET_SOL:
        return

    state.daily_target_hit_this_run = True
    speak(
        title="🏆 Daily Target Hit",
        description=(
            f"Balance reached {state.balance_sol:.4f} SOL — past the {DAILY_TARGET_SOL} SOL target. "
            f"Still running, not resetting on a win — only a wipeout triggers a restart."
        ),
        color=COLOR_PROFIT,
        journal_kind="commentary",
    )
    state.save()


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
    if stats["sniper_win_rate_pct"] is not None:
        lines.append(f"**Sniper win rate:** {stats['sniper_win_rate_pct']:.0f}% ({stats['sniper_exit_count']} exits)")
    lines.append(f"**Total realized PnL:** {stats['total_pnl_sol']:+.4f} SOL")
    lines.append(f"**Current balance:** {state.balance_sol:.4f} SOL  •  **Bankroll resets so far:** {state.total_resets}")

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
        journal_kind="commentary", journal_meta={"total_pnl_sol": stats["total_pnl_sol"], "total_exits": stats["total_exits"]},
    )


# ── Sniper Mode: launch listener + evaluation ─────────────────────────

SNIPER_LAUNCH_QUEUE = queue.Queue()


def _sniper_listener_thread():
    """
    Runs in a background thread so the WebSocket connection doesn't
    block the main polling loop. Connects to the free Pump.fun launch
    feed and pushes every new-token event into a thread-safe queue for
    the main loop to evaluate. Reconnects automatically on drops.
    """
    import websockets

    async def listen():
        while True:
            try:
                async with websockets.connect(SNIPER_WS_URL) as ws:
                    await ws.send(json.dumps({"method": "subscribeNewToken"}))
                    print("[SNIPER] Connected to launch feed.")
                    async for raw_msg in ws:
                        try:
                            event = json.loads(raw_msg)
                            SNIPER_LAUNCH_QUEUE.put(event)
                        except json.JSONDecodeError:
                            continue
            except Exception as e:
                print(f"[SNIPER] Listener connection error, retrying in 10s: {e}")
                time.sleep(10)

    asyncio.run(listen())


def start_sniper_listener():
    thread = threading.Thread(target=_sniper_listener_thread, daemon=True)
    thread.start()
    print("[SNIPER] Listener thread started.")


def evaluate_snipe_candidate(candidate: dict, state: "LedgerState"):
    """
    Runs a launch through the preset's feasible filters once it has
    aged into the preset's [age_min, age_max] window — this matters:
    the "Hyper-Early Scalp" preset targets tokens 2-45 minutes old,
    not the literal instant of launch, since bonding curve progress
    and holder count need a little time to form. See SNIPER_PRESETS
    for exactly which fields are enforced and which aren't (several
    of the originally described fields — snipers %, insiders %,
    bundle %, pro traders %, audit score, Dex Paid — have no free
    data source and are honestly skipped, not faked).
    """
    preset = SNIPER_PRESETS[SNIPER_ACTIVE_PRESET]
    mint = candidate["mint"]
    symbol = candidate.get("symbol", "?")
    name = candidate.get("name", "")

    if mint in state.open_positions:
        print(f"[SNIPE SKIP] {symbol}: already holding a position in this token.")
        return

    if candidate.get("initial_buy_sol", 0) < SNIPER_MIN_DEV_BUY_SOL:
        print(f"[SNIPE SKIP] {symbol}: dev buy too low ({candidate.get('initial_buy_sol', 0)} SOL)")
        return

    # Narrative-Filter Hunt: keyword gate, checked against name+symbol
    if "keywords_include" in preset:
        text = f"{name} {symbol}".lower()
        if not any(kw in text for kw in preset["keywords_include"]):
            print(f"[SNIPE SKIP] {symbol}: no matching narrative keyword")
            return
        if any(kw in text for kw in preset.get("keywords_exclude", [])):
            print(f"[SNIPE SKIP] {symbol}: matched an excluded (scam-pattern) keyword")
            return

    if preset.get("require_ca_ends_pump") and not mint.endswith("pump"):
        print(f"[SNIPE SKIP] {symbol}: CA doesn't end in 'pump'")
        return

    top10_pct = get_top10_holder_pct(mint)
    if top10_pct is not None and top10_pct > preset["top10_holders_max_pct"]:
        print(f"[SNIPE SKIP] {symbol}: top 10 holders too concentrated ({top10_pct:.1f}%)")
        return

    dev_pct = get_dev_holding_pct(mint, candidate.get("creator", ""))
    if dev_pct is not None and dev_pct > preset["dev_holding_max_pct"]:
        print(f"[SNIPE SKIP] {symbol}: dev holding too high ({dev_pct:.1f}%)")
        return

    if preset.get("min_holders"):
        holder_count = get_approx_holder_count(mint)
        if holder_count is not None and holder_count < preset["min_holders"]:
            print(f"[SNIPE SKIP] {symbol}: too few holders ({holder_count} < {preset['min_holders']})")
            return

    if preset.get("require_socials"):
        has_socials = bool(candidate.get("twitter") or candidate.get("telegram") or candidate.get("website"))
        if not has_socials:
            print(f"[SNIPE SKIP] {symbol}: no social links present")
            return

    liquidity_usd, market_cap_usd = get_liquidity_and_market_cap(mint)
    if liquidity_usd is not None and liquidity_usd < SNIPER_MIN_LIQUIDITY_USD:
        print(f"[SNIPE SKIP] {symbol}: liquidity too thin (${liquidity_usd:,.0f})")
        return
    if market_cap_usd is not None and market_cap_usd > SNIPER_MAX_ENTRY_MARKET_CAP_USD:
        print(f"[SNIPE SKIP] {symbol}: market cap too high for an early entry (${market_cap_usd:,.0f})")
        return

    wash_flag = get_wash_trading_flag(mint)
    if wash_flag["suspicious"]:
        print(f"[SNIPE SKIP] {symbol}: wash-trading flag — {wash_flag['reason']}")
        return

    entry_price = get_sniper_entry_price(mint)
    if entry_price is None:
        print(f"[SNIPE SKIP] {symbol}: no price data yet")
        return

    # Selection is now based on genuine confidence instead of a coin
    # flip — a token that passed every mechanical filter still only
    # gets bought if Ledger's own read on it clears a real conviction
    # bar, not at random.
    prior_entries = get_token_history(symbol, limit=5)
    judgment = get_snipe_confidence(
        symbol, name, top10_pct, dev_pct, max_multiplier=SNIPER_MAX_SIZE_MULTIPLIER,
        history_context=summarize_token_history(prior_entries),
    )
    snipe_opinion = judgment["opinion"]
    confidence_multiplier = judgment["confidence_multiplier"]

    if confidence_multiplier < SNIPER_MIN_CONFIDENCE_TO_ENTER:
        print(f"[SNIPE SKIP] {symbol}: passed filters but confidence too low ({confidence_multiplier:.1f}x < {SNIPER_MIN_CONFIDENCE_TO_ENTER}x)")
        return

    # Size as a % of CURRENT bankroll, scaled by confidence — this
    # scales automatically as the balance grows toward the 10 SOL
    # target or resets to 1 SOL after a wipeout.
    ultra_conservative_multiplier = ULTRA_CONSERVATIVE_SIZE_MULTIPLIER if state.ultra_conservative_mode else 1.0
    size_sol = max(
        SNIPER_MIN_POSITION_SOL,
        state.balance_sol * SNIPER_POSITION_SIZE_PCT * confidence_multiplier * ultra_conservative_multiplier,
    )

    ok, block_reason = can_open_position(state, size_sol)
    if not ok:
        print(f"[SNIPE BLOCKED] {symbol}: {block_reason}")
        return

    mc_display = format_market_cap(market_cap_usd)
    speak(
        title=f"🎯 TRADE OPENED — {symbol}",
        description=(
            f"Entry: `{mc_display}` · Size: `{size_sol:.4f} SOL` (confidence {confidence_multiplier:.1f}x)\n"
            f"**{snipe_opinion}**"
        ),
        color=COLOR_BUY,
        fields=[{"name": "CA:", "value": mint, "inline": False}],
        journal_kind="did", token_ticker=symbol,
        journal_meta={"preset": SNIPER_ACTIVE_PRESET, "size_sol": size_sol, "confidence_multiplier": confidence_multiplier, "prior_encounters": len(prior_entries)},
    )

    # opened_by stores the actual creator address (not a placeholder
    # string) — check_sniper_positions needs this to re-check the dev's
    # holding later and detect a sell-off.
    creator_address = candidate.get("creator", "")
    open_paper_position(
        state, mint, entry_price, size_sol, opened_by=creator_address, strength="weak",
        thesis=snipe_opinion, entry_market_cap_usd=market_cap_usd,
    )
    if mint in state.open_positions:
        state.open_positions[mint]["risk_level"] = "🎯 Sniper Play"
        state.open_positions[mint]["original_size_sol"] = size_sol
        state.open_positions[mint]["entry_dev_holding_pct"] = dev_pct
        state.save()


def format_market_cap(usd_value: float) -> str:
    """Formats a market cap for display: $1.84M, $234K, $890 — no decimals below $1K."""
    if usd_value is None:
        return "unknown MC"
    if usd_value >= 1_000_000:
        return f"${usd_value / 1_000_000:.2f}M MC"
    if usd_value >= 1_000:
        return f"${usd_value / 1_000:.0f}K MC"
    return f"${usd_value:.0f} MC"


def get_sol_price_usd() -> float:
    """Current SOL/USD price — used to convert SOL-denominated PnL and balance into USDC-style dollar figures."""
    prices = get_token_prices_usd([SOL_MINT])
    return prices.get(SOL_MINT, 0)


def get_liquidity_and_market_cap(mint: str):
    """
    Fetches current liquidity and market cap for a token via
    DexScreener's free public API (no key needed) — used for the
    "min liquidity" and "max entry market cap" filters. Returns
    (liquidity_usd, market_cap_usd), either of which may be None if
    unavailable (e.g. too new to be indexed yet).
    """
    try:
        resp = requests.get(
            "https://api.dexscreener.com/latest/dex/tokens/" + mint, timeout=15
        )
        resp.raise_for_status()
        pairs = resp.json().get("pairs") or []
        solana_pairs = [p for p in pairs if p.get("chainId") == "solana"]
        if not solana_pairs:
            return None, None
        pair = max(solana_pairs, key=lambda p: (p.get("liquidity") or {}).get("usd", 0) or 0)
        liquidity_usd = (pair.get("liquidity") or {}).get("usd")
        market_cap_usd = pair.get("fdv") or pair.get("marketCap")
        return liquidity_usd, market_cap_usd
    except Exception as e:
        print(f"[WARN] liquidity/mcap lookup failed for {mint}: {e}")
        return None, None


# Wash-trading / one-sided-volume filter — applied at every entry
# decision point (sniper, scout/conviction, priority-copy). DexScreener's
# pair object exposes txn COUNTS split by buy/sell per timeframe
# (txns.h1.buys / txns.h1.sells) — it does NOT expose per-trade dollar
# size or a $ volume split by side, only a single total $ volume per
# timeframe. That means this can only approximate "few large one-sided
# trades" using transaction-count concentration and count-based
# one-sidedness, not actual dollar-volume concentration — see the
# get_wash_trading_flag() docstring for what this can and can't detect
# given that limitation.
WASH_TRADING_MIN_TXNS_H1 = 15       # fewer than this many buys+sells in the last hour — too thin a sample to trust
WASH_TRADING_MAX_ONE_SIDED_PCT = 0.80  # >80% of last-hour txns on one side (mostly buys or mostly sells) — not organic two-sided trading


def get_wash_trading_flag(mint: str) -> dict:
    """
    Best-effort wash-trading / thin-volume flag using DexScreener's
    txns.h1 buy/sell COUNTS (not dollar volume — DexScreener doesn't
    split $ volume by side, so a true "few giant trades dominating
    dollar volume" check isn't possible with this data source; this
    approximates it via transaction-count concentration instead).

    Flags suspicious if EITHER:
      - fewer than WASH_TRADING_MIN_TXNS_H1 total txns in the last
        hour (too few trades to treat the hour's activity as a real
        signal), OR
      - more than WASH_TRADING_MAX_ONE_SIDED_PCT of last-hour txns
        are on one side (overwhelmingly buys or overwhelmingly sells
        — real two-sided trading doesn't look like this).

    Returns {"suspicious": bool, "h1_buys": int or None,
    "h1_sells": int or None, "h1_total_txns": int or None,
    "reason": str or None}. Fails open (suspicious=False) on any
    missing data or request error — absence of a signal is never
    treated as a red flag, consistent with every other data-quality
    gap elsewhere in this file.
    """
    try:
        resp = requests.get(
            "https://api.dexscreener.com/latest/dex/tokens/" + mint, timeout=15
        )
        resp.raise_for_status()
        pairs = resp.json().get("pairs") or []
        solana_pairs = [p for p in pairs if p.get("chainId") == "solana"]
        if not solana_pairs:
            return {"suspicious": False, "h1_buys": None, "h1_sells": None, "h1_total_txns": None, "reason": None}

        pair = max(solana_pairs, key=lambda p: (p.get("liquidity") or {}).get("usd", 0) or 0)
        txns_h1 = (pair.get("txns") or {}).get("h1") or {}
        buys, sells = txns_h1.get("buys"), txns_h1.get("sells")
        if buys is None or sells is None:
            return {"suspicious": False, "h1_buys": None, "h1_sells": None, "h1_total_txns": None, "reason": None}

        total = buys + sells
        if total == 0:
            return {"suspicious": False, "h1_buys": buys, "h1_sells": sells, "h1_total_txns": 0, "reason": None}

        if total < WASH_TRADING_MIN_TXNS_H1:
            return {
                "suspicious": True, "h1_buys": buys, "h1_sells": sells, "h1_total_txns": total,
                "reason": f"only {total} txns in the last hour — too thin to trust as a volume signal",
            }

        one_sided_pct = max(buys, sells) / total
        if one_sided_pct > WASH_TRADING_MAX_ONE_SIDED_PCT:
            side = "buys" if buys > sells else "sells"
            return {
                "suspicious": True, "h1_buys": buys, "h1_sells": sells, "h1_total_txns": total,
                "reason": f"{one_sided_pct:.0%} of last hour's txns are {side} ({buys} buys / {sells} sells) — one-sided, doesn't look organic",
            }

        return {"suspicious": False, "h1_buys": buys, "h1_sells": sells, "h1_total_txns": total, "reason": None}
    except Exception as e:
        print(f"[WARN] wash-trading check failed for {mint}: {e}")
        return {"suspicious": False, "h1_buys": None, "h1_sells": None, "h1_total_txns": None, "reason": None}


def get_dev_holding_pct(mint: str, creator: str) -> float:
    """
    % of total supply still held by the token's own creator wallet —
    free via the same Solana RPC methods as get_top10_holder_pct.
    Returns None if the creator address is unknown or the check fails.
    """
    if not creator or not HELIUS_API_KEY:
        return None
    try:
        payload = {
            "jsonrpc": "2.0", "id": 1, "method": "getTokenAccountsByOwner",
            "params": [creator, {"mint": mint}, {"encoding": "jsonParsed"}],
        }
        resp = request_with_backoff("POST", HELIUS_RPC_URL, json=payload, timeout=15)
        data = resp.json()
        accounts = data.get("result", {}).get("value", [])
        dev_amount = sum(
            float(a["account"]["data"]["parsed"]["info"]["tokenAmount"]["uiAmount"] or 0)
            for a in accounts
        )

        supply_payload = {"jsonrpc": "2.0", "id": 1, "method": "getTokenSupply", "params": [mint]}
        supply_resp = request_with_backoff("POST", HELIUS_RPC_URL, json=supply_payload, timeout=15)
        total_supply = float(supply_resp.json().get("result", {}).get("value", {}).get("uiAmount") or 0)
        if total_supply <= 0:
            return None

        return (dev_amount / total_supply) * 100
    except Exception as e:
        print(f"[WARN] dev holding check failed for {mint}: {e}")
        return None


def get_approx_holder_count(mint: str) -> int:
    """
    Best-effort holder count via the top-20-accounts RPC call — this
    genuinely only sees the top 20, so it under-counts real holder
    totals for popular tokens. Treat this as a rough floor, not an
    exact count — a full accurate count needs a paid indexing service.
    Returns None on failure.
    """
    if not HELIUS_API_KEY:
        return None
    try:
        payload = {"jsonrpc": "2.0", "id": 1, "method": "getTokenLargestAccounts", "params": [mint]}
        resp = request_with_backoff("POST", HELIUS_RPC_URL, json=payload, timeout=15)
        accounts = resp.json().get("result", {}).get("value", [])
        return len([a for a in accounts if float(a.get("uiAmount") or 0) > 0])
    except Exception as e:
        print(f"[WARN] holder count check failed for {mint}: {e}")
        return None


SNIPER_PENDING = {}  # mint -> candidate dict with launch metadata, held until it ages into the preset's window


def drain_sniper_queue(state: "LedgerState"):
    """
    Moves fresh launches from the raw WS queue into the pending list
    (does NOT evaluate them yet — they need to age into the preset's
    window first). Processes up to SNIPER_MAX_QUEUE_DRAIN_PER_CYCLE
    per call so a burst of launches can't stall the main loop.
    """
    processed = 0
    while processed < SNIPER_MAX_QUEUE_DRAIN_PER_CYCLE:
        try:
            event = SNIPER_LAUNCH_QUEUE.get_nowait()
        except queue.Empty:
            break
        processed += 1

        # Only real "new token" events — defensive, in case the feed
        # ever sends other event types over the same subscription.
        if event.get("txType") and event.get("txType") != "create":
            continue

        mint = event.get("mint")
        if mint and mint not in SNIPER_PENDING:
            SNIPER_PENDING[mint] = {
                "mint": mint,
                "symbol": event.get("symbol", "?"),
                "name": event.get("name", ""),
                # pumpdev.io's actual field names — "creator" and
                # "initialBuySol" (the earlier guesses) don't exist in
                # their real event schema, which is why every dev-buy
                # check was silently reading 0 until this fix.
                "creator": event.get("traderPublicKey", ""),
                "initial_buy_sol": event.get("solAmount", 0) or 0,
                "twitter": event.get("twitter"),
                "telegram": event.get("telegram"),
                "website": event.get("website"),
                "first_seen": time.time(),
            }


def scan_sniper_pending(state: "LedgerState"):
    """
    Checks every pending candidate's age each cycle: evaluates it
    once it's within the active preset's [age_min, age_max] window,
    and drops it (gives up) if it ages past the max without ever
    being evaluated. This is what lets "Hyper-Early Scalp" mean
    2-45 minutes old, not literally the instant of launch.
    """
    preset = SNIPER_PRESETS[SNIPER_ACTIVE_PRESET]
    now = time.time()
    processed = 0

    for mint in list(SNIPER_PENDING.keys()):
        if processed >= SNIPER_MAX_PENDING_EVAL_PER_CYCLE:
            break
        candidate = SNIPER_PENDING[mint]
        age_minutes = (now - candidate["first_seen"]) / 60

        if age_minutes < preset["age_min_minutes"]:
            continue  # still too young for this preset — check again next cycle

        if age_minutes > preset["age_max_minutes"]:
            print(f"[SNIPE EXPIRED] {candidate['symbol']}: aged past the {SNIPER_ACTIVE_PRESET} window, giving up")
            del SNIPER_PENDING[mint]
            continue

        # Within the window — evaluate now, one shot, then remove either way
        try:
            evaluate_snipe_candidate(candidate, state)
        except Exception as e:
            print(f"[ERROR] sniper evaluation failed for {candidate['symbol']}: {e}")
        del SNIPER_PENDING[mint]
        processed += 1


# ── Main loop ─────────────────────────────────────────────────────────

def main():
    if RESET_STATE_ON_BOOT and STATE_FILE.exists():
        STATE_FILE.unlink()
        print(f"[RESET] RESET_STATE_ON_BOOT is set — wiped {STATE_FILE}, starting fresh at {STARTING_PAPER_BALANCE_SOL} SOL. Remember to unset this variable so it doesn't wipe progress again on the next restart.")

    state = LedgerState.load()
    print(f"Ledger booting up. Paper balance: {state.balance_sol} SOL")

    start_api_server()

    if SNIPER_MODE_ENABLED:
        start_sniper_listener()
        preset = SNIPER_PRESETS[SNIPER_ACTIVE_PRESET]
        print(f"[SNIPER] Mode ENABLED — preset '{SNIPER_ACTIVE_PRESET}', "
              f"min confidence to enter {SNIPER_MIN_CONFIDENCE_TO_ENTER}x, "
              f"age window {preset['age_min_minutes']}-{preset['age_max_minutes']} min, "
              f"max top10 {preset['top10_holders_max_pct']}%")

    whale_wallets = set()  # kept for backward compatibility with functions below, always empty now
    cycle_count = 0

    while True:
        cycle_count += 1

        if SNIPER_MODE_ENABLED:
            drain_sniper_queue(state)
            scan_sniper_pending(state)

        # Reload every cycle — edit wallets.json anytime, no restart needed
        watched_wallets, wallet_handles, priority_wallets = load_wallets()
        globals()["WALLET_HANDLES"] = wallet_handles  # analyze_conviction() reads this via WALLET_HANDLES.get()

        if not watched_wallets:
            print(f"No wallets in {WALLETS_CONFIG_FILE} — add some and it'll pick them up next cycle.")
            time.sleep(POLL_SECONDS)
            continue

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
                    trader_name = WALLET_HANDLES.get(wallet, wallet[:6] + "...")  # "..." kept for unknown handles
                    platform_name = get_source_display_name(buy["source"])

                    metadata = get_token_metadata(token)
                    display_symbol = metadata.get("symbol") or token[:6] + "..."  # no "$" — avoids triggering another bot

                    if is_priority:
                        # Priority wallets are trusted enough to mirror
                        # directly — no independent-conviction gate, no
                        # "pass" possible. Uses the sniper-style Cupsey
                        # exit ladder, not the main patient trailing stop,
                        # per your instruction to keep the sniper strategy
                        # for these copies.
                        copy_priority_wallet_entry(token, wallet, trader_name, platform_name, metadata, state)
                        continue

                    if token in state.open_positions:
                        print(f"  [SKIP] {display_symbol}: already holding a position, skipping analysis.")
                        continue

                    analysis = analyze_conviction(token, metadata, trader_name, platform_name)

                    if analysis["conviction"] != "buy":
                        print(f"  [PASS] {display_symbol} — no independent conviction, skipping (not posted).")
                        # Not spoken to Discord (deliberately — see the
                        # print above), but still worth a journal entry:
                        # this is the "refused" kind, and analyze_conviction
                        # already generates a thesis/reasoning even on a
                        # pass, so there's real text to capture here, not
                        # just a bare log line.
                        log_journal(
                            kind="refused",
                            text=analysis["thesis"] or f"Passed on {display_symbol} — no independent conviction.",
                            token_ticker=display_symbol,
                            meta={"risk_score": analysis["risk_score"], "wallet": trader_name, "platform": platform_name},
                        )
                        continue

                    wash_flag = get_wash_trading_flag(token)
                    if wash_flag["suspicious"]:
                        print(f"  [PASS] {display_symbol} — wash-trading flag: {wash_flag['reason']}")
                        log_journal(
                            kind="refused",
                            text=f"Passed on {display_symbol} — {wash_flag['reason']}",
                            token_ticker=display_symbol,
                            meta={"h1_buys": wash_flag["h1_buys"], "h1_sells": wash_flag["h1_sells"]},
                        )
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

                    # target_size_sol scales continuously with conviction
                    # (risk_score), not just two fixed tiers — concentrated
                    # conviction on your best-scoring ideas beats spreading
                    # evenly thin. risk_score 0 (safest) -> MAX_CONVICTION_SIZE_SOL;
                    # risk_score 10 (riskiest) -> MIN_SCOUT_SIZE_SOL. This is
                    # the TARGET, not the initial buy — see
                    # CONVICTION_INITIAL_ENTRY_FRACTION and
                    # top_up_conviction_position for the rest of the pacing.
                    target_size_sol = MIN_SCOUT_SIZE_SOL + (MAX_CONVICTION_SIZE_SOL - MIN_SCOUT_SIZE_SOL) * (1 - risk_score / 10)
                    target_size_sol = min(target_size_sol, MAX_POSITION_SOL)
                    size_sol = target_size_sol * CONVICTION_INITIAL_ENTRY_FRACTION
                    strength = "weak"  # priority wallets never reach here — they branch off above into copy_priority_wallet_entry

                    ok, block_reason = can_open_position(state, size_sol)
                    if not ok:
                        print(f"  [BLOCKED] {display_symbol}: {block_reason}")
                        continue

                    notes_lines = [
                        f"• **Risk Assessment:** {risk_bucket} {risk_score}/10",
                        f"• **Amount Bought:** {size_sol:.4f} SOL (of {target_size_sol:.4f} SOL target — scaling in on confirmed growth)",
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
                        color=COLOR_NEUTRAL,
                        fields=[
                            {"name": "CA:", "value": token, "inline": False},
                            {"name": "🧠 Thesis", "value": analysis["thesis"], "inline": False},
                            {"name": "ℹ️ Additional Notes", "value": additional_message, "inline": False},
                        ],
                        journal_kind="did", token_ticker=display_symbol,
                        journal_meta={"risk_score": risk_score, "size_sol": size_sol, "wallet": trader_name, "platform": platform_name, "independent": analysis["independent"]},
                    )

                    open_paper_position(
                        state, token, entry_price, size_sol, opened_by=wallet, strength=strength,
                        thesis=analysis["thesis"], risk_score=risk_score, target_size_sol=target_size_sol,
                        entry_condition=analysis.get("entry_condition"), invalidation=analysis.get("invalidation"),
                    )
            except Exception as e:
                print(f"[ERROR] wallet {wallet}: {e}")
            time.sleep(0.3)  # spread requests out across the cycle

        check_open_positions(state)
        if SNIPER_MODE_ENABLED:
            check_sniper_positions(state)
        check_for_daily_target_hit(state)
        check_for_blowup_reset(state)
        check_daily_loss_pause(state)
        check_ultra_conservative_mode(state)
        check_goal_deadline(state)
        state.save()

        if SNIPER_MODE_ENABLED:
            # Sniper positions need much faster monitoring than the main
            # 2-minute cycle — Cupsey's average hold is ~40 seconds, so a
            # single check per full cycle would miss most of that window.
            # Break the wait into smaller chunks and re-check sniper
            # positions (and drain/scan new launches) on each one.
            elapsed = 0
            while elapsed < POLL_SECONDS:
                time.sleep(min(SNIPER_CHECK_INTERVAL_SECONDS, POLL_SECONDS - elapsed))
                elapsed += SNIPER_CHECK_INTERVAL_SECONDS
                drain_sniper_queue(state)
                scan_sniper_pending(state)
                check_sniper_positions(state)
                check_for_daily_target_hit(state)
                check_for_blowup_reset(state)
                check_daily_loss_pause(state)
                check_ultra_conservative_mode(state)
                check_goal_deadline(state)
                state.save()
        else:
            time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    main()
