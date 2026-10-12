"""
Main Interactive Launcher for 3-Leg Options Strategy.
Run this script to start the 3-Leg Strategy for BANKNIFTY.
"""

import sys
import io
from pathlib import Path

# Ensure UTF-8 output and instant line-buffering on Windows console
if sys.platform == "win32":
    try:
        if hasattr(sys.stdout, "reconfigure"):
            sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
        else:
            sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace", line_buffering=True)
    except Exception:
        pass


# Ensure ROOT and .venv are on sys.path
ROOT = Path(__file__).resolve().parent.parent.parent
SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) in sys.path:
    sys.path.remove(str(SCRIPT_DIR))
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

VENV_SITE_PACKAGES = ROOT / ".venv" / "Lib" / "site-packages"
if VENV_SITE_PACKAGES.exists() and str(VENV_SITE_PACKAGES) not in sys.path:
    sys.path.insert(0, str(VENV_SITE_PACKAGES))


import argparse
from live.threelegs.config import INSTRUMENT_CONFIG, ENTRY_TIME, EXIT_TIME, MONITORING_TIMES
from live.threelegs.executor import login_smart_api, get_ltp
from live.threelegs.strategy import run_3leg_strategy
from live.threelegs.logger import logger


def main():
    parser = argparse.ArgumentParser(
        description="3-Leg Options Strategy (Entry: 9:30 AM, Exit: 3:10 PM)"
    )
    parser.add_argument(
        "--side",
        "-s",
        choices=["CE", "PE", "AUTO"],
        default="AUTO",
        help="Strategy side: CE (Bullish), PE (Bearish), or AUTO (default: AUTO)"
    )
    parser.add_argument(
        "--lots",
        "-l",
        type=int,
        default=5,
        help="Number of lots to trade (default: 5)"
    )
    parser.add_argument(
        "--paper",
        action="store_true",
        default=False,
        help="Run in paper trading mode (no real orders placed)"
    )
    parser.add_argument(
        "--instrument",
        "-i",
        choices=["BANKNIFTY"],
        default="BANKNIFTY",
        help="Instrument to trade (default: BANKNIFTY)"
    )
    args = parser.parse_args()

    instrument_name = args.instrument
    inst_config = INSTRUMENT_CONFIG[instrument_name]

    print("\n" + "=" * 65)
    print(f"       3-LEG OPTIONS STRATEGY - {instrument_name}")
    print("=" * 65)
    print(f"  Entry Time   : {ENTRY_TIME}")
    print(f"  Exit Time    : {EXIT_TIME}")
    print(f"  Monitor Times: {', '.join(MONITORING_TIMES)}")
    print(f"  Side         : {args.side}")
    print(f"  Lots         : {args.lots}")
    print(f"  Paper Trade  : {args.paper}")
    print("=" * 65 + "\n")

    logger.info(f"Starting 3-Leg Strategy - Instrument: {instrument_name}, Side: {args.side}, Lots: {args.lots}, Paper Trading: {args.paper}")

    # Login to SmartAPI
    print("[*] Authenticating with Angel One SmartAPI...")
    logger.info("Authenticating with Angel One SmartAPI")
    smart_api = login_smart_api()

    # Brief pause to avoid rate limits
    import time as _time
    _time.sleep(2)

    # Get current spot price
    current_spot = get_ltp(
        smart_api,
        inst_config["spot_token"],
        inst_config["spot_symbol"],
        inst_config["spot_exchange"]
    )
    print(f"[+] Current {inst_config['display_name']} Spot: {current_spot:.2f}")
    logger.info(f"Current {inst_config['display_name']} Spot: {current_spot:.2f}")

    # Print risk parameters
    print(f"\n[*] Risk Parameters:")
    print(f"    Loss Threshold: {inst_config['loss_threshold']}")
    print(f"    Spread Adjustment: {inst_config['spread_adjustment']}")
    print(f"    Stop Loss Threshold: {inst_config['stop_loss_threshold']}")
    print(f"    Hedge Multiplier: {inst_config['hedge_multiplier']}")
    logger.info(f"Risk Parameters - Loss Threshold: {inst_config['loss_threshold']}, Spread Adjustment: {inst_config['spread_adjustment']}, Stop Loss: {inst_config['stop_loss_threshold']}, Hedge Multiplier: {inst_config['hedge_multiplier']}")

    print("\n[*] Starting strategy execution...")
    logger.info("Starting strategy execution")

    try:
        result = run_3leg_strategy(
            smart_api=smart_api,
            side=args.side,
            lots=args.lots,
            paper_trade=args.paper,
            instrument_name=instrument_name
        )

        # Print results
        print("\n" + "=" * 65)
        print("           STRATEGY COMPLETED")
        print("=" * 65)
        print(f"  Status       : {result['status']}")
        print(f"  Side         : {result['side']}")
        print(f"  Entry Time   : {result['entry_time']}")
        print(f"  Exit Time    : {result['exit_time']}")
        print(f"  Exit Reason  : {result['exit_reason']}")
        print(f"  Hedge Bought : {result['hedge_bought']}")
        print(f"  Final P&L    : Rs.{result['final_pnl']:.2f}")
        print(f"  Max P&L      : Rs.{result['max_pnl']:.2f}")
        print(f"  Min P&L      : Rs.{result['min_pnl']:.2f}")
        print("=" * 65 + "\n")

        logger.info(
            f"Strategy Completed - Status: {result['status']}, Side: {result['side']}, "
            f"Entry: {result['entry_time']}, Exit: {result['exit_time']}, "
            f"Reason: {result['exit_reason']}, Hedge: {result['hedge_bought']}, "
            f"Final P&L: Rs.{result['final_pnl']:.2f}"
        )

    except KeyboardInterrupt:
        print("\n[!] Strategy stopped by user.")
        logger.warning("Strategy stopped by user")
    except Exception as e:
        print(f"\n[!] Strategy error: {e}")
        logger.error(f"Strategy error: {e}")
        raise


if __name__ == "__main__":
    main()
