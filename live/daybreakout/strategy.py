"""
Main Strategy Runner for Day Breakout Strategy.

Default confirmation matches the 15-minute backtest:
  a completed 15-minute candle close beyond High/Low (or recovery back inside
  the range on LARGE / VERY_LARGE gap days).

Optional --tick-confirm mode keeps the older 5-minute live LTP hold.
"""

import time
import threading
from datetime import datetime, timedelta
from typing import Dict, Any, Optional, Tuple
from SmartApi.smartConnect import SmartConnect

from live.daybreakout.config import INSTRUMENTS, BREAKOUT_CONFIG, RISK_CONFIG, GAP_CONFIG, VIX_CONFIG
from live.daybreakout.logger import logger

# Import at function level to avoid circular dependency
def get_stop_monitor_function():
    from live.daybreakout.executor import stop_position_monitor
    return stop_position_monitor
from live.daybreakout.range_calculator import (
    get_previous_day_range,
    fetch_15m_candles,
    is_15m_candle_complete,
    seconds_until_next_15m_close,
)
from live.daybreakout.executor import (
    execute_daybreakout_spread,
    get_ltp,
    login_smart_api,
    get_daily_trade_stats,
    get_daily_profit_stats,
    get_last_stop_loss_time,
    record_triggered_trade,
    square_off_all,
    has_active_trade,
    get_consecutive_losses,
    check_position_timeout,
    poll_order_status,
    retry_order_with_backoff,
    fetch_manual_exit_price,
    apply_x_factor,
)
from live.daybreakout.gap_analyzer import (
    GapInfo,
    get_opening_gap,
    print_gap_summary,
    wait_until_market_open,
    _get_prev_close,
)
from live.daybreakout.signals import (
    apply_reverse,
    evaluate_15m_close,
    extra_15m_candles_wait,
    update_gap_territory,
)
from live.daybreakout.delay_state import (
    save_delay_state,
    load_delay_state,
    clear_delay_state,
)


# ---------------------------------------------------------------------------
# EOD Watchdog — Thread 3
# Sleeps precisely until 3:10:00 PM and forces square_off_all() unconditionally.
# Runs independently of Thread 1 (signal monitor) and Thread 2 (P&L monitor),
# so positions are always closed even if both other threads are sleeping or dead.
# ---------------------------------------------------------------------------

def _start_eod_watchdog(
    smart_api,
    inst_name: str,
    eod_hhmm: str = "15:10",
    trading_day: Optional[str] = None,
    instrument_name: str = "NIFTY",
    spot_exchange: str = "NSE",
    spot_token: str = None,
    stop_event: Optional[threading.Event] = None,
) -> threading.Thread:
    """
    Starts a daemon thread that wakes exactly at `eod_hhmm` (default 15:10)
    and calls square_off_all() unconditionally.

    - On expiry day, uses expiry_exit_hhmm (14:45) instead of normal EOD time.
    - Safe to call even if no trade is active (square_off_all handles that).
    - If EOD time has already passed, the thread does nothing and returns.
    - Can be cleanly stopped early via stop_event.
    - Daemon=True: auto-killed if the main process exits cleanly.
    """
    def _watchdog():
        now = datetime.now()

        # Check if today is expiry day and adjust exit time
        logger.info(f"[EOD Watchdog] Checking expiry for {instrument_name} with trading_day={trading_day}")
        expiry = is_expiry_day(instrument_name, trading_day, smart_api, spot_exchange, spot_token)
        if expiry:
            exit_time = BREAKOUT_CONFIG.get("expiry_exit_hhmm", "14:45")
            logger.info(f"[EOD Watchdog] EXPIRY DAY detected for {instrument_name} - using early exit time {exit_time}")
            print(f"[EOD Watchdog] ⚠️ EXPIRY DAY ({instrument_name}) - Early exit at {exit_time}")
        else:
            exit_time = eod_hhmm
        
        h, m = map(int, exit_time.split(":"))
        target = now.replace(hour=h, minute=m, second=0, microsecond=0)

        if now >= target:
            logger.info(f"[EOD Watchdog] Already past {exit_time} — watchdog will not run.")
            return

        sleep_secs = (target - now).total_seconds()
        logger.info(
            f"[EOD Watchdog] Started. Will force-exit all {inst_name} positions at "
            f"{exit_time} (in {sleep_secs:.0f}s)."
        )
        print(
            f"[EOD Watchdog] Armed — will force square_off_all('{inst_name}') "
            f"at exactly {exit_time} (in {sleep_secs:.0f}s)"
        )

        if stop_event is not None:
            stopped = stop_event.wait(timeout=sleep_secs)
            if stopped:
                logger.info("[EOD Watchdog] Stop event received. Watchdog exiting cleanly.")
                return
        else:
            time.sleep(sleep_secs)

        logger.info(f"[EOD Watchdog] {exit_time} reached — forcing square_off_all('{inst_name}').")
        print(f"\n[EOD Watchdog] ⏰ {exit_time} — forcing exit of ALL {inst_name} positions.")
        try:
            square_off_all(smart_api, inst_name, is_eod=True)
        except Exception as exc:
            logger.error(f"[EOD Watchdog] Error during square_off_all: {exc}")
            print(f"[EOD Watchdog] ❌ Error during square_off_all: {exc}")

    t = threading.Thread(
        target=_watchdog,
        daemon=True,
        name=f"EOD_Watchdog_{inst_name}",
    )
    t.start()
    return t


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def get_instrument_expiry_date(
    instrument_name: str,
    trading_day: Optional[str] = None,
    smart_api: Optional[SmartConnect] = None,
    spot_exchange: str = "NSE",
    spot_token: str = None,
) -> Optional[str]:
    """
    Get the expiry date for the given instrument (NIFTY or SENSEX).
    For Indian markets (matching backtest logic):
    - NIFTY/BANKNIFTY: Weekly options expire on Tuesday
    - SENSEX: Weekly options expire on Thursday
    - Uses weekly expiry for intraday trading
    - Handles holidays by checking if the expiry day has trading data
    
    Returns expiry date in YYYY-MM-DD format or None if not found.
    """
    if trading_day:
        check_date = datetime.strptime(trading_day, "%Y-%m-%d").date()
    else:
        check_date = datetime.now().date()
    
    # Determine expiry day based on instrument (matching backtest logic)
    # Monday=0, Tuesday=1, Wednesday=2, Thursday=3, Friday=4, Saturday=5, Sunday=6
    instr_upper = instrument_name.upper()
    if instr_upper in ("NIFTY", "BANKNIFTY"):
        expiry_weekday = 1  # Tuesday
    elif instr_upper == "SENSEX":
        expiry_weekday = 3  # Thursday
    else:
        expiry_weekday = 3  # Default to Thursday
    
    current_weekday = check_date.weekday()
    
    if current_weekday < expiry_weekday:
        days_to_expiry = expiry_weekday - current_weekday
    elif current_weekday == expiry_weekday:
        days_to_expiry = 0  # Same day expiry
    else:
        days_to_expiry = 7 - current_weekday + expiry_weekday  # Next expiry day
    
    # Calculate the expiry date
    if days_to_expiry == 0:
        expiry_date = check_date
    else:
        expiry_date = check_date + timedelta(days=days_to_expiry)
    
    # If today is the expiry day, check if it's a trading day (has candles)
    if days_to_expiry == 0:
        # If smart_api is provided, check if this day has trading data
        if smart_api and spot_token:
            try:
                from live.daybreakout.range_calculator import fetch_15m_candles
                expiry_date_str = expiry_date.strftime("%Y-%m-%d")
                candles = fetch_15m_candles(
                    smart_api,
                    spot_exchange,
                    spot_token,
                    from_str=f"{expiry_date_str} 09:15",
                    to_str=f"{expiry_date_str} 15:30",
                )
                if not candles:
                    # Expiry day is a holiday, find previous trading day
                    logger.info(f"Expiry day {expiry_date_str} is a holiday. Finding previous trading day...")
                    # Step back to find the previous trading day
                    check_back = expiry_date - timedelta(days=1)
                    while check_back.weekday() >= 5:  # Skip Saturday/Sunday
                        check_back -= timedelta(days=1)
                    
                    # Check if that day has candles
                    check_back_str = check_back.strftime("%Y-%m-%d")
                    candles = fetch_15m_candles(
                        smart_api,
                        spot_exchange,
                        spot_token,
                        from_str=f"{check_back_str} 09:15",
                        to_str=f"{check_back_str} 15:30",
                    )
                    if candles:
                        logger.info(f"Previous trading day {check_back_str} has candles. Using as expiry day.")
                        return check_back_str
                    else:
                        # Keep stepping back
                        for _ in range(5):
                            check_back -= timedelta(days=1)
                            while check_back.weekday() >= 5:
                                check_back -= timedelta(days=1)
                            check_back_str = check_back.strftime("%Y-%m-%d")
                            candles = fetch_15m_candles(
                                smart_api,
                                spot_exchange,
                                spot_token,
                                from_str=f"{check_back_str} 09:15",
                                to_str=f"{check_back_str} 15:30",
                            )
                            if candles:
                                logger.info(f"Found trading day {check_back_str} for expiry.")
                                return check_back_str
            except Exception as e:
                logger.warning(f"Could not check holiday status for expiry day: {e}. Using calculated expiry date.")
        
        return expiry_date.strftime("%Y-%m-%d")
    
    return None


def is_expiry_day(
    instrument_name: str,
    trading_day: Optional[str] = None,
    smart_api: Optional[SmartConnect] = None,
    spot_exchange: str = "NSE",
    spot_token: str = None,
) -> bool:
    """
    Check if the given trading day is an expiry day for the instrument.
    Returns True if today matches the instrument's expiry date.
    Handles holidays by checking if the expiry day has trading data.
    """
    expiry_date = get_instrument_expiry_date(
        instrument_name,
        trading_day,
        smart_api,
        spot_exchange,
        spot_token,
    )
    if not expiry_date:
        return False
    
    if trading_day:
        check_date = trading_day
    else:
        check_date = datetime.now().strftime("%Y-%m-%d")
    
    return check_date == expiry_date

def evaluate_vix_reversal_filter(
    smart_api,
    orig_direction: str,
    reverse: bool,
    prev_vix_close: Optional[float] = None,
) -> Tuple[str, bool, Optional[float], bool, str]:
    """
    Evaluates live India VIX percentage change against threshold rules:
    - If instrument is negative (BEARISH) and VIX > +8%: take no reverse trade (keep BEARISH)
    - If instrument is positive (BULLISH) and VIX <= -8%: take no reverse trade (keep BULLISH)

    Returns:
        (exec_direction, should_reverse, vix_pct, vix_filter_applied, vix_filter_reason)
    """
    should_reverse = reverse
    vix_filter_applied = False
    vix_filter_reason = ""
    vix_pct = None

    if smart_api is not None and prev_vix_close and prev_vix_close > 0:
        try:
            vix_ltp = get_ltp(smart_api, VIX_CONFIG["token"], VIX_CONFIG["symbol"], VIX_CONFIG["exchange"], timeout=5.0)
            if vix_ltp > 0:
                vix_pct = ((vix_ltp - prev_vix_close) / prev_vix_close) * 100.0
                bearish_thresh = VIX_CONFIG.get("bearish_vix_threshold", 8.0)
                bullish_thresh = VIX_CONFIG.get("bullish_vix_threshold", -8.0)

                if orig_direction == "BEARISH" and vix_pct > bearish_thresh:
                    if should_reverse:
                        should_reverse = False
                        vix_filter_applied = True
                        vix_filter_reason = f"VIX Spike (+{vix_pct:.2f}% > +{bearish_thresh:.1f}%): No Reverse (Kept BEARISH)"
                elif orig_direction == "BULLISH" and vix_pct <= bullish_thresh:
                    if should_reverse:
                        should_reverse = False
                        vix_filter_applied = True
                        vix_filter_reason = f"VIX Crush ({vix_pct:.2f}% <= {bullish_thresh:.1f}%): No Reverse (Kept BULLISH)"
        except Exception as e:
            logger.warning(f"Could not fetch live India VIX for filter: {e}")

    exec_direction = apply_reverse(orig_direction, should_reverse)
    return exec_direction, should_reverse, vix_pct, vix_filter_applied, vix_filter_reason


def should_apply_vix_entry_delay(
    smart_api,
    direction: str,
    prev_vix_close: Optional[float] = None,
) -> Tuple[bool, Optional[float], str]:
    """
    Evaluates if entry should be delayed based on India VIX percentage change:
    - If VIX increases by +2% to +8% and direction is BEARISH (breached Low): delay entry
    - If VIX decreases by -6% to -2% and direction is BULLISH (breached High): delay entry

    Returns:
        (should_delay, vix_pct, delay_reason)
    """
    should_delay = False
    vix_pct = None
    delay_reason = ""

    if smart_api is not None and prev_vix_close and prev_vix_close > 0:
        try:
            vix_ltp = get_ltp(smart_api, VIX_CONFIG["token"], VIX_CONFIG["symbol"], VIX_CONFIG["exchange"], timeout=5.0)
            if vix_ltp > 0:
                vix_pct = ((vix_ltp - prev_vix_close) / prev_vix_close) * 100.0
                vix_inc_min = VIX_CONFIG.get("vix_increase_min", 2.0)
                vix_inc_max = VIX_CONFIG.get("bearish_vix_threshold", 8.0)
                vix_dec_min = VIX_CONFIG.get("bullish_vix_threshold", -6.0)
                vix_dec_max = VIX_CONFIG.get("vix_decrease_max", -2.0)
                delay_minutes = VIX_CONFIG.get("vix_delay_minutes", 30)

                # Check for VIX increase (+2% to +8%) with BEARISH breakout
                if direction == "BEARISH" and vix_inc_min <= vix_pct <= vix_inc_max:
                    should_delay = True
                    delay_reason = f"VIX increased by +{vix_pct:.2f}% (threshold: +{vix_inc_min:.1f}% to +{vix_inc_max:.1f}%) with BEARISH breakout - wait {delay_minutes} minutes"

                # Check for VIX decrease (-6% to -2%) with BULLISH breakout
                elif direction == "BULLISH" and vix_dec_min <= vix_pct <= vix_dec_max:
                    should_delay = True
                    delay_reason = f"VIX decreased by {vix_pct:.2f}% (threshold: {vix_dec_min:.1f}% to {vix_dec_max:.1f}%) with BULLISH breakout - wait {delay_minutes} minutes"

        except Exception as e:
            logger.warning(f"Could not fetch live India VIX for entry delay check: {e}")

    return should_delay, vix_pct, delay_reason


def _is_same_as_gap_direction(breakout_direction: str, gap_info: GapInfo) -> bool:
    """Returns True when the breakout direction matches the overnight gap direction."""
    return (
        (breakout_direction == "BULLISH" and gap_info.direction == "UP") or
        (breakout_direction == "BEARISH" and gap_info.direction == "DOWN")
    )


def _build_and_execute(
    inst: Dict[str, Any],
    spot_ltp: float,
    direction: str,
    lots: int,
    smart_api,
    paper_trade: bool,
    auto_execute: bool,
    gap_info: GapInfo,
    trade_type: str = "BREAKOUT",
    structure: str = "spread",
    trade_reason: str = "",
) -> Dict[str, Any]:
    """Builds the option plan, prints it, and executes (or paper-trades)."""
    from live.daybreakout.contract_finder import find_strategy_contracts

    spread_plan = find_strategy_contracts(
        symbol_base=inst["name"],
        spot_price=spot_ltp,
        direction=direction,
        strike_step=inst["strike_step"],
        spread_width=inst["spread_width"],
        option_exchange=inst["option_exchange"],
        lots=lots,
        structure=structure,
    )

    opt_label = "CE" if direction == "BULLISH" else "PE"
    struct_label = "Naked " + opt_label if structure.lower() == "naked" else ("Bull Call Spread" if direction == "BULLISH" else "Bear Put Spread")
    trade_tag = f"[{trade_type}]" if trade_type != "BREAKOUT" else ""

    print(f"\n[+] Executing {struct_label} ({structure.upper()}) {trade_tag}:")
    print(f"    Reason  : {trade_reason}")
    print(f"    BUY Leg : {spread_plan['buy_leg']['symbol']} (Strike: {spread_plan['buy_leg']['strike']} {opt_label})")
    if spread_plan.get("sell_leg"):
        print(f"    SELL Leg: {spread_plan['sell_leg']['symbol']} (Strike: {spread_plan['sell_leg']['strike']} {opt_label})")
    print(f"    Quantity: {spread_plan['quantity']} ({lots} lots)")
    logger.info(f"Executing {struct_label} ({structure.upper()}) {trade_tag} - Reason: {trade_reason}, Direction: {direction}, Lots: {lots}")

    if paper_trade:
        print(f"\n[PAPER TRADING] {trade_tag} Confirmed ({structure.upper()}). Skipping live order placement.")
        logger.info(f"[PAPER TRADING] {trade_tag} Confirmed ({structure.upper()}) - Skipping live order placement")
        record_triggered_trade(
            symbol_base=inst["name"],
            structure=structure.upper(),
            lots=lots,
            quantity=spread_plan.get("quantity", 0),
            trade_reason=trade_reason,
            buy_symbol=spread_plan.get("buy_leg", {}).get("symbol", ""),
            sell_symbol=spread_plan.get("sell_leg", {}).get("symbol", "N/A") if spread_plan.get("sell_leg") else "N/A",
            mode="PAPER"
        )
        return {
            "status": "CONFIRMED_PAPER",
            "direction": direction,
            "trade_type": trade_type,
            "structure": structure.upper(),
            "trade_reason": trade_reason,
            "spread_plan": spread_plan,
            "gap_info": gap_info,
        }

    if auto_execute:
        # Apply x-factor from config
        x_factor = RISK_CONFIG.get("x_factor", 1.0)
        logger.info(f"Auto-executing trade via execute_daybreakout_spread - this will handle P&L internally (X-Factor: {x_factor}x)")
        result = execute_daybreakout_spread(spread_plan, smart_api, trade_reason=trade_reason, x_factor=x_factor)
        logger.info(f"execute_daybreakout_spread returned with status: {result.get('status', 'UNKNOWN')}")
        return result
    else:
        return {
            "status": "CONFIRMED",
            "direction": direction,
            "trade_type": trade_type,
            "structure": structure.upper(),
            "trade_reason": trade_reason,
            "spread_plan": spread_plan,
            "gap_info": gap_info,
        }


_STOP_AFTER_EXECUTE = {
    "DAILY_MAX_TRADES_EXCEEDED",
    "DAILY_MAX_LOSS_EXCEEDED",
    "DAILY_MAX_PROFIT_EXCEEDED",
    "BUY_ORDER_FAILED",
    "SELL_ORDER_FAILED",
    "CONSECUTIVE_LOSS_LIMIT_REACHED",
}


def _print_trade_result(result: Dict[str, Any], inst: Dict[str, Any]) -> None:
    """Prints a concise trade result / P&L summary after execution."""
    status    = result.get("status", "UNKNOWN")
    direction = result.get("direction", "")
    trade_type = result.get("trade_type", "")
    structure  = result.get("structure", "")
    reason    = result.get("trade_reason", "")
    pnl       = result.get("pnl")
    max_pnl   = result.get("max_pnl")
    exit_reason = result.get("exit_reason", "")
    plan      = result.get("spread_plan", {})

    print("\n" + "=" * 65)
    print(f"  TRADE RESULT — {inst.get('display_name', inst.get('name', ''))} | {structure} | {direction}")
    print("=" * 65)
    print(f"  Status        : {status}")
    print(f"  Type          : {trade_type}")
    print(f"  Reason        : {reason}")

    if plan:
        buy_leg  = plan.get("buy_leg",  {})
        sell_leg = plan.get("sell_leg")
        print(f"  BUY Leg       : {buy_leg.get('symbol', '-')}  (Strike: {buy_leg.get('strike', '-')})")
        if sell_leg:
            print(f"  SELL Leg      : {sell_leg.get('symbol', '-')}  (Strike: {sell_leg.get('strike', '-')})")
        print(f"  Qty           : {plan.get('quantity', '-')} ({plan.get('lots', '-')} lots)")

    if pnl is not None:
        pnl_sign  = "+" if pnl >= 0 else ""
        pnl_emoji = "🟢" if pnl >= 0 else "🔴"
        print(f"  P&L           : {pnl_emoji}  Rs.{pnl_sign}{pnl:.2f}")
        if max_pnl is not None:
            mp_sign = "+" if max_pnl >= 0 else ""
            print(f"  Peak P&L      : Rs.{mp_sign}{max_pnl:.2f}")
    if exit_reason:
        print(f"  Exit Reason   : {exit_reason}")

    print("=" * 65 + "\n")
    
    # Log trade result
    log_message = f"Trade Result - Status: {status}, Type: {trade_type}, Direction: {direction}, Structure: {structure}"
    if pnl is not None:
        log_message += f", P&L: Rs.{pnl:.2f}"
    if exit_reason:
        log_message += f", Exit Reason: {exit_reason}"
    logger.info(log_message)


def _run_15m_close_monitor(
    inst: Dict[str, Any],
    range_high: float,
    range_low: float,
    lots: int,
    smart_api,
    paper_trade: bool,
    auto_execute: bool,
    gap_info: GapInfo,
    reverse: bool,
    structure: str,
    prev_vix_close: Optional[float] = None,
    trading_day: Optional[str] = None,
    instrument_name: str = "NIFTY",
    spot_exchange: str = "NSE",
    spot_token: str = None,
    watchdog_stop_event: Optional[threading.Event] = None,
) -> Dict[str, Any]:
    """Wait for completed 15-minute closes and apply the same rules as the backtest."""
    spot_symbol = inst["spot_symbol"]
    # Use passed parameters if available, otherwise from inst
    if spot_token is None:
        spot_token = inst["spot_token"]
    if spot_exchange is None:
        spot_exchange = inst["spot_exchange"]
    extra_wait = extra_15m_candles_wait()
    last_entry = BREAKOUT_CONFIG["last_entry_hhmm"]

    processed = set()
    gap_territory_touched = False
    last_result: Dict[str, Any] = {"status": "NO_SIGNAL"}
    today = datetime.now().strftime("%Y-%m-%d")

    # VIX delay state management (non-blocking) - load from persistence
    vix_delay_active: Dict[str, Any] = load_delay_state()  # signal_key -> {start_time, direction, delay_minutes}
    if vix_delay_active:
        logger.info(f"Loaded {len(vix_delay_active)} persisted VIX delays from previous session")

    # Consecutive loss tracking (load from state)
    consecutive_losses = get_consecutive_losses(inst["name"])

    def _load_candles():
        # Wait until 9:15 AM if we're still pre-market (avoids AB1012 API error)
        wait_until_market_open(status_interval=60)
        now = datetime.now()
        from_str = f"{today} 09:15"
        to_str = now.strftime("%Y-%m-%d %H:%M")
        if to_str < from_str:
            to_str = from_str
        res = fetch_15m_candles(
            smart_api,
            spot_exchange,
            spot_token,
            from_str,
            to_str,
        )
        return res if res is not None else []

    # Prime: update gap territory from already-closed bars, do not enter on them.
    primed = 0
    for i, candle in enumerate(_load_candles()):
        if not candle or not is_15m_candle_complete(candle):
            continue
        key = str(candle[0])[:16]
        processed.add(key)
        primed += 1
        gap_territory_touched = update_gap_territory(
            float(candle[4]), range_high, range_low, gap_info, gap_territory_touched
        )
    print(f"[*] 15-min close monitor ready. Primed {primed} completed candle(s) (no late entries on them).")
    if gap_territory_touched:
        print("[*] Gap territory already touched on an earlier bar — recovery is armed.")
    print(f"[*] Next signals require a completed 15-min close. Last entry candle: {last_entry}.")
    print(f"[*] No-chase extra wait: {extra_wait} opening candle(s) when breakout is with the gap.\n")
    logger.info(f"15-min close monitor ready - Primed {primed} candles, Last entry: {last_entry}, Extra wait: {extra_wait}")

    while True:
        try:
            now_dt = datetime.now()
            candles = _load_candles()
            if not candles:
                time.sleep(2.0)
                continue
            traded_this_pass = False

            for i, candle in enumerate(candles):
                if not candle or not isinstance(candle, (list, tuple)) or len(candle) < 5:
                    continue
                key = str(candle[0])[:16]
                if key in processed:
                    continue
                if not is_15m_candle_complete(candle, now_dt):
                    continue

                processed.add(key)
                c_time = str(candle[0])[11:16]
                c_open  = float(candle[1])
                c_high  = float(candle[2])
                c_low   = float(candle[3])
                c_close = float(candle[4])
                c_vol   = candle[5] if len(candle) > 5 else "-"

                # ── Candle detail print ──────────────────────────────────────
                dist_high = range_high - c_close
                dist_low  = c_close - range_low
                breakout_tag = ""
                if c_close > range_high:
                    breakout_tag = f"  ⬆ ABOVE range by {-dist_high:.1f} pts"
                elif c_close < range_low:
                    breakout_tag = f"  ⬇ BELOW range by {-dist_low:.1f} pts"
                else:
                    breakout_tag = f"  (H-{dist_high:.1f} | L+{dist_low:.1f})"

                print(
                    f"\n[{datetime.now().strftime('%H:%M:%S')}] ── 15m Candle @ {c_time} ──────────────────────"
                )
                print(
                    f"   O:{c_open:.2f}  H:{c_high:.2f}  L:{c_low:.2f}  C:{c_close:.2f}  Vol:{c_vol}"
                )
                print(
                    f"   Range [{range_low:.2f} ... {range_high:.2f}]{breakout_tag}"
                )
                logger.info(f"15m Candle @ {c_time} - O:{c_open:.2f} H:{c_high:.2f} L:{c_low:.2f} C:{c_close:.2f} Range:[{range_low:.2f}-{range_high:.2f}]{breakout_tag}")
                # ────────────────────────────────────────────────────────────

                signal = evaluate_15m_close(
                    c_time=c_time,
                    c_close=c_close,
                    range_high=range_high,
                    range_low=range_low,
                    gap_info=gap_info,
                    gap_territory_touched=gap_territory_touched,
                    candle_index=i,
                    extra_candles_wait=extra_wait,
                    last_entry_hhmm=last_entry,
                    first_entry_hhmm=BREAKOUT_CONFIG.get("first_entry_hhmm", "09:30"),
                )
                gap_territory_touched = signal.gap_territory_touched

                if signal.skip:
                    if signal.skip_reason:
                        reason_text = "Price closed inside the range boundaries" if signal.skip_reason == "No 15m close signal" else signal.skip_reason
                        print(f"   [!] No trade triggered because: {reason_text}")
                        logger.info(f"Candle {c_time} skipped: {reason_text}")
                    continue

                # ── Single-Trade Gating Check with Timeout ────────────────────
                if has_active_trade(inst["name"]):
                    max_hold_minutes = RISK_CONFIG.get("max_position_hold_minutes", 120)
                    has_timeout, timeout_trade_id = check_position_timeout(inst["name"], max_hold_minutes)
                    
                    if has_timeout:
                        print(f"   [POSITION TIMEOUT] Trade {timeout_trade_id} has exceeded {max_hold_minutes} minutes. Forcing square-off.")
                        logger.warning(f"Position timeout: Trade {timeout_trade_id} exceeded {max_hold_minutes} minutes. Forcing square-off.")
                        try:
                            square_off_all(smart_api, inst["name"])
                            # Reset consecutive losses on forced exit (not a loss)
                            consecutive_losses = 0
                        except Exception as e:
                            logger.error(f"Error during forced square-off: {e}")
                            print(f"   [!] Error during forced square-off: {e}")
                        continue
                    else:
                        print(f"   [ACTIVE POSITION] An open trade is currently active for {inst['name']}. Waiting for position to exit before triggering new trades.")
                        logger.info(f"Candle {c_time}: Active trade open for {inst['name']}. Skipping new breakout signal evaluation.")
                        continue

                if gap_info.be_very_selective:
                    print("    [CAUTION] VERY LARGE GAP DAY — be extremely selective.")
                    logger.warning("VERY LARGE GAP DAY - Being extremely selective")

                daily_trades, daily_loss = get_daily_trade_stats(symbol_base=inst["name"])
                daily_profit = get_daily_profit_stats(symbol_base=inst["name"])
                if daily_trades >= RISK_CONFIG["max_daily_trades"]:
                    print(f"[!] Daily max trades reached ({daily_trades}). Stopping.")
                    logger.warning(f"Daily max trades reached ({daily_trades}) - Stopping")
                    return {"status": "DAILY_MAX_TRADES_EXCEEDED"}
                if daily_loss <= RISK_CONFIG["daily_max_loss"]:
                    print(f"[!] Daily max loss reached (Cumulative Loss: Rs.{daily_loss:.2f}). Stopping.")
                    logger.warning(f"Daily max loss reached (Cumulative Loss: Rs.{daily_loss:.2f}) - Stopping")
                    return {"status": "DAILY_MAX_LOSS_EXCEEDED"}
                if daily_profit >= RISK_CONFIG.get("daily_max_profit", 25000.0):
                    print(f"[!] Daily max profit reached (Net P&L: Rs.{daily_profit:.2f} >= Rs.{RISK_CONFIG.get('daily_max_profit', 25000.0):.2f}). Stopping for the day.")
                    logger.info(f"Daily max profit reached (Net P&L: Rs.{daily_profit:.2f}) - Stopping for the day")
                    return {"status": "DAILY_MAX_PROFIT_EXCEEDED"}

                # ── Stop-Loss Cooldown Check (wait 45-60 min after SL) ──────
                sl_cooldown = RISK_CONFIG.get("sl_cooldown_minutes", 45)
                last_sl = get_last_stop_loss_time(symbol_base=inst["name"])
                if last_sl:
                    elapsed_mins = (datetime.now() - last_sl).total_seconds() / 60.0
                    if elapsed_mins < sl_cooldown:
                        remaining_mins = sl_cooldown - elapsed_mins
                        print(f"   [SL COOLDOWN] Stop-loss occurred at {last_sl.strftime('%H:%M:%S')}. Waiting {remaining_mins:.0f}m more ({sl_cooldown}m cooldown).")
                        logger.info(f"Candle {c_time} skipped: SL cooldown active ({remaining_mins:.0f}m remaining)")
                        continue

                # ── Consecutive Loss Circuit Breaker ───────────────────────
                max_consecutive_losses = RISK_CONFIG.get("max_consecutive_losses", 3)
                if consecutive_losses >= max_consecutive_losses:
                    print(f"[!] CONSECUTIVE LOSS LIMIT REACHED ({consecutive_losses} losses). Stopping for the day.")
                    logger.warning(f"Consecutive loss limit reached ({consecutive_losses}). Stopping for the day.")
                    return {"status": "CONSECUTIVE_LOSS_LIMIT_REACHED"}

                # ── VIX Volatility Filter + Reversal ────────────────────────
                exec_direction, should_reverse, vix_pct, vix_filter_applied, vix_filter_reason = evaluate_vix_reversal_filter(
                    smart_api=smart_api,
                    orig_direction=signal.direction,
                    reverse=reverse,
                    prev_vix_close=prev_vix_close,
                )

                # ── VIX Entry Delay Check (Non-blocking) ─────────────────────
                should_delay, vix_delay_pct, delay_reason = should_apply_vix_entry_delay(
                    smart_api=smart_api,
                    direction=signal.direction,
                    prev_vix_close=prev_vix_close,
                )

                delay_minutes = VIX_CONFIG.get("vix_delay_minutes", 30)
                max_stall_minutes = VIX_CONFIG.get("vix_max_stall_minutes", 45)
                signal_key = f"{c_time}_{signal.direction}"

                # Check if this signal is already being delayed
                was_delayed = False
                forced_decision = False  # Track if we forced a decision due to max stall time
                exec_direction_override = None  # Track if we overrode the direction

                if signal_key in vix_delay_active:
                    delay_info = vix_delay_active[signal_key]
                    elapsed_mins = (datetime.now() - delay_info["start_time"]).total_seconds() / 60.0

                    # Check absolute maximum stall time (cap)
                    if elapsed_mins >= max_stall_minutes:
                        print(f"   [VIX DELAY] Maximum stall time reached ({max_stall_minutes}m). Forcing trade decision based on candle analysis.")
                        logger.warning(f"VIX Delay max stall time reached ({max_stall_minutes}m) - forcing trade decision")

                        # Check last 3 15-minute candles for low breakdown
                        try:
                            candle_check_count = VIX_CONFIG.get("vix_candle_check_count", 3)
                            candles = _load_candles()
                            recent_candles = candles[-candle_check_count:] if len(candles) >= candle_check_count else candles

                            if len(recent_candles) >= 2:
                                # Check based on original direction
                                if delay_info["direction"] == "BULLISH":
                                    # Original was BULLISH breakout (VIX -2% to -6%)
                                    # Check if any candle's low is broken by next candle's low
                                    low_broken = False
                                    for i in range(len(recent_candles) - 1):
                                        current_low = float(recent_candles[i][3])
                                        next_low = float(recent_candles[i + 1][3])
                                        if next_low < current_low:
                                            low_broken = True
                                            print(f"   [VIX DELAY] Candle {i} low ({current_low:.2f}) broken by next candle low ({next_low:.2f})")
                                            logger.info(f"VIX Delay: Low breakdown detected - switching to BEARISH")
                                            break

                                    # Force trade decision with direction override
                                    if low_broken:
                                        # Switch to BEARISH
                                        print(f"   [VIX DELAY] LOW BREAKOUT DETECTED - Taking BEARISH trade instead of {delay_info['direction']}")
                                        logger.warning(f"VIX Delay forced decision: Switching from {delay_info['direction']} to BEARISH due to low breakdown")
                                        exec_direction_override = "BEARISH"
                                    else:
                                        print(f"   [VIX DELAY] No low breakdown - Proceeding with original {delay_info['direction']} direction")
                                        logger.info(f"VIX Delay forced decision: No low breakdown - keeping {delay_info['direction']} direction")
                                        exec_direction_override = delay_info["direction"]
                                elif delay_info["direction"] == "BEARISH":
                                    # Original was BEARISH breakout (VIX +2% to +8%)
                                    # Check if any candle's high is broken by next candle's high
                                    high_broken = False
                                    for i in range(len(recent_candles) - 1):
                                        current_high = float(recent_candles[i][2])
                                        next_high = float(recent_candles[i + 1][2])
                                        if next_high > current_high:
                                            high_broken = True
                                            print(f"   [VIX DELAY] Candle {i} high ({current_high:.2f}) broken by next candle high ({next_high:.2f})")
                                            logger.info(f"VIX Delay: High breakdown detected - switching to BULLISH")
                                            break

                                    # Force trade decision with direction override
                                    if high_broken:
                                        # Switch to BULLISH
                                        print(f"   [VIX DELAY] HIGH BREAKOUT DETECTED - Taking BULLISH trade instead of {delay_info['direction']}")
                                        logger.warning(f"VIX Delay forced decision: Switching from {delay_info['direction']} to BULLISH due to high breakdown")
                                        exec_direction_override = "BULLISH"
                                    else:
                                        print(f"   [VIX DELAY] No high breakdown - Proceeding with original {delay_info['direction']} direction")
                                        logger.info(f"VIX Delay forced decision: No high breakdown - keeping {delay_info['direction']} direction")
                                        exec_direction_override = delay_info["direction"]
                                else:
                                    # Unknown direction - keep original
                                    print(f"   [VIX DELAY] Unknown direction {delay_info['direction']} - keeping original")
                                    exec_direction_override = delay_info["direction"]

                            # Remove delay and proceed regardless
                            del vix_delay_active[signal_key]
                            save_delay_state(vix_delay_active)
                            was_delayed = True
                            forced_decision = True
                            # Skip to trade execution with override direction
                        except Exception as candle_err:
                            logger.error(f"Error during candle analysis for forced decision: {candle_err}")
                            print(f"   [VIX DELAY] Error analyzing candles - proceeding with original direction")
                            del vix_delay_active[signal_key]
                            save_delay_state(vix_delay_active)
                            was_delayed = True
                            forced_decision = True
                            exec_direction_override = delay_info["direction"]
                        # Skip the rest of the delay logic if we made a forced decision
                        if forced_decision:
                            continue
                    else:
                        # Normal delay flow - check if initial delay period has elapsed
                        if elapsed_mins >= delay_info["delay_minutes"]:
                            # Delay period elapsed, verify breakout AND re-check VIX conditions
                            try:
                                current_ltp = get_ltp(smart_api, spot_token, spot_symbol, spot_exchange, timeout=5.0)
                                breakout_valid = False
                                vix_still_delayed = False

                                # Re-check VIX conditions to see if delay is still needed
                                if prev_vix_close and prev_vix_close > 0:
                                    try:
                                        vix_ltp = get_ltp(smart_api, VIX_CONFIG["token"], VIX_CONFIG["symbol"], VIX_CONFIG["exchange"], timeout=5.0)
                                        if vix_ltp > 0:
                                            vix_pct = ((vix_ltp - prev_vix_close) / prev_vix_close) * 100.0
                                            vix_inc_min = VIX_CONFIG.get("vix_increase_min", 2.0)
                                            vix_inc_max = VIX_CONFIG.get("bearish_vix_threshold", 8.0)
                                            vix_dec_min = VIX_CONFIG.get("bullish_vix_threshold", -6.0)
                                            vix_dec_max = VIX_CONFIG.get("vix_decrease_max", -2.0)

                                            # Check if VIX conditions still apply
                                            if delay_info["direction"] == "BEARISH" and vix_inc_min <= vix_pct <= vix_inc_max:
                                                vix_still_delayed = True
                                                print(f"   [VIX DELAY] VIX still elevated (+{vix_pct:.2f}%) - keeping delay active")
                                                logger.info(f"VIX Delay extended: VIX still at +{vix_pct:.2f}% (threshold: +{vix_inc_min:.1f}% to +{vix_inc_max:.1f}%)")
                                            elif delay_info["direction"] == "BULLISH" and vix_dec_min <= vix_pct <= vix_dec_max:
                                                vix_still_delayed = True
                                                print(f"   [VIX DELAY] VIX still suppressed ({vix_pct:.2f}%) - keeping delay active")
                                                logger.info(f"VIX Delay extended: VIX still at {vix_pct:.2f}% (threshold: {vix_dec_min:.1f}% to {vix_dec_max:.1f}%)")
                                            else:
                                                print(f"   [VIX DELAY] VIX normalized ({vix_pct:+.2f}%) - proceeding with verification")
                                                logger.info(f"VIX Delay complete: VIX normalized to {vix_pct:+.2f}%")
                                    except Exception as vix_err:
                                        logger.warning(f"Could not re-check VIX during delay verification: {vix_err}")
                                        # Proceed with price verification if VIX check fails

                                # Verify price breakout if VIX conditions are normalized
                                if not vix_still_delayed:
                                    if delay_info["direction"] == "BULLISH" and current_ltp > range_high:
                                        print(f"   [VIX DELAY] Breakout still valid - Price {current_ltp:.2f} > High {range_high:.2f}")
                                        logger.info(f"VIX Delay confirmed: BULLISH breakout still valid (Price {current_ltp:.2f} > High {range_high:.2f})")
                                        breakout_valid = True
                                    elif delay_info["direction"] == "BEARISH" and current_ltp < range_low:
                                        print(f"   [VIX DELAY] Breakout still valid - Price {current_ltp:.2f} < Low {range_low:.2f}")
                                        logger.info(f"VIX Delay confirmed: BEARISH breakout still valid (Price {current_ltp:.2f} < Low {range_low:.2f})")
                                        breakout_valid = True
                                    else:
                                        print(f"   [VIX DELAY] Breakout NO LONGER VALID - Price {current_ltp:.2f}. Skipping entry.")
                                        logger.warning(f"VIX Delay cancelled: Breakout no longer valid (Price {current_ltp:.2f}, Direction: {delay_info['direction']})")
                                        del vix_delay_active[signal_key]
                                        save_delay_state(vix_delay_active)
                                        continue

                                if breakout_valid:
                                    # Remove from delay map and proceed to execution
                                    del vix_delay_active[signal_key]
                                    # Save updated delay state
                                    save_delay_state(vix_delay_active)
                                    # Mark as delayed for trade reason
                                    was_delayed = True
                                else:
                                    # VIX still triggers delay - extend delay by another cycle
                                    delay_info["start_time"] = datetime.now()
                                    remaining_mins = delay_info["delay_minutes"]
                                    print(f"   [VIX DELAY] Delay extended by {remaining_mins:.0f}m more due to VIX conditions")
                                    continue
                            except Exception as e:
                                logger.error(f"Error during VIX delay verification: {e}")
                                print(f"   [VIX DELAY] Error verifying breakout: {e}. Skipping entry.")
                                del vix_delay_active[signal_key]
                                save_delay_state(vix_delay_active)
                                continue
                        else:
                            # Still waiting, skip this signal for now
                            remaining_mins = delay_info["delay_minutes"] - elapsed_mins
                            print(f"   [VIX DELAY] Waiting {remaining_mins:.0f}m more for entry confirmation...")
                            continue
                elif should_delay:
                    # Check if delay would push past last entry time using datetime objects
                    current_dt = datetime.now()
                    delay_end_dt = current_dt + timedelta(minutes=delay_minutes)

                    # Parse last_entry as datetime for proper comparison
                    last_entry_h, last_entry_m = map(int, last_entry.split(":"))
                    last_entry_dt = current_dt.replace(hour=last_entry_h, minute=last_entry_m, second=0, microsecond=0)

                    if delay_end_dt > last_entry_dt:
                        delay_end_str = delay_end_dt.strftime("%H:%M")
                        print(f"   [VIX DELAY] Delay would push entry past {last_entry} (delay ends at {delay_end_str}). Skipping delay, entering immediately.")
                        logger.warning(f"VIX Delay skipped: Would push entry past last entry time {last_entry}")
                        # Proceed without delay - skip the delay logic and continue to trade execution
                    else:
                        # Start delay tracking (non-blocking)
                        print(f"\n   [VIX DELAY] {delay_reason}")
                        print(f"   [VIX DELAY] Non-blocking delay started. Will verify in {delay_minutes} minutes.")
                        logger.info(f"VIX Entry Delay triggered (non-blocking): {delay_reason}. Will verify in {delay_minutes} minutes.")

                        vix_delay_active[signal_key] = {
                            "start_time": datetime.now(),
                            "direction": signal.direction,
                            "delay_minutes": delay_minutes,
                            "c_close": c_close,
                            "c_time": c_time,
                        }
                        # Save delay state to persistence
                        save_delay_state(vix_delay_active)
                        continue

                trade_type_label = 'Breakout' if signal.trade_type == 'BREAKOUT' else 'Recovery'
                if should_reverse:
                    trade_reason = (
                        f"Reverse {exec_direction} "
                        f"({trade_type_label}): {signal.spot_reason}"
                    )
                else:
                    trade_reason = (
                        f"{signal.direction} "
                        f"{trade_type_label}: {signal.spot_reason}"
                    )

                if vix_filter_applied:
                    trade_reason += f" [{vix_filter_reason}]"

                # Apply direction override if forced decision was made
                if exec_direction_override:
                    exec_direction = exec_direction_override
                    print(f"   [VIX DELAY FORCED] Using override direction: {exec_direction}")
                    logger.warning(f"VIX Delay forced direction override: {exec_direction}")

                # Add delay reason if this signal was delayed
                if was_delayed:
                    if forced_decision:
                        trade_reason += f" [VIX Delay: Max stall ({max_stall_minutes}m) reached - Forced decision]"
                    else:
                        trade_reason += f" [VIX Delay Applied: {delay_minutes}min wait verified]"

                vix_pct_str = f"{vix_pct:+.2f}%" if vix_pct is not None else "N/A"
                print(f"\n{'=' * 65}")
                print(f"[CONFIRMED] 15m {signal.trade_type}: {signal.direction} close {c_close:.2f}")
                print(f"   India VIX: {vix_pct_str} | VIX Filter: {'ACTIVE — ' + vix_filter_reason if vix_filter_applied else 'OFF'}")
                if was_delayed:
                    print(f"   VIX Entry Delay: APPLIED (verified after {delay_minutes}min)")
                if should_reverse:
                    print(f"[REVERSE MODE] Flipping direction to {exec_direction}")
                elif reverse and not vix_filter_applied:
                    print(f"[REVERSE MODE] Flipping direction to {exec_direction}")
                if vix_filter_applied:
                    print(f"[VIX FILTER] Reversal suppressed — keeping original direction {exec_direction}")
                print(f"{'=' * 65}")
                logger.info(
                    f"[CONFIRMED] 15m {signal.trade_type}: {signal.direction} close {c_close:.2f} | "
                    f"VIX {vix_pct_str} | VIX Filter: {vix_filter_applied} | VIX Delay: {should_delay} | Exec Direction: {exec_direction}"
                )

                last_result = _build_and_execute(
                    inst, c_close, exec_direction, lots, smart_api, paper_trade,
                    auto_execute, gap_info, trade_type=signal.trade_type,
                    structure=structure, trade_reason=trade_reason,
                )
                if not isinstance(last_result, dict):
                    last_result = {"status": "EXECUTION_ERROR"}
                traded_this_pass = True
                status = last_result.get("status", "")

                # ── Trade result summary ─────────────────────────────────────
                _print_trade_result(last_result, inst)
                # ────────────────────────────────────────────────────────────

                # Track consecutive losses synced from shared monitoring state
                consecutive_losses = get_consecutive_losses(inst["name"])
                exit_reason = last_result.get("exit_reason", "")
                pnl = last_result.get("pnl")
                if pnl is not None:
                    if pnl < 0 or "MAX_LOSS" in exit_reason.upper() or "STOP_LOSS" in exit_reason.upper():
                        print(f"   [CONSECUTIVE LOSSES] Loss recorded. Total consecutive losses: {consecutive_losses}")
                        logger.info(f"Consecutive losses: {consecutive_losses}")
                    elif pnl > 0:
                        print(f"   [CONSECUTIVE LOSSES] Profit recorded. Consecutive losses reset to 0.")
                        logger.info(f"Consecutive losses reset to 0 (profit exit)")

                if status == "POSITION_EXISTS":
                    print(f"   [!] Trade entry skipped because an active position already exists for {inst['name']}.")
                    continue

                if status in _STOP_AFTER_EXECUTE:
                    if watchdog_stop_event:
                        watchdog_stop_event.set()
                    return last_result
                
                # Continue monitoring for additional signals on the same day
                # P&L monitoring now runs in parallel in a separate thread
                continue

            if traded_this_pass:
                continue

            # Check market close time (use expiry time if expiry day)
            expiry = is_expiry_day(instrument_name, trading_day, smart_api, spot_exchange, spot_token)
            close_time_str = BREAKOUT_CONFIG.get("expiry_exit_hhmm", "14:45") if expiry else BREAKOUT_CONFIG.get("eod_exit_hhmm", "15:10")

            # Parse close time as datetime for proper comparison
            now_dt = datetime.now()
            close_h, close_m = map(int, close_time_str.split(":"))
            close_dt = now_dt.replace(hour=close_h, minute=close_m, second=0, microsecond=0)

            if now_dt >= close_dt:
                print(f"[*] Market close time reached ({close_time_str}). 15-min close monitor stopping.")
                logger.info(f"Market close time reached ({close_time_str}) - 15-min close monitor stopping")
                if watchdog_stop_event:
                    watchdog_stop_event.set()
                square_off_all(smart_api, inst["name"], is_eod=True)
                if isinstance(last_result, dict):
                    last_result.setdefault("status", "SESSION_END")
                else:
                    last_result = {"status": "SESSION_END"}
                return last_result

            wait_s = seconds_until_next_15m_close()
            now_dt = datetime.now()
            eod_target = now_dt.replace(hour=15, minute=10, second=0, microsecond=0)
            if now_dt < eod_target:
                seconds_to_eod = (eod_target - now_dt).total_seconds()
                if seconds_to_eod < wait_s:
                    wait_s = max(1.0, seconds_to_eod)

            print(
                f"[{datetime.now().strftime('%H:%M:%S')}] {spot_symbol}: waiting {wait_s:.0f}s "
                f"for next 15-min close | range [{range_low:.2f} ... {range_high:.2f}]"
            )
            time.sleep(wait_s)

        except KeyboardInterrupt:
            print("\n[!] Strategy monitoring stopped by user.")
            logger.warning("Strategy monitoring stopped by user")
            if watchdog_stop_event:
                watchdog_stop_event.set()
            try:
                stop_monitor = get_stop_monitor_function()
                stop_monitor()  # Stop parallel P&L monitor
            except Exception as e:
                logger.error(f"Error stopping position monitor: {e}")
            return {"status": "INTERRUPTED"}
        except Exception as e:
            print(f"[!] Error in 15-min monitor: {e}")
            logger.error(f"Error in 15-min monitor: {e}")
            time.sleep(5.0)


# ---------------------------------------------------------------------------
# Main strategy runner
# ---------------------------------------------------------------------------

def run_daybreakout_strategy(
    instrument_name: str = "NIFTY",
    range_high: Optional[float] = None,
    range_low: Optional[float] = None,
    trading_day: Optional[str] = None,
    lots: int = 10,
    confirmation_seconds: int = BREAKOUT_CONFIG["confirmation_seconds"],
    poll_interval: float = BREAKOUT_CONFIG["poll_interval_seconds"],
    smart_api: Optional[SmartConnect] = None,
    paper_trade: bool = False,
    auto_execute: bool = True,
    gap_info: Optional[GapInfo] = None,
    skip_gap_analysis: bool = False,
    reverse: bool = True,
    structure: str = "spread",
    confirm_mode: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Runs the complete Day Breakout workflow:
    1.  Obtains previous day range (12:30 → 15:15) if not provided.
    1b. Fetches and classifies the overnight gap (unless skip_gap_analysis=True).
    2.  Default: waits for completed 15-minute candle closes (same as backtest).
        Optional tick mode: 5-minute live LTP hold.
    3.  Executes Bull Call Spread / Bear Put Spread or Naked Call / Naked Put.
    """
    instrument_key = instrument_name.upper()
    if instrument_key not in INSTRUMENTS:
        raise ValueError(f"Unknown instrument: {instrument_name}. Choose NIFTY or SENSEX.")

    inst = INSTRUMENTS[instrument_key]
    spot_symbol   = inst["spot_symbol"]
    spot_token    = inst["spot_token"]
    spot_exchange = inst["spot_exchange"]

    if smart_api is None:
        smart_api = login_smart_api()

    # ------------------------------------------------------------------
    # Step 1 — Mark Range
    # ------------------------------------------------------------------
    if range_high is None or range_low is None:
        range_data  = get_previous_day_range(smart_api, spot_exchange, spot_token, trading_day)
        range_high  = range_data["high"]
        range_low   = range_data["low"]
        trading_day = range_data["date"]

    # ------------------------------------------------------------------
    # Step 1b — Gap & Opening Analysis
    # ------------------------------------------------------------------
    if not skip_gap_analysis:
        if gap_info is None:
            print("[*] Fetching overnight gap data...")
            gap_info = get_opening_gap(smart_api, spot_exchange, spot_token)
        print_gap_summary(gap_info)
    else:
        gap_info = GapInfo()   # Neutral — no restrictions

    large_gap_extra_wait = GAP_CONFIG["large_gap_extra_wait_seconds"]
    recovery_enabled     = gap_info.caution_level >= 3   # Only for LARGE / VERY_LARGE
    mode = (confirm_mode or BREAKOUT_CONFIG.get("confirm_mode") or "15m").lower()
    if mode not in ("15m", "tick"):
        mode = "15m"

    # ------------------------------------------------------------------
    # Step 1c — Position Recovery (detect and register existing positions)
    # ------------------------------------------------------------------
    print("\n[*] Checking for existing positions to recover for monitoring...")
    logger.info("Checking for existing positions to recover for monitoring")
    try:
        from live.daybreakout.executor import recover_existing_positions as _recover_positions
        recovered = _recover_positions(
            smart_api=smart_api,
            symbol_base=inst["name"],
            check_interval=900.0,  # 15 minutes (900 seconds)
            max_loss=RISK_CONFIG["max_loss"],
            max_profit=RISK_CONFIG["max_profit"],
            profit_threshold=RISK_CONFIG["profit_threshold"],
            min_profit_step=RISK_CONFIG["min_profit_step"],
        )
        if recovered > 0:
            print(f"[+] Successfully recovered {recovered} position(s) for monitoring")
            logger.info(f"Successfully recovered {recovered} position(s) for monitoring")
        else:
            print("[*] No existing positions found to recover")
            logger.info("No existing positions found to recover")
    except Exception as e:
        logger.error(f"Error during position recovery: {e}")
        print(f"[!] Error during position recovery: {e}")

    # ------------------------------------------------------------------
    # Console header
    # ------------------------------------------------------------------
    print("\n" + "=" * 65)
    print(f"[*] DAY BREAKOUT STRATEGY INITIALIZED: {inst['display_name']}")
    print("=" * 65)
    print(f"  Date Range Window        : {trading_day} (12:30 PM - 3:15 PM)")
    print(f"  [^] Resistance (High)     : {range_high:.2f}")
    print(f"  [v] Support (Low)         : {range_low:.2f}")
    first_entry_cfg = BREAKOUT_CONFIG.get("first_entry_hhmm", "09:30")
    if mode == "15m":
        print(f"  [T] Confirmation          : 15-minute candle CLOSE (matches backtest)")
        print(f"  [T] Entry Window          : {first_entry_cfg} to {BREAKOUT_CONFIG['last_entry_hhmm']}")
    else:
        print(f"  [T] Confirmation Window   : {confirmation_seconds} seconds (tick hold)")
        print(f"  [T] Entry Window          : {first_entry_cfg} onward")
    # Fetch actual lot size from contract finder (JSON/API)
    try:
        from live.daybreakout.contract_finder import get_instrument_lot_size
        unit_lot_size = get_instrument_lot_size(inst["name"], inst.get("option_exchange"))
        lot_source = "OpenAPIScripMaster.json"
    except Exception as e:
        unit_lot_size = inst.get("default_lot_size", 65)
        lot_source = f"fallback ({e})"
    total_qty = lots * unit_lot_size

    print(f"  [#] Option Structure      : {structure.upper()} {'(' + str(inst['spread_width']) + ' pt spread)' if structure.lower() == 'spread' else '(Single Leg Buy)'}")
    print(f"  [Q] Position Sizing       : {lots} lots ({total_qty} Qty | Lot Size: {unit_lot_size} [{lot_source}])")
    print(f"  [S] Risk Management       : Max Loss Rs.{RISK_CONFIG['max_loss']:.0f} | Trailing Profit Rs.{RISK_CONFIG['profit_threshold']:.0f}")
    print(f"  [S] Daily Limits          : Max Loss Rs.{RISK_CONFIG['daily_max_loss']:.0f} | Max Profit Rs.{RISK_CONFIG.get('daily_max_profit', 25000.0):.0f} | SL Cooldown {RISK_CONFIG.get('sl_cooldown_minutes', 45)}m")
    print(f"  Gap Classification        : {gap_info.label or 'N/A'} (Caution {gap_info.caution_level}/4)")
    if gap_info.no_chase_same_direction:
        if mode == "15m":
            print(f"  No-Chase Rule             : ACTIVE — skip first {extra_15m_candles_wait()} opening 15m candle(s) in gap direction")
        else:
            print(f"  No-Chase Rule             : ACTIVE — extra {large_gap_extra_wait}s wait in gap direction")
    if recovery_enabled:
        print(f"  Recovery Detection        : ACTIVE — monitors for gap reversal signal")
    print(f"  Trade Direction Mode      : {'REVERSE (Fade Breakouts)' if reverse else 'STANDARD (Follow Breakouts)'}")
    print(f"  Mode                      : {'PAPER TRADING' if paper_trade else 'LIVE TRADING'}")
    print("=" * 65 + "\n")
    
    # Log strategy initialization
    logger.info(f"DAY BREAKOUT STRATEGY INITIALIZED: {inst['display_name']}")
    logger.info(f"Configuration - Range: {trading_day} (12:30-15:15), High: {range_high:.2f}, Low: {range_low:.2f}")
    logger.info(f"Settings - Structure: {structure.upper()}, Lots: {lots}, Mode: {mode}, Reverse: {reverse}, Paper: {paper_trade}")
    logger.info(f"Risk Management - Max Loss: Rs.{RISK_CONFIG['max_loss']:.0f}, Profit Threshold: Rs.{RISK_CONFIG['profit_threshold']:.0f}")
    logger.info(f"Gap Classification: {gap_info.label or 'N/A'} (Caution {gap_info.caution_level}/4)")

    # ------------------------------------------------------------------
    # Step 1c — Fetch previous VIX close for live VIX filter
    # ------------------------------------------------------------------
    prev_vix_close: Optional[float] = None
    try:
        from live.daybreakout.range_calculator import fetch_15m_candles as _fetch_candles
        from datetime import timedelta as _td
        _today = datetime.now().date()
        _from_date = (_today - _td(days=7)).strftime("%Y-%m-%d")
        _to_date = _today.strftime("%Y-%m-%d")
        _vix_candles = _fetch_candles(
            smart_api,
            VIX_CONFIG["exchange"],
            VIX_CONFIG["token"],
            from_str=f"{_from_date} 09:15",
            to_str=f"{_to_date} 09:00",
        )
        if _vix_candles:
            prev_vix_close = float(_vix_candles[-1][4])
            print(f"[*] India VIX previous close: {prev_vix_close:.2f} (VIX filter ACTIVE — Bearish threshold: +{VIX_CONFIG['bearish_vix_threshold']:.1f}% | Bullish threshold: {VIX_CONFIG['bullish_vix_threshold']:.1f}%)")
            logger.info(f"India VIX prev close: {prev_vix_close:.2f}")
        else:
            print("[!] Could not determine previous VIX close — VIX filter will be DISABLED.")
            logger.warning("Could not fetch previous VIX close - VIX filter disabled")
    except Exception as _vix_exc:
        print(f"[!] VIX fetch error: {_vix_exc} — VIX filter will be DISABLED.")
        logger.warning(f"VIX fetch error: {_vix_exc} - VIX filter disabled")

    # ------------------------------------------------------------------
    # Step 2 — Check Market Hours and Wait if Outside Session
    # ------------------------------------------------------------------
    now_dt = datetime.now()
    market_open_str = "09:15"

    # Check if today is expiry day and adjust close time
    logger.info(f"Checking expiry for {inst['name']} with trading_day={trading_day}")
    expiry = is_expiry_day(inst["name"], trading_day, smart_api, spot_exchange, spot_token)
    market_close_str = BREAKOUT_CONFIG.get("expiry_exit_hhmm", "14:45") if expiry else BREAKOUT_CONFIG.get("eod_exit_hhmm", "15:10")

    if expiry:
        logger.info(f"EXPIRY DAY detected for {inst['name']} - early market close at {market_close_str}")
        print(f"[⚠️] EXPIRY DAY ({inst['name']}) - Early market close at {market_close_str}")

    # Parse times as datetime objects for proper comparison
    open_h, open_m = map(int, market_open_str.split(":"))
    close_h, close_m = map(int, market_close_str.split(":"))
    market_open_dt = now_dt.replace(hour=open_h, minute=open_m, second=0, microsecond=0)
    market_close_dt = now_dt.replace(hour=close_h, minute=close_m, second=0, microsecond=0)

    if now_dt >= market_close_dt or now_dt < market_open_dt:
        # Calculate time until next market open
        if now_dt >= market_close_dt:
            # After market close - wait until next day 9:15
            next_open = now_dt.replace(hour=9, minute=15, second=0, microsecond=0) + timedelta(days=1)
        else:
            # Before market open - wait until today 9:15
            next_open = now_dt.replace(hour=9, minute=15, second=0, microsecond=0)
        
        wait_seconds = (next_open - now_dt).total_seconds()
        wait_hours = int(wait_seconds // 3600)
        wait_minutes = int((wait_seconds % 3600) // 60)

        current_time_str = now_dt.strftime("%H:%M")
        print(f"\n{'=' * 65}")
        print(f"[!] MARKET CLOSED - Current time: {current_time_str}")
        print(f"[*] Market hours: {market_open_str} to {market_close_str}")
        print(f"[*] Waiting for next market session...")
        print(f"[*] Next market open: {next_open.strftime('%Y-%m-%d %H:%M')}")
        print(f"[*] Time to wait: {wait_hours}h {wait_minutes}m")
        print(f"{'=' * 65}\n")
        logger.info(f"Market closed (current: {current_time_str}). Waiting until {next_open.strftime('%Y-%m-%d %H:%M')} ({wait_hours}h {wait_minutes}m)")
        
        # Wait until market open
        time.sleep(wait_seconds)
        
        # Refresh range data for the new trading day
        print(f"\n[*] Market session started - refreshing range data for new trading day...")
        logger.info("Market session started - refreshing range data")
        try:
            range_data = get_previous_day_range(smart_api, spot_exchange, spot_token, None)
            range_high = range_data["high"]
            range_low = range_data["low"]
            trading_day = range_data["date"]
            print(f"[+] Updated Range for {trading_day}: High {range_high:.2f} | Low {range_low:.2f}")
            logger.info(f"Updated Range for {trading_day}: High {range_high:.2f} | Low {range_low:.2f}")
            
            # Re-fetch gap analysis for new day
            gap_info = get_opening_gap(smart_api, spot_exchange, spot_token)
            print_gap_summary(gap_info)
            logger.info(f"Gap Analysis refreshed: Direction={gap_info.direction}, Size={gap_info.gap_pct:.4f}")
        except Exception as e:
            print(f"[!] Error refreshing range data: {e}")
            logger.error(f"Error refreshing range data: {e}")

    # ------------------------------------------------------------------
    # Step 3 — Start EOD Watchdog (Thread 3)
    # Guarantees square_off at 3:10 PM (or 2:45 PM on expiry day) regardless of Thread 1/2 state.
    # ------------------------------------------------------------------
    watchdog_stop_event = threading.Event()
    _eod_watchdog_thread = _start_eod_watchdog(
        smart_api,
        inst["name"],
        trading_day=trading_day,
        instrument_name=inst["name"],
        spot_exchange=spot_exchange,
        spot_token=spot_token,
        stop_event=watchdog_stop_event,
    )
    logger.info(f"EOD Watchdog thread started: {_eod_watchdog_thread.name}")

    if mode == "15m":
        return _run_15m_close_monitor(
            inst=inst,
            range_high=range_high,
            range_low=range_low,
            lots=lots,
            smart_api=smart_api,
            paper_trade=paper_trade,
            auto_execute=auto_execute,
            gap_info=gap_info,
            reverse=reverse,
            structure=structure,
            prev_vix_close=prev_vix_close,
            trading_day=trading_day,
            instrument_name=inst["name"],
            spot_exchange=spot_exchange,
            spot_token=spot_token,
            watchdog_stop_event=watchdog_stop_event,
        )

    # ------------------------------------------------------------------
    # Tick-mode monitoring loop (legacy 5-minute LTP hold)
    # ------------------------------------------------------------------
    high_breakout_start:  Optional[float] = None
    low_breakout_start:   Optional[float] = None
    last_print_sec: int = 0

    # No-chase announcement flags
    no_chase_announced_high: bool = False
    no_chase_announced_low:  bool = False

    # Recovery detection state (only active when recovery_enabled)
    gap_territory_touched: bool  = False   # Has spot been in gap territory today?
    recovery_start: Optional[float] = None # Timestamp when recovery signal first appeared
    recovery_direction: Optional[str] = None  # "BULLISH" or "BEARISH"
    recovery_announced: bool = False

    # Check if today is expiry day
    expiry = is_expiry_day(inst["name"], trading_day, smart_api, spot_exchange, spot_token)
    if expiry:
        logger.info(f"EXPIRY DAY detected for {inst['name']} - early market close at {BREAKOUT_CONFIG.get('expiry_exit_hhmm', '14:45')}")
        print(f"[⚠️] EXPIRY DAY ({inst['name']}) - Early market close at {BREAKOUT_CONFIG.get('expiry_exit_hhmm', '14:45')}")

    print(f"[*] Monitoring live spot price for {spot_symbol} on {spot_exchange}...")
    logger.info(f"Starting tick-mode monitoring for {spot_symbol} on {spot_exchange}")

    while True:
        try:
            # ── Single-Trade Gating Check for tick mode ──
            if has_active_trade(inst["name"]):
                now_check = time.time()
                now_check_str = datetime.now().strftime("%H:%M:%S")
                if int(now_check) % 30 == 0 and int(now_check) != last_print_sec:
                    last_print_sec = int(now_check)
                    print(f"[{now_check_str}] [ACTIVE POSITION] Position currently open for {inst['name']}. Tick monitor waiting for trade exit...")
                time.sleep(poll_interval)
                continue

            spot_ltp = get_ltp(smart_api, spot_token, spot_symbol, spot_exchange)
            now      = time.time()
            now_dt   = datetime.now()
            now_str  = now_dt.strftime("%H:%M:%S")

            if spot_ltp <= 0:
                time.sleep(poll_interval)
                continue

            first_entry_cfg = BREAKOUT_CONFIG.get("first_entry_hhmm", "09:30")
            if now_str[:5] < first_entry_cfg:
                if int(now) % 60 == 0 and int(now) != last_print_sec:
                    last_print_sec = int(now)
                    print(f"[{now_str}] Waiting for {first_entry_cfg} entry start time...")
                time.sleep(poll_interval)
                continue

            daily_trades, daily_loss = get_daily_trade_stats(symbol_base=inst["name"])
            daily_profit = get_daily_profit_stats(symbol_base=inst["name"])
            if daily_trades >= RISK_CONFIG["max_daily_trades"]:
                print(f"[!] Daily max trades reached ({daily_trades}). Stopping.")
                return {"status": "DAILY_MAX_TRADES_EXCEEDED"}
            if daily_loss <= RISK_CONFIG["daily_max_loss"]:
                print(f"[!] Daily max loss reached (Cumulative Loss: Rs.{daily_loss:.2f}). Stopping.")
                return {"status": "DAILY_MAX_LOSS_EXCEEDED"}
            if daily_profit >= RISK_CONFIG.get("daily_max_profit", 25000.0):
                print(f"[!] Daily max profit reached (Net P&L: Rs.{daily_profit:.2f} >= Rs.{RISK_CONFIG.get('daily_max_profit', 25000.0):.2f}). Stopping for the day.")
                return {"status": "DAILY_MAX_PROFIT_EXCEEDED"}

            sl_cooldown_min = RISK_CONFIG.get("sl_cooldown_minutes", 45)
            last_sl = get_last_stop_loss_time(symbol_base=inst["name"])
            if last_sl:
                elapsed_mins = (datetime.now() - last_sl).total_seconds() / 60.0
                if elapsed_mins < sl_cooldown_min:
                    if int(now) % 60 == 0 and int(now) != last_print_sec:
                        last_print_sec = int(now)
                        remaining = sl_cooldown_min - elapsed_mins
                        print(f"[{now_str}] [SL COOLDOWN] Stop-loss at {last_sl.strftime('%H:%M:%S')}. Waiting {remaining:.0f}m more ({sl_cooldown_min}m cooldown)...")
                    time.sleep(poll_interval)
                    continue

            # ==============================================================
            # GAP TERRITORY TRACKER (LARGE / VERY_LARGE gap days only)
            # ==============================================================
            if recovery_enabled and not gap_territory_touched:
                if gap_info.direction == "DOWN" and spot_ltp < range_low:
                    gap_territory_touched = True
                    print(f"[{now_str}] [GAP TERRITORY] Spot {spot_ltp:.2f} is in Gap-Down territory (< {range_low:.2f}). Recovery detection armed.")
                    logger.info(f"Gap territory touched - Spot {spot_ltp:.2f} in Gap-Down territory (< {range_low:.2f})")
                elif gap_info.direction == "UP" and spot_ltp > range_high:
                    gap_territory_touched = True
                    print(f"[{now_str}] [GAP TERRITORY] Spot {spot_ltp:.2f} is in Gap-Up territory (> {range_high:.2f}). Recovery detection armed.")
                    logger.info(f"Gap territory touched - Spot {spot_ltp:.2f} in Gap-Up territory (> {range_high:.2f})")

            # ==============================================================
            # BULLISH BREAKOUT CHECK (Spot > High)
            # ==============================================================
            if spot_ltp > range_high:
                # Reset low timer and recovery timer
                if low_breakout_start is not None:
                    print(f"[{now_str}] Low breakout timer reset.")
                    low_breakout_start = None
                    no_chase_announced_low = False
                if recovery_start is not None and recovery_direction == "BEARISH":
                    print(f"[{now_str}] Recovery (bearish) timer cancelled — price broke above High.")
                    recovery_start = None
                    recovery_direction = None
                    recovery_announced = False

                if high_breakout_start is None:
                    high_breakout_start = now
                    no_chase_announced_high = False
                    print(f"\n[{now_str}] [BREAKOUT ALERT] BULLISH BREAKOUT DETECTED!")
                    print(f"    Spot: {spot_ltp:.2f} > High: {range_high:.2f}")
                    if gap_info.be_very_selective:
                        print(f"    {'=' * 57}")
                        print(f"    [CAUTION] VERY LARGE GAP DAY — Be extremely selective!")
                        print(f"    {'=' * 57}")
                    print(f"    Starting {confirmation_seconds}s confirmation timer...\n")

                elapsed = now - high_breakout_start
                same_dir = _is_same_as_gap_direction("BULLISH", gap_info)
                if gap_info.no_chase_same_direction and same_dir:
                    effective_wait = confirmation_seconds + large_gap_extra_wait
                    if not no_chase_announced_high:
                        no_chase_announced_high = True
                        print(f"[{now_str}] [NO-CHASE] {gap_info.size} GAP-UP day: Bullish breakout in gap direction.")
                        print(f"           Extended wait: {effective_wait}s ({confirmation_seconds}s + {large_gap_extra_wait}s extra).\n")
                else:
                    effective_wait = confirmation_seconds

                if int(elapsed) != last_print_sec and int(elapsed) % 10 == 0:
                    last_print_sec = int(elapsed)
                    print(f"[{now_str}] [CONFIRMING] Bullish: {spot_ltp:.2f} > {range_high:.2f} | {elapsed:.0f}/{effective_wait}s ({elapsed/effective_wait*100:.0f}%)")

                if elapsed >= effective_wait:
                    exec_direction = "BEARISH" if reverse else "BULLISH"
                    t_reason = f"Reverse Bearish: Bullish Breakout Spot {spot_ltp:.1f} > High {range_high:.1f}" if reverse else f"Bullish Breakout: Spot {spot_ltp:.1f} > High {range_high:.1f}"
                    print(f"\n{'=' * 65}")
                    print(f"[{now_str}] [CONFIRMED] BULLISH BREAKOUT CONFIRMED!")
                    if reverse:
                        print(f"[{now_str}] [REVERSE MODE] Flipping direction to BEARISH")
                    print(f"{'=' * 65}")
                    logger.info(f"[CONFIRMED] BULLISH BREAKOUT - Spot: {spot_ltp:.1f} > High: {range_high:.1f}, Exec Direction: {exec_direction}")
                    last_result = _build_and_execute(
                        inst, spot_ltp, exec_direction, lots, smart_api, paper_trade, auto_execute, gap_info,
                        trade_type="BREAKOUT", structure=structure, trade_reason=t_reason
                    )
                    _print_trade_result(last_result, inst)
                    status = (last_result or {}).get("status", "")
                    if status in _STOP_AFTER_EXECUTE:
                        if watchdog_stop_event:
                            watchdog_stop_event.set()
                        return last_result
                    high_breakout_start = None
                    low_breakout_start = None
                    no_chase_announced_high = False
                    no_chase_announced_low = False
                    continue

            elif high_breakout_start is not None:
                duration = now - high_breakout_start
                print(f"\n[{now_str}] [RESET] Bullish timer cancelled: {spot_ltp:.2f} <= {range_high:.2f} after {duration:.0f}s.\n")
                high_breakout_start = None
                no_chase_announced_high = False

            # ==============================================================
            # BEARISH BREAKOUT CHECK (Spot < Low)
            # ==============================================================
            if spot_ltp < range_low:
                # Reset high timer and recovery timer
                if high_breakout_start is not None:
                    print(f"[{now_str}] High breakout timer reset.")
                    high_breakout_start = None
                    no_chase_announced_high = False
                if recovery_start is not None and recovery_direction == "BULLISH":
                    print(f"[{now_str}] Recovery (bullish) timer cancelled — price broke below Low.")
                    recovery_start = None
                    recovery_direction = None
                    recovery_announced = False

                if low_breakout_start is None:
                    low_breakout_start = now
                    no_chase_announced_low = False
                    print(f"\n[{now_str}] [BREAKOUT ALERT] BEARISH BREAKOUT DETECTED!")
                    print(f"    Spot: {spot_ltp:.2f} < Low: {range_low:.2f}")
                    if gap_info.be_very_selective:
                        print(f"    {'=' * 57}")
                        print(f"    [CAUTION] VERY LARGE GAP DAY — Be extremely selective!")
                        print(f"    {'=' * 57}")
                    print(f"    Starting {confirmation_seconds}s confirmation timer...\n")

                elapsed = now - low_breakout_start
                same_dir = _is_same_as_gap_direction("BEARISH", gap_info)
                if gap_info.no_chase_same_direction and same_dir:
                    effective_wait = confirmation_seconds + large_gap_extra_wait
                    if not no_chase_announced_low:
                        no_chase_announced_low = True
                        print(f"[{now_str}] [NO-CHASE] {gap_info.size} GAP-DOWN day: Bearish breakout in gap direction.")
                        print(f"           Extended wait: {effective_wait}s ({confirmation_seconds}s + {large_gap_extra_wait}s extra).\n")
                else:
                    effective_wait = confirmation_seconds

                if int(elapsed) != last_print_sec and int(elapsed) % 10 == 0:
                    last_print_sec = int(elapsed)
                    print(f"[{now_str}] [CONFIRMING] Bearish: {spot_ltp:.2f} < {range_low:.2f} | {elapsed:.0f}/{effective_wait}s ({elapsed/effective_wait*100:.0f}%)")

                if elapsed >= effective_wait:
                    exec_direction = "BULLISH" if reverse else "BEARISH"
                    t_reason = f"Reverse Bullish: Bearish Breakout Spot {spot_ltp:.1f} < Low {range_low:.1f}" if reverse else f"Bearish Breakout: Spot {spot_ltp:.1f} < Low {range_low:.1f}"
                    print(f"\n{'=' * 65}")
                    print(f"[{now_str}] [CONFIRMED] BEARISH BREAKOUT CONFIRMED!")
                    if reverse:
                        print(f"[{now_str}] [REVERSE MODE] Flipping direction to BULLISH")
                    print(f"{'=' * 65}")
                    logger.info(f"[CONFIRMED] BEARISH BREAKOUT - Spot: {spot_ltp:.1f} < Low: {range_low:.1f}, Exec Direction: {exec_direction}")
                    last_result = _build_and_execute(
                        inst, spot_ltp, exec_direction, lots, smart_api, paper_trade, auto_execute, gap_info,
                        trade_type="BREAKOUT", structure=structure, trade_reason=t_reason
                    )
                    _print_trade_result(last_result, inst)
                    status = (last_result or {}).get("status", "")
                    if status in _STOP_AFTER_EXECUTE:
                        if watchdog_stop_event:
                            watchdog_stop_event.set()
                        return last_result
                    high_breakout_start = None
                    low_breakout_start = None
                    no_chase_announced_high = False
                    no_chase_announced_low = False
                    continue

            elif low_breakout_start is not None:
                duration = now - low_breakout_start
                print(f"\n[{now_str}] [RESET] Bearish timer cancelled: {spot_ltp:.2f} >= {range_low:.2f} after {duration:.0f}s.\n")
                low_breakout_start = None
                no_chase_announced_low = False

            # ==============================================================
            # RECOVERY DETECTION (LARGE / VERY_LARGE gap days, inside range)
            # Price is now between range_low and range_high
            # ==============================================================
            if (recovery_enabled
                    and gap_territory_touched
                    and range_low <= spot_ltp <= range_high
                    and high_breakout_start is None
                    and low_breakout_start is None):

                # Determine expected recovery direction
                expected_recovery = "BULLISH" if gap_info.direction == "DOWN" else "BEARISH"

                if recovery_start is None:
                    # First tick back inside range after gap territory → start timer
                    recovery_start    = now
                    recovery_direction = expected_recovery
                    recovery_announced = False
                    recovery_label = "GAP-DOWN RECOVERY" if gap_info.direction == "DOWN" else "GAP-UP RECOVERY"
                    recovery_trade = "Bull Call" if expected_recovery == "BULLISH" else "Bear Put"
                    print(f"\n[{now_str}] [RECOVERY ALERT] {recovery_label} DETECTED!")
                    print(f"    Spot {spot_ltp:.2f} crossed back {'above' if expected_recovery == 'BULLISH' else 'below'} Range {'Low' if expected_recovery == 'BULLISH' else 'High'} {range_low if expected_recovery == 'BULLISH' else range_high:.2f}")
                    if gap_info.be_very_selective:
                        print(f"    [CAUTION] VERY LARGE GAP — Confirm recovery with extra care!")
                    print(f"    Target: {recovery_trade} | Starting {confirmation_seconds}s confirmation timer...\n")

                elapsed = now - recovery_start
                if int(elapsed) != last_print_sec and int(elapsed) % 10 == 0:
                    last_print_sec = int(elapsed)
                    print(f"[{now_str}] [RECOVERY CONFIRMING] {recovery_direction}: Spot {spot_ltp:.2f} inside range | {elapsed:.0f}/{confirmation_seconds}s ({elapsed/confirmation_seconds*100:.0f}%)")

                if elapsed >= confirmation_seconds:
                    exec_direction = ("BEARISH" if recovery_direction == "BULLISH" else "BULLISH") if reverse else recovery_direction
                    rec_bound = f"Low {range_low:.1f}" if gap_info.direction == "DOWN" else f"High {range_high:.1f}"
                    t_reason = f"Reverse {exec_direction} Recovery: Reentered range past {rec_bound}" if reverse else f"{recovery_direction} Recovery: Reentered range past {rec_bound}"
                    print(f"\n{'=' * 65}")
                    print(f"[{now_str}] [CONFIRMED] RECOVERY CONFIRMED! ({gap_info.label} → {recovery_direction})")
                    if reverse:
                        print(f"[{now_str}] [REVERSE MODE] Flipping recovery direction from {recovery_direction} to {exec_direction}")
                    print(f"{'=' * 65}")
                    logger.info(f"[CONFIRMED] RECOVERY - {gap_info.label} → {recovery_direction}, Exec Direction: {exec_direction}")
                    # Reset gap territory so another recovery needs re-entry into gap zone
                    gap_territory_touched = False
                    recovery_start = None
                    last_result = _build_and_execute(
                        inst, spot_ltp, exec_direction, lots,
                        smart_api, paper_trade, auto_execute, gap_info,
                        trade_type="RECOVERY", structure=structure, trade_reason=t_reason
                    )
                    _print_trade_result(last_result, inst)
                    status = (last_result or {}).get("status", "")
                    if status in _STOP_AFTER_EXECUTE:
                        if watchdog_stop_event:
                            watchdog_stop_event.set()
                        return last_result
                    recovery_start = None
                    recovery_direction = None
                    recovery_announced = False
                    continue

            elif recovery_start is not None:
                # Price went back to gap territory — reset recovery timer
                duration = now - recovery_start
                print(f"\n[{now_str}] [RECOVERY RESET] Price returned to gap territory ({spot_ltp:.2f}) after {duration:.0f}s. Recovery timer reset.\n")
                recovery_start     = None
                recovery_direction = None
                recovery_announced = False

            # ==============================================================
            # Inside-range heartbeat (every 30 seconds)
            # ==============================================================
            if high_breakout_start is None and low_breakout_start is None and recovery_start is None:
                if int(now) % 30 == 0 and int(now) != last_print_sec:
                    last_print_sec = int(now)
                    diff_high = range_high - spot_ltp
                    diff_low  = spot_ltp - range_low
                    caution_tag = f" [Caution {gap_info.caution_level}/4]" if gap_info.caution_level >= 3 else ""
                    recovery_tag = " [Recovery armed]" if recovery_enabled and gap_territory_touched else ""
                    print(f"[{now_str}] {spot_symbol}: {spot_ltp:.2f} | [{range_low:.2f} ... {range_high:.2f}] (H-{diff_high:.1f} pts, L+{diff_low:.1f} pts){caution_tag}{recovery_tag}")

            # ==============================================================
            # EOD Session Check
            # ==============================================================
            # Check if today is expiry day and adjust close time
            expiry = is_expiry_day(inst["name"], trading_day, smart_api, spot_exchange, spot_token)
            close_time = BREAKOUT_CONFIG.get("expiry_exit_hhmm", "14:45") if expiry else BREAKOUT_CONFIG.get("eod_exit_hhmm", "15:10")
            close_time_with_seconds = close_time + ":00"
            
            if now_str >= close_time_with_seconds:
                print(f"[*] Market close time reached ({close_time}). Tick-mode monitor stopping.")
                logger.info(f"Market close time reached ({close_time}) - Tick-mode monitor stopping")
                if watchdog_stop_event:
                    watchdog_stop_event.set()
                square_off_all(smart_api, inst["name"], is_eod=True)
                return {"status": "SESSION_END"}

            time.sleep(poll_interval)

        except KeyboardInterrupt:
            print("\n[!] Strategy monitoring stopped by user.")
            logger.warning("Strategy monitoring stopped by user")
            if watchdog_stop_event:
                watchdog_stop_event.set()
            try:
                stop_monitor = get_stop_monitor_function()
                stop_monitor()  # Stop parallel P&L monitor
            except Exception as e:
                logger.error(f"Error stopping position monitor: {e}")
            return {"status": "INTERRUPTED"}
        except Exception as e:
            print(f"[!] Error in monitoring loop: {e}")
            logger.error(f"Error in monitoring loop: {e}")
            time.sleep(1.0)
