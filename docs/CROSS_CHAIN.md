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
