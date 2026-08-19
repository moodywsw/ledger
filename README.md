# Ledger

Ledger is a Solana memecoin trading agent, currently running in **paper
trading mode only** — no real funds ever move. It watches a list of trader
wallets, turns their buys into a thesis in its own voice, simulates trades
against a paper balance, and (optionally) speaks live to a Discord channel
and answers questions from a Discord bot grounded in its own trade history
and market research.

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
  `websockets`, `flask`, `base58`).
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

Real trade execution is **not** wired up. `ledger_bot.py` contains an
`execute_real_trade()` stub that raises unless explicitly enabled, pending a
reviewed paper-trading track record and real wallet-signing logic.
