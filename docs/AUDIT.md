# Ledger trading audit (branch `audit-hardening`)

> **Read this first.** No set of rules makes memecoin trading profitable by
> default. Most Solana memecoins go to zero within hours, the tracked wallets
> can (and do) stop performing, and every number below comes from a short
> replay with known simplifications. The goal of this branch is to **lose
> less when wrong, stop trading when the edge disappears, and stop silent
> bugs from costing money** — not to promise returns. `REAL_TRADING_ENABLED`
> still defaults to `false`; nothing here arms real money.

## 1. What data this audit is based on

| Source | Available? | Notes |
|---|---|---|
| Code + 71 commits on `main` | yes | Full read of `ledger_bot.py`, `real_trading.py`, `real_only_positions.py`, `pumpfun_listener.py` (sniper), `market_intel.py`, the Discord bot and the journal/theses stores. |
| Live paper trade log (`ledger_state.json`, `journal.jsonl`) | **no** | Lives on the Railway volume (`DATA_DIR`). The copy in the repo has an empty `trade_log`. |
| Real trade history (`real_positions.json`, journal) | **no** | Same — Railway volume only. |
| Railway logs | **no** | No Railway access yet. |
| Tracked wallets' on-chain buys | **yes** | `backtest/collect_wallet_buys.py` pulled the last 10 days of buys for all 12 wallets in `wallets.json` from Solana RPC. |
| Minute candles for every token those wallets bought | **yes** | `backtest/fetch_prices.py`, GeckoTerminal free API. |

So the win-rate / expectancy / drawdown numbers are from a **signal replay**:
every real buy the tracked wallets made is fed, with a 45 s detection delay,
through the old rules and through the new rules against real 1-minute
prices. Once the Railway volume is available, run
`python -m backtest.replay --trade-log ledger_state.json` to get the same
metrics (by wallet, by exit reason) from the bot's actual trades.

Replay window: **2026-09-30 → 2026-10-09** (Lisbon time). It covers 364 wallet
buys, which collapse to **167 unique (wallet, token) signals** across 154
tokens. 155 of those signals have candle data. Starting equity is 10 SOL in
every run. "+fees" means 1.5% per side for fees and slippage. That is optimistic
for thin pump.fun pools.

## 2. Headline numbers (signal replay, 45 s detection delay)

| metric | legacy_paper (no fees, as dashboard) | legacy_paper (+fees) | legacy_cupsey real-only (+fees) | new_v2 (+fees) | new_v2 exits only, no filters (+fees) |
|---|---|---|---|---|---|
| trades | 110 | 87 | 135 | 33 | 126 |
| win_rate | 31% | 30% | 35% | 30% | 27% |
| avg_win_pct | +54.5% | +48.3% | +15.2% | +24.0% | +20.2% |
| avg_loss_pct | -35.4% | -34.9% | -12.0% | -25.1% | -26.0% |
| expectancy_pct | -7.6% | -10.0% | -2.5% | -10.3% | -13.6% |
| profit_factor | 0.69 | 0.59 | 0.69 | 0.42 | 0.29 |
| total_pnl_sol | -4.183 | -4.359 | -4.251 | -1.080 | -8.198 |
| return_pct | -41.8% | -43.6% | -42.5% | -10.8% | -82.0% |
| max_drawdown_pct | -42.6% | -44.2% | -46.5% | -12.8% | -82.2% |
| median_hold_min | 8.3 | 10.4 | 1.2 | 8.6 | 2.2 |
| worst_trade_pct | -88.9% | -89.3% | -51.3% | -46.0% | -65.1% |


How to read the columns:

| Column | What it is |
|---|---|
| **legacy_paper** | The old paper rules for priority copies: −25% stop, capital recovery at +40%, then a 20% trailing stop and 2× peels. Sizing is 8% × LLM confidence, capped at 0.5 SOL. Checked about every 75 s. |
| **legacy_cupsey real-only** | What `PAPER_TRADING_ENABLED=false` + `REAL_TRADING_ENABLED=true` would have done: TP1 at +20%, −35% stop, hard exit after 60 s. Sizing up to 30% of the wallet per coin. |
| **new_v2** | This branch with its shipped defaults: filters, risk sizing, portfolio limits and the v2 exits. |
| **new_v2 exits only** | The new exit rules with the old sizing and no filters. Isolates what the exits alone do. |

### What the replay says, plainly

1. **The signals themselves lose money.** We are copying the tracked wallets
   about 45 s late. The token's median move from our fill is −6% after 5
   minutes, −21% after 30 minutes and −40% after 2 hours. Only 25% of tokens
   are higher 30 minutes later. These traders often profit from being
   *first*, and we buy what they are already selling into. **No exit rule
   turns that into a profit.** Every rule set has negative expectancy, the new
   one included.
2. **What the new rules do is lose far less.** The return goes from −44% to
   **−11%**, and max drawdown from −44% to **−13%** over the same signals. The
   worst trade goes from −89% to −46%. Most of that comes from *not taking*
   the bad trades:
   - 57 skipped for chasing (price already 25%+ above the wallet's fill)
   - 16 skipped by the loss-streak cooldown
   - 6 skipped by the daily loss limit
   - 15 skipped by the market-cap band
   - 5 Token-2022 transfer-hook or transfer-fee tokens skipped
   - 2 tokens with a live mint authority skipped
   - 2 skipped because the wallet was benched by scoring
   - the remaining entries sized at about 3% of equity instead of up to 16%
3. **Per-trade expectancy did not improve much** (−10.0% → −10.3%). The
   exits alone are slightly worse than the legacy exits on this data (−13.6%
   vs −10.0%), because a −20% stop checked every 15 s gets shaken out more
   often. The default TP ladder was changed from +50% / +100% to **sell half
   at +25%, a quarter at +60%, trail the rest**. Across 10 / 30 / 45 / 90 s
   latencies this was consistently 2–5 points better than the first ladder.
   With about 33 trades, that is weak evidence: treat it as a starting point,
   not a tuned optimum.
4. **The dashboard flattered performance.** The same legacy rules with no
   fees, which is what the paper dashboard showed, read −7.6% per trade. With
   realistic costs it is −10.0%.
5. **Latency matters but is not the main problem.** The old rules lose 42%
   of equity at 10 s and 60% at 180 s. The new rules lose 9–12% at every
   latency. Above 120 s they refuse the copy (stale signal).

### By exit reason

| exit | legacy n | legacy expectancy | legacy PnL (SOL) | new n | new expectancy | new PnL (SOL) |
|---|---|---|---|---|---|---|
| stop loss | 56 | −37.0% | −10.36 | 20 | −27.5% | −1.76 |
| trailing stop | 26 | +48.3% | +6.28 | 5 | +37.1% | +0.60 |
| breakeven stop | — | — | — | 5 | +10.8% | +0.17 |
| time stop | — | — | — | 3 | −9.3% | −0.09 |
| still open at end | 5 | −11.0% | −0.27 | — | — | — |

The legacy stop loss averaged **−37%** against a −25% trigger. Memecoins gap
through stops, and the 75 s check interval made it worse. The new stop
averages −27.5% against a −20% trigger, and sizing now assumes the gap
(`RISK_STOP_GAP_BUFFER=1.5`).

### By wallet

Raw quality is the move 30 minutes after our fill, before any rules.

| wallet | signals | 30m median | % up at 30m | legacy n | legacy expectancy | legacy SOL | new n | new expectancy | new SOL |
|---|---|---|---|---|---|---|---|---|---|
| RC calendar | 31 | −31% | 21% | 20 | −8% | −0.80 | 5 | −21% | −0.34 |
| OCR | 28 | −43% | 19% | 14 | −11% | −0.75 | 5 | −25% | −0.39 |
| Rowdy | 17 | −11% | 18% | 11 | −12% | −0.65 | 2 | +4% | +0.02 |
| aurelius | 15 | −12% | 40% | 8 | −9% | −0.34 | 7 | +8% | +0.16 |
| Lizzerd | 12 | −6% | 45% | 12 | −5% | −0.28 | 4 | −17% | −0.22 |
| remus | 8 | −15% | 43% | 4 | −10% | −0.21 | 3 | −25% | −0.23 |
| omo | 8 | −51% | 0% | 4 | +6% | +0.12 | 0 | — | 0 |
| binkie | 5 | −57% | 20% | 5 | −29% | −0.74 | 1 | +38% | +0.13 |
| ansem | 4 | −27% | 25% | 4 | −32% | −0.64 | 3 | −6% | −0.05 |
| pow | 3 | −57% | 0% | 3 | −14% | −0.21 | 3 | −16% | −0.16 |
| Quantzer, frank | 2 | — | — | 2 | — | +0.14 | 0 | — | 0 |

No wallet is clearly profitable to copy at this latency. aurelius and Lizzerd
are the least bad, and OCR, RC calendar, omo and pow are the worst. Samples
are tiny (3–31 signals), so wallet scoring is in the code
(`WALLET_SCORE_*`): it benches a wallet automatically once its copied trades
show negative expectancy over at least 5 trades. Removing wallets by hand is
the user's call.

### PnL by source

The replay only covers priority copies. Every wallet in `wallets.json` is
`"priority": true`, so that is the live strategy. Two other sources had no
history to measure:

- **Sniper:** it never traded on paper because of bugs #9 and #10 below.
- **Conviction:** it needs the real `journal.jsonl`.

`python -m backtest.replay --trade-log ledger_state.json` reports PnL by
source, wallet and exit reason from the real state file once it is available.

## 3. Top issues, ranked by money impact

"Real" means it could cost real money with `REAL_TRADING_ENABLED=true`.
"Paper" means it distorted paper results and therefore every decision based
on them.

| # | Issue | Impact | Fixed? |
|---|---|---|---|
| 1 | **Position sizing far too large for memecoins.** Paper used 8% of balance × LLM confidence (1–5×), so up to 40% per coin, capped at 0.5 SOL. Real-only allowed up to 30% of the USDC wallet per coin and 85% total exposure. The LLM confidence number was uncalibrated, and the prompt pushed it above 1. In the replay, the old real-only rules lose **42%** of the wallet in 9 days. | Real + paper | Yes. Risk-based sizing: 1% of equity risked per trade, about 3.3% position, max 5%, at most 4 positions and 20% exposure. The LLM no longer sizes. Real-trading caps lowered to 10% per position and 40% total (env-overridable). |
| 2 | **No circuit breakers in real-only mode.** No daily loss limit, no loss-streak pause, no trade-rate limit. In paper mode the "daily" limit used the **lifetime** PnL, so after one bad day the paper bot was blocked forever. | Real + paper | Yes. UTC-day loss limit (5% of day-start equity), 60 min cooldown after 3 straight losses, max 6 entries per hour, 4 h token re-entry cooldown. All apply to paper and real. |
| 3 | **Jupiter price-impact guard never fired.** Jupiter reports adverse impact as a **negative** number (verified live: −30.9% on a large quote), and the code checked `> 5.0`. Buys and sells could fill at 30%+ impact. | Real | Yes. Uses `abs()`, with separate entry (3%) and exit (8%) ceilings. |
| 4 | **Chasing.** No check of how far price had moved since the wallet bought, and no age check. Boot, downtime or adding a wallet would copy old buys. 57 of 167 replay signals were already 25%+ above the wallet's fill. | Real + paper | Yes. `FILTER_MAX_CHASE_PCT` (25%) and `FILTER_MAX_SIGNAL_AGE_SECONDS` (120 s). |
| 5 | **Mirror partial sells could dump the whole real position.** The paper slice in SOL was converted to USDC at today's price and sold. Real buys are often clamped smaller, so a 50% take-profit could sell 100%. | Real | Yes. Sells the same *fraction* of the real cost basis. |
| 6 | **LLM could average down without limit.** At the stop loss the LLM could answer "buy_dip" and add 50% more, again and again. | Real + paper | Yes. Off by default (`ALLOW_LLM_DIP_BUYS=false`), max 1 when enabled, legacy engine only. |
| 7 | **No token-safety checks.** No mint or freeze authority check (the creator can mint or freeze your tokens), and no Token-2022 transfer-hook or fee check. Priority copies also skipped the liquidity and top-10 filters. In the replay: 5 transfer-hook or fee tokens and 2 live-mint tokens. | Real + paper | Yes. Fail-closed safety gate on every entry: liquidity ≥ $15k, MC band, top-10 ≤ 35%, authorities revoked, dangerous extensions rejected, impact ≤ 2%. |
| 8 | **About 53% of signals were invisible.** Solana v1 transactions now exist, and `getTransaction` asked for `maxSupportedTransactionVersion: 0`. Those buys errored and were silently dropped. In the replay window, 88 of 167 signals were v1. OCR and remus were 100% v1, so the bot never saw any of their buys. Pagination of 10 signatures also lost bursts. | Paper, then real | Yes. Version 1, plus pagination up to 3 pages. |
| 9 | **Sniper paper sizing never clamped.** 10 SOL × 8% × confidence ≥ 2 = 1.6 SOL against the 0.5 SOL cap, so every paper snipe was "blocked". | Paper | Yes. Sized by the risk gate. |
| 10 | **Sniper holder filter impossible to pass.** It required ≥ 50 holders, but `getTokenLargestAccounts` returns at most 20, so every candidate was rejected. | Paper / real | Yes. |
| 11 | **Paper PnL had no fees or slippage**, so the dashboard looked better than reality (−7.6% vs −10.0% per trade in the replay). | Paper | Yes. 1.5% per side (`PAPER_COST_PER_SIDE_PCT`). |
| 12 | **Mixed price providers.** Priority copies entered at a DexScreener price and exited against Jupiter's staler price, which produced phantom PnL and wrong stop triggers. | Paper | Yes. Exits are priced by the same provider as the entry. |
| 13 | **No time stop, and positions with no price stayed open forever.** Dead tokens tied up capital and exposure slots. | Real + paper | Yes. Exit if not up 10% after 30 min, 24 h max hold, write-off after 60 min with no price. |
| 14 | **One exception crashed the main loop**, and a corrupt state file crashed the bot at boot. State writes weren't atomic. | Ops | Yes. Each stage is guarded, writes are atomic, and a corrupt file is quarantined. |
| 15 | **Equity ignored unrealized PnL**, so drawdown breakers fired late. | Paper | Yes. Marked to market. |
| 16 | **Multi-token transactions only handled the first token.** The seen-key was signature-only. | Paper | Yes. `sig:mint`. |
| 17 | **The Discord bot read the wrong state file** (the repo copy, not `DATA_DIR`), so its answers about positions were stale. | UX | Yes. |
| 18 | **The "dev sell" check on priority copies measures the copied trader's holding**, not the token developer's. | Logic | Documented, not changed. It works as a "trader is selling" signal. |
| 19 | **Conviction entries opened paper positions even with paper trading disabled.** | Paper | Yes. Conviction is now paper-only, explicitly. |

## 4. Behavior changes that need the user's review before deploy

- **Exit engine.** `EXIT_ENGINE=v2` is the default for every position type,
  including real-only. `EXIT_ENGINE=legacy` restores the old exits.
- **Real-trading caps lowered.** `MAX_REAL_POSITION_PCT` 0.30 → 0.10 and
  `MAX_TOTAL_EXPOSURE_PCT` 0.85 → 0.40. If these env vars are set in Railway,
  **the Railway values still win.**
- **Fail-closed entries.** If DexScreener or RPC data is missing, the entry is
  refused. Expect far fewer trades: about 33 instead of about 87 in the replay.
- **Conviction entries are paper-only.**
- **Discord trade messages are now short embed cards** (see `docs/DISCORD_CARDS.md`). Scale-ins and the real mirror of a paper trade are no longer posted; they are journaled.
  `DISCORD_TRADE_FORMAT=text` gives a markdown fallback.
- `REAL_TRADING_ENABLED` is untouched and still defaults to `false`. No keys or
  secrets were read, changed or printed.

## 5. Limits of this analysis

- **Short window, small sample.** 9 days, 167 signals, 33 new-rule trades. The
  direction (much smaller losses) is robust across latencies. The exact
  numbers are not.
- **Candle resolution.** 1-minute candles, with intra-minute order guessed
  (low before high on red candles). Real fills on thin pools can be worse than
  candle prices.
- **Filters not replayed.** Historical liquidity and top-10 holder share
  aren't free, so those two filters are not replayed. The live bot will reject
  more.
- **Market cap estimate.** Market cap at fill time is estimated from today's
  FDV/price ratio, and authorities from current on-chain state.
- **Not the bot's own trades.** The replay is not the bot's real history.
  Railway logs plus `ledger_state.json`, `journal.jsonl` and
  `real_positions.json` from the volume are needed to measure what actually
  happened.

## 6. What would actually move the needle next

1. **Get the real data** from the Railway volume and logs, and run
   `--trade-log`.
2. **Reduce latency.** Use a websocket or Helius/Yellowstone stream instead of
   60 s polling. Even so, the replay suggests copying these wallets is
   negative at any latency we tested.
3. **Find a better signal.** Evaluate wallets *before* adding them: forward
   returns after their buys, as `signal_quality` does here. Prefer wallets
   whose buys keep rising for 30 minutes or more, not ones that flip within
   minutes.
4. **Stay on paper with the new rules for at least 2–4 weeks**, and only arm
   real money if paper expectancy is positive *after costs*.


## 7. Phase 2: risk profiles, per-trader sizing, sniper, learning loop

### Replay per profile (same 167 signals, 9.3 days, 45 s latency, 1.5%/side costs)

Wallet edge is computed walk-forward: each copy is sized only from outcomes
closed *before* it. Risk of ruin is a bootstrap of each run's per-trade equity
returns over 30 days at the observed trade rate (5,000 paths). It treats trades
as independent and ignores the circuit breakers, so read the tail numbers as
rough.

| | conservative (default) | balanced | degen | scalper @45 s | scalper @15 s | legacy paper (old bot) |
|---|---|---|---|---|---|---|
| Trades | 33 | 43 | 50 | 34 | 34 | 87 |
| Win rate | 30% | 33% | 32% | 26% | 29% | 30% |
| Expectancy / trade | −10.3% | −13.5% | −16.0% | −7.7% | −6.8% | −10.0% |
| Avg size (% equity) | 3.3% | 1.4% | 2.1% | 2.0% | 2.0% | 6.6% |
| Return, 9.3 days | −10.8% | −8.4% | −17.5% | −4.9% | −3.8% | −43.6% |
| Max drawdown | −12.8% | −9.3% | −18.6% | −6.3% | −5.0% | −44.2% |
| Median 30-day return (bootstrap) | −31% | −25% | −46% | −15% | −12% | −84% |
| P(−50% drawdown in 30 days) | 0% | 0% | 23% | 0% | 0% | 99.7% |
| P(−80% drawdown in 30 days) | 0% | 0% | 0% | 0% | 0% | 73% |
| P(losing month) | 100% | 100% | 100% | 100% | 100% | 100% |

**Plain reading.** The copied wallets had no edge over this period: about −17%
per signal after realistic latency and costs, whatever the exits. Edge sizing
does what it is designed to do. It starts every wallet small and benches
wallets as their losses come in, so degen never reached its 8–10% "top" tier
(average size was 2.1%). Degen still loses about 1.6× more than conservative
because its filters are looser and its stops wider. No profile is profitable on
this data. Degen sizing only pays off **after** some wallets show a real,
measured edge, and the scoring is built to detect exactly that.

### Walk-forward exit tuning (`python -m tuning --profile X`)

| Profile | Out-of-sample mean/trade, tuned vs current | Verdict |
|---|---|---|
| conservative | −17.9% vs −16.6% | rejected (kept current) |
| balanced | −18.5% vs −19.2% | rejected (< +1pp) |
| degen | −16.7% vs −21.2% | would be accepted: stop 20%, TP1 +20%:½, time stop 60 m, trail 25% |

Tuning makes losing signals lose less. It cannot create an edge. The bot only
retunes from its own recorded signals (≥ 60), at most daily, within the
`TUNABLE_BOUNDS`. Nothing tuned on this backtest is shipped.

### Sniper: why it never traded, and the fixes

| Blocker | Fix |
|---|---|
| LLM conviction ≥ 2.0 required; without an Anthropic key (or on any error) confidence = 1.0 | `SNIPER_MIN_CONFIDENCE` defaults to 0 (off) |
| `require_socials`, but the pumpdev WS event has no socials | Socials are read from the launch's IPFS `uri` metadata |
| DexScreener returns no pairs for bonding-curve tokens, so there was no price, liquidity or MC, and exits were written off at 60 min | GeckoTerminal fallback (throttled and cached) for price, liquidity, MC and txns |
| Top-10 holders included the bonding-curve and pool vaults (PDAs), so it was always over the limit | Off-curve owners are excluded |
| No migration feed | Polls GeckoTerminal `new_pools` for pumpswap/raydium/meteora pools of `…pump` mints |
| Not indexed yet at first look meant dropped | Up to 6 re-checks while inside the age window |

The age window, size and liquidity floor now come from the profile. All the
token-safety rails still apply.

### Scalper profile (`RISK_PROFILE=scalper`)

The scalper copies fresh signals only (≤ 45 s old). It uses a 10% stop,
takes ½ at +12% and 30% at +25%, arms the trailing stop at +15% (8% trail),
time-stops after 5 min without a +4% move, and holds 30 min at most.
`WALLET_POLL_SECONDS` defaults to 15 s for this profile; it is 60 s
otherwise. On both datasets the scalper loses least, because its losses are
small. Speed is what matters: on the Fomo dataset, going from 45 s to 15 s
moves expectancy from −8.0% to −1.2% per copy.

### Fomo leaderboard traders (user request, 2026-10-09)

**Source of handles:** the user's screenshots (ALL and 30D tabs) plus the
public pages of unofficial trackers (fomo-api.com for the 24h/7d/30d top
10; fomoindex.xyz for ALL-time). 35 handles resolved to 29 Solana wallets
and are listed in `backtest/data/fomo_candidates.json`. These
handle→wallet mappings come from third parties. fomo.family's own API needs a
logged-in session, so it was not used. Handles with no Solana wallet found:
fxcxc, Adolf, Hifive_07, jxnwa, hitscanr, TradBengal, Burgz.

**Copy data:** each wallet's last 7 days (up to its newest 300
signatures) of on-chain buys, via `collect_wallet_buys`. That is 931 buys
and 178 unique (wallet, token) signals. Some wallets showed 0–2 buys
(end837, DonnyDicey, leo, Salem, uncsnipes, unipcs, frankdegods). Either
they trade from other wallets, or their swaps go through routes our buy
parser does not decode.

| Fomo dataset, 6.9 days | conservative | balanced | degen | scalper @45 s | scalper @15 s | legacy paper |
|---|---|---|---|---|---|---|
| Trades | 35 | 45 | 54 | 40 | 39 | 93 |
| Expectancy / trade | −11.5% | −13.7% | −11.0% | −8.0% | **−1.2%** | −0.9% |
| Profit factor | 0.35 | 0.37 | 0.48 | 0.37 | **0.88** | 0.95 |
| Return | −12.8% | −9.2% | −10.1% | −5.4% | **−1.1%** | −4.2% |
| Max drawdown | −16.4% | −10.8% | −11.3% | −5.8% | **−5.1%** | −24.4% |
| Median 30-day return (bootstrap) | −45% | −34% | −37% | −21% | −5% | −17% |
| P(−50% drawdown, 30 d) | 22% | 0% | 1% | 0% | 0% | 18% |
| P(losing month) | 100% | 100% | 100% | 100% | 68% | 68% |

Raw Fomo signals are clearly better than the current `wallets.json` set: a
median of −6% at 30 min (versus −21%), and 35% are up at 30 min (versus 25%).
Copying them is still not profitable after costs at a realistic latency. The
fastest scalper is close to breakeven. This is one week of data.

**Re-ranked by OUR copy result** (`python -m backtest.copy_score`, scalper
exits, 15 s latency, after costs; small samples):

| Rank | Handle | Copies | Win | Mean / copy | Shrunk R |
|---|---|---|---|---|---|
| 1 | OmakaseOnly | 11 | 36% | +7.4% | +0.51 |
| 2 | pointfarmcap | 3 | 67% | +2.6% | +0.10 |
| 3 | bystevenr | 15 | 27% | +0.2% | +0.01 |
| … | Iri0o | 24 | 54% | −3.7% | −0.31 |
| … | bigbabba | 13 | 23% | −6.2% | −0.45 |
| … | domain0X | 14 | 14% | −13.7% | −1.01 |
| last | ExactTallTakin (#1 on 30D) | 21 | 33% | −13.2% | −1.07 |

The #1 trader on the 30D leaderboard was the worst wallet to copy. Leaderboard
PnL is not copy edge. Size, holding conviction and our fill delay all
differ. The bot seeds these wallets into **shadow** in copy-score order
(`LEARNING_SEED_FILE`). They are promoted only after their own shadow
results clear `LEARNING_PROMOTE_MIN_R` over `LEARNING_PROMOTE_MIN_TRADES`.

### DegenCapitalLLC (added 2026-10-10, paper-only candidate)

- **Wallet:** `4CRX74nxAdmFY4Eh1WhTrmYqHoxwxGzwXfgzTWvhUfFn`. The trader described it as "my public fomo wallet" on X; Fomo has not verified it.
- **Data:** 158 buys across 94 tokens, 2026-09-28 to 2026-10-09.

| Copy setup | Copies | Win rate | Mean / copy | Shrunk R |
|---|---|---|---|---|
| scalper @15 s | 83 | 47% | **+1.8%** | +0.17 |
| scalper @45 s | 81 | 43% | −3.1% | −0.29 |
| degen @45 s | 81 | 38% | −15.0% | −0.47 |

- **Forward returns after their buys:** median −8.7% at +5 min and −45% at +30 min. They win by exiting fast. A copy only works if it is just as fast.
