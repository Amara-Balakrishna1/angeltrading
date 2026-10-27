"""
Unit tests for Approach B: True Simultaneous Dual Execution (Like Backtest)
Tests:
1. Mode-scoped active trade gating: has_active_trade(symbol, mode) allows simultaneous REVERSE and NO-REVERSE trades.
2. Specific token/leg square-off: exiting one trade/mode does not flatten the other mode's positions.
3. Separate cooldowns & limits: SL time and consecutive losses are isolated by (symbol_base, mode).
4. Margin precaution check: verify_margin_precaution queries rmsLimit and logs margin adequacy.
"""

import sys
from pathlib import Path
from datetime import datetime, timedelta
import unittest
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

VENV_SITE_PACKAGES = ROOT / ".venv" / "Lib" / "site-packages"
if VENV_SITE_PACKAGES.exists() and str(VENV_SITE_PACKAGES) not in sys.path:
    sys.path.insert(0, str(VENV_SITE_PACKAGES))

from live.daybreakout.executor import (
    _monitoring_state,
    has_active_trade,
    get_last_stop_loss_time,
    get_consecutive_losses,
    square_off,
    square_off_all,
    execute_daybreakout_spread,
    verify_margin_precaution,
    get_account_margin,
)
from live.daybreakout.thread_safety import flatten


class TestSimultaneousDualExecution(unittest.TestCase):

    def setUp(self):
        # Clear monitoring state and flatten coordinator before each test
        for tid in list(_monitoring_state.get_all_trades().keys()):
            _monitoring_state.remove_trade(tid)
        _monitoring_state._last_stop_loss_time.clear()
        _monitoring_state._consecutive_losses.clear()
        flatten.reset("NIFTY", mode="REVERSE")
        flatten.reset("NIFTY", mode="NO-REVERSE")

    def tearDown(self):
        for tid in list(_monitoring_state.get_all_trades().keys()):
            _monitoring_state.remove_trade(tid)
        _monitoring_state._last_stop_loss_time.clear()
        _monitoring_state._consecutive_losses.clear()

    def test_mode_scoped_trade_gating_allows_both_modes(self):
        """Verify REVERSE and NO-REVERSE can enter and hold positions at the same time."""
        # 1. Register REVERSE trade
        reverse_trade = {
            "trade_id": "NIFTY_REV_1",
            "symbol_base": "NIFTY",
            "mode": "REVERSE",
            "exchange": "NFO",
            "structure": "SPREAD",
            "buy_leg": {"symbol": "NIFTY_23400_PE", "token": "1001", "quantity": 65, "entry_price": 120.0},
            "sell_leg": {"symbol": "NIFTY_23100_PE", "token": "1002", "quantity": 65, "entry_price": 40.0},
            "max_loss": -5000.0,
            "max_profit": 50000.0,
            "profit_threshold": 3000.0,
            "min_profit_step": 2000.0,
        }
        _monitoring_state.add_trade("NIFTY_REV_1", reverse_trade)

        # REVERSE should report active trade, but NO-REVERSE should NOT be blocked
        self.assertTrue(has_active_trade("NIFTY", mode="REVERSE"))
        self.assertFalse(has_active_trade("NIFTY", mode="NO-REVERSE"))

        # 2. Register NO-REVERSE trade simultaneously
        no_reverse_trade = {
            "trade_id": "NIFTY_NOREV_1",
            "symbol_base": "NIFTY",
            "mode": "NO-REVERSE",
            "exchange": "NFO",
            "structure": "SPREAD",
            "buy_leg": {"symbol": "NIFTY_23500_CE", "token": "2001", "quantity": 65, "entry_price": 110.0},
            "sell_leg": {"symbol": "NIFTY_23800_CE", "token": "2002", "quantity": 65, "entry_price": 35.0},
            "max_loss": -5000.0,
            "max_profit": 50000.0,
            "profit_threshold": 3000.0,
            "min_profit_step": 2000.0,
        }
        _monitoring_state.add_trade("NIFTY_NOREV_1", no_reverse_trade)

        # Both modes should report active simultaneously
        self.assertTrue(has_active_trade("NIFTY", mode="REVERSE"))
        self.assertTrue(has_active_trade("NIFTY", mode="NO-REVERSE"))

        # Attempting second REVERSE trade should be blocked by REVERSE gating
        second_rev_plan = {
            "symbol_base": "NIFTY",
            "mode": "REVERSE",
            "exchange": "NFO",
            "strategy_name": "Bear Put Spread",
            "structure": "SPREAD",
            "buy_leg": {"symbol": "NIFTY_23400_PE", "token": "1001", "quantity": 65},
            "sell_leg": {"symbol": "NIFTY_23100_PE", "token": "1002", "quantity": 65},
            "lots": 1,
            "expiry": "27OCT26",
            "direction": "BEARISH",
        }
        mock_api = MagicMock()
        res = execute_daybreakout_spread(second_rev_plan, mock_api, check_existing=True, mode="REVERSE")
        self.assertEqual(res["status"], "POSITION_EXISTS")
        self.assertIn("Only one trade allowed at a time per mode", res["message"])

    def test_specific_token_square_off_isolates_modes(self):
        """Verify closing a trade only exits its specific option tokens and does not flatten the other mode."""
        reverse_trade = {
            "trade_id": "NIFTY_REV_1",
            "symbol_base": "NIFTY",
            "mode": "REVERSE",
            "exchange": "NFO",
            "structure": "SPREAD",
            "buy_leg": {"symbol": "NIFTY_23400_PE", "token": "1001", "quantity": 65, "entry_price": 120.0},
            "sell_leg": {"symbol": "NIFTY_23100_PE", "token": "1002", "quantity": 65, "entry_price": 40.0},
            "max_loss": -5000.0,
            "max_profit": 50000.0,
            "profit_threshold": 3000.0,
            "min_profit_step": 2000.0,
        }
        no_reverse_trade = {
            "trade_id": "NIFTY_NOREV_1",
            "symbol_base": "NIFTY",
            "mode": "NO-REVERSE",
            "exchange": "NFO",
            "structure": "SPREAD",
            "buy_leg": {"symbol": "NIFTY_23500_CE", "token": "2001", "quantity": 65, "entry_price": 110.0},
            "sell_leg": {"symbol": "NIFTY_23800_CE", "token": "2002", "quantity": 65, "entry_price": 35.0},
            "max_loss": -5000.0,
            "max_profit": 50000.0,
            "profit_threshold": 3000.0,
            "min_profit_step": 2000.0,
        }
        _monitoring_state.add_trade("NIFTY_REV_1", reverse_trade)
        _monitoring_state.add_trade("NIFTY_NOREV_1", no_reverse_trade)

        mock_api = MagicMock()
        mock_api.placeOrderFullResponse.return_value = {"status": True, "data": {"orderid": "EXIT_ORD_1"}}

        # Mode-scoped square_off_all for REVERSE only
        with patch("live.daybreakout.executor.square_off") as mock_sq:
            square_off_all(mock_api, symbol_base="NIFTY", mode="REVERSE")

            # Must have called square_off on REVERSE's specific legs only
            mock_sq.assert_called_once()
            called_kwargs = mock_sq.call_args[1]
            self.assertEqual(called_kwargs["trade_id"], "NIFTY_REV_1")
            self.assertEqual(called_kwargs["buy_item"]["symbol"], "NIFTY_23400_PE")
            self.assertEqual(called_kwargs["sell_item"]["symbol"], "NIFTY_23100_PE")

        # NO-REVERSE trade remains active and untouched
        self.assertIn("NIFTY_NOREV_1", _monitoring_state.get_all_trades())
        self.assertTrue(has_active_trade("NIFTY", mode="NO-REVERSE"))

    def test_separate_cooldowns_and_consecutive_losses(self):
        """Verify Thread 1's SL does not impose a cooldown or consecutive loss limit on Thread 2."""
        now = datetime.now()
        # Record stop loss for REVERSE mode only
        _monitoring_state.record_stop_loss(symbol_base="NIFTY", exit_time=now, mode="REVERSE")

        # REVERSE has SL timestamp
        rev_sl = get_last_stop_loss_time(symbol_base="NIFTY", mode="REVERSE")
        self.assertEqual(rev_sl, now)

        # NO-REVERSE has NO SL timestamp and is NOT on cooldown
        norev_sl = get_last_stop_loss_time(symbol_base="NIFTY", mode="NO-REVERSE")
        self.assertIsNone(norev_sl)

        # Check consecutive losses isolation
        self.assertEqual(get_consecutive_losses("NIFTY", mode="REVERSE"), 1)
        self.assertEqual(get_consecutive_losses("NIFTY", mode="NO-REVERSE"), 0)

        # Add 2 more consecutive losses to REVERSE (hitting max 3)
        _monitoring_state.record_stop_loss(symbol_base="NIFTY", exit_time=now, mode="REVERSE")
        _monitoring_state.record_stop_loss(symbol_base="NIFTY", exit_time=now, mode="REVERSE")
        self.assertEqual(get_consecutive_losses("NIFTY", mode="REVERSE"), 3)
        self.assertEqual(get_consecutive_losses("NIFTY", mode="NO-REVERSE"), 0)

        # Thread 2 (NO-REVERSE) can proceed without being blocked by Thread 1's consecutive losses
        no_rev_plan = {
            "symbol_base": "NIFTY",
            "mode": "NO-REVERSE",
            "exchange": "NFO",
            "strategy_name": "Bull Call Spread",
            "structure": "SPREAD",
            "buy_leg": {"symbol": "NIFTY_23500_CE", "token": "2001", "strike": 23500, "option_type": "CE", "entry_price": 100.0, "quantity": 25},
            "sell_leg": {"symbol": "NIFTY_23800_CE", "token": "2002", "strike": 23800, "option_type": "CE", "entry_price": 30.0, "quantity": 25},
            "lots": 1,
            "expiry": "27OCT26",
            "direction": "BULLISH",
        }
        mock_api = MagicMock()
        with patch("live.daybreakout.executor.get_daily_trade_stats", return_value=(0, 0.0)), \
             patch("live.daybreakout.executor.get_daily_profit_stats", return_value=0.0), \
             patch("live.daybreakout.executor.get_open_positions", return_value=[]), \
             patch("live.daybreakout.executor.place_order_with_validation", return_value={"success": True, "execution_price": 100.0, "order_id": "ORD_NO_REV"}), \
             patch("live.daybreakout.executor.verify_position_state", return_value=(True, [], [])), \
             patch("live.daybreakout.executor._monitoring_state.ensure_monitor_running"):
            res = execute_daybreakout_spread(no_rev_plan, mock_api, check_existing=False, mode="NO-REVERSE")
            self.assertEqual(res["status"], "SUCCESS")

    def test_margin_precaution_check(self):
        """Verify Angel One account margin verification via rmsLimit."""
        mock_api = MagicMock()
        mock_api.rmsLimit.return_value = {
            "status": True,
            "message": "SUCCESS",
            "errorcode": "",
            "data": {
                "net": "150000.50",
                "availablecash": "120000.00",
                "collateral": "30000.50",
            }
        }

        margin_data = get_account_margin(mock_api)
        self.assertTrue(margin_data["success"])
        self.assertEqual(margin_data["net"], 150000.50)
        self.assertEqual(margin_data["available_cash"], 120000.00)

        # verify_margin_precaution returns True
        res = verify_margin_precaution(mock_api, lots=10, structure="spread", symbol_base="NIFTY")
        self.assertTrue(res)


if __name__ == "__main__":
    unittest.main()
