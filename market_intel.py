"""
market_intel.py — Ledger's autonomous research loop

Periodically asks Claude (with web search enabled) to research current
Solana memecoin market conditions, trends, and narratives, and saves
the findings to market_intel.json. This file is then loaded as context
by ledger_discord_bot.py, so Ledger's conversational answers are
grounded in something researched recently — not just static persona
text or training data.

IMPORTANT — what "learning" means here: this does NOT retrain or
fine-tune any model. It builds an accumulating, timestamped research
log that gets fed back in as context on every conversation. That's
what "getting smarter over time" means in practice — growing context,
not changing model weights. Being upfront about that distinction
matters so you don't expect something this isn't.

Env vars required:
  ANTHROPIC_API_KEY

Install:
  pip install requests --break-system-packages

Usage:
  python3 market_intel.py          # runs forever, researching every 4 hours
"""

import os
import json
import time
import requests
from pathlib import Path
from datetime import datetime, timezone

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
ANTHROPIC_API_URL = "https://api.anthropic.com/v1/messages"
MODEL = "claude-sonnet-5"

INTEL_FILE = Path("market_intel.json")
MAX_ENTRIES_KEPT = 30  # keep the log from growing forever — rolls off old entries
RESEARCH_INTERVAL_SECONDS = 4 * 60 * 60  # how often to run a fresh research pass

RESEARCH_PROMPT = """You're researching the current state of the Solana \
memecoin market for a trader persona named Ledger. Search for what's \
happening RIGHT NOW — the last 24-48 hours — and summarize in a tight, \
practical way:

1. What's the overall market mood in the Solana trenches (risk-on / \
risk-off / choppy)?
2. Any narrative or "meta" that's clearly rotating in or out right now \
(e.g. a theme of coins getting attention)?
3. Any major single events worth knowing (a big rug, a major KOL call, \
a notable launch)?

Keep the whole summary under 200 words, written as dense factual notes \
— not a full article. This will be fed to another AI as context, not \
read directly by a person, so skip pleasantries and get straight to \
the substance."""


def run_research() -> str:
    if not ANTHROPIC_API_KEY:
        raise RuntimeError("Set ANTHROPIC_API_KEY env var first.")

    headers = {
        "x-api-key": ANTHROPIC_API_KEY,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }
    payload = {
        "model": MODEL,
        "max_tokens": 600,
        "messages": [{"role": "user", "content": RESEARCH_PROMPT}],
        "tools": [{"type": "web_search_20250305", "name": "web_search"}],
    }

    resp = requests.post(ANTHROPIC_API_URL, headers=headers, json=payload, timeout=60)
    resp.raise_for_status()
    data = resp.json()

    # Response content may include tool_use/tool_result blocks alongside
    # text — concatenate just the text parts for the final summary.
    text_parts = [block["text"] for block in data.get("content", []) if block.get("type") == "text"]
    return "\n".join(text_parts).strip()


def save_finding(summary: str):
    entries = []
    if INTEL_FILE.exists():
        entries = json.loads(INTEL_FILE.read_text())

    entries.append({
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "summary": summary,
    })
    entries = entries[-MAX_ENTRIES_KEPT:]  # keep only the most recent N

    INTEL_FILE.write_text(json.dumps(entries, indent=2))
    print(f"Saved research finding. Log now has {len(entries)} entries.")


def get_latest_intel(n: int = 3) -> list:
    """Used by ledger_discord_bot.py to pull recent research as context."""
    if not INTEL_FILE.exists():
        return []
    entries = json.loads(INTEL_FILE.read_text())
    return entries[-n:]


if __name__ == "__main__":
    while True:
        print("Ledger is researching the market...")
        try:
            summary = run_research()
            print(f"\n{summary}\n")
            save_finding(summary)
        except Exception as e:
            print(f"[ERROR] research run failed: {e}")
        print(f"Sleeping {RESEARCH_INTERVAL_SECONDS // 3600}h until next research pass...")
        time.sleep(RESEARCH_INTERVAL_SECONDS)
