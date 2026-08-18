"""
theses_store.py — Ledger's persistent per-token thesis store

Unlike journal_store.py (append-only), this is a single dict keyed by
ticker, rewritten in full on every update — theses.json holds ONE
current record per token, not a history of every change to it.

The point this exists to fix: PaperPosition.thesis (in ledger_bot.py)
only lives as long as the position is open — once a position fully
closes, its dict entry is popped from state.open_positions and the
reasoning behind the trade is gone for good, with only the numeric
pnl surviving in trade_log. upsert_thesis() is called when a position
opens (status="holding") and again when it fully closes
(status="closed") so the thesis text and its outcome stay queryable
after the fact.

Record shape:
    {
        "token_ticker": str,
        "token_mint": str,
        "status": "stalking" | "holding" | "closed" | "invalidated",
        "thesis_text": str,
        "entry_condition": str or null,
        "invalidation": str or null,
        "risk_score": int or null,
        "first_seen_at": ISO8601 UTC,
        "updated_at": ISO8601 UTC,
    }
"""

import json
from datetime import datetime, timezone
from pathlib import Path

THESES_FILE = Path("theses.json")

VALID_STATUSES = {"stalking", "holding", "closed", "invalidated"}


def _load_all() -> dict:
    if not THESES_FILE.exists():
        return {}
    try:
        return json.loads(THESES_FILE.read_text())
    except json.JSONDecodeError:
        return {}


def _save_all(theses: dict):
    THESES_FILE.write_text(json.dumps(theses, indent=2))


def upsert_thesis(
    ticker: str,
    token_mint: str = None,
    status: str = None,
    thesis_text: str = None,
    entry_condition: str = None,
    invalidation: str = None,
    risk_score: int = None,
):
    """
    Creates or updates the thesis record for `ticker`. Only fields
    passed as non-None overwrite the existing value — this lets a
    close-time call (which usually only has status="closed" to give)
    update just the status without clobbering the thesis_text, risk
    score, etc. set when the position was opened. Never raises — a
    thesis write failure must not affect the actual trade.
    """
    if not ticker:
        print("[WARN] upsert_thesis called with no ticker, skipping")
        return
    if status is not None and status not in VALID_STATUSES:
        print(f"[WARN] upsert_thesis: unknown status '{status}' for {ticker}, saving anyway")

    try:
        theses = _load_all()
        now = datetime.now(timezone.utc).isoformat()
        existing = theses.get(ticker, {})

        record = {
            "token_ticker": ticker,
            "token_mint": token_mint if token_mint is not None else existing.get("token_mint"),
            "status": status if status is not None else existing.get("status", "stalking"),
            "thesis_text": thesis_text if thesis_text is not None else existing.get("thesis_text", ""),
            "entry_condition": entry_condition if entry_condition is not None else existing.get("entry_condition"),
            "invalidation": invalidation if invalidation is not None else existing.get("invalidation"),
            "risk_score": risk_score if risk_score is not None else existing.get("risk_score"),
            "first_seen_at": existing.get("first_seen_at", now),
            "updated_at": now,
        }
        theses[ticker] = record
        _save_all(theses)
    except Exception as e:
        print(f"[WARN] thesis upsert failed for {ticker}: {e}")


def get_theses(statuses: set = None) -> list:
    """
    Returns thesis records as a list. If `statuses` is given, only
    records whose status is in that set are returned (e.g. the API's
    /api/theses endpoint wants only stalking/holding — active ones).
    """
    theses = _load_all()
    records = list(theses.values())
    if statuses:
        records = [r for r in records if r.get("status") in statuses]
    return records
