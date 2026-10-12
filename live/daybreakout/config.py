"""
Configuration for Day Breakout Strategy.
"""

from pathlib import Path

# Paths
ROOT_DIR = Path(__file__).resolve().parent.parent.parent
DATA_DIR = ROOT_DIR / "data"
DATA_DIR.mkdir(parents=True, exist_ok=True)
TRADES_FILE = DATA_DIR / "trades.csv"
TRIGGERED_TRADES_FILE = DATA_DIR / "triggered_trades.csv"
ORDERS_FILE = DATA_DIR / "orders.csv"

# Supported Instruments
INSTRUMENTS = {
    "NIFTY": {
        "name": "NIFTY",
        "display_name": "NIFTY 50",
        "spot_symbol": "Nifty 50",
        "spot_token": "99926000",
        "spot_exchange": "NSE",
        "option_exchange": "NFO",
        "strike_step": 50,
        "spread_width": 300,        # 300-point Bull Call / Bear Put Spread
        "default_lot_size": 65,
        "csv_prefix": "NIFTY",
    },
    "SENSEX": {
        "name": "SENSEX",
        "display_name": "BSE SENSEX",
        "spot_symbol": "SENSEX",
        "spot_token": "99919000",
        "spot_exchange": "BSE",
        "option_exchange": "BFO",
        "strike_step": 100,
        "spread_width": 1000,       # 1,000-point Bull Call / Bear Put Spread
        "default_lot_size": 25,
        "csv_prefix": "SENSEX",
    }
}

# Previous Day Range Window (same filter as the 15-minute backtest)
RANGE_CONFIG = {
    "start_time": "12:30",
    "end_time": "15:15",
    "interval": "FIFTEEN_MINUTE",
}

# Breakout Confirmation Settings
# Default live path matches the 15-minute backtest: act on completed candle closes.
BREAKOUT_CONFIG = {
    "confirm_mode": "15m",          # "15m" candle close (backtest) or "tick" 5-min LTP hold
    "confirmation_seconds": 300,    # Used only in tick mode
    "poll_interval_seconds": 1.0,   # Used only in tick mode
    "first_entry_hhmm": "09:30",    # Entry only 09:30 onward (skip earlier candles/ticks)
    "last_entry_hhmm": "14:45",     # No new entries after this candle start (matches backtest)
    "candle_close_buffer_seconds": 5,  # Wait after the 15m boundary before fetching the close
    "eod_exit_hhmm": "15:10",       # Normal EOD exit time
    "expiry_exit_hhmm": "14:45",    # EOD exit time on expiry day
}

# Risk Management Settings (Matching broken_call.py)
RISK_CONFIG = {
    "x_factor": 1,                   # Multiplier for position sizing and risk parameters (1x = base, 2x = double, etc.)
    "max_loss": -5000.0,
    "max_profit": 50000.0,
    "profit_threshold": 3000.0,
    "min_profit_step": 2000.0,
    "daily_max_loss": -10000.0,
    "daily_max_profit": 35000.0,    # Daily profit > Rs.25K -> no more trades of that day
    "sl_cooldown_minutes": 45,      # Wait at least 45-60 min after a stop-loss before another entry
    "max_daily_trades": 4,
    "check_interval": 900.0,  # 15 minutes interval
    # Consecutive loss circuit breaker
    "max_consecutive_losses": 3,    # Stop if 3 consecutive stop-losses occur
    "consecutive_loss_cooldown_minutes": 60,  # Wait time after consecutive losses
    # Maximum position holding time (to prevent stuck positions)
    "max_position_hold_minutes": 120,  # Force square-off after 2 hours
    # Fixed monetary loss stop loss (broker-side STOPLOSS_LIMIT order placed at trade entry)
    # Trigger price = entry_price - (fixed_loss_amount / quantity)
    # Example: entry=200, lots=10 (qty=200), fixed_loss=9000 → trigger = 200 - 45 = 155
    "enable_fixed_loss_sl": True,      # Broker-side fixed monetary loss SL
    "fixed_loss_amount": 9000.0,       # Fixed loss cap per trade in Rs.
    # Exit-type specific cooldowns (in minutes)
    "exit_cooldown_minutes": {
        "EXIT_MAX_LOSS": 0,              # Stop loss - longer cooldown to avoid revenge trading
        "EXIT_TRAILING_PROFIT": 0,       # Profit trailed off - market reversing, wait for confirmation
        "EXIT_MAX_PROFIT": 0,            # Hit big profit - wait to see if trend continues
        "EXIT_EOD": 9999,                 # End of day - no re-entry
        "EXIT_DAILY_MAX_LOSS": 9999,      # Daily max loss - stop for the day
        "MANUAL_EXIT": 5,                 # Manual exit - short cooldown to check status
        "EXIT_EOD_SQUARE_OFF": 9999,      # EOD square off - no re-entry
    },
    # Order execution settings
    "enable_order_retry": True,        # Enable retry logic for rejected orders
    "order_retry_max_attempts": 3,     # Maximum retry attempts for rejected orders
    "order_retry_initial_delay": 180.0,  # Initial retry delay in seconds (3 minutes)
    "enable_order_polling": True,      # Enable order status polling
    "order_poll_interval": 15.0,       # Poll interval in seconds (15-20s)
    "order_poll_max_time": 120.0,      # Maximum polling time in seconds (2 minutes)
    "enable_manual_exit_price_fetch": True,  # Enable historical price fetch for manual exits
}

# Gap & Opening Rules Configuration
# Thresholds are stored as fractions (e.g. 0.003 = 0.30%)
GAP_CONFIG = {
    # |gap%| < flat_threshold  → treated as flat open (no gap)
    "flat_threshold":   0.0005,   # ±0.05%

    # |gap%| < small_threshold → SMALL gap
    "small_threshold":  0.0030,   # < 0.30%

    # |gap%| < medium_threshold → MEDIUM gap (0.30 – 0.60%)
    "medium_threshold": 0.0060,   # < 0.60%

    # |gap%| < large_threshold → LARGE gap (0.60 – 1.00%)
    "large_threshold":  0.0100,   # < 1.00%

    # |gap%| >= large_threshold → VERY_LARGE gap (> 1.00%)

    # Extra confirmation wait for LARGE gaps: additional seconds on top of the
    # standard confirmation_seconds, applied when price moves in the same
    # direction as the gap (no-chase rule).
    "large_gap_extra_wait_seconds": 300,   # additional 5 minutes
}

# India VIX Configuration for Breakout Filter
VIX_CONFIG = {
    "exchange": "NSE",
    "token": "99926017",
    "symbol": "India VIX",
    "bearish_vix_threshold": 8.0,   # If instrument is negative (BEARISH breakout) and VIX > +8%, take no reverse trade
    "bullish_vix_threshold": -8.0,  # If instrument is positive (BULLISH breakout) and VIX <= -6%, take no reverse trade
    # VIX-based entry delay configuration
    "vix_increase_min": 8,        # Minimum VIX increase to trigger delay (+2% to +8%)
    "vix_decrease_max": -8,       # Maximum VIX decrease to trigger delay (-6% to -2%)
    "vix_delay_minutes": 15,        # Initial delay duration in minutes when VIX conditions are met
    "vix_max_stall_minutes": 30,    # Absolute maximum stall time (cap on total delay + extensions)
    "vix_candle_check_count": 3,    # Number of recent 15-min candles to check for low breakdown
}                       