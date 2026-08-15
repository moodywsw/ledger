# Wallet Feed Inspection Tool

This Python command-line tool fetches recent transactions for one or more
Solana wallets through the Helius API and prints each transaction's
`tokenTransfers` array in full. Use it to inspect the real response shape
before adding transaction parsing logic to a ledger bot.

## Run it

1. Add `HELIUS_API_KEY` to the Replit Secrets panel.
2. Run:

   ```bash
   python3 test_wallet_feed.py
   ```

The default run checks the `frank` and `RC` wallets and requests five
transactions for each.

## Options

Request more transactions:

```bash
python3 test_wallet_feed.py --limit 10
```

Inspect a custom wallet:

```bash
python3 test_wallet_feed.py \
  --wallet my-wallet=YOUR_SOLANA_WALLET_ADDRESS
```

Pass `--wallet` more than once to inspect multiple custom wallets. The output
includes the transaction type, source, signature, and the complete
`tokenTransfers` array. Each run also saves the same filtered data to
`wallet_feed_output.json`.

Choose a different output path if needed:

```bash
python3 test_wallet_feed.py --json-output output.json
```