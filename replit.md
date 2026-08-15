# Wallet Feed Inspection Tool

Python command-line tool for inspecting recent Solana wallet transactions through Helius.

## Run & Operate

- `python3 test_wallet_feed.py` — inspect the default wallets
- `python3 test_wallet_feed.py --limit 10` — request more transactions
- `python3 test_wallet_feed.py --wallet name=SOLANA_ADDRESS` — inspect a custom wallet
- Required secret: `HELIUS_API_KEY`
- `pnpm --filter @workspace/api-server run dev` — run the API server (port 5000)
- `pnpm run typecheck` — full typecheck across all packages
- `pnpm run build` — typecheck + build all packages
- `pnpm --filter @workspace/api-spec run codegen` — regenerate API hooks and Zod schemas from the OpenAPI spec
- `pnpm --filter @workspace/db run push` — push DB schema changes (dev only)
- Required env: `DATABASE_URL` — Postgres connection string

## Stack

- Python 3.12
- Requests
- pnpm workspaces, Node.js 24, TypeScript 5.9
- API: Express 5
- DB: PostgreSQL + Drizzle ORM
- Validation: Zod (`zod/v4`), `drizzle-zod`
- API codegen: Orval (from OpenAPI spec)
- Build: esbuild (CJS bundle)

## Where things live

- `test_wallet_feed.py` — wallet inspection CLI
- `requirements.txt` — Python dependency declaration
- `README.md` — usage instructions

## Architecture decisions

- Helius credentials are read only from Replit Secrets through `HELIUS_API_KEY`.
- The tool prints raw transaction JSON so parsing rules can be calibrated against live wallet data.

## Product

Fetches and prints recent raw transactions for the configured Solana wallets.

## User preferences

_Populate as you build — explicit user instructions worth remembering across sessions._

## Gotchas

_Populate as you build — sharp edges, "always run X before Y" rules._

## Pointers

- See the `pnpm-workspace` skill for workspace structure, TypeScript setup, and package details
