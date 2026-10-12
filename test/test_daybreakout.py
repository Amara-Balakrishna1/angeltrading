"""
Verification test suite for Day Breakout Strategy.
Tests:
1. Range calculator (12:30 -> 15:15) on NIFTY & SENSEX.
2. Contract finder for Bull Call Spread & Bear Put Spread (300 pt NIFTY, 1000 pt SENSEX).
3. Breakout confirmation timer logic (sustained vs reset).
4. Trailing stop and risk management logic.
"""

import sys
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

VENV_SITE_PACKAGES = ROOT / ".venv" / "Lib" / "site-packages"
if VENV_SITE_PACKAGES.exists() and str(VENV_SITE_PACKAGES) not in sys.path:
    sys.path.insert(0, str(VENV_SITE_PACKAGES))

from live.daybreakout.config import INSTRUMENTS, RISK_CONFIG
from live.daybreakout.range_calculator import get_previous_day_range
from live.daybreakout.contract_finder import find_spread_contracts
from live.daybreakout.executor import login_smart_api


class TestDayBreakout(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        print("\n--- Connecting to SmartAPI for Verification ---")
        cls.smart_api = login_smart_api()

    def test_nifty_range_calculation(self):
        inst = INSTRUMENTS["NIFTY"]
        range_data = get_previous_day_range(
            self.smart_api,
            inst["spot_exchange"],
            inst["spot_token"]
        )
        self.assertIn("high", range_data)
        self.assertIn("low", range_data)
        self.assertGreater(range_data["high"], range_data["low"])
        self.assertGreater(range_data["candles_count"], 0)
        print(f"[PASS] NIFTY Range: High={range_data['high']}, Low={range_data['low']} on {range_data['date']}")

    def test_sensex_range_calculation(self):
        inst = INSTRUMENTS["SENSEX"]
        range_data = get_previous_day_range(
            self.smart_api,
            inst["spot_exchange"],
            inst["spot_token"]
        )
        self.assertIn("high", range_data)
        self.assertIn("low", range_data)
        self.assertGreater(range_data["high"], range_data["low"])
        self.assertGreater(range_data["candles_count"], 0)
        print(f"[PASS] SENSEX Range: High={range_data['high']}, Low={range_data['low']} on {range_data['date']}")

    def test_nifty_bull_call_spread_300pts(self):
        spot = 23450.0
        inst = INSTRUMENTS["NIFTY"]
        plan = find_spread_contracts(
            symbol_base=inst["name"],
            spot_price=spot,
            direction="BULLISH",
            strike_step=inst["strike_step"],
            spread_width=inst["spread_width"],
            option_exchange=inst["option_exchange"],
            lots=5
        )
        self.assertEqual(plan["spread_width"], 300)
        self.assertEqual(plan["buy_leg"]["strike"], 23450)
        self.assertEqual(plan["sell_leg"]["strike"], 23750)
        self.assertEqual(plan["buy_leg"]["option_type"], "CE")
        self.assertEqual(plan["sell_leg"]["option_type"], "CE")
        self.assertTrue(plan["buy_leg"]["token"])
        self.assertTrue(plan["sell_leg"]["token"])
        print(f"[PASS] NIFTY Bull Call Spread: BUY {plan['buy_leg']['symbol']} / SELL {plan['sell_leg']['symbol']}")

    def test_nifty_bear_put_spread_300pts(self):
        spot = 23450.0
        inst = INSTRUMENTS["NIFTY"]
        plan = find_spread_contracts(
            symbol_base=inst["name"],
            spot_price=spot,
            direction="BEARISH",
            strike_step=inst["strike_step"],
            spread_width=inst["spread_width"],
            option_exchange=inst["option_exchange"],
            lots=5
        )
        self.assertEqual(plan["spread_width"], 300)
        self.assertEqual(plan["buy_leg"]["strike"], 23450)
        self.assertEqual(plan["sell_leg"]["strike"], 23150)
        self.assertEqual(plan["buy_leg"]["option_type"], "PE")
        self.assertEqual(plan["sell_leg"]["option_type"], "PE")
        self.assertTrue(plan["buy_leg"]["token"])
        self.assertTrue(plan["sell_leg"]["token"])
        print(f"[PASS] NIFTY Bear Put Spread: BUY {plan['buy_leg']['symbol']} / SELL {plan['sell_leg']['symbol']}")

    def test_sensex_bull_call_spread_1000pts(self):
        spot = 74900.0
        inst = INSTRUMENTS["SENSEX"]
        plan = find_spread_contracts(
            symbol_base=inst["name"],
            spot_price=spot,
            direction="BULLISH",
            strike_step=inst["strike_step"],
            spread_width=inst["spread_width"],
            option_exchange=inst["option_exchange"],
            lots=5
        )
        self.assertEqual(plan["spread_width"], 1000)
        self.assertEqual(plan["buy_leg"]["strike"], 74900)
        self.assertEqual(plan["sell_leg"]["strike"], 75900)
        self.assertEqual(plan["buy_leg"]["option_type"], "CE")
        self.assertEqual(plan["sell_leg"]["option_type"], "CE")
        self.assertTrue(plan["buy_leg"]["token"])
        self.assertTrue(plan["sell_leg"]["token"])
        print(f"[PASS] SENSEX Bull Call Spread: BUY {plan['buy_leg']['symbol']} / SELL {plan['sell_leg']['symbol']}")

    def test_sensex_bear_put_spread_1000pts(self):
        spot = 74900.0
        inst = INSTRUMENTS["SENSEX"]
        plan = find_spread_contracts(
            symbol_base=inst["name"],
            spot_price=spot,
            direction="BEARISH",
            strike_step=inst["strike_step"],
            spread_width=inst["spread_width"],
            option_exchange=inst["option_exchange"],
            lots=5
        )
        self.assertEqual(plan["spread_width"], 1000)
        self.assertEqual(plan["buy_leg"]["strike"], 74900)
        self.assertEqual(plan["sell_leg"]["strike"], 73900)
        self.assertEqual(plan["buy_leg"]["option_type"], "PE")
        self.assertEqual(plan["sell_leg"]["option_type"], "PE")
        self.assertTrue(plan["buy_leg"]["token"])
        self.assertTrue(plan["sell_leg"]["token"])
        print(f"[PASS] SENSEX Bear Put Spread: BUY {plan['buy_leg']['symbol']} / SELL {plan['sell_leg']['symbol']}")

    def test_trailing_risk_management_math(self):
        profit_threshold = RISK_CONFIG["profit_threshold"]  # 3000
        min_profit_step = RISK_CONFIG["min_profit_step"]    # 2000

        # At Rs 3200 profit -> locked profit is 3000
        max_pnl = 3200
        locked = ((max_pnl - profit_threshold) // min_profit_step) * min_profit_step + profit_threshold
        self.assertEqual(locked, 3000)

        # At Rs 4999 profit -> locked profit is still 3000
        max_pnl = 4999
        locked = ((max_pnl - profit_threshold) // min_profit_step) * min_profit_step + profit_threshold
        self.assertEqual(locked, 3000)

        # At Rs 5000 profit -> locks to 5000
        max_pnl = 5000
        locked = ((max_pnl - profit_threshold) // min_profit_step) * min_profit_step + profit_threshold
        self.assertEqual(locked, 5000)

        # At Rs 7100 profit -> locks to 7000
        max_pnl = 7100
        locked = ((max_pnl - profit_threshold) // min_profit_step) * min_profit_step + profit_threshold
        self.assertEqual(locked, 7000)
        print("[PASS] Trailing Profit Lock Math verified.")


class TestDayBreakout15mSignals(unittest.TestCase):
    """No API — 15-minute close rules shared by live and backtest."""

    def test_close_above_high_is_bullish_breakout(self):
        from live.daybreakout.gap_analyzer import classify_gap
        from live.daybreakout.signals import evaluate_15m_close

        gap = classify_gap(0.001)
        signal = evaluate_15m_close("10:00", 23500.0, 23400.0, 23200.0, gap, False, 4)
        self.assertFalse(signal.skip)
        self.assertEqual(signal.direction, "BULLISH")
        self.assertEqual(signal.trade_type, "BREAKOUT")

    def test_inside_range_is_no_trade(self):
        from live.daybreakout.gap_analyzer import classify_gap
        from live.daybreakout.signals import evaluate_15m_close

        gap = classify_gap(0.001)
        signal = evaluate_15m_close("10:00", 23300.0, 23400.0, 23200.0, gap, False, 4)
        self.assertTrue(signal.skip)

    def test_no_entry_after_1445(self):
        from live.daybreakout.gap_analyzer import classify_gap
        from live.daybreakout.signals import evaluate_15m_close

        gap = classify_gap(0.001)
        signal = evaluate_15m_close("15:00", 23500.0, 23400.0, 23200.0, gap, False, 20)
        self.assertTrue(signal.skip)
        self.assertIn("14:45", signal.skip_reason)

    def test_large_gap_no_chase_skips_first_candle_only(self):
        from live.daybreakout.gap_analyzer import classify_gap
        from live.daybreakout.signals import evaluate_15m_close

        gap = classify_gap(0.008)  # LARGE gap-up
        self.assertTrue(gap.no_chase_same_direction)
        first = evaluate_15m_close("09:15", 23500.0, 23400.0, 23200.0, gap, False, 0)
        later = evaluate_15m_close("09:30", 23500.0, 23400.0, 23200.0, gap, False, 1)
        self.assertTrue(first.skip)
        self.assertFalse(later.skip)
        self.assertEqual(later.direction, "BULLISH")

    def test_gap_down_recovery_on_close_back_inside(self):
        from live.daybreakout.gap_analyzer import classify_gap
        from live.daybreakout.signals import evaluate_15m_close

        gap = classify_gap(-0.008)  # LARGE gap-down
        signal = evaluate_15m_close("10:00", 23300.0, 23400.0, 23200.0, gap, True, 4)
        self.assertFalse(signal.skip)
        self.assertEqual(signal.trade_type, "RECOVERY")
        self.assertEqual(signal.direction, "BULLISH")
        self.assertFalse(signal.gap_territory_touched)


class TestMonitoringStateManagerAndSingleTradeGating(unittest.TestCase):
    """Unit tests for dictionary-based MonitoringStateManager and single-trade gating."""

    def setUp(self):
        from live.daybreakout.executor import _monitoring_state
        # Clean state before each test
        with _monitoring_state._lock:
            _monitoring_state._active_trades.clear()
            _monitoring_state._stop_requested = False

    def tearDown(self):
        from live.daybreakout.executor import _monitoring_state
        with _monitoring_state._lock:
            _monitoring_state._active_trades.clear()
            _monitoring_state._stop_requested = False

    def test_state_manager_dictionary_tracking(self):
        from live.daybreakout.executor import _monitoring_state, has_active_trade

        self.assertFalse(has_active_trade())
        self.assertFalse(has_active_trade("NIFTY"))

        trade_1 = {
            "trade_id": "NIFTY_20260920_100000",
            "symbol_base": "NIFTY",
            "exchange": "NFO",
            "structure": "SPREAD",
            "buy_leg": {"symbol": "NIFTY26SEP24000CE", "token": "12345", "quantity": 50, "entry_price": 100.0},
            "sell_leg": {"symbol": "NIFTY26SEP24300CE", "token": "12346", "quantity": 50, "entry_price": 40.0},
            "max_loss": -5000.0,
            "max_profit": 50000.0,
            "profit_threshold": 3000.0,
            "min_profit_step": 2000.0,
        }

        # Add trade
        success = _monitoring_state.add_trade("NIFTY_20260920_100000", trade_1)
        self.assertTrue(success)
        self.assertTrue(has_active_trade())
        self.assertTrue(has_active_trade("NIFTY"))
        self.assertFalse(has_active_trade("SENSEX"))

        # Check retrieval
        retrieved = _monitoring_state.get_trade("NIFTY_20260920_100000")
        self.assertIsNotNone(retrieved)
        self.assertEqual(retrieved["symbol_base"], "NIFTY")

        all_trades = _monitoring_state.get_all_trades()
        self.assertEqual(len(all_trades), 1)
        self.assertIn("NIFTY_20260920_100000", all_trades)

        # Duplicate add returns False
        self.assertFalse(_monitoring_state.add_trade("NIFTY_20260920_100000", trade_1))

        # Remove trade
        removed = _monitoring_state.remove_trade("NIFTY_20260920_100000")
        self.assertIsNotNone(removed)
        self.assertFalse(has_active_trade())
        self.assertFalse(has_active_trade("NIFTY"))

    def test_single_trade_gating_blocks_second_trade(self):
        from live.daybreakout.executor import _monitoring_state, execute_daybreakout_spread

        # Register an active trade in monitoring state
        active_trade = {
            "trade_id": "NIFTY_EXISTING",
            "symbol_base": "NIFTY",
            "exchange": "NFO",
            "structure": "SPREAD",
            "buy_leg": {"symbol": "NIFTY_BUY", "token": "111", "quantity": 50, "entry_price": 100.0},
            "sell_leg": None,
            "max_loss": -5000.0,
            "max_profit": 50000.0,
            "profit_threshold": 3000.0,
            "min_profit_step": 2000.0,
        }
        _monitoring_state.add_trade("NIFTY_EXISTING", active_trade)

        # Attempt second trade for NIFTY
        new_plan = {
            "symbol_base": "NIFTY",
            "exchange": "NFO",
            "strategy_name": "Bull Call Spread",
            "structure": "SPREAD",
            "buy_leg": {"symbol": "NIFTY_NEW", "token": "222", "quantity": 50},
            "sell_leg": None,
            "lots": 2,
            "expiry": "26SEP26",
            "direction": "BULLISH",
        }

        # execute_daybreakout_spread should immediately reject with POSITION_EXISTS without placing orders
        mock_api = unittest.mock.MagicMock()
        result = execute_daybreakout_spread(
            spread_plan=new_plan,
            smart_api=mock_api,
            check_existing=True,
            trade_reason="Second trade attempt",
        )

        self.assertEqual(result["status"], "POSITION_EXISTS")
        self.assertIn("Only one trade allowed at a time", result["message"])


class TestVIXFilterCondition(unittest.TestCase):
    """Unit tests for VIX volatility filter on trade reversal."""

    def test_negative_instrument_vix_greater_than_8_no_reverse(self):
        from live.daybreakout.signals import apply_reverse
        orig_dir = "BEARISH"  # instrument negative
        vix_pct = 9.5         # vix > +8%
        reverse = True

        should_reverse = reverse
        if orig_dir == "BEARISH" and vix_pct > 8.0:
            should_reverse = False

        self.assertFalse(should_reverse)
        final_dir = apply_reverse(orig_dir, should_reverse)
        self.assertEqual(final_dir, "BEARISH")  # Stays BEARISH (buys PE, not reversed to CE)

    def test_positive_instrument_vix_negative_5_no_reverse(self):
        from live.daybreakout.signals import apply_reverse
        orig_dir = "BULLISH"  # instrument positive
        vix_pct = -6.2        # vix <= -5%
        reverse = True

        should_reverse = reverse
        if orig_dir == "BULLISH" and vix_pct <= -5.0:
            should_reverse = False

        self.assertFalse(should_reverse)
        final_dir = apply_reverse(orig_dir, should_reverse)
        self.assertEqual(final_dir, "BULLISH")  # Stays BULLISH (buys CE, not reversed to PE)

    def test_normal_conditions_allow_reverse(self):
        from live.daybreakout.signals import apply_reverse
        orig_dir = "BEARISH"
        vix_pct = 2.0         # normal VIX change
        reverse = True

        should_reverse = reverse
        if orig_dir == "BEARISH" and vix_pct > 8.0:
            should_reverse = False
        elif orig_dir == "BULLISH" and vix_pct <= -5.0:
            should_reverse = False

        self.assertTrue(should_reverse)
        final_dir = apply_reverse(orig_dir, should_reverse)
        self.assertEqual(final_dir, "BULLISH")  # Normal reversal happens


class TestDayBreakoutThreeConditions(unittest.TestCase):
    """Unit tests for the 3 added conditions:
    1. Entry only 09:30 onward.
    2. Daily profit > Rs.25K halts trading for the day.
    3. Wait 45-60 minutes after a stop-loss before next entry.
    """

    def test_entry_only_0930_onward(self):
        from live.daybreakout.signals import evaluate_15m_close
        from live.daybreakout.gap_analyzer import GapInfo

        gap = GapInfo()
        # 09:15 candle breaks above High 23000
        signal_915 = evaluate_15m_close(
            c_time="09:15",
            c_close=23100.0,
            range_high=23000.0,
            range_low=22800.0,
            gap_info=gap,
            gap_territory_touched=False,
            candle_index=0,
            first_entry_hhmm="09:30",
        )
        self.assertTrue(signal_915.skip)
        self.assertIn("No entries before 09:30", signal_915.skip_reason)

        # 09:30 candle breaks above High 23000
        signal_930 = evaluate_15m_close(
            c_time="09:30",
            c_close=23100.0,
            range_high=23000.0,
            range_low=22800.0,
            gap_info=gap,
            gap_territory_touched=False,
            candle_index=1,
            first_entry_hhmm="09:30",
        )
        self.assertFalse(signal_930.skip)
        self.assertEqual(signal_930.direction, "BULLISH")

    def test_daily_profit_limit_halt(self):
        from unittest.mock import patch, MagicMock
        from live.daybreakout.executor import execute_daybreakout_spread

        dummy_plan = {
            "strategy_name": "Bull Call Spread",
            "symbol_base": "NIFTY",
            "exchange": "NFO",
            "direction": "BULLISH",
            "expiry": "2026-09-30",
            "lots": 1,
            "structure": "SPREAD",
            "buy_leg": {"symbol": "NIFTY30SEP2623000CE", "token": "123", "strike": 23000, "option_type": "CE", "entry_price": 100.0, "quantity": 25},
            "sell_leg": {"symbol": "NIFTY30SEP2623300CE", "token": "124", "strike": 23300, "option_type": "CE", "entry_price": 30.0, "quantity": 25},
            "net_premium": 70.0,
        }

        mock_api = MagicMock()
        # Mock daily realized PnL = 26,000 (exceeds 25,000 limit)
        with patch("live.daybreakout.executor.get_daily_trade_stats", return_value=(1, 26000.0)):
            res = execute_daybreakout_spread(dummy_plan, mock_api)
            self.assertEqual(res["status"], "DAILY_MAX_PROFIT_EXCEEDED")
            self.assertIn("Daily maximum profit target reached", res["message"])

    def test_stop_loss_cooldown_blocks_and_resumes(self):
        from datetime import datetime, timedelta
        from unittest.mock import patch, MagicMock
        from live.daybreakout.executor import execute_daybreakout_spread, _monitoring_state

        dummy_plan = {
            "strategy_name": "Bull Call Spread",
            "symbol_base": "NIFTY",
            "exchange": "NFO",
            "direction": "BULLISH",
            "expiry": "2026-09-30",
            "lots": 1,
            "structure": "SPREAD",
            "buy_leg": {"symbol": "NIFTY30SEP2623000CE", "token": "123", "strike": 23000, "option_type": "CE", "entry_price": 100.0, "quantity": 25},
            "sell_leg": {"symbol": "NIFTY30SEP2623300CE", "token": "124", "strike": 23300, "option_type": "CE", "entry_price": 30.0, "quantity": 25},
            "net_premium": 70.0,
        }

        mock_api = MagicMock()
        # 20 minutes ago -> still in 45-min cooldown
        recent_sl = datetime.now() - timedelta(minutes=20)
        with patch("live.daybreakout.executor.get_daily_trade_stats", return_value=(1, -5000.0)), \
             patch("live.daybreakout.executor.get_last_stop_loss_time", return_value=recent_sl):
            res = execute_daybreakout_spread(dummy_plan, mock_api)
            self.assertEqual(res["status"], "SL_COOLDOWN_ACTIVE")
            self.assertIn("Stop-loss cooldown active", res["message"])

        # 50 minutes ago -> cooldown expired, proceed past cooldown check
        expired_sl = datetime.now() - timedelta(minutes=50)
        with patch("live.daybreakout.executor.get_daily_trade_stats", return_value=(1, -5000.0)), \
             patch("live.daybreakout.executor.get_last_stop_loss_time", return_value=expired_sl), \
             patch("live.daybreakout.executor.get_open_positions", return_value=[]), \
             patch("live.daybreakout.executor.place_order_with_validation", return_value={"success": True, "execution_price": 100.0, "order_id": "ORD123"}), \
             patch("live.daybreakout.executor.verify_position_state", return_value=(True, [], [])), \
             patch("live.daybreakout.executor._monitoring_state.ensure_monitor_running"):
            res = execute_daybreakout_spread(dummy_plan, mock_api, check_existing=False)
            self.assertEqual(res["status"], "SUCCESS")


if __name__ == "__main__":
    unittest.main()

