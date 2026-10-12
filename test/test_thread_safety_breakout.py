import unittest
from unittest.mock import MagicMock, patch
import threading
import time
from datetime import datetime

from live.daybreakout.thread_safety import FlattenCoordinator, smart_api_lock
from live.daybreakout.executor import (
    MonitoringStateManager,
    get_ltp,
    square_off,
    square_off_all,
)
from live.daybreakout.strategy import _start_eod_watchdog


class TestThreadSafetyAndCoordination(unittest.TestCase):
    def test_flatten_coordinator_lifecycle(self):
        coordinator = FlattenCoordinator()
        symbol = "NIFTY"
        self.assertFalse(coordinator.is_completed(symbol))

        coordinator.mark_completed(symbol)
        self.assertTrue(coordinator.is_completed(symbol))

        # Test reset when a new trade is opened
        coordinator.reset(symbol)
        self.assertFalse(coordinator.is_completed(symbol))

    def test_monitoring_state_consecutive_losses(self):
        mgr = MonitoringStateManager()
        self.assertEqual(mgr.get_consecutive_losses("NIFTY"), 0)

        # Record stop loss
        mgr.record_stop_loss("NIFTY")
        self.assertEqual(mgr.get_consecutive_losses("NIFTY"), 1)

        mgr.record_stop_loss("NIFTY")
        self.assertEqual(mgr.get_consecutive_losses("NIFTY"), 2)

        # Record profit exit
        mgr.record_profit_exit("NIFTY")
        self.assertEqual(mgr.get_consecutive_losses("NIFTY"), 0)

    def test_get_ltp_lock_acquisition_and_timeout(self):
        mock_api = MagicMock()
        mock_api.ltpData.return_value = {"status": True, "data": {"ltp": "24500.50"}}

        # Basic successful LTP fetch
        ltp = get_ltp(mock_api, "12345", "NIFTY24OCTFUT", exchange="NFO", timeout=1.0)
        self.assertEqual(ltp, 24500.50)

        # Test lock busy handling: acquire smart_api_lock from another thread
        def hold_lock():
            with smart_api_lock:
                time.sleep(0.4)

        t = threading.Thread(target=hold_lock)
        t.start()
        time.sleep(0.05)

        # get_ltp should wait up to timeout, acquire after hold_lock finishes, and return
        ltp = get_ltp(mock_api, "12345", "NIFTY24OCTFUT", exchange="NFO", timeout=1.0)
        self.assertEqual(ltp, 24500.50)
        t.join()

    def test_watchdog_clean_stop_event(self):
        mock_api = MagicMock()
        stop_event = threading.Event()

        # Start watchdog with eod_hhmm far in the future
        t = _start_eod_watchdog(
            smart_api=mock_api,
            inst_name="NIFTY",
            eod_hhmm="23:59",
            stop_event=stop_event,
        )
        self.assertTrue(t.is_alive())

        # Signal stop
        stop_event.set()
        t.join(timeout=2.0)
        self.assertFalse(t.is_alive(), "Watchdog should have cleanly exited upon stop_event")

    def test_square_off_order_failure_returns_false(self):
        mock_api = MagicMock()
        # Mock placeOrder to fail
        mock_api.placeOrder.return_value = {"status": False, "message": "Insufficient margin"}

        buy_leg = {"symbol": "NIFTY24OCT24500CE", "token": "123", "quantity": 50}
        sell_leg = {"symbol": "NIFTY24OCT24800CE", "token": "124", "quantity": 50}

        success = square_off(mock_api, buy_leg, sell_leg, exchange="NFO")
        self.assertFalse(success, "square_off should report failure if orders failed")


if __name__ == "__main__":
    unittest.main()
