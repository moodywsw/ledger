# "Live small" config: prepared, NOT enabled

This setup has negative expectancy in every replay so far. The best case is
the scalper at 15 s latency on the Fomo top traders: about −1% per copy over
one week. Treat the 1.5 SOL as money you are prepared to lose. Nothing here
is turned on. `REAL_TRADING_ENABLED` defaults to `false`.

## Railway variables (names only; set values yourself)

| Variable | Suggested | Purpose |
|---|---|---|
| `REAL_TRADING_ENABLED` | `false` until you decide | Master arm. |
| `REAL_KILL_SWITCH` | `false` (`true` = stop new buys now) | Blocks every new real buy. Exits keep running. |
| `REAL_BUDGET_USDC` | 1.5 SOL in USD (e.g. 1.5 × SOL price) | Hard ceiling on open real cost basis. |
| `REAL_MAX_TRADE_USDC` | 3% of budget | Absolute per-trade ceiling. |
| `MAX_REAL_POSITION_PCT` | `0.03` | Per-trade cap as % of wallet USDC (backstop). |
| `MAX_TOTAL_EXPOSURE_PCT` | `0.40` | Wallet-level exposure backstop. |
| `REAL_DAILY_LOSS_CAP_USDC` | ~10% of budget | No new real buys after this realized loss (UTC day). |
| `REAL_COPY_ALLOWLIST` | comma list of promoted Fomo wallets | ONLY these wallets' copies go real. |
| `REAL_SNIPER_ENABLED` | `false` | Sniper stays paper. |
| `LEARNING_PROMOTE_TO_REAL` | `true` | Lets promoted (shadow-proven) wallets mirror. The allowlist still applies. |
| `RISK_PROFILE` | `scalper` | Fast in/out exits. |
| `EDGE_MAX_PCT` / `EDGE_TOP_PCT` / `RISK_MAX_POSITION_PCT` | `0.03` | Paper sizing capped at 3% too, so paper and real match. |
| `WALLET_POLL_SECONDS` | `15` | The scalper only works with fast detection. |
| `SOLANA_PRIVATE_KEY` | secret | A **new, dedicated** bot wallet, never your Fomo wallet. |
| `SOLANA_WALLET_ADDRESS` | public address | Pins the key to the expected wallet. |
| `ALCHEMY_RPC_URL` (or `HELIUS_API_KEY`) | paid/free-tier key | Public RPC rate-limits (429s); required for 15 s polling. |
| `JUPITER_API_KEY` | key | Jupiter Ultra order/execute. |
| `MIN_SOL_FOR_GAS` | `0.02` | Gas reserve. |

**Wallet funding.** Real buys spend **USDC**, so fund the dedicated wallet
with the budget in USDC, plus about 0.05 SOL for fees and token-account rent.

**Allowlist.** Set it only after a wallet is promoted in shadow, i.e. it
clears `LEARNING_PROMOTE_MIN_TRADES` / `LEARNING_PROMOTE_MIN_R` on its own
paper copies. The Discord learning summary lists promotions. Leaving the
allowlist empty means every priority wallet in `wallets.json` can mirror to
real. Don't do that.

**Kill switch.** Set `REAL_KILL_SWITCH=true` on Railway and redeploy/restart.
New buys stop and open positions still exit. For a full stop, set
`REAL_TRADING_ENABLED=false`.

## Go-live checklist
1. Run 1–2 weeks of paper with these exact settings and check the shadow
   results of the allowlisted wallets.
2. Start with half the budget for the first week.
3. Review daily. If the realized loss reaches the daily cap twice in a week,
   switch the kill switch on.
