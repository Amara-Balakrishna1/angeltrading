"""
Hourly P&L Monitor for 3-Leg Options Strategy.
Threaded monitor that checks P&L at specified times and handles:
- Dynamic spread adjustment
- Hedging at 11:00
- Hard stop-loss
- Exit at 15:10
"""

import sys
from pathlib import Path

# Add parent directory to path for imports
ROOT = Path(__file__).resolve().parent.parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import time
import threading
from datetime import datetime
from dataclasses import dataclass, field
from typing import Dict, Any, Optional

from SmartApi.smartConnect import SmartConnect
from live.threelegs.config import INSTRUMENT_CONFIG, MONITORING_TIMES, EXIT_TIME, RISK_CONFIG
from live.threelegs.logger import logger
from live.threelegs.executor import get_ltp, place_order, get_open_positions, square_off_all
from live.threelegs.instrument_map import download_instrument_map


@dataclass
class PositionState:
    """Tracks the state of the 3-leg position."""
    # Entry data
    entry_time: str
    side: str  # "CE" or "PE"
    atm_strike: int
    strike_1: int
    strike_2: int
    hedge_strike: int
    hedge_type: str
    lot_size: int
    lots: int
    total_quantity: int
    exchange: str

    # Contract info
    atm_token: str
    atm_symbol: str
    strike_1_token: str
    strike_1_symbol: str
    strike_2_token: str
    strike_2_symbol: str
    hedge_token: str
    hedge_symbol: str

    # Entry prices
    atm_entry_price: float
    strike_1_entry_price: float
    strike_2_entry_price: float

    # Current state
    current_spread_2: int
    current_strike_2_token: str
    current_strike_2_symbol: str
    current_strike_2_entry_price: float

    # Hedge state
    hedge_bought: bool = False
    hedge_entry_price: Optional[float] = None
    hedge_buy_time: Optional[str] = None

    # Order IDs
    atm_order_id: Optional[str] = None
    strike_1_order_id: Optional[str] = None
    strike_2_order_id: Optional[str] = None
    hedge_order_id: Optional[str] = None

    # Exit state
    exit_triggered: bool = False
    exit_time: Optional[str] = None
    exit_reason: Optional[str] = None

    # P&L tracking
    max_pnl: float = 0.0
    min_pnl: float = 0.0


class HourlyMonitor:
    """
    Threaded hourly P&L monitor for the 3-leg strategy.
    Wakes at each MONITORING_TIMES boundary and checks P&L.
    """

    def __init__(
        self,
        smart_api: SmartConnect,
        position_state: PositionState,
        paper_trade: bool = False
    ):
        self.smart_api = smart_api
        self.position_state = position_state
        self.paper_trade = paper_trade
        self._stop_requested = False
        self._thread: Optional[threading.Thread] = None
        self.inst_config = INSTRUMENT_CONFIG["BANKNIFTY"]

    def start(self) -> threading.Thread:
        """Starts the monitor thread."""
        self._stop_requested = False
        self._thread = threading.Thread(
            target=self._monitor_loop,
            daemon=True,
            name="Hourly_PnL_Monitor"
        )
        self._thread.start()
        logger.info("Hourly P&L monitor thread started")
        return self._thread

    def stop(self):
        """Signals the monitor thread to stop."""
        self._stop_requested = True
        logger.info("Signaled hourly P&L monitor to stop")

    def _wait_until_time(self, target_time: str):
        """Waits until the specified time (HH:MM format)."""
        while not self._stop_requested:
            now = datetime.now()
            current_time = now.strftime("%H:%M")
            if current_time >= target_time:
                return

            # Calculate seconds to wait
            target_parts = target_time.split(":")
            now_parts = current_time.split(":")
            target_seconds = int(target_parts[0]) * 3600 + int(target_parts[1]) * 60
            now_seconds = int(now_parts[0]) * 3600 + int(now_parts[1]) * 60
            wait_seconds = target_seconds - now_seconds

            if wait_seconds > 0:
                time.sleep(min(wait_seconds, 10))  # Sleep in chunks to check stop flag
            else:
                return

    def _calculate_pnl(self) -> Dict[str, float]:
        """Calculates current P&L for all active legs."""
        state = self.position_state

        # Fetch current LTPs
        atm_ltp = get_ltp(
            self.smart_api,
            state.atm_token,
            state.atm_symbol,
            state.exchange,
            last_price=state.atm_entry_price
        )
        strike_1_ltp = get_ltp(
            self.smart_api,
            state.strike_1_token,
            state.strike_1_symbol,
            state.exchange,
            last_price=state.strike_1_entry_price
        )
        strike_2_ltp = get_ltp(
            self.smart_api,
            state.current_strike_2_token,
            state.current_strike_2_symbol,
            state.exchange,
            last_price=state.current_strike_2_entry_price
        )

        # Calculate leg P&L
        atm_pnl = (atm_ltp - state.atm_entry_price) * state.total_quantity
        strike_1_pnl = (state.strike_1_entry_price - strike_1_ltp) * state.total_quantity
        strike_2_pnl = (state.current_strike_2_entry_price - strike_2_ltp) * state.total_quantity
        total_pnl = atm_pnl + strike_1_pnl + strike_2_pnl

        # Calculate hedge P&L if hedge was bought
        hedge_pnl = 0.0
        if state.hedge_bought and state.hedge_entry_price:
            hedge_ltp = get_ltp(
                self.smart_api,
                state.hedge_token,
                state.hedge_symbol,
                state.exchange,
                last_price=state.hedge_entry_price
            )
            hedge_multiplier = self.inst_config["hedge_multiplier"]
            hedge_pnl = (hedge_ltp - state.hedge_entry_price) * state.total_quantity * hedge_multiplier

        return {
            "atm_pnl": atm_pnl,
            "strike_1_pnl": strike_1_pnl,
            "strike_2_pnl": strike_2_pnl,
            "total_pnl": total_pnl,
            "hedge_pnl": hedge_pnl,
            "total_pnl_with_hedge": total_pnl + hedge_pnl,
        }

    def _handle_spread_adjustment(self, pnl: float):
        """Handles dynamic spread adjustment when loss threshold is exceeded."""
        state = self.position_state
        inst_config = self.inst_config

        if pnl >= -inst_config["loss_threshold"]:
            return

        # Calculate new spread 2
        if state.side == "CE":
            adj_step = -inst_config["spread_adjustment"]  # Move downward
            new_spread_2 = state.current_spread_2 + adj_step
            is_exit_met = new_spread_2 <= state.strike_1
        else:
            adj_step = inst_config["spread_adjustment"]  # Move upward
            new_spread_2 = state.current_spread_2 + adj_step
            is_exit_met = new_spread_2 >= state.strike_1

        if is_exit_met:
            # Exit condition met
            state.exit_triggered = True
            state.exit_time = datetime.now().strftime("%H:%M")
            op_symbol = "<=" if state.side == "CE" else ">="
            state.exit_reason = f"Spread 2 ({new_spread_2}) {op_symbol} Spread 1 ({state.strike_1})"
            logger.warning(f"Exit condition triggered: {state.exit_reason}")
            return

        # Perform spread adjustment
        logger.info(f"Spread adjustment: {state.current_spread_2} -> {new_spread_2} (P&L: {pnl:.2f})")

        # BUY back old strike 2
        if not self.paper_trade:
            buy_resp = place_order(
                self.smart_api,
                state.current_strike_2_symbol,
                state.current_strike_2_token,
                "BUY",
                state.total_quantity,
                state.exchange,
                paper_trade=False
            )
            if not buy_resp:
                logger.error("Failed to buy back old strike 2 in spread adjustment")
                return

        # Find new strike 2 contract info from instrument map
        option_map = download_instrument_map()
        new_key = (float(new_spread_2), state.side)
        if new_key not in option_map:
            logger.error(f"New strike 2 contract not found: {new_spread_2} {state.side}")
            return

        new_contract = option_map[new_key]
        new_entry_price = get_ltp(
            self.smart_api,
            new_contract["token"],
            new_contract["symbol"],
            state.exchange
        )

        # SELL new strike 2
        if not self.paper_trade:
            sell_resp = place_order(
                self.smart_api,
                new_contract["symbol"],
                new_contract["token"],
                "SELL",
                state.total_quantity,
                state.exchange,
                paper_trade=False
            )
            if not sell_resp:
                logger.error("Failed to sell new strike 2 in spread adjustment")
                return

        # Update state
        state.current_spread_2 = new_spread_2
        state.current_strike_2_token = new_contract["token"]
        state.current_strike_2_symbol = new_contract["symbol"]
        state.current_strike_2_entry_price = new_entry_price

        logger.info(f"Spread adjustment completed: {new_spread_2} {state.side}")

    def _handle_hedging(self, pnl: float, current_time: str):
        """Handles hedging at 11:00 if position is negative."""
        state = self.position_state
        inst_config = self.inst_config

        if current_time != "11:00":
            return

        if state.hedge_bought:
            return

        if pnl >= 0:
            return

        logger.info(f"Hedging triggered at 11:00 (P&L: {pnl:.2f})")

        hedge_multiplier = inst_config["hedge_multiplier"]
        hedge_quantity = state.total_quantity * hedge_multiplier

        # Validate hedge quantity is a multiple of lot size
        if hedge_quantity % state.lot_size != 0:
            logger.warning(f"Hedge quantity {hedge_quantity} not a multiple of lot size {state.lot_size}, adjusting")
            hedge_quantity = (hedge_quantity // state.lot_size) * state.lot_size

        if not self.paper_trade:
            resp = place_order(
                self.smart_api,
                state.hedge_symbol,
                state.hedge_token,
                "BUY",
                hedge_quantity,
                state.exchange,
                paper_trade=False
            )
            if not resp:
                logger.error("Failed to buy hedge contract")
                return

        # Update state
        state.hedge_bought = True
        state.hedge_entry_price = get_ltp(
            self.smart_api,
            state.hedge_token,
            state.hedge_symbol,
            state.exchange
        )
        state.hedge_buy_time = current_time

        logger.info(f"Hedge bought: {state.hedge_strike} {state.hedge_type} @ {state.hedge_entry_price:.2f}")

    def _check_and_act(self, current_time: str):
        """Checks P&L and takes appropriate actions."""
        if self.position_state.exit_triggered:
            return

        pnl_data = self._calculate_pnl()
        total_pnl = pnl_data["total_pnl"]
        total_pnl_with_hedge = pnl_data["total_pnl_with_hedge"]

        # Update max/min P&L
        self.position_state.max_pnl = max(self.position_state.max_pnl, total_pnl_with_hedge)
        self.position_state.min_pnl = min(self.position_state.min_pnl, total_pnl_with_hedge)

        # Log current P&L
        logger.info(
            f"[{current_time}] P&L: ATM={pnl_data['atm_pnl']:.2f}, "
            f"S1={pnl_data['strike_1_pnl']:.2f}, S2={pnl_data['strike_2_pnl']:.2f}, "
            f"Total={total_pnl:.2f}, Hedge={pnl_data['hedge_pnl']:.2f}, "
            f"Net={total_pnl_with_hedge:.2f}"
        )

        # HARD STOP-LOSS CHECK
        stop_loss_threshold = RISK_CONFIG["stop_loss_threshold"]
        if total_pnl_with_hedge < stop_loss_threshold:
            self.position_state.exit_triggered = True
            self.position_state.exit_time = current_time
            self.position_state.exit_reason = f"Stop-loss triggered: Loss {total_pnl_with_hedge:.2f} < {stop_loss_threshold}"
            logger.error(f"Stop-loss triggered: {self.position_state.exit_reason}")
            return

        # Handle hedging at 11:00
        self._handle_hedging(total_pnl, current_time)

        # Handle spread adjustment
        self._handle_spread_adjustment(total_pnl)

    def _monitor_loop(self):
        """Main monitor loop."""
        logger.info(f"Starting hourly P&L monitor at {datetime.now().strftime('%H:%M:%S')}")

        for monitor_time in MONITORING_TIMES:
            if self._stop_requested:
                break

            if self.position_state.exit_triggered:
                break

            self._wait_until_time(monitor_time)

            if self._stop_requested:
                break

            if self.position_state.exit_triggered:
                break

            # Check and act
            self._check_and_act(monitor_time)

            # Exit if this was the last monitoring time
            if monitor_time == EXIT_TIME:
                self.position_state.exit_triggered = True
                self.position_state.exit_time = monitor_time
                self.position_state.exit_reason = "Market close (15:10)"
                logger.info("Market close time reached (15:10)")
                break

        logger.info("Hourly P&L monitor stopped")
