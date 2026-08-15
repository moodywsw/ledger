"""Inspect token transfers from recent Helius transactions.

This is intended to run before the full ledger bot so the transaction shape
can be inspected before writing any transaction parsing rules.

Usage:
    python3 test_wallet_feed.py
    python3 test_wallet_feed.py --limit 10
    python3 test_wallet_feed.py --wallet frank=498g1rVnFcnjBjpfw1xyqA1WvgQXUU8RWuELjxkjAayQ

The Helius API key is read from the HELIUS_API_KEY environment variable.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass
from typing import Any

import requests


HELIUS_BASE_URL = "https://api.helius.xyz/v0"
DEFAULT_LIMIT = 5
DEFAULT_OUTPUT_FILE = "wallet_feed_output.json"


@dataclass(frozen=True)
class Wallet:
    name: str
    address: str


DEFAULT_WALLETS = (
    Wallet("frank", "498g1rVnFcnjBjpfw1xyqA1WvgQXUU8RWuELjxkjAayQ"),
    Wallet("RC", "DxM1hfY8FQ8dNGrucuJzhJcF8KRbjk8WBwrgKvQ9spPv"),
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Print recent raw Helius transactions for selected wallets."
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=DEFAULT_LIMIT,
        help=f"Transactions to request per wallet (default: {DEFAULT_LIMIT}).",
    )
    parser.add_argument(
        "--wallet",
        action="append",
        metavar="NAME=ADDRESS",
        help="Wallet to inspect; repeat for multiple wallets. Defaults to frank and RC.",
    )
    parser.add_argument(
        "--json-output",
        default=DEFAULT_OUTPUT_FILE,
        metavar="PATH",
        help=f"Save the filtered JSON output (default: {DEFAULT_OUTPUT_FILE}).",
    )
    return parser.parse_args()


def parse_wallets(values: list[str] | None) -> tuple[Wallet, ...]:
    if not values:
        return DEFAULT_WALLETS

    wallets: list[Wallet] = []
    for value in values:
        name, separator, address = value.partition("=")
        if not separator or not name.strip() or not address.strip():
            raise ValueError(
                f"Invalid wallet {value!r}; use the format NAME=SOLANA_ADDRESS."
            )
        wallets.append(Wallet(name.strip(), address.strip()))
    return tuple(wallets)


def fetch_transactions(
    session: requests.Session,
    wallet: Wallet,
    api_key: str,
    limit: int,
) -> list[dict[str, Any]]:
    url = f"{HELIUS_BASE_URL}/addresses/{wallet.address}/transactions"
    response = session.get(
        url,
        params={"api-key": api_key, "limit": limit},
        timeout=15,
    )
    response.raise_for_status()

    payload = response.json()
    if not isinstance(payload, list):
        raise ValueError("Helius returned an unexpected response shape.")
    return [item for item in payload if isinstance(item, dict)]


def print_wallet_transactions(
    session: requests.Session,
    wallet: Wallet,
    api_key: str,
    limit: int,
) -> tuple[bool, list[dict[str, Any]]]:
    print(f"\n{'=' * 60}")
    print(f"  {wallet.name}  ({wallet.address})")
    print("=" * 60)

    try:
        transactions = fetch_transactions(session, wallet, api_key, limit)
    except requests.exceptions.HTTPError as error:
        status = error.response.status_code if error.response is not None else "unknown"
        print(f"Request failed with HTTP {status}.")
        return False, []
    except requests.exceptions.RequestException as error:
        print(f"Request failed: {error}")
        return False, []
    except (ValueError, json.JSONDecodeError) as error:
        print(f"Could not read Helius response: {error}")
        return False, []

    if not transactions:
        print("No recent transactions found.")
        return True, []

    for index, transaction in enumerate(transactions, start=1):
        print(f"\n--- transaction {index} ---")
        print(f"type: {transaction.get('type', 'UNKNOWN')}")
        print(f"source: {transaction.get('source', 'UNKNOWN')}")
        print(f"signature: {transaction.get('signature', 'UNKNOWN')}")
        print("tokenTransfers:")
        print(json.dumps(transaction.get("tokenTransfers", []), indent=2))
    return True, transactions


def main() -> int:
    api_key = os.environ.get("HELIUS_API_KEY", "").strip()
    if not api_key:
        print(
            "HELIUS_API_KEY is not set. Add it to your Replit Secrets, "
            "then run this tool again.",
            file=sys.stderr,
        )
        return 1

    args = parse_args()
    if args.limit < 1:
        print("--limit must be at least 1.", file=sys.stderr)
        return 2

    try:
        wallets = parse_wallets(args.wallet)
    except ValueError as error:
        print(error, file=sys.stderr)
        return 2

    output: list[dict[str, Any]] = []
    success = True
    with requests.Session() as session:
        for wallet in wallets:
            wallet_success, transactions = print_wallet_transactions(
                session, wallet, api_key, args.limit
            )
            success = wallet_success and success
            output.append(
                {
                    "wallet": wallet.name,
                    "address": wallet.address,
                    "transactions": [
                        {
                            "type": transaction.get("type", "UNKNOWN"),
                            "source": transaction.get("source", "UNKNOWN"),
                            "signature": transaction.get("signature", "UNKNOWN"),
                            "tokenTransfers": transaction.get("tokenTransfers", []),
                        }
                        for transaction in transactions
                    ],
                }
            )

    try:
        with open(args.json_output, "w", encoding="utf-8") as output_file:
            json.dump(output, output_file, indent=2)
            output_file.write("\n")
        print(f"\nFiltered JSON saved to {args.json_output}")
    except OSError as error:
        print(f"Could not save JSON output: {error}", file=sys.stderr)
        success = False

    return 0 if success else 1


if __name__ == "__main__":
    raise SystemExit(main())