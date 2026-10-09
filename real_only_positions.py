"""
real_only_positions.py — lightweight position tracking for the Cupsey
exit ladder (entry price/time, TP-ladder progress) when
PAPER_TRADING_ENABLED=false in ledger_bot.py, so the sniper/priority-copy
decision points and check_sniper_positions can keep running unmodified
without depending on ledger_state.json (paper trading's own state file).

Deliberately NOT the same store as real_trading.py's real_positions.json
(raw on-chain token amount / cost basis, reconciled against the chain —
the actual real-money source of truth). This file is smaller and exists
only so the ladder has somewhere to keep "when did I enter, at what
price, which rungs have already fired" without touching paper state.

Position shape (per mint):
    {
        "token": str, "symbol": str, "entry_price": float,
        "opened_at": str (ISO), "opened_by": str (wallet address),
        "risk_level": str,  # must contain "Sniper" for check_sniper_positions to pick it up
        "original_cost_basis_usdc": float,  # for TP2's remaining-fraction ratio
        "entry_dev_holding_pct": float or None,
        "tp1_hit": bool, "tp2_hit": bool,
        "commented_at_checkpoint": bool,
        "dip_buys": int,
    }
"""
import json
import os
from pathlib import Path

REAL_ONLY_POSITIONS_FILE = Path(os.environ.get("DATA_DIR", ".")) / "real_only_positions.json"


def load_real_only_positions() -> dict:
    if not REAL_ONLY_POSITIONS_FILE.exists():
        return {}
    try:
        with REAL_ONLY_POSITIONS_FILE.open("r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _atomic_write_json(path: Path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, path)


def save_real_only_positions(positions: dict):
    try:
        _atomic_write_json(REAL_ONLY_POSITIONS_FILE, positions)
    except Exception as e:
        print(f"[WARN] real_only_positions.json write failed: {e}")


# Round-trip history for real-only positions — feeds the risk desk's wallet
# scoring, loss-streak cooldown and token re-entry cooldown, which had no
# real-money equivalent before. Shape per entry:
#   {"mint", "symbol", "wallet", "source", "pnl_usdc", "pnl_pct", "reason",
#    "opened_ts", "closed_ts"}
REAL_ONLY_CLOSED_FILE = Path(os.environ.get("DATA_DIR", ".")) / "real_only_closed.json"


def _load_json(path: Path, default):
    if not path.exists():
        return default
    try:
        with path.open("r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def load_closed_positions() -> list:
    return _load_json(REAL_ONLY_CLOSED_FILE, {}).get("closed", [])


def load_recent_entries() -> list:
    """Unix timestamps of recent real-only entries (hourly trade limit)."""
    return _load_json(REAL_ONLY_CLOSED_FILE, {}).get("entries", [])


def _update_closed_file(fn):
    data = _load_json(REAL_ONLY_CLOSED_FILE, {})
    data.setdefault("closed", [])
    data.setdefault("entries", [])
    fn(data)
    data["closed"] = data["closed"][-3000:]
    data["entries"] = data["entries"][-500:]
    try:
        _atomic_write_json(REAL_ONLY_CLOSED_FILE, data)
    except Exception as e:
        print(f"[WARN] real_only_closed.json write failed: {e}")


def append_closed_position(record: dict):
    _update_closed_file(lambda d: d["closed"].append(record))


def record_entry(ts: float):
    _update_closed_file(lambda d: d["entries"].append(ts))
