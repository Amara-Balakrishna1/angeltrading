"""
Test script for dual-mode tracking functionality.
Verifies that REVERSE and NO-REVERSE modes track trades separately.
"""

import sys
import csv
from pathlib import Path
from datetime import datetime
import shutil

# Add parent directory to path
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Directly import tracking functions without going through __init__.py
sys.path.insert(0, str(ROOT / "live" / "daybreakout"))

import tracking_dual
from tracking_dual import (
    record_triggered_trade_dual,
    get_daily_trade_stats_dual,
    get_daily_profit_stats_dual,
    print_dual_mode_summary,
)

# Simple logger for test output
class TestLogger:
    @staticmethod
    def info(msg):
        print(f"[INFO] {msg}")

# Inject logger into tracking module
tracking_dual.logger = TestLogger()


def test_dual_tracking():
    """Test dual-mode tracking with simulated trades."""
    print("=" * 80)
    print("  DUAL-MODE TRACKING TEST")
    print("=" * 80)

    symbol = "NIFTY"

    # Clear existing test data for today
    now_dt = datetime.now()
    date_str = now_dt.strftime("%Y-%m-%d")
    data_dir = Path(__file__).resolve().parent.parent.parent / "data" / "trades" / date_str

    if data_dir.exists():
        # Delete the entire date folder to ensure clean test
        shutil.rmtree(data_dir)
        print(f"[*] Cleared existing test data directory: {data_dir}")

    # Recreate directory
    data_dir.mkdir(parents=True, exist_ok=True)

    # Record some test trades for REVERSE mode
    print("\n[*] Recording test trades for REVERSE mode...")
    record_triggered_trade_dual(
        symbol_base=symbol,
        structure="SPREAD",
        lots=10,
        quantity=650,
        trade_reason="Test REVERSE trade 1",
        buy_symbol="NIFTY 23000 CE",
        sell_symbol="NIFTY 23300 CE",
        mode="REVERSE",
        paper_mode="PAPER",
    )

    record_triggered_trade_dual(
        symbol_base=symbol,
        structure="SPREAD",
        lots=10,
        quantity=650,
        trade_reason="Test REVERSE trade 2",
        buy_symbol="NIFTY 22700 PE",
        sell_symbol="NIFTY 22400 PE",
        mode="REVERSE",
        paper_mode="PAPER",
    )

    # Record some test trades for NO-REVERSE mode
    print("[*] Recording test trades for NO-REVERSE mode...")
    record_triggered_trade_dual(
        symbol_base=symbol,
        structure="NAKED",
        lots=10,
        quantity=650,
        trade_reason="Test NO-REVERSE trade 1",
        buy_symbol="NIFTY 23100 CE",
        sell_symbol="N/A",
        mode="NO-REVERSE",
        paper_mode="PAPER",
    )

    record_triggered_trade_dual(
        symbol_base=symbol,
        structure="NAKED",
        lots=10,
        quantity=650,
        trade_reason="Test NO-REVERSE trade 2",
        buy_symbol="NIFTY 22800 PE",
        sell_symbol="N/A",
        mode="NO-REVERSE",
        paper_mode="PAPER",
    )

    record_triggered_trade_dual(
        symbol_base=symbol,
        structure="NAKED",
        lots=10,
        quantity=650,
        trade_reason="Test NO-REVERSE trade 3",
        buy_symbol="NIFTY 23200 CE",
        sell_symbol="N/A",
        mode="NO-REVERSE",
        paper_mode="PAPER",
    )

    # Get statistics for each mode
    print("\n[*] Retrieving statistics for each mode...")
    reverse_trades, reverse_loss = get_daily_trade_stats_dual(symbol, mode="REVERSE")
    reverse_profit = get_daily_profit_stats_dual(symbol, mode="REVERSE")

    no_reverse_trades, no_reverse_loss = get_daily_trade_stats_dual(symbol, mode="NO-REVERSE")
    no_reverse_profit = get_daily_profit_stats_dual(symbol, mode="NO-REVERSE")

    print("\n" + "-" * 80)
    print("  TEST RESULTS")
    print("-" * 80)
    print(f"  REVERSE Mode:")
    print(f"    Total Trades (Triggered + Completed): {reverse_trades}")
    print(f"    Net P&L: Rs. {reverse_profit:+.2f}")
    print(f"    Cumulative Loss: Rs. {reverse_loss:+.2f}")
    print("-" * 80)
    print(f"  NO-REVERSE Mode:")
    print(f"    Total Trades (Triggered + Completed): {no_reverse_trades}")
    print(f"    Net P&L: Rs. {no_reverse_profit:+.2f}")
    print(f"    Cumulative Loss: Rs. {no_reverse_loss:+.2f}")
    print("-" * 80)

    # Verify separation (we only recorded triggered trades, so should be 2 and 3)
    if reverse_trades == 2 and no_reverse_trades == 3:
        print("\n[+] TEST PASSED: Modes are tracking separately!")
        print(f"    REVERSE: {reverse_trades} trades")
        print(f"    NO-REVERSE: {no_reverse_trades} trades")
    else:
        print("\n[!] TEST FAILED: Modes are not tracking correctly!")
        print(f"    Expected REVERSE: 2 trades, Got: {reverse_trades}")
        print(f"    Expected NO-REVERSE: 3 trades, Got: {no_reverse_trades}")

    # Print full summary
    print("\n[*] Full Dual-Mode Summary:")
    print_dual_mode_summary(symbol)

    print("\n[*] Test completed. Check data/trades/<date>/ for generated CSV files.")
    print("=" * 80)


if __name__ == "__main__":
    test_dual_tracking()
