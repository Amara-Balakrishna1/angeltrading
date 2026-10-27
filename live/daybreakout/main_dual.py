"""
Dual-Mode Launcher for Day Breakout Strategy.
Runs both REVERSE and NO-REVERSE strategy instances in parallel with separate tracking.
"""

import sys
import io
import os
import signal
import time
from pathlib import Path
from datetime import datetime
import threading
import argparse

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


from live.daybreakout.config import INSTRUMENTS, RISK_CONFIG, BREAKOUT_CONFIG
from live.daybreakout.range_calculator import get_previous_day_range
from live.daybreakout.executor import (
    login_smart_api, get_daily_trade_stats, get_ltp, verify_margin_precaution,
    start_order_update_stream, stop_order_update_stream,
    recover_existing_positions, get_active_trade_count, apply_x_factor,
)
from live.daybreakout.gap_analyzer import get_opening_gap, print_gap_summary
from live.daybreakout.logger import logger
from live.daybreakout.tracking_dual import (
    get_daily_trade_stats_dual,
    get_daily_profit_stats_dual,
    print_dual_mode_summary,
)
from live.daybreakout.strategy_patched import (
    run_daybreakout_strategy_patched,
    set_mode,
)


_interrupt_count = 0
_interrupt_cleanup_started = False


def _handle_sigint(signum, frame):
    """Allow graceful Ctrl+C handling, then force-exit after three attempts."""
    global _interrupt_count, _interrupt_cleanup_started
    _interrupt_count += 1
    if _interrupt_count > 3:
        print("\n[!] Ctrl+C pressed more than 3 times; terminating process immediately.", flush=True)
        os._exit(130)
    # Only interrupts before cleanup should unwind the main loop. Later ones
    # are counted without interrupting the thread joins.
    if not _interrupt_cleanup_started:
        raise KeyboardInterrupt


def str2bool(v):
    if isinstance(v, bool):
        return v
    if str(v).strip().lower() in ("yes", "true", "t", "y", "1"):
        return True
    elif str(v).strip().lower() in ("no", "false", "f", "n", "0"):
        return False
    else:
        raise argparse.ArgumentTypeError("Boolean value expected (true/false).")


def get_nearest_expiry_instrument(for_date: str = None) -> str:
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


def run_strategy_instance(
    mode_name: str,
    reverse: bool,
    instrument_name: str,
    range_high: float,
    range_low: float,
    trading_day: str,
    lots: int,
    smart_api,
    paper_trade: bool,
    auto_execute: bool,
    gap_info,
    structure: str,
    confirm_mode: str,
    stop_event: threading.Event,
):
    """
    Runs a single strategy instance (reverse or no-reverse) with mode-specific tracking.
    """
    print(f"\n{'=' * 80}")
    print(f"  STARTING {mode_name} STRATEGY INSTANCE")
    print(f"{'=' * 80}")
    x_factor = RISK_CONFIG.get("x_factor", 1.0)
    effective = max(1, int(lots * x_factor))
    print(f"  Mode: {'REVERSE (Fade Breakouts)' if reverse else 'NO-REVERSE (Follow Breakouts)'}")
    print(f"  Instrument: {instrument_name}")
    print(f"  Structure: {structure.upper()}")
    print(f"  Lots: {lots} (raw) × {x_factor}x (x_factor) = {effective} effective lots")
    print(f"{'=' * 80}\n")
    logger.info(f"Starting {mode_name} strategy instance - Reverse: {reverse}, Instrument: {instrument_name}")

    # Set the tracking mode before running
    tracking_mode = "REVERSE" if reverse else "NO-REVERSE"
    set_mode(tracking_mode)

    try:
        result = run_daybreakout_strategy_patched(
            instrument_name=instrument_name,
            range_high=range_high,
            range_low=range_low,
            trading_day=trading_day,
            lots=lots,
            confirmation_seconds=BREAKOUT_CONFIG["confirmation_seconds"],
            smart_api=smart_api,
            paper_trade=paper_trade,
            auto_execute=auto_execute,
            gap_info=gap_info,
            reverse=reverse,
            structure=structure,
            confirm_mode=confirm_mode,
        )
        print(f"\n[+] {mode_name} Strategy completed with status: {result.get('status', 'UNKNOWN')}")
        logger.info(f"{mode_name} Strategy completed - Status: {result.get('status', 'UNKNOWN')}")

        # Print mode-specific summary
        from live.daybreakout.tracking_dual import get_daily_trade_stats_dual, get_daily_profit_stats_dual
        trades, loss = get_daily_trade_stats_dual(instrument_name, mode=tracking_mode)
        profit = get_daily_profit_stats_dual(instrument_name, mode=tracking_mode)
        print(f"\n[*] {mode_name} Summary:")
        print(f"    Trades: {trades}")
        print(f"    Net P&L: Rs. {profit:+.2f}")
        print(f"    Cumulative Loss: Rs. {loss:+.2f}")

        return result
    except Exception as e:
        print(f"\n[!] {mode_name} Strategy encountered error: {e}")
        logger.error(f"{mode_name} Strategy error: {e}")
        return {"status": "ERROR", "error": str(e)}
    finally:
        stop_event.set()


def main():
    global _interrupt_cleanup_started
    # Keep a cumulative count so repeated interrupts during cleanup cannot
    # leave the non-daemon strategy threads holding the process open.
    signal.signal(signal.SIGINT, _handle_sigint)

    parser = argparse.ArgumentParser(description="Dual-Mode Day Breakout Strategy (REVERSE + NO-REVERSE)")
    parser.add_argument("--instrument", "-i", choices=["NIFTY", "SENSEX"], default=None, help="Instrument to trade (NIFTY or SENSEX)")
    parser.add_argument("--combination", type=str2bool, nargs="?", const=True, default=None, help="Combination mode (true/false): find nearest expiry instrument and trade accordingly (default: false)")
    parser.add_argument("--structure", "-s", choices=["spread", "naked"], default=None, help="Option structure (spread or naked call/put) (default: spread)")
    parser.add_argument("--lots", "-l", type=int, default=10, help="Number of lots to trade (default: 10)")
    parser.add_argument("--high", type=float, default=None, help="Manual override for Range High")
    parser.add_argument("--low", type=float, default=None, help="Manual override for Range Low")
    parser.add_argument("--date", type=str, default=None, help="Previous trading day override (YYYY-MM-DD)")
    parser.add_argument("--paper", action="store_true", default=False, help="Run in paper trading mode (no real orders placed)")
    parser.add_argument("--tick-confirm", action="store_true", help="Use 5-minute live LTP hold instead of 15-minute candle close")
    parser.add_argument("--x-factor", "-x", type=float, default=None, help=f"Risk/position sizing multiplier (default: {RISK_CONFIG.get('x_factor', 1.0)}x from config). Overrides RISK_CONFIG['x_factor'] for this run.")
    args = parser.parse_args()

    # Apply x-factor override: if supplied via CLI, update RISK_CONFIG so both
    # REVERSE and NO-REVERSE threads pick it up through strategy.py automatically.
    if args.x_factor is not None:
        RISK_CONFIG["x_factor"] = args.x_factor
        print(f"[*] X-Factor overridden via CLI: {args.x_factor}x")
        logger.info(f"X-Factor overridden via CLI: {args.x_factor}x")
    active_x_factor = RISK_CONFIG.get("x_factor", 1.0)
    # Effective lots = raw lots scaled by x_factor (minimum 1)
    effective_lots = max(1, int(args.lots * active_x_factor))

    # Select Instrument & Structure
    combination_mode = args.combination if args.combination is not None else False
    instrument_name = args.instrument

    if args.combination is True:
        combination_mode = True
    elif args.combination is False:
        combination_mode = False
    elif not instrument_name:
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
            combination_mode = True
        elif choice in ("2", "SENSEX"):
            instrument_name = "SENSEX"
        else:
            instrument_name = "NIFTY"

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
            structure_choice = "naked"
        else:
            structure_choice = "spread"

    inst = INSTRUMENTS[instrument_name]

    # Login to SmartAPI
    print("\n[*] Authenticating with Angel One SmartAPI...")
    logger.info(f"Authenticating with Angel One SmartAPI for {instrument_name}")
    smart_api = login_smart_api()

    # Brief pause — Angel One rate-limits the first API call if fired immediately after login
    time.sleep(3)

    # Margin Precaution Check (for simultaneous dual execution).
    # Both threads run at the same time, so effective exposure is 2x the per-thread lots.
    # Lots are already scaled by x_factor, so use effective_lots for the margin check.
    if not args.paper:
        print(f"[*] Dual-mode margin check: {args.lots} lots × {active_x_factor}x (x_factor) = {effective_lots} lots/instance × 2 threads = {effective_lots * 2} lots total exposure")
        logger.info(f"Dual-mode margin check: {args.lots} lots x {active_x_factor}x = {effective_lots} lots/instance x 2 threads = {effective_lots * 2} lots total exposure")
        verify_margin_precaution(
            smart_api=smart_api,
            lots=effective_lots * 2,   # simultaneous REVERSE + NO-REVERSE exposure (x_factor already applied)
            structure=structure_choice,
            symbol_base=instrument_name,
        )

    # Check Daily Risk Stats — shown per mode so the display is meaningful in dual execution
    rev_trades, rev_loss = get_daily_trade_stats(symbol_base=instrument_name, mode="REVERSE")
    nr_trades,  nr_loss  = get_daily_trade_stats(symbol_base=instrument_name, mode="NO-REVERSE")
    print(f"[*] Daily Stats ({instrument_name}):")
    print(f"    REVERSE    : {rev_trades}/{RISK_CONFIG['max_daily_trades']} trades | Realized Loss: Rs.{rev_loss:.2f}")
    print(f"    NO-REVERSE : {nr_trades}/{RISK_CONFIG['max_daily_trades']} trades | Realized Loss: Rs.{nr_loss:.2f}")
    logger.info(f"Daily Stats ({instrument_name}) — REVERSE: {rev_trades} trades / Rs.{rev_loss:.2f} | NO-REVERSE: {nr_trades} trades / Rs.{nr_loss:.2f}")

    # Fetch Previous Day Range
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

    # Get current spot price
    current_spot = get_ltp(smart_api, inst["spot_token"], inst["spot_symbol"], inst["spot_exchange"])
    print(f"\n[+] Current {inst['display_name']} Spot: {current_spot:.2f}")
    print(f"    [^] High Resistance Level : {range_high:.2f} (Gap: {range_high - current_spot:+.2f} pts)")
    print(f"    [v] Low Support Level     : {range_low:.2f} (Gap: {current_spot - range_low:+.2f} pts)")
    logger.info(f"Current {inst['display_name']} Spot: {current_spot:.2f} | Range High: {range_high:.2f} | Range Low: {range_low:.2f}")

    # Gap & Opening Analysis
    print("\n[*] Analysing overnight gap...")
    logger.info("Analysing overnight gap...")
    gap_info = get_opening_gap(
        smart_api=smart_api,
        spot_exchange=inst["spot_exchange"],
        spot_token=inst["spot_token"],
    )
    print_gap_summary(gap_info)
    logger.info(f"Gap Analysis: Direction={gap_info.direction}, Size={gap_info.gap_pct:.4f}, Level={gap_info.label}")

    # Very Large gap — extra caution prompt
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

    # Print dual-mode header
    print("\n" + "=" * 80)
    print("  DUAL-MODE STRATEGY: REVERSE + NO-REVERSE")
    print("=" * 80)
    print("  Instance 1: REVERSE    (Fade Breakouts)")
    print("  Instance 2: NO-REVERSE (Follow Breakouts)")
    print("  Both instances will run in parallel with separate tracking")
    print(f"  X-Factor  : {active_x_factor}x  (risk & position sizing multiplier)")
    print("=" * 80 + "\n")
    logger.info(f"Starting Dual-Mode Strategy - REVERSE and NO-REVERSE instances | X-Factor: {active_x_factor}x")

    # One shared socket routes broker order updates for both strategy threads.
    # Failure is non-fatal: order status continues to reconcile through REST.
    if not args.paper:
        start_order_update_stream(smart_api)

    # Create stop events for both instances
    reverse_stop_event = threading.Event()
    no_reverse_stop_event = threading.Event()

    # Create threads for both instances
    reverse_thread = threading.Thread(
        target=run_strategy_instance,
        args=(
            "REVERSE",
            True,  # reverse=True
            instrument_name,
            range_high,
            range_low,
            trading_day,
            args.lots,
            smart_api,
            args.paper,
            not args.paper,  # auto_execute=True if not paper
            gap_info,
            structure_choice,
            "tick" if args.tick_confirm else "15m",
            reverse_stop_event,
        ),
        name="REVERSE_Instance",
        daemon=False,
    )

    no_reverse_thread = threading.Thread(
        target=run_strategy_instance,
        args=(
            "NO-REVERSE",
            False,  # reverse=False
            instrument_name,
            range_high,
            range_low,
            trading_day,
            args.lots,
            smart_api,
            args.paper,
            not args.paper,  # auto_execute=True if not paper
            gap_info,
            structure_choice,
            "tick" if args.tick_confirm else "15m",
            no_reverse_stop_event,
        ),
        name="NO-REVERSE_Instance",
        daemon=False,
    )

    # Start REVERSE first, then stagger NO-REVERSE to reduce simultaneous
    # SmartAPI candle requests from the two strategy instances.
    thread_start_gap_seconds = 30
    try:
        print("[*] Starting REVERSE instance...")
        reverse_thread.start()
        logger.info("REVERSE instance thread started")

        print(f"[*] Starting NO-REVERSE instance in {thread_start_gap_seconds} seconds...")
        logger.info(f"NO-REVERSE instance start delayed by {thread_start_gap_seconds} seconds")
        time.sleep(thread_start_gap_seconds)

        no_reverse_thread.start()
        logger.info("NO-REVERSE instance thread started")

        print("\n[*] Both instances are now running in parallel.")
        print("[*] Press Ctrl+C to stop both instances.\n")
        logger.info("Both instances running in parallel after staggered startup")

        # Poll both threads so Ctrl+C stops both instances immediately.
        while reverse_thread.is_alive() or no_reverse_thread.is_alive():
            reverse_thread.join(timeout=0.5)
            no_reverse_thread.join(timeout=0.5)
    except KeyboardInterrupt:
        _interrupt_cleanup_started = True
        print("\n[!] Interrupt received — signalling both instances to stop...")
        logger.warning("KeyboardInterrupt: stopping both REVERSE and NO-REVERSE instances")
        reverse_stop_event.set()
        no_reverse_stop_event.set()
        # Let any in-flight broker operation finish before reconciling positions.
        # Repeated Ctrl+C still reaches the handler's explicit emergency exit.
        while reverse_thread.is_alive() or no_reverse_thread.is_alive():
            if reverse_thread.is_alive():
                reverse_thread.join(timeout=5)
            if no_reverse_thread.is_alive():
                no_reverse_thread.join(timeout=5)
            if reverse_thread.is_alive() or no_reverse_thread.is_alive():
                print("[*] Waiting for in-flight strategy/broker calls to stop; no new signals will be evaluated.", flush=True)

        # Ctrl+C stops new signals, but keep the P&L worker alive until every
        # broker position is represented in monitoring and all monitored
        # positions are flat. The worker is a daemon and would otherwise die
        # when this launcher returns.
        print("[*] Checking broker positions; new entries are stopped.", flush=True)
        shutdown_risk = apply_x_factor(RISK_CONFIG.get("x_factor", 1.0))
        while True:
            recovered = recover_existing_positions(
                smart_api=smart_api,
                symbol_base=instrument_name,
                check_interval=shutdown_risk["check_interval"],
                max_loss=shutdown_risk["max_loss"],
                max_profit=shutdown_risk["max_profit"],
                profit_threshold=shutdown_risk["profit_threshold"],
                min_profit_step=shutdown_risk["min_profit_step"],
                daily_max_loss=shutdown_risk["daily_max_loss"],
            )
            if recovered >= 0:
                break
            print("[!] Broker position check failed; retrying in 30 seconds. Monitoring remains active.", flush=True)
            time.sleep(30)

        last_status_report = 0.0
        while get_active_trade_count() > 0:
            now = time.monotonic()
            if now - last_status_report >= 60:
                count = get_active_trade_count()
                print(f"[*] Ctrl+C stopped new entries. P&L monitoring continues for {count} open trade(s); waiting for exit/flat confirmation.", flush=True)
                last_status_report = now
            time.sleep(5)

    stop_order_update_stream()

    if not reverse_thread.is_alive():
        logger.info("REVERSE instance completed")
    if not no_reverse_thread.is_alive():
        logger.info("NO-REVERSE instance completed")

    print("\n" + "=" * 80)
    print("  DUAL-MODE STRATEGY: ALL INSTANCES COMPLETED")
    print("=" * 80 + "\n")
    logger.info("Dual-Mode Strategy: All instances completed")

    # Print dual-mode performance summary
    print_dual_mode_summary(instrument_name)


if __name__ == "__main__":
    main()
