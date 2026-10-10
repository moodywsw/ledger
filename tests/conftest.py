import os
import sys
import tempfile
from pathlib import Path

# Isolate every test run from real state files and never arm anything.
os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="ledger-test-")
os.environ["REAL_TRADING_ENABLED"] = "false"
os.environ.pop("ANTHROPIC_API_KEY", None)
os.environ.pop("DISCORD_WEBHOOK_URL", None)
os.environ.pop("ALCHEMY_RPC_URL", None)
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)
