"""Runnable entry point for the uploaded Ledger paper-trading bot."""

from __future__ import annotations

import runpy
from pathlib import Path


_SOURCE = Path(__file__).parent / "attached_assets" / "ledger_bot_(4)_1786746336112.py"
_loaded = runpy.run_path(str(_SOURCE))
globals().update({key: value for key, value in _loaded.items() if not key.startswith("__")})


if __name__ == "__main__":
    main()