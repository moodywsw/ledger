# Cross-chain (EVM) copy trading: assessment, not built

Fomo users trade on Solana, Base, BSC, Ethereum and (heavily, lately)
Robinhood Chain. The bot must use **its own wallets**: Fomo's ToS forbids
automated control of a Fomo account.

## What it takes (Base first)

| Piece | Option (free tier) | Notes |
|---|---|---|
| Detect copied trader buys | Base RPC `eth_getLogs` on ERC-20 `Transfer` *to* the trader + router swaps, polled every 2 s blocks; or Alchemy/QuickNode webhooks | Free public RPCs (mainnet.base.org) rate-limit hard; an Alchemy free key is enough for ~30 wallets |
| Map handle → EVM wallet | FomoLens/third-party trackers (EVM family stored per trader) | Same third-party-mapping caveat as Solana |
| Execution | 0x Swap API v2 (free key), 1inch (key), Uniswap Universal Router direct, or LI.FI/Relay for routing incl. cross-chain | 0x/1inch handle Permit2/allowances; need ETH on Base for gas |
| Prices/liquidity | GeckoTerminal (`base`, `bsc` networks), already wired | Same throttle |
| Safety | Honeypot/tax check (simulate sell via `eth_call`, or GoPlus token security API, free), owner/blacklist/mint functions, LP lock | EVM rugs are different from Solana: sell taxes, blacklists, proxy upgrades |
| Keys | separate EVM private key env (`EVM_PRIVATE_KEY`), separate budget | Never reuse Solana key |
| Funds | USDC/ETH on Base; bridge via Relay/LI.FI manually | Bot should not bridge automatically at first |

## Effort (rough)
- Base paper-only (detect, price, paper trade, replay): **3–5 days**.
- Base real execution (0x + honeypot sim + allowances + gas mgmt + tests): **+4–6 days**.
- BSC after Base: **+2–3 days** (same EVM code, PancakeSwap routes, more honeypots).
- Robinhood Chain: depends on aggregator/GeckoTerminal coverage; check first.

## Risks
- Honeypots / sell taxes: the biggest EVM-specific loss mode. A sell simulation before every buy is mandatory.
- MEV/sandwiching on Base/BSC public mempools: use private RPC (e.g. Flashbots Protect on Ethereum; on Base the sequencer is private but bundles still front-run via ordering).
- Latency: our detection lags the trader by blocks. Replay showed that latency is the main driver of results.
- Gas: Ethereum L1 gas makes small tickets pointless. Base/BSC are fine.
- More code surface handling real keys.

## Proposed plan
1. Resolve EVM wallets of promoted Fomo traders (FomoLens free lookup by hand).
2. Base **paper** fetcher + replay (copy_score on Base buys). Go further only if copy-score > 0.
3. Real execution behind its own `EVM_REAL_TRADING_ENABLED=false`, same live-small rails (budget, kill switch, allowlist).
4. BSC, then others, only if step 2 shows edge.

## Chain usage of active traders (2026-10-10)
`wallets.json` now stores a `chains` map per trader (solana / base / bsc / robinhood; one EVM address covers all three EVM chains).
Data sources: fomoapi.io/top100 (public page, full wallets), Provadata trader pages (which chains are verified).
Daily per-chain activity is written by `/workspace/fomo-watch/run.py` to `candidates.json -> chain_mix`.

| Trader | Solana | EVM wallet | Verified chains (Provadata) | Observed activity (first scan, 6h RH window) |
|---|---|---|---|---|
| unipcs | 2heJ…DogF | 0x0a6e…119e | Solana, BNB, Robinhood | Robinhood 41 transfers, Solana quiet |
| DumbCrayonEater | 5FGo…rp8V | 0x8f62…80a3 | Solana, Robinhood | Robinhood 18, Solana active |
| frank | 498g…AayQ | 0x696d…8e28 | Solana, Robinhood | Robinhood 8, Solana active |
| OuterHeavyBat | 49nv…xmgS (not his trading wallet) | 0x4f…4c55 (partial) | Solana, BNB, Robinhood | EVM unknown until full address |
| ogle | jrbG…s5Ux | 0x1b…1143 / 0xe9…57be (partial) | Solana, Robinhood | Solana quiet |
| seralberttrades (inactive) | Cqu5…QSsR (partial) | 0x9164…8fda | Solana, Robinhood | not yet scanned |
| ansem, Lizzerd, RC calendar, DegenCapitalLLC | yes | not resolved | n/a | Solana only |

Takeaway: the big Fomo names do most of their trading on **Robinhood Chain**, then Solana. None is verified on Base, and two (unipcs, OHB) are verified on BSC.
Revised order: Robinhood Chain paper fetcher first (free RPC allows a 30k-block getLogs window and it works), then BSC. Base comes last: no tracked trader is verified there.
Base/BSC public RPCs rate-limit eth_getLogs. Set `BASE_RPC` / `BSC_RPC` to a free-tier key (Alchemy/QuickNode) before relying on those numbers.
