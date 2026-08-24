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


def save_real_only_positions(positions: dict):
    try:
        REAL_ONLY_POSITIONS_FILE.parent.mkdir(parents=True, exist_ok=True)
        with REAL_ONLY_POSITIONS_FILE.open("w", encoding="utf-8") as f:
            json.dump(positions, f, indent=2)
    except Exception as e:
        print(f"[WARN] real_only_positions.json write failed: {e}")
