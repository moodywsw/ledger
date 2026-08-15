"""
pumpfun_listener.py — Pump.fun new-token & whale-trade listener

Pump.fun has no OFFICIAL public API, but there are free, keyless
third-party WebSocket feeds that stream Pump.fun's on-chain activity
directly (new token creations, buys, sells) with sub-second latency.
This uses one such feed (pumpdev.io) as an example.

IMPORTANT — this is a third-party, unofficial service:
  - Verify the current docs/endpoint before relying on this, since
    unofficial feeds can change or disappear without notice
  - Don't wire this to real trades until you've watched it run for a
    while and trust what it reports

What this does:
  - Prints every new token creation on Pump.fun as it happens
  - Flags "whale" launches/buys — trades above WHALE_SOL_THRESHOLD —
    since a token launched or bought heavy by one wallet is a
    different signal than the usual small-dev-buy launch spam
  - Cross-references the launch creator against WATCHED_WALLETS from
    ledger_bot.py — if a trader you already track deployed the token
    themselves, that's flagged as the highest-conviction signal

Install:
  pip install websockets --break-system-packages

Usage:
  python3 pumpfun_listener.py
"""

import asyncio
import json
import websockets
from ledger_bot import WALLET_HANDLES, WATCHED_WALLETS

PUMPFUN_WS_URL = "wss://pumpdev.io/ws"

# A launch or buy at/above this size gets flagged as a whale-level move
WHALE_SOL_THRESHOLD = 5.0


async def listen():
    print(f"Connecting to {PUMPFUN_WS_URL} ...")
    async with websockets.connect(PUMPFUN_WS_URL) as ws:
        # Subscribe to new token creation events
        await ws.send(json.dumps({"method": "subscribeNewToken"}))
        print("Subscribed to new token launches. Listening...\n")

        async for raw_msg in ws:
            try:
                event = json.loads(raw_msg)
            except json.JSONDecodeError:
                continue

            handle_event(event)


def handle_event(event: dict):
    """
    Expected shape (per this feed's docs — verify against current
    docs since third-party formats can drift):
        {
          "name": ..., "symbol": ..., "mint": ...,
          "creator": ..., "initialBuySol": ..., "marketCapSol": ...
        }
    """
    name = event.get("name", "UNKNOWN")
    symbol = event.get("symbol", "?")
    mint = event.get("mint", "")
    creator = event.get("creator", "")
    initial_buy = event.get("initialBuySol", 0) or 0

    # Cross-reference: is this launch tied to a wallet we already track?
    known_handle = WALLET_HANDLES.get(creator)

    whale_tag = " [WHALE LAUNCH]" if initial_buy >= WHALE_SOL_THRESHOLD else ""
    tracked_tag = f" [TRACKED: {known_handle}!]" if known_handle else ""

    print(f"[NEW TOKEN]{whale_tag}{tracked_tag} {name} (${symbol})")
    print(f"  mint: {mint}")
    print(f"  creator: {creator}")
    print(f"  initial buy: {initial_buy} SOL")

    if known_handle:
        # This is the strongest possible signal: a trader you already
        # track deployed the token themselves, day one.
        print(f"  >>> {known_handle} is behind this launch — highest conviction signal available.\n")
    else:
        print()

    # Further hook point: for buys (not just launches), this feed can
    # also stream trade events per-token (subscribeTokenTrade) — worth
    # adding once launches alone prove useful, to catch a tracked
    # wallet buying INTO someone else's launch, not just creating one.


if __name__ == "__main__":
    asyncio.run(listen())
