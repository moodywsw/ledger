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
| `DISCORD_TRADE_FORMAT` | `ledger_bot.py` (optional) | `embed` (default) posts every ENTRY / TRIM / EXIT as a short embed card (scale-ins are not posted) (`trade_cards.py`, see `docs/DISCORD_CARDS.md`); `text` posts the same card as plain markdown. |
| `DISCORD_BOT_TOKEN` | `ledger_discord_bot.py` | Needs the "Message Content" privileged intent enabled |
| `ANTHROPIC_API_KEY` | `ledger_discord_bot.py`, `market_intel.py` | Powers conversational replies and market research |
| `REAL_TRADING_ENABLED` | `real_trading.py` (optional) | `"true"` to arm real execution. Defaults to unarmed (`false`) — paper trading is unaffected either way. |
| `PAPER_TRADING_ENABLED` | `ledger_bot.py` (optional) | `"false"` decouples real trading from paper trading — the sniper/priority-copy entry points and the Cupsey exit ladder call `real_trading.py` directly instead of going through `open_paper_position`/`close_paper_position`, with no `ledger_state.json` writes and no paper Discord messages (see `real_only_positions.py`). Defaults to `"true"` (today's behavior — real trades only ever happen as a mirror of a paper trade) so an unset var never silently switches to real-only. |
| `SOLANA_PRIVATE_KEY` | `real_trading.py`, only if armed | Base58 secret key of a dedicated trading wallet. Never written to a file, logged, or committed — env var only. |
| `SOLANA_WALLET_ADDRESS` | `real_trading.py` (optional) | Pins the expected public key; if `SOLANA_PRIVATE_KEY` derives a different address, loading fails loudly instead of trading from an unexpected wallet. |
| `JUPITER_API_KEY` | `real_trading.py`, only if armed | From https://developers.jup.ag/portal — required by Jupiter's Ultra Swap API (`x-api-key`). |
| `MAX_REAL_POSITION_PCT` | `real_trading.py` (optional) | Per-position ceiling as a fraction of the CURRENT live on-chain USDC balance, recomputed on every buy — not a fixed dollar figure. Default `0.10` (10%; was 0.30 before the audit-hardening branch). |
| `MAX_TOTAL_EXPOSURE_PCT` | `real_trading.py` (optional) | Ceiling on total USDC value across every open real position combined (existing + new), as a fraction of total balance (liquid + committed), confirmed against the chain. Stops Sniper Mode's rapid-fire entries from committing the whole wallet even though each individual buy respects `MAX_REAL_POSITION_PCT`. Default `0.40` (40%; was 0.85 before the audit-hardening branch). |
| `MIN_REAL_TICKET_USDC` | `real_trading.py` (optional) | Real buys below this size are skipped (mostly fees at that point). Default `1.00`. |
| `MIN_SOL_FOR_GAS` | `real_trading.py` (optional) | Trades are in USDC, but every Solana transaction still costs SOL for network fees — below this SOL balance, a real trade is refused outright instead of failing mid-transaction. Default `0.01`. |
| `MAX_ENTRY_PRICE_IMPACT_PCT` | `real_trading.py` (optional) | Refuse a real buy whose Jupiter quote shows a larger \|price impact\| (percent). Default `3.0`. Jupiter reports adverse impact as a negative number, so the absolute value is used. |
| `MAX_EXIT_PRICE_IMPACT_PCT` | `real_trading.py` (optional) | Same guard on normal sells. Default `8.0`. |
| `STUCK_POSITION_FORCED_MAX_PRICE_IMPACT_PCT` | `real_trading.py` (optional) | Ceiling for the forced-exit path on stuck positions. Default `20`. |
| `EXIT_ENGINE` | `ledger_bot.py` (optional) | `v2` (default, risk_engine exits) or `legacy` (pre-audit LLM/Cupsey exits, kept as a rollback). |
| `POSITION_CHECK_SECONDS` | `ledger_bot.py` (optional) | How often open positions are re-priced and exit rules evaluated. Default `15`. |
| `WALLET_MAX_PAGES` | `ledger_bot.py` (optional) | Pages of signatures fetched per tracked wallet per poll, so bursts of buys aren't lost. Default `3`. |

### Risk engine (`risk_engine.py`)

All thresholds are env-driven; defaults are deliberately conservative. Bad
values fall back to the default. The full resolved config is printed at boot
as `[RISK] ...`.

| Variable | Default | Meaning |
|---|---|---|
| `RISK_PER_TRADE_PCT` | `0.01` | Equity risked per trade (size = equity × risk ÷ (stop × gap buffer)). |
| `RISK_MAX_POSITION_PCT` | `0.05` | Hard cap on a single position as a fraction of equity. |
| `RISK_STOP_GAP_BUFFER` | `1.5` | Memecoins gap through stops; sizing assumes the realised loss is 1.5× the stop. |
| `RISK_MIN_POSITION_SOL` | `0.01` | Below this the trade is skipped. |
| `RISK_MAX_CONCURRENT_POSITIONS` | `4` | Open positions at once. |
| `RISK_MAX_TOTAL_EXPOSURE_PCT` | `0.20` | Total open cost basis as a fraction of equity. |
| `RISK_DAILY_LOSS_LIMIT_PCT` | `0.05` | Stop opening trades once today's (UTC) realised loss reaches this fraction of the day-start equity. |
| `RISK_MAX_CONSECUTIVE_LOSSES` | `3` | Loss streak that triggers the cooldown. |
| `RISK_LOSS_COOLDOWN_MINUTES` | `60` | Pause after the loss streak. |
| `RISK_TOKEN_REENTRY_COOLDOWN_MINUTES` | `240` | No re-buying a token soon after exiting it. |
| `RISK_MAX_TRADES_PER_HOUR` | `6` | Entry rate limit. |
| `FILTER_MIN_LIQUIDITY_USD` | `15000` | Minimum pool liquidity. |
| `FILTER_MIN_MARKET_CAP_USD` / `FILTER_MAX_MARKET_CAP_USD` | `25000` / `5000000` | Market-cap band (floor not applied to sniper candidates). |
| `FILTER_MAX_TOP10_HOLDER_PCT` | `35` | Max supply held by the 10 largest accounts. |
| `FILTER_REQUIRE_MINT_AUTHORITY_REVOKED` / `FILTER_REQUIRE_FREEZE_AUTHORITY_REVOKED` | `true` / `true` | Reject tokens whose creator can still mint or freeze. Dangerous Token-2022 extensions (transfer fee, transfer hook, permanent delegate, non-transferable, pausable, default account state) are always rejected. |
| `FILTER_MAX_ENTRY_PRICE_IMPACT_PCT` | `2.0` | Estimated impact of our size against pool liquidity. |
| `FILTER_MAX_SIGNAL_AGE_SECONDS` | `120` | Don't copy a wallet buy older than this. |
| `FILTER_MAX_CHASE_PCT` | `0.25` | Don't copy if price already ran this much above the wallet's fill. |
| `FILTER_FAIL_CLOSED` | `true` | Missing data (API down) = reject, not pass. |
| `EXIT_STOP_LOSS_PCT` | `0.20` | Hard stop from entry. |
| `EXIT_TP_LADDER` | `0.25:0.5,0.6:0.25` | `gain:fraction_of_original_size` rungs: sell half at +25%, a quarter at +60%, trail the rest. |
| `EXIT_BREAKEVEN_AFTER_TP1` | `true` | Stop moves to entry after the first take-profit. |
| `EXIT_TRAILING_ACTIVATION_PCT` / `EXIT_TRAILING_STOP_PCT` / `EXIT_TRAILING_STOP_TIGHT_PCT` | `0.30` / `0.25` / `0.20` | Trailing stop arms at +30%, trails 25% from peak, tightens to 20% once +100%. |
| `EXIT_TIME_STOP_MINUTES` / `EXIT_TIME_STOP_MIN_GAIN_PCT` | `30` / `0.10` | Exit if not up 10% after 30 min. |
| `EXIT_MAX_HOLD_HOURS` | `24` | Absolute max hold. |
| `EXIT_NO_PRICE_WRITEOFF_MINUTES` | `60` | Close (write off) a position with no price quote for this long. |
| `PAPER_COST_PER_SIDE_PCT` | `0.015` | Fees + slippage charged to paper fills per side, so paper PnL is realistic. |
| `WALLET_SCORE_MIN_TRADES` / `WALLET_SCORE_MIN_EXPECTANCY_PCT` / `WALLET_SCORE_LOOKBACK_DAYS` | `5` / `0.0` / `30` | Stop copying a wallet whose copied trades have negative expectancy. |
| `ALLOW_LLM_DIP_BUYS` / `MAX_DIP_BUYS` | `false` / `1` | Legacy engine only: LLM averaging down is off by default. |

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

## Tests & backtest

```bash
pip install pytest
python -m pytest -q                       # risk engine + paper engine unit tests
python -m backtest.collect_wallet_buys     # tracked-wallet buys from RPC -> backtest/data/
python -m backtest.fetch_prices            # GeckoTerminal minute candles + mint facts
python -m backtest.replay                  # old vs new rules on the same signals
python -m backtest.replay --trade-log path/to/ledger_state.json   # stats from a real state file
```

See `docs/AUDIT.md` for the audit and results.

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
(10% by default) of the current live USDC balance, recomputed fresh on every
call — never a stored number. A second, independent ceiling,
`MAX_TOTAL_EXPOSURE_PCT` (40% by default), caps the combined USDC value
across every open real position at once, confirmed against the chain — this
exists because Sniper Mode can open several positions in quick succession,
and the per-position cap alone wouldn't stop that sequence from eventually
committing nearly the whole wallet. There is deliberately no daily spend
cap — the wallet is also managed manually from time to time, so a
bot-tracked "spent today" figure can't be trusted to mean what it implies.
Every real sell re-derives the actual
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
