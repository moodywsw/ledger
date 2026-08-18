"""
journal_store.py — Ledger's append-only public journal

A continuous, diary-style record of everything Ledger already says out
loud via speak() in ledger_bot.py (trade opens/closes, refusals,
pauses, live commentary) — this module doesn't decide WHAT to log or
generate any new text, it just captures what's already been produced
and appends it to journal.jsonl, one JSON object per line.

Why JSONL instead of a single JSON array: appending a line is a cheap,
atomic-enough operation (open, write, close) that never requires
reading or rewriting the whole file, unlike a JSON array which would
need a full read-modify-write on every entry. That matters here since
this gets called from ledger_bot.py's main loop potentially many times
per cycle.

Entry shape:
    {
        "timestamp": ISO8601 UTC,
        "kind": "read" | "did" | "refused" | "commentary",
        "token_ticker": str or null,
        "text": str,
        "meta": dict or null
    }
"""

import json
import os
from datetime import datetime, timezone
from pathlib import Path

# DATA_DIR points at a mounted persistent volume in production (Railway:
# DATA_DIR=/data) so this file survives deploys/restarts instead of
# living in the working directory, which gets wiped every time. Defaults
# to "." for local dev, where no volume is mounted.
JOURNAL_FILE = Path(os.environ.get("DATA_DIR", ".")) / "journal.jsonl"

VALID_KINDS = {"read", "did", "refused", "commentary"}


def log_journal(kind: str, text: str, token_ticker: str = None, meta: dict = None):
    """
    Appends one journal entry. Never raises — a journal write failure
    (disk full, permissions, whatever) must not take down the trading
    loop that's calling this from inside speak() or a decision point.
    """
    if kind not in VALID_KINDS:
        print(f"[WARN] log_journal: unknown kind '{kind}', logging anyway")

    entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "kind": kind,
        "token_ticker": token_ticker,
        "text": text,
        "meta": meta,
    }
    try:
        JOURNAL_FILE.parent.mkdir(parents=True, exist_ok=True)
        with JOURNAL_FILE.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")
    except Exception as e:
        print(f"[WARN] journal write failed: {e}")


def get_recent_journal(limit: int = 50) -> list:
    """
    Returns up to `limit` most recent journal entries, newest first.
    Reads the whole file — fine at this scale (a trading bot's journal
    isn't going to hit sizes where that matters any time soon); skips
    any line that fails to parse instead of letting one bad line take
    the whole read down.
    """
    if not JOURNAL_FILE.exists():
        return []

    entries = []
    with JOURNAL_FILE.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                continue

    return list(reversed(entries[-limit:]))


def get_token_history(token_ticker: str, limit: int = 5) -> list:
    """
    Returns up to `limit` most recent journal entries for one specific
    token ticker, newest first — everything Ledger has already said or
    done about this exact token in past encounters, not just the most
    recent overall activity. Same full-file read as get_recent_journal
    (fine at this scale), filtered to the ticker before truncating, so
    a busy day for other tokens can't crowd this token's own past
    entries out of the last `limit`.
    """
    if not token_ticker or not JOURNAL_FILE.exists():
        return []

    entries = []
    with JOURNAL_FILE.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            if entry.get("token_ticker") == token_ticker:
                entries.append(entry)

    return list(reversed(entries[-limit:]))
