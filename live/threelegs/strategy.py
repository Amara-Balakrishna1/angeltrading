"""
Main Strategy Runner for 3-Leg Options Strategy.
Entry at 9:30 AM, exit at 3:10 PM with hourly monitoring.
"""

import sys
from pathlib import Path

# Add parent directory to path for imports
ROOT = Path(__file__).resolve().parent.parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import time
from datetime import datetime
from typing import Dict, Any, Optional

from SmartApi.smartConnect import SmartConnect
from live.threelegs.config import INSTRUMENT_CONFIG, ENTRY_TIME, EXIT_TIME
from live.threelegs.logger import logger
from live.threelegs.executor import login_smart_api, get_ltp, place_order, get_open_positions, square_off_all
from live.threelegs.instrument_map import find_expiry_and_strikes
from live.threelegs.monitor import HourlyMonitor, PositionState


def run_3leg_strategy(
    smart_api: SmartConnect,
    side: str = "AUTO",
    lots: int = 5,
    paper_trade: bool = False,
    instrument_name: str = "BANKNIFTY"
) -> Dict[str, Any]:
    """
    Runs the complete 3-leg strategy workflow.

    Args:
        smart_api: SmartConnect instance
        side: "CE", "PE", or "AUTO" (default: AUTO)
        lots: Number of lots to trade
        paper_trade: If True, skip actual order placement
        instrument_name: Instrument to trade (default: BANKNIFTY)

    Returns:
        Dict with strategy results
    """
    inst_config = INSTRUMENT_CONFIG[instrument_name]

    # Wait until 9:25 to fetch data before entry
    now = datetime.now()
    entry_time_str = now.strftime("%H:%M")
    if entry_time_str < "09:25":
        wait_seconds = (9 * 3600 + 25 * 60) - (now.hour * 3600 + now.minute * 60 + now.second)
        if wait_seconds > 0:
            print(f"[*] Waiting until 9:25 AM ({wait_seconds} seconds)...")
            logger.info(f"Waiting until 9:25 AM ({wait_seconds} seconds)")
            time.sleep(wait_seconds)

    # Fetch current spot LTP at 9:30
    print(f"[*] Fetching spot LTP at {datetime.now().strftime('%H:%M:%S')}...")
    logger.info("Fetching spot LTP for entry")
    spot_ltp = get_ltp(
        smart_api,
        inst_config["spot_token"],
        inst_config["spot_symbol"],
        inst_config["spot_exchange"]
    )
    print(f"[+] Current Spot: {spot_ltp:.2f}")
    logger.info(f"Current Spot: {spot_ltp:.2f}")

    # Determine side
    if side.upper() == "AUTO":
        # For AUTO mode, we need 9:15 open price to decide
        # In live trading, we'll default to CE if AUTO and no 9:15 data available
        # In a full implementation, you'd fetch the 9:15 candle
        chosen_side = "CE"  # Default to CE for now
        decision_reason = "AUTO mode - defaulting to CE (implement 9:15 comparison for full logic)"
    elif side.upper() == "PE":
        chosen_side = "PE"
        decision_reason = "Manual PE mode"
    else:
        chosen_side = "CE"
        decision_reason = "Manual CE mode"

    print(f"[+] Strategy Side: {chosen_side} ({decision_reason})")
    logger.info(f"Strategy Side: {chosen_side} - {decision_reason}")

    # Find expiry and strikes
    strike_info = find_expiry_and_strikes(spot_ltp, chosen_side, instrument_name=instrument_name)

    atm_strike = strike_info["atm"]
    strike_1 = strike_info["strike_1"]
    strike_2 = strike_info["strike_2"]
    hedge_strike = strike_info["hedge_strike"]
    hedge_type = strike_info["hedge_type"]
    option_map = strike_info["option_map"]
    lot_size = strike_info["lot_size"]
    exchange = strike_info["exchange"]

    print(f"[+] Strike Selection:")
    print(f"    ATM: {atm_strike} {chosen_side}")
    print(f"    Spread 1: {strike_1} {chosen_side}")
    print(f"    Spread 2: {strike_2} {chosen_side}")
    print(f"    Hedge: {hedge_strike} {hedge_type}")
    print(f"    Lot Size: {lot_size}")
    logger.info(f"Strike Selection - ATM: {atm_strike}, S1: {strike_1}, S2: {strike_2}, Hedge: {hedge_strike} {hedge_type}, Lot Size: {lot_size}")

    # Get contract info
    atm_key = (float(atm_strike), chosen_side)
    strike_1_key = (float(strike_1), chosen_side)
    strike_2_key = (float(strike_2), chosen_side)
    hedge_key = (float(hedge_strike), hedge_type)

    if atm_key not in option_map or strike_1_key not in option_map or strike_2_key not in option_map or hedge_key not in option_map:
        raise RuntimeError("Required contracts not found in option map")

    atm_contract = option_map[atm_key]
    strike_1_contract = option_map[strike_1_key]
    strike_2_contract = option_map[strike_2_key]
    hedge_contract = option_map[hedge_key]

    # Use the lot size from the actual contract for more accuracy
    actual_lot_size = atm_contract.get("lot_size", lot_size)
    print(f"[*] Using actual lot size from contract: {actual_lot_size}")
    logger.info(f"Using actual lot size from contract: {actual_lot_size}")

    total_quantity = lots * actual_lot_size

    # Validate quantity is a multiple of lot size
    if total_quantity % actual_lot_size != 0:
        print(f"[!] Warning: Quantity {total_quantity} is not a multiple of lot size {actual_lot_size}")
        # Adjust to nearest valid multiple
        total_quantity = (total_quantity // actual_lot_size) * actual_lot_size
        print(f"[!] Adjusted quantity to {total_quantity}")

    print(f"[*] Order Quantity: {total_quantity} ({lots} lots × {actual_lot_size} lot size)")
    logger.info(f"Order Quantity: {total_quantity} ({lots} lots × {actual_lot_size} lot size)")

    # Additional validation: ensure quantity is at least lot size
    if total_quantity < actual_lot_size:
        print(f"[!] Warning: Quantity {total_quantity} is less than lot size {actual_lot_size}")
        total_quantity = actual_lot_size
        print(f"[!] Adjusted quantity to minimum lot size: {total_quantity}")

    # Wait until 9:30 for entry
    now = datetime.now()
    entry_time_str = now.strftime("%H:%M")
    if entry_time_str < ENTRY_TIME:
        wait_seconds = (9 * 3600 + 30 * 60) - (now.hour * 3600 + now.minute * 60 + now.second)
        if wait_seconds > 0:
            print(f"[*] Waiting until 9:30 AM for entry ({wait_seconds} seconds)...")
            logger.info(f"Waiting until 9:30 AM for entry ({wait_seconds} seconds)")
            time.sleep(wait_seconds)

    # Get entry prices
    print(f"[*] Fetching entry prices at {datetime.now().strftime('%H:%M:%S')}...")
    logger.info("Fetching entry prices")

    atm_entry_price = get_ltp(smart_api, atm_contract["token"], atm_contract["symbol"], exchange)
    strike_1_entry_price = get_ltp(smart_api, strike_1_contract["token"], strike_1_contract["symbol"], exchange)
    strike_2_entry_price = get_ltp(smart_api, strike_2_contract["token"], strike_2_contract["symbol"], exchange)

    print(f"[+] Entry Prices:")
    print(f"    ATM: {atm_entry_price:.2f}")
    print(f"    Spread 1: {strike_1_entry_price:.2f}")
    print(f"    Spread 2: {strike_2_entry_price:.2f}")
    logger.info(f"Entry Prices - ATM: {atm_entry_price:.2f}, S1: {strike_1_entry_price:.2f}, S2: {strike_2_entry_price:.2f}")

    # Place entry orders
    print(f"[*] Placing entry orders...")
    logger.info("Placing entry orders")

    # BUY ATM
    atm_order_id = place_order(
        smart_api,
        atm_contract["symbol"],
        atm_contract["token"],
        "BUY",
        total_quantity,
        exchange,
        paper_trade=paper_trade
    )

    # SELL Strike 1
    strike_1_order_id = place_order(
        smart_api,
        strike_1_contract["symbol"],
        strike_1_contract["token"],
        "SELL",
        total_quantity,
        exchange,
        paper_trade=paper_trade
    )

    # SELL Strike 2
    strike_2_order_id = place_order(
        smart_api,
        strike_2_contract["symbol"],
        strike_2_contract["token"],
        "SELL",
        total_quantity,
        exchange,
        paper_trade=paper_trade
    )

    # Create position state
    position_state = PositionState(
        entry_time=datetime.now().strftime("%H:%M:%S"),
        side=chosen_side,
        atm_strike=atm_strike,
        strike_1=strike_1,
        strike_2=strike_2,
        hedge_strike=hedge_strike,
        hedge_type=hedge_type,
        lot_size=actual_lot_size,
        lots=lots,
        total_quantity=total_quantity,
        exchange=exchange,
        atm_token=atm_contract["token"],
        atm_symbol=atm_contract["symbol"],
        strike_1_token=strike_1_contract["token"],
        strike_1_symbol=strike_1_contract["symbol"],
        strike_2_token=strike_2_contract["token"],
        strike_2_symbol=strike_2_contract["symbol"],
        hedge_token=hedge_contract["token"],
        hedge_symbol=hedge_contract["symbol"],
        atm_entry_price=atm_entry_price,
        strike_1_entry_price=strike_1_entry_price,
        strike_2_entry_price=strike_2_entry_price,
        current_spread_2=strike_2,
        current_strike_2_token=strike_2_contract["token"],
        current_strike_2_symbol=strike_2_contract["symbol"],
        current_strike_2_entry_price=strike_2_entry_price,
        atm_order_id=atm_order_id,
        strike_1_order_id=strike_1_order_id,
        strike_2_order_id=strike_2_order_id,
    )

    print(f"[+] Entry completed at {position_state.entry_time}")
    logger.info(f"Entry completed at {position_state.entry_time}")

    # Start hourly monitor
    monitor = HourlyMonitor(smart_api, position_state, paper_trade=paper_trade)
    monitor_thread = monitor.start()

    # Wait for monitor to complete
    monitor_thread.join()

    # Square off all positions
    print(f"\n[*] Strategy completed. Exit reason: {position_state.exit_reason}")
    logger.info(f"Strategy completed. Exit reason: {position_state.exit_reason}")

    if not paper_trade:
        open_positions = get_open_positions(smart_api, instrument_name)
        if open_positions:
            square_off_all(smart_api, open_positions, paper_trade=False, exchange=exchange)
    else:
        print("[PAPER TRADE] Would square off all positions at market")
        logger.info("[PAPER TRADE] Would square off all positions at market")

    # Calculate final P&L
    final_pnl = position_state.max_pnl if position_state.exit_triggered else 0.0

    return {
        "status": "completed",
        "side": chosen_side,
        "entry_time": position_state.entry_time,
        "exit_time": position_state.exit_time,
        "exit_reason": position_state.exit_reason,
        "atm_strike": atm_strike,
        "strike_1": strike_1,
        "strike_2": strike_2,
        "hedge_strike": hedge_strike,
        "hedge_type": hedge_type,
        "hedge_bought": position_state.hedge_bought,
        "final_pnl": final_pnl,
        "max_pnl": position_state.max_pnl,
        "min_pnl": position_state.min_pnl,
        "lots": lots,
        "lot_size": actual_lot_size,
        "total_quantity": total_quantity,
    }
