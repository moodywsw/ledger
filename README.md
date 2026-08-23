# Ledger

Ledger is a Solana memecoin trading agent. It watches a list of trader
wallets, turns their buys into a thesis in its own voice, simulates trades
against a paper balance, and (optionally) speaks live to a Discord channel
and answers questions from a Discord bot grounded in its own trade history
and market research.

Real on-chain execution exists as an opt-in layer on top of paper trading
(see [`real_trading.py`](#components) and [Status](#status)) — it is
**disabled by default** and stays that way until `REAL_TRADING_ENABLED` is
set explicitly.

## Components

- **`ledger_bot.py`** — The core loop. Polls watched wallets (via Alchemy's
  Solana RPC) for new buys, flags "whale" wallets by total portfolio value,
  generates a thesis for each buy, and simulates opening/scaling/closing
  paper positions under hard-coded risk limits. Optionally posts every
  thesis/trade/exit to a Discord channel via webhook. State persists to
  `ledger_state.json`.
- **`ledger_discord_bot.py`** — The conversational half. Responds when
  mentioned or DM'd in a Discord server, using Claude and grounding its
  replies in `ledger_state.json` (current paper positions/PnL) and
  `market_intel.json` (recent research).
- **`market_intel.py`** — Autonomous research loop. Asks Claude (with web
  search) to summarize current Solana memecoin market conditions and appends
  the findings to `market_intel.json`. Meant to be run periodically (e.g. a
  scheduled job every few hours).
- **`pumpfun_listener.py`** — Listens to a third-party Pump.fun WebSocket
  feed for new token launches, flags whale-sized launches/buys, and
  cross-references launch creators against the wallets tracked in
  `ledger_bot.py`.
- **`test_wallet_feed.py`** — Standalone CLI to fetch and print raw Helius
  transaction data for one or more wallets. Useful for inspecting the real
  response shape before writing/adjusting transaction-parsing logic.
- **`real_trading.py`** — Optional real on-chain execution layer, isolated
  from the paper-trading logic in `ledger_bot.py` on purpose (it's the only
  module that ever touches a private key). Mirrors Sniper Mode and
  priority-copy entries/exits into real Jupiter Ultra API swaps when
  `REAL_TRADING_ENABLED=true`; otherwise every decision still runs and
  journals normally, just unsigned. See [Status](#status).

## Data & config files

- **`wallets.json`** — The watchlist: wallet handles and addresses tracked by
  `ledger_bot.py` and `pumpfun_listener.py`. Edit this to add/remove/swap
  tracked traders without touching code.
- **`ledger_state.json`** — Persisted paper-trading state (balance, realized
  PnL, open positions, trade log, seen transaction signatures), produced by
  `ledger_bot.py`.
- **`market_intel.json`** — Rolling log of research findings produced by
  `market_intel.py` (not committed until first run).
- **`wallet_feed_output.json`** — Output dump written by
  `test_wallet_feed.py` from its most recent run.
- **`ledger_background.log`** — Log output from running the bot(s) in the
  background.
- **`requirements.txt`** — Python dependencies (`requests`, `discord.py`,
  `websockets`, `flask`, `base58`, `solders`).
- **`Procfile`** — Process declaration for deployment (`web: python3
  ledger_bot.py`).

## Environment variables

| Variable | Required by | Notes |
|---|---|---|
| `ALCHEMY_RPC_URL` | `ledger_bot.py` | Solana Mainnet RPC URL from an Alchemy app, e.g. `https://solana-mainnet.g.alchemy.com/v2/<key>` |
| `HELIUS_API_KEY` | `test_wallet_feed.py` only | From https://helius.dev — no longer used by `ledger_bot.py` (migrated to Alchemy) |
| `DISCORD_WEBHOOK_URL` | `ledger_bot.py` (optional) | Ledger's public voice; leave unset to run silently |
| `LEDGER_AVATAR_URL` | `ledger_bot.py` (optional) | Avatar for the Discord webhook posts |
| `DISCORD_BOT_TOKEN` | `ledger_discord_bot.py` | Needs the "Message Content" privileged intent enabled |
| `ANTHROPIC_API_KEY` | `ledger_discord_bot.py`, `market_intel.py` | Powers conversational replies and market research |
| `REAL_TRADING_ENABLED` | `real_trading.py` (optional) | `"true"` to arm real execution. Defaults to unarmed (`false`) — paper trading is unaffected either way. |
| `SOLANA_PRIVATE_KEY` | `real_trading.py`, only if armed | Base58 secret key of a dedicated trading wallet. Never written to a file, logged, or committed — env var only. |
| `SOLANA_WALLET_ADDRESS` | `real_trading.py` (optional) | Pins the expected public key; if `SOLANA_PRIVATE_KEY` derives a different address, loading fails loudly instead of trading from an unexpected wallet. |
| `JUPITER_API_KEY` | `real_trading.py`, only if armed | From https://developers.jup.ag/portal — required by Jupiter's Ultra Swap API (`x-api-key`). |
| `MAX_REAL_POSITION_PCT` | `real_trading.py` (optional) | Per-position ceiling as a fraction of the CURRENT live on-chain USDC balance, recomputed on every buy — not a fixed dollar figure. Default `0.30` (30%). |
| `MAX_TOTAL_EXPOSURE_PCT` | `real_trading.py` (optional) | Ceiling on total USDC value across every open real position combined (existing + new), as a fraction of total balance (liquid + committed), confirmed against the chain. Stops Sniper Mode's rapid-fire entries from committing the whole wallet even though each individual buy respects `MAX_REAL_POSITION_PCT`. Default `0.85` (85%, leaving a 15% floor always liquid). |
| `MAX_REAL_DAILY_USDC` | `real_trading.py` (optional) | Hard ceiling on total real USDC spent per rolling UTC day, across all positions — a fixed dollar figure, independent of the percentage caps above. Default `6.00`. |
| `MIN_REAL_TICKET_USDC` | `real_trading.py` (optional) | Real buys below this size are skipped (mostly fees at that point). Default `1.00`. |
| `MIN_SOL_FOR_GAS` | `real_trading.py` (optional) | Trades are in USDC, but every Solana transaction still costs SOL for network fees — below this SOL balance, a real trade is refused outright instead of failing mid-transaction. Default `0.01`. |

## Run it

Install dependencies:

```bash
pip install -r requirements.txt --break-system-packages
```

Run the paper-trading loop:

```bash
python3 ledger_bot.py
```

Run the Discord conversational bot (separate process):

```bash
python3 ledger_discord_bot.py
```

Run a market research pass (schedule this periodically):

```bash
python3 market_intel.py
```

Run the Pump.fun listener:

```bash
python3 pumpfun_listener.py
```

Inspect raw wallet transaction data:

```bash
python3 test_wallet_feed.py
python3 test_wallet_feed.py --limit 10
python3 test_wallet_feed.py --wallet my-wallet=YOUR_SOLANA_WALLET_ADDRESS
```

Pass `--wallet` more than once to inspect multiple custom wallets. Each run
also saves the filtered data to `wallet_feed_output.json` (override with
`--json-output PATH`).

## Status

Real trade execution exists, wired to Sniper Mode and priority-copy
entries/exits via `real_trading.py`, and is **disabled by default**
(`REAL_TRADING_ENABLED` unset/`false`). Unarmed is a normal, reported state —
every decision still runs and journals on paper exactly as before; only the
signing step is gated. Arming it needs no code change or deploy, just setting
`REAL_TRADING_ENABLED=true` alongside `SOLANA_PRIVATE_KEY` and
`JUPITER_API_KEY`.

The trading wallet holds **USDC**, not SOL — real buys quote USDC→token and
real sells quote token→USDC. SOL is still required unconditionally for
network fees on every Solana transaction regardless of what the trade itself
is denominated in; a live SOL balance below `MIN_SOL_FOR_GAS` refuses the
trade outright (logged as a `refused` journal entry) rather than letting a
transaction fail midway for lack of gas. Position sizing is dynamic, not a
fixed dollar amount: every real buy is capped at `MAX_REAL_POSITION_PCT`
(30% by default) of the current live USDC balance, recomputed fresh on every
call — never a stored number. A second, independent ceiling,
`MAX_TOTAL_EXPOSURE_PCT` (85% by default), caps the combined USDC value
across every open real position at once, confirmed against the chain — this
exists because Sniper Mode can open several positions in quick succession,
and the per-position cap alone wouldn't stop that sequence from eventually
committing nearly the whole wallet. `MAX_REAL_DAILY_USDC` is a third, fixed-
dollar ceiling on top of both. Every real sell re-derives the actual
on-chain token balance before selling a single unit more than genuinely
exists in the wallet.

Use `real_trading.dry_run_quote(token_mint, amount_usdc, side)` to
sanity-check a quote against Jupiter before ever arming — e.g.
`dry_run_quote("<mint>", 2.0, "buy")` for a $2 buy quote. It only reads a
quote, never signs or sends anything.

## Acknowledgements

Some of `real_trading.py`'s real-execution logic — most notably treating an
unarmed/no-key state as a normal, clearly-reported condition rather than a
failure, and re-deriving wallet balances from the chain immediately before
each order instead of trusting local state alone — was adapted from
[omo](https://github.com/omotrades/omo) (MIT license), an open-source
autonomous memecoin trader.
