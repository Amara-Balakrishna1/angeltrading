"""
Dual-Mode Trade Tracking for Day Breakout Strategy.
Supports separate tracking for REVERSE and NO-REVERSE strategy instances.
"""

import csv
from pathlib import Path
from datetime import datetime
from typing import Dict, Optional, Tuple

try:
    from live.daybreakout.config import DATA_DIR
    from live.daybreakout.logger import logger
except ImportError:
    # Fallback for standalone testing
    DATA_DIR = Path(__file__).resolve().parent.parent.parent / "data"
    logger = None


def get_date_trades_file(date: datetime, symbol_base: str = "NIFTY", mode: str = "REVERSE") -> Path:
    """
    Get the daily trades file path for a specific mode (REVERSE or NO-REVERSE).
    """
    date_str = date.strftime("%Y-%m-%d")
    date_folder = DATA_DIR / "trades" / date_str
    date_folder.mkdir(parents=True, exist_ok=True)
    suffix = f"{symbol_base}_{mode.lower()}"
    return date_folder / f"trades_{suffix}.csv"


def get_date_triggered_trades_file(date: datetime, symbol_base: str = "NIFTY", mode: str = "REVERSE") -> Path:
    """
    Get the daily triggered trades file path for a specific mode.
    """
    date_str = date.strftime("%Y-%m-%d")
    date_folder = DATA_DIR / "trades" / date_str
    date_folder.mkdir(parents=True, exist_ok=True)
    suffix = f"{symbol_base}_{mode.lower()}"
    return date_folder / f"triggered_trades_{suffix}.csv"


def record_triggered_trade_dual(
    symbol_base: str = "NIFTY",
    structure: str = "SPREAD",
    lots: int = 1,
    quantity: int = 0,
    trade_reason: str = "",
    buy_symbol: str = "",
    sell_symbol: str = "",
    mode: str = "REVERSE",
    paper_mode: str = "LIVE",
):
    """
    Record a triggered trade with mode-specific tracking (REVERSE or NO-REVERSE).
    """
    now_dt = datetime.now()
    date_str = now_dt.strftime("%Y-%m-%d")
    time_str = now_dt.strftime("%H:%M:%S")

    daily_file = get_date_triggered_trades_file(now_dt, symbol_base=symbol_base, mode=mode)

    headers = ["date", "time", "symbol", "structure", "lots", "quantity", "trade_reason", "buy_symbol", "sell_symbol", "mode", "strategy_mode"]
    row = [date_str, time_str, symbol_base, str(structure).upper(), str(lots), str(quantity), trade_reason, buy_symbol, sell_symbol, paper_mode, mode]

    for target_file in (daily_file,):
        file_exists = target_file.exists()
        with open(target_file, "a", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            if not file_exists:
                writer.writerow(headers)
            writer.writerow(row)

    if logger:
        logger.info(f"Recorded triggered trade - Symbol: {symbol_base}, Mode: {mode}, Structure: {structure}, Reason: {trade_reason}")


def get_daily_trade_stats_dual(
    symbol_base: str = "NIFTY",
    mode: str = "REVERSE",
    for_date: Optional[str] = None,
) -> Tuple[int, float]:
    """
    Get daily trade statistics for a specific mode (REVERSE or NO-REVERSE).
    Returns (trade_count, cumulative_loss).
    Counts both triggered and completed trades.
    """
    now_dt = datetime.now()
    for_date_str = for_date if isinstance(for_date, str) else now_dt.strftime("%Y-%m-%d")

    # Live execution currently writes the shared triggered file under
    # data/<DD-MM-YYYY>; prefer the mode-specific file when one exists, then
    # fall back to the shared file and filter by its mode column.
    triggered_file = get_date_triggered_trades_file(now_dt, symbol_base=symbol_base, mode=mode)
    legacy_triggered_file = DATA_DIR / now_dt.strftime("%d-%m-%Y") / f"triggered_trades_{symbol_base}.csv"
    if not triggered_file.exists() and legacy_triggered_file.exists():
        triggered_file = legacy_triggered_file
    triggered_count = 0

    if triggered_file.exists():
        with open(triggered_file, "r", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                row_date = (row.get("date") or "").strip()
                if row_date and row_date != for_date_str:
                    continue
                row_mode = (row.get("mode") or row.get("strategy_mode") or "").strip().upper()
                if row_mode and row_mode != mode.strip().upper():
                    continue
                # A shared file without a mode field cannot be attributed safely
                # to either strategy mode.
                if not row_mode and triggered_file == legacy_triggered_file:
                    continue
                triggered_count += 1

    # Count completed trades and calculate P&L
    daily_file = get_date_trades_file(now_dt, symbol_base=symbol_base, mode=mode)

    completed_count = 0
    total_pnl = 0.0

    if daily_file.exists():
        with open(daily_file, "r", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                # Check if row has a status column and if it indicates a completed trade
                # If no status column exists (backward compatibility), count all rows with valid P&L
                status = row.get("status", "").strip().upper()
                is_completed = (
                    status in ("FILLED", "EXIT_EOD", "EXIT_EOD_SQUARE_OFF", "EXIT_MAX_LOSS",
                              "EXIT_MAX_PROFIT", "EXIT_TRAILING_PROFIT", "EXIT_DAILY_MAX_LOSS",
                              "MANUAL_EXIT", "COMPLETED") or not status
                )
                if is_completed:
                    completed_count += 1
                    try:
                        pnl = float(row.get("pnl", 0))
                        if pnl < 0:
                            total_pnl += pnl
                    except (ValueError, TypeError):
                        pass

    # Triggered rows and their later completion rows describe the same trades.
    return max(triggered_count, completed_count), total_pnl


def get_daily_profit_stats_dual(
    symbol_base: str = "NIFTY",
    mode: str = "REVERSE",
    for_date: Optional[str] = None,
) -> float:
    """
    Get daily net profit for a specific mode (from completed trades only).
    """
    now_dt = datetime.now()
    for_date_str = for_date if isinstance(for_date, str) else now_dt.strftime("%Y-%m-%d")

    daily_file = get_date_trades_file(now_dt, symbol_base=symbol_base, mode=mode)

    total_pnl = 0.0
    if daily_file.exists():
        with open(daily_file, "r", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                # Check if row has a status column and if it indicates a completed trade
                # If no status column exists (backward compatibility), count all rows with valid P&L
                status = row.get("status", "").strip().upper()
                is_completed = (
                    status in ("FILLED", "EXIT_EOD", "EXIT_MAX_LOSS", "EXIT_MAX_PROFIT",
                              "EXIT_TRAILING_PROFIT", "EXIT_DAILY_MAX_LOSS", "COMPLETED") or not status
                )
                if is_completed:
                    try:
                        pnl = float(row.get("pnl", 0))
                        total_pnl += pnl
                    except (ValueError, TypeError):
                        pass

    return total_pnl


def log_trade_completion_dual(
    symbol_base: str,
    trade_id: str,
    entry_time: str,
    exit_time: str,
    buy_symbol: str,
    sell_symbol: str,
    entry_premium: float,
    exit_premium: float,
    quantity: int,
    lots: int,
    structure: str,
    pnl: float,
    max_pnl: float,
    exit_reason: str,
    trade_reason: str,
    mode: str = "REVERSE",
    for_date: Optional[str] = None,
):
    """
    Log completed trade with mode-specific tracking.
    """
    now_dt = datetime.now()
    for_date_str = for_date if isinstance(for_date, str) else now_dt.strftime("%Y-%m-%d")

    daily_file = get_date_trades_file(now_dt, symbol_base=symbol_base, mode=mode)

    headers = [
        "date", "trade_id", "entry_time", "exit_time", "symbol", "structure",
        "buy_symbol", "sell_symbol", "entry_premium", "exit_premium",
        "quantity", "lots", "pnl", "max_pnl", "exit_reason", "trade_reason", "mode", "status"
    ]

    row = [
        for_date_str, trade_id, entry_time, exit_time, symbol_base, structure.upper(),
        buy_symbol, sell_symbol, f"{entry_premium:.2f}", f"{exit_premium:.2f}",
        str(quantity), str(lots), f"{pnl:.2f}", f"{max_pnl:.2f}",
        exit_reason, trade_reason, mode, "COMPLETED"
    ]

    for target_file in (daily_file,):
        file_exists = target_file.exists()
        with open(target_file, "a", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            if not file_exists:
                writer.writerow(headers)
            writer.writerow(row)

    if logger:
        logger.info(f"Logged trade completion - Mode: {mode}, Symbol: {symbol_base}, P&L: Rs.{pnl:.2f}, Reason: {exit_reason}")


def print_dual_mode_summary(symbol_base: str = "NIFTY"):
    """
    Print a summary comparing REVERSE and NO-REVERSE performance (independent tracking).
    Each strategy has its own 4-trade limit and P&L tracking - NOT combined.
    """
    now_dt = datetime.now()

    reverse_trades, reverse_loss = get_daily_trade_stats_dual(symbol_base, mode="REVERSE")
    reverse_profit = get_daily_profit_stats_dual(symbol_base, mode="REVERSE")

    no_reverse_trades, no_reverse_loss = get_daily_trade_stats_dual(symbol_base, mode="NO-REVERSE")
    no_reverse_profit = get_daily_profit_stats_dual(symbol_base, mode="NO-REVERSE")

    print("\n" + "=" * 90)
    print("  DUAL-MODE PERFORMANCE SUMMARY (Independent Tracking)")
    print("  Each strategy has its own 4-trade limit per day")
    print("=" * 90)
    print(f"  Instrument: {symbol_base}")
    print(f"  Date: {now_dt.strftime('%Y-%m-%d')}")
    print("-" * 90)
    print(f"{'Metric':<25} {'REVERSE Mode (Fade)':<30} {'NO-REVERSE Mode (Follow)':<30}")
    print("-" * 90)
    print(f"{'Total Trades':<25} {reverse_trades:<30} {no_reverse_trades:<30}")
    print(f"{'Net P&L':<25} Rs. {reverse_profit:<26,.2f} Rs. {no_reverse_profit:<26,.2f}")
    print(f"{'Cumulative Loss':<25} Rs. {reverse_loss:<26,.2f} Rs. {no_reverse_loss:<26,.2f}")
    print("=" * 90)
    print("\n  NOTE: Strategies are tracked independently. Each has its own:")
    print("        - 4-trade limit per day")
    print("        - Risk management (max loss, profit targets)")
    print("        - P&L tracking (NOT combined)")
    print("=" * 90 + "\n")
