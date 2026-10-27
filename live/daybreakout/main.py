"""
Main Interactive Launcher for Day Breakout Strategy.
Run this script to start the Day Breakout Strategy for NIFTY or SENSEX.
"""

import sys
import io
from pathlib import Path
from datetime import datetime

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
ROOT = Path(__file__).resolve().parent.parent
SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) in sys.path:
    sys.path.remove(str(SCRIPT_DIR))
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

VENV_SITE_PACKAGES = ROOT / ".venv" / "Lib" / "site-packages"
if VENV_SITE_PACKAGES.exists() and str(VENV_SITE_PACKAGES) not in sys.path:
    sys.path.insert(0, str(VENV_SITE_PACKAGES))


import argparse
from live.daybreakout.config import INSTRUMENTS, RISK_CONFIG, BREAKOUT_CONFIG
from live.daybreakout.range_calculator import get_previous_day_range
from live.daybreakout.executor import login_smart_api, get_daily_trade_stats, get_ltp
from live.daybreakout.strategy import run_daybreakout_strategy
from typing import Optional, Tuple
from live.daybreakout.gap_analyzer import get_opening_gap, print_gap_summary
from live.daybreakout.logger import logger


def str2bool(v):
    if isinstance(v, bool):
        return v
    if str(v).strip().lower() in ("yes", "true", "t", "y", "1"):
        return True
    elif str(v).strip().lower() in ("no", "false", "f", "n", "0"):
        return False
    else:
        raise argparse.ArgumentTypeError("Boolean value expected (true/false).")


def get_nearest_expiry_instrument(for_date: Optional[str] = None) -> str:
    """
    Determines whether NIFTY or SENSEX has the nearest expiry for the given date.
    Checks OpenAPIScripMaster contracts first, falling back to weekly expiry weekday logic.
    """
    target_dt = datetime.strptime(for_date, "%Y-%m-%d") if for_date else datetime.now()
    target_date = target_dt.date()

    try:
        from live.daybreakout.contract_finder import get_nearest_expiry
        nifty_exp_str, nifty_exp_date = get_nearest_expiry("NIFTY", "NFO", for_date=target_dt)
        sensex_exp_str, sensex_exp_date = get_nearest_expiry("SENSEX", "BFO", for_date=target_dt)

        nifty_days = (nifty_exp_date - target_date).days if hasattr(nifty_exp_date, 'days') else (nifty_exp_date - target_date).days
        sensex_days = (sensex_exp_date - target_date).days if hasattr(sensex_exp_date, 'days') else (sensex_exp_date - target_date).days

        print(f"[*] Nearest Expiry Evaluation:")
        print(f"    - NIFTY  expiry: {nifty_exp_str} ({nifty_days} days away)")
        print(f"    - SENSEX expiry: {sensex_exp_str} ({sensex_days} days away)")
        logger.info(f"Combination check: NIFTY {nifty_exp_str} ({nifty_days}d) vs SENSEX {sensex_exp_str} ({sensex_days}d)")

        if sensex_days < nifty_days:
            print(f"[+] SENSEX has nearest expiry ({sensex_days}d vs {nifty_days}d).")
            return "SENSEX"
        else:
            print(f"[+] NIFTY has nearest expiry ({nifty_days}d vs {sensex_days}d).")
            return "NIFTY"
    except Exception as exc:
        logger.warning(f"Error checking nearest expiry via scrip master: {exc}. Using weekday calculation fallback.")

    # Weekday calculation fallback:
    # NIFTY = Tuesday (weekday 1), SENSEX = Thursday (weekday 3)
    curr_weekday = target_date.weekday()
    nifty_days = (1 - curr_weekday) if curr_weekday <= 1 else (7 - curr_weekday + 1)
    sensex_days = (3 - curr_weekday) if curr_weekday <= 3 else (7 - curr_weekday + 3)

    print(f"[*] Nearest Expiry Fallback (Weekday):")
    print(f"    - NIFTY  Tuesday in {nifty_days} days")
    print(f"    - SENSEX Thursday in {sensex_days} days")

    if sensex_days < nifty_days:
        return "SENSEX"
    return "NIFTY"


def prompt_instrument_selection() -> Tuple[str, bool]:
    """Prompts user to select NIFTY, SENSEX, or COMBINATION."""
    print("\n" + "=" * 55)
    print("       DAY BREAKOUT STRATEGY - INSTRUMENT SELECTION")
    print("=" * 55)
    print("1. NIFTY       (Spread width: 300 pts | Step: 50 | Lot size: fetched from current contract)")
    print("2. SENSEX      (Spread width: 1,000 pts | Step: 100 | Lot size: fetched from current contract)")
    print("3. COMBINATION (Auto-pick Nearest Expiry Instrument)")
    print("=" * 55)

    try:
        choice = input("Select Instrument [1 for NIFTY, 2 for SENSEX, 3 for COMBINATION] (default: 1): ").strip().upper()
    except (EOFError, KeyboardInterrupt):
        choice = "1"

    if choice in ("3", "COMBINATION", "COMB"):
        selected = "COMBINATION"
        is_combination = True
    elif choice in ("2", "SENSEX"):
        selected = "SENSEX"
        is_combination = False
    else:
        selected = "NIFTY"
        is_combination = False

    print(f"\n[+] Selected Selection: {selected}")
    logger.info(f"Selected Selection: {selected}")
    return selected, is_combination


def prompt_structure_selection() -> str:
    """Prompts user to select Option Structure (SPREAD or NAKED)."""
    print("\n" + "=" * 55)
    print("       OPTION STRUCTURE SELECTION")
    print("=" * 55)
    print("1. SPREAD  (Bull Call / Bear Put Spread - 2 legs: Hedged Risk)")
    print("2. NAKED   (Naked Call / Naked Put - 1 leg: Single Option Buy)")
    print("=" * 55)

    try:
        choice = input("Select Structure [1 for SPREAD, 2 for NAKED] (default: 1): ").strip().upper()
    except (EOFError, KeyboardInterrupt):
        choice = "1"

    if choice in ("2", "NAKED", "NAKED_CALL", "CALL"):
        selected = "naked"
    else:
        selected = "spread"

    print(f"\n[+] Selected Structure: {selected.upper()}")
    logger.info(f"Selected Structure: {selected.upper()}")
    return selected


def main():
    parser = argparse.ArgumentParser(description="Day Breakout Strategy (12:30 - 15:15 Previous Day Range)")
    parser.add_argument("--instrument", "-i", choices=["NIFTY", "SENSEX"], default=None, help="Instrument to trade (NIFTY or SENSEX)")
    parser.add_argument("--combination", type=str2bool, nargs="?", const=True, default=None, help="Combination mode (true/false): find nearest expiry instrument and trade accordingly (default: false)")
    parser.add_argument("--structure", "-s", choices=["spread", "naked"], default=None, help="Option structure (spread or naked call/put) (default: spread)")
    parser.add_argument("--lots", "-l", type=int, default=10, help="Number of lots to trade (default: 10)")
    parser.add_argument("--high", type=float, default=None, help="Manual override for Range High")
    parser.add_argument("--low", type=float, default=None, help="Manual override for Range Low")
    parser.add_argument("--date", type=str, default=None, help="Previous trading day override (YYYY-MM-DD)")
    parser.add_argument("--confirmation", "-c", type=int, default=BREAKOUT_CONFIG["confirmation_seconds"], help="Tick-mode confirmation time in seconds (default: 300). Ignored unless --tick-confirm.")
    parser.add_argument("--paper", action="store_true", default=False, help="Run in paper trading mode (no real orders placed)")
    parser.add_argument("--tick-confirm", action="store_true", help="Use 5-minute live LTP hold instead of 15-minute candle close (default is 15m close, matching the backtest)")
    # Reverse flag – default ON (matches backtest performance). Use --no-reverse to disable.
    reverse_group = parser.add_mutually_exclusive_group()
    reverse_group.add_argument("--reverse", dest="reverse", action="store_true", default=True, help="Reverse trade direction (fade breakouts) (default: ON)")
    reverse_group.add_argument("--no-reverse", dest="reverse", action="store_false", help="Do NOT reverse trade direction (follow breakouts)")
    args = parser.parse_args()

    # 1. Select Instrument & Structure
    combination_mode = args.combination if args.combination is not None else False
    instrument_name = args.instrument

    if args.combination is True:
        combination_mode = True
    elif args.combination is False:
        combination_mode = False
    elif not instrument_name:
        instrument_choice, prompted_comb = prompt_instrument_selection()
        if prompted_comb:
            combination_mode = True
        else:
            instrument_name = instrument_choice

    if combination_mode:
        print("\n" + "=" * 55)
        print("  COMBINATION MODE ACTIVE: AUTO-SELECTING NEAREST EXPIRY")
        print("=" * 55)
        instrument_name = get_nearest_expiry_instrument(for_date=args.date)
        print(f"[+] Active Instrument: {instrument_name}")
        logger.info(f"Combination mode resolved active instrument to: {instrument_name}")
    elif not instrument_name:
        instrument_name = "NIFTY"

    structure_choice = args.structure
    if not structure_choice:
        structure_choice = prompt_structure_selection()

    inst = INSTRUMENTS[instrument_name]

    # 2. Login to SmartAPI
    print("\n[*] Authenticating with Angel One SmartAPI...")
    logger.info(f"Authenticating with Angel One SmartAPI for {instrument_name}")
    smart_api = login_smart_api()

    # Brief pause — Angel One rate-limits the first API call if fired immediately after login
    import time as _time
    _time.sleep(3)

    # 3. Check Daily Risk Stats
    trades_count, daily_pnl = get_daily_trade_stats(symbol_base=instrument_name)
    print(f"[*] Daily Stats ({instrument_name}): {trades_count}/{RISK_CONFIG['max_daily_trades']} trades | Realized P&L: Rs.{daily_pnl:.2f}")
    logger.info(f"Daily Stats ({instrument_name}): {trades_count}/{RISK_CONFIG['max_daily_trades']} trades | Realized P&L: Rs.{daily_pnl:.2f}")

    # 4. Fetch Previous Day Range
    range_high = args.high
    range_low = args.low
    trading_day = args.date if args.date else datetime.now().strftime("%Y-%m-%d")

    if range_high is None or range_low is None:
        try:
            range_data = get_previous_day_range(
                smart_api=smart_api,
                spot_exchange=inst["spot_exchange"],
                spot_token=inst["spot_token"],
                trading_day=trading_day
            )
            range_high = range_data["high"] if range_high is None else range_high
            range_low = range_data["low"] if range_low is None else range_low
            # Don't overwrite trading_day - it should be today's date, not the range date
        except Exception as e:
            print(f"\n[!] Could not fetch previous day range automatically: {e}")
            print("    You can enter the levels manually or press Ctrl+C to exit.")
            logger.error(f"Could not fetch previous day range automatically: {e}")
            try:
                if range_high is None:
                    range_high = float(input(f"    Enter {instrument_name} Resistance (High): ").strip())
                if range_low is None:
                    range_low = float(input(f"    Enter {instrument_name} Support (Low): ").strip())
                if not trading_day:
                    trading_day = "MANUAL"
                logger.info(f"Manual range entry - High: {range_high}, Low: {range_low}")
            except (ValueError, KeyboardInterrupt, EOFError):
                print("[!] Invalid range inputs. Exiting.")
                logger.error("Invalid range inputs - Exiting")
                return

    # 5. Get current spot price
    current_spot = get_ltp(smart_api, inst["spot_token"], inst["spot_symbol"], inst["spot_exchange"])
    print(f"\n[+] Current {inst['display_name']} Spot: {current_spot:.2f}")
    print(f"    [^] High Resistance Level : {range_high:.2f} (Gap: {range_high - current_spot:+.2f} pts)")
    print(f"    [v] Low Support Level     : {range_low:.2f} (Gap: {current_spot - range_low:+.2f} pts)")
    logger.info(f"Current {inst['display_name']} Spot: {current_spot:.2f} | Range High: {range_high:.2f} | Range Low: {range_low:.2f}")

    # 6. Gap & Opening Analysis
    print("\n[*] Analysing overnight gap...")
    logger.info("Analysing overnight gap...")
    gap_info = get_opening_gap(
        smart_api=smart_api,
        spot_exchange=inst["spot_exchange"],
        spot_token=inst["spot_token"],
    )
    print_gap_summary(gap_info)
    logger.info(f"Gap Analysis: Direction={gap_info.direction}, Size={gap_info.gap_pct:.4f}, Level={gap_info.label}")

    # 7. Very Large gap — extra caution prompt
    if gap_info.be_very_selective:
        print("[!] VERY LARGE GAP DETECTED. The strategy will run with maximum caution.")
        print("    Consider skipping today's trade if no clear direction emerges.")
        logger.warning("VERY LARGE GAP DETECTED - Strategy will run with maximum caution")
        try:
            vl_confirm = input("    Proceed with Very Large gap day? [Y/n] (default: Y): ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            vl_confirm = "y"
        if vl_confirm in ("n", "no"):
            print("[!] Strategy launch cancelled — Very Large gap day skipped by user.")
            logger.warning("Strategy launch cancelled by user - Very Large gap day skipped")
            return

    # Auto‑start monitoring (user confirmation disabled)
    confirm = "y"
    # if confirm in ("n", "no"):
    #     print("[!] Strategy launch cancelled by user.")
    #     return

    # 9. Start Strategy (pass pre-fetched gap_info so it isn't fetched again)
    logger.info(f"Starting Day Breakout Strategy - Instrument: {instrument_name}, Structure: {structure_choice}, Lots: {args.lots}, Paper Trading: {args.paper}")
    run_daybreakout_strategy(
        instrument_name=instrument_name,
        range_high=range_high,
        range_low=range_low,
        trading_day=trading_day,
        lots=args.lots,
        confirmation_seconds=args.confirmation,
        smart_api=smart_api,
        paper_trade=args.paper,
        auto_execute=True,
        gap_info=gap_info,
        reverse=args.reverse,
        structure=structure_choice,
        confirm_mode="tick" if args.tick_confirm else "15m",
    )


if __name__ == "__main__":
    main()

