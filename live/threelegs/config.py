"""
Configuration for 3-Leg Options Strategy.
"""

import sys
from pathlib import Path

# Add parent directory to path for imports
ROOT = Path(__file__).resolve().parent.parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Paths
DATA_DIR = ROOT / "data"
DATA_DIR.mkdir(parents=True, exist_ok=True)
LOG_DIR = DATA_DIR / "threelegs-logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)

# Supported Instruments
INSTRUMENT_CONFIG = {
    "BANKNIFTY": {
        "name": "BANKNIFTY",
        "display_name": "BANKNIFTY",
        "spot_symbol": "BANKNIFTY",
        "spot_token": "99926009",
        "spot_exchange": "NSE",
        "option_exchange": "NFO",
        "spread_1": 1000,  # ATM ± 1000
        "spread_2": 2500,  # ATM ± 2500
        "loss_threshold": 2000,  # Loss threshold to trigger spread adjustment
        "spread_adjustment": 500,  # Points to move spread 2 when threshold hit
        "stop_loss_threshold": 10000,  # Hard stop-loss - exit immediately
        "hedge_multiplier": 2,  # Hedge contract quantity multiplier
    }
}

# Risk Management Configuration
RISK_CONFIG = {
    "max_loss": -10000.0,
    "stop_loss_threshold": -10000.0,
    "loss_threshold": -2000.0,
}

# Timing Configuration
ENTRY_TIME = "09:30"
EXIT_TIME = "15:10"
MONITORING_TIMES = ["10:00", "11:00", "12:00", "13:00", "14:00", "15:00", "15:10"]

# Trade logging
TRADES_FILE = DATA_DIR / "threelegs-trades.csv"
