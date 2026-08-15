"""
ledger_discord_bot.py — Ledger, live in your Discord server

This is the conversational half of Ledger: he responds when mentioned
or DM'd, in his own voice, grounded in:
  - His actual persona (trench-native, balanced risk, businessman core)
  - His current paper trading state (open positions, recent PnL) —
    read from ledger_state.json, produced by ledger_bot.py
  - His recent market research — read from market_intel.json,
    produced by market_intel.py
This keeps his answers consistent with what he's actually "seen" and
"done," instead of generic chatbot responses.

Env vars required:
  DISCORD_BOT_TOKEN   - from discord.com/developers/applications
                         (needs the "Message Content" privileged intent
                         enabled in the Bot settings, or he can't read
                         what people write)
  ANTHROPIC_API_KEY

Install:
  pip install discord.py requests --break-system-packages

Usage:
  python3 ledger_discord_bot.py
"""

import os
import json
import requests
import discord
from pathlib import Path

DISCORD_BOT_TOKEN = os.environ.get("DISCORD_BOT_TOKEN", "")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
ANTHROPIC_API_URL = "https://api.anthropic.com/v1/messages"
MODEL = "claude-sonnet-4-6"

LEDGER_STATE_FILE = Path("ledger_state.json")
MARKET_INTEL_FILE = Path("market_intel.json")

LEDGER_SYSTEM_PROMPT = """You are Ledger, a self-made Solana memecoin \
trader turned businessman. You cut your teeth in the trenches — \
survived enough rugs and 100x's to develop real discipline. You're not \
a hype-poster; you've seen too many people blow up chasing green \
candles. You think like a businessman first, degen second: every \
trade is a position sized against a thesis, not a vibe.

Voice: trench-native, a little cocky when you're right, direct and \
undecorated when you're wrong or passing on something. You say things \
like "this one's got legs," "thin liquidity, I'm not touching it," \
"sized in small, this is a scout not a full position." You're \
fundamentally a businessman underneath the slang, not defined by it.

Risk profile: balanced. You take decent-conviction setups, not just \
A+ ones — you're not waiting around for perfect. You size smaller on \
unproven "scout" plays, bigger on high-conviction setups. You call out \
obvious rugs and thin liquidity without hesitation.

You're currently running in PAPER TRADING mode — no real money is on \
the line yet. Be honest about that if asked; don't pretend trades are \
real. You're building a track record before real capital gets involved.

Keep replies conversational and Discord-appropriate — a few sentences, \
not an essay, unless someone genuinely asks for a deep breakdown."""


def load_context() -> str:
    """
    Pulls in current trading state and recent market research to
    ground Ledger's replies in what he's actually seen and done.
    """
    context_parts = []

    if LEDGER_STATE_FILE.exists():
        try:
            state = json.loads(LEDGER_STATE_FILE.read_text())
            open_positions = state.get("open_positions", {})
            balance = state.get("balance_sol", "unknown")
            pnl = state.get("realized_pnl_sol", "unknown")

            position_lines = []
            for mint, pos in open_positions.items():
                symbol = pos.get("symbol") or "(unresolved symbol)"
                position_lines.append(
                    f"  - {symbol} — full mint: {mint} — entry {pos.get('entry_price')}, "
                    f"size {pos.get('size_sol')} SOL, opened by {pos.get('opened_by', 'unknown')}"
                )
            positions_text = "\n".join(position_lines) if position_lines else "  (none)"

            context_parts.append(
                f"Current paper trading state: balance {balance} SOL, "
                f"realized PnL {pnl} SOL, {len(open_positions)} open position(s):\n{positions_text}\n\n"
                f"IMPORTANT: when asked for a token's address/mint, always give the FULL "
                f"address shown above verbatim — never truncate or abbreviate it with '...'."
            )
        except (json.JSONDecodeError, OSError) as e:
            print(f"[WARN] couldn't load ledger state: {e}")

    if MARKET_INTEL_FILE.exists():
        try:
            entries = json.loads(MARKET_INTEL_FILE.read_text())
            recent = entries[-3:]  # most recent research findings
            if recent:
                notes = "\n".join(f"- ({e['timestamp']}) {e['summary']}" for e in recent)
                context_parts.append(f"Your recent market research:\n{notes}")
        except (json.JSONDecodeError, OSError) as e:
            print(f"[WARN] couldn't load market intel: {e}")

    return "\n\n".join(context_parts) if context_parts else "No trading state or research data available yet."


def ask_claude(user_message: str) -> str:
    if not ANTHROPIC_API_KEY:
        return "(Ledger's brain isn't wired up — ANTHROPIC_API_KEY isn't set.)"

    context = load_context()
    full_system_prompt = f"{LEDGER_SYSTEM_PROMPT}\n\n--- Current context ---\n{context}"

    headers = {
        "x-api-key": ANTHROPIC_API_KEY,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }
    payload = {
        "model": MODEL,
        "max_tokens": 400,
        "system": full_system_prompt,
        "messages": [{"role": "user", "content": user_message}],
    }

    try:
        resp = requests.post(ANTHROPIC_API_URL, headers=headers, json=payload, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        text_parts = [b["text"] for b in data.get("content", []) if b.get("type") == "text"]
        return "\n".join(text_parts).strip() or "(no response generated)"
    except Exception as e:
        # Broad on purpose — this feeds directly into a Discord reply,
        # so ANY failure here (network, malformed response, whatever)
        # must degrade gracefully instead of crashing the bot's message
        # handler and going silent.
        print(f"[ERROR] Claude API call failed: {e}")
        return "Having trouble thinking straight right now — try again in a bit."


# ── Discord client ───────────────────────────────────────────────────

intents = discord.Intents.default()
intents.message_content = True  # required to read message text — must
                                 # also be enabled in the Dev Portal

client = discord.Client(intents=intents)


@client.event
async def on_ready():
    print(f"Ledger is live as {client.user}")


@client.event
async def on_message(message):
    if message.author == client.user:
        return  # never respond to himself

    is_mentioned = client.user in message.mentions
    is_dm = isinstance(message.channel, discord.DMChannel)

    if not (is_mentioned or is_dm):
        return  # only respond when directly addressed — not every message in the channel

    async with message.channel.typing():
        # Strip the mention itself out of the text sent to Claude
        clean_content = message.content.replace(f"<@{client.user.id}>", "").strip()
        reply = ask_claude(clean_content or "Someone said hi with no message.")
        await message.reply(reply)


if __name__ == "__main__":
    if not DISCORD_BOT_TOKEN:
        raise RuntimeError("Set DISCORD_BOT_TOKEN env var first.")
    client.run(DISCORD_BOT_TOKEN)
