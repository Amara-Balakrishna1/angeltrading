"""
Common utility functions for backtest operations
Shared between BANKNIFTY and SENSEX backtest scripts
"""

import csv
from pathlib import Path
from datetime import datetime


def save_day_results(date_str, trades, csv_rows, expiry_month, instrument, csv_prefix, data_root):
    """
    Saves daily minute CSV and daily trades CSV into data/<DD-MM-YYYY>/
    
    Args:
        date_str: Date string in YYYY-MM-DD format
        trades: List of trade dictionaries
        csv_rows: List of minute-by-minute CSV rows
        expiry_month: Expiry month string
        instrument: Instrument name (e.g., "BANKNIFTY", "SENSEX")
        csv_prefix: CSV file prefix (e.g., "BN", "SENSEX")
        data_root: Root directory for data storage
    """
    dt = datetime.strptime(date_str, "%Y-%m-%d")
    dd_mm_yyyy = dt.strftime("%d-%m-%Y")
    day_folder = data_root / dd_mm_yyyy
    day_folder.mkdir(parents=True, exist_ok=True)

    # 1. Save Minute CSV
    spot_column = f"{instrument.lower()}_spot"
    csv_header = [
        "date", "time", spot_column, "atm",
        "ce_strike_1", "ce_1", "ce_strike_2", "ce_2", "ce_strike_3", "ce_3", "ce_strike_4", "ce_4", "ce_strike_5", "ce_5",
        "pe_strike_1", "pe_1", "pe_strike_2", "pe_2", "pe_strike_3", "pe_3", "pe_strike_4", "pe_4", "pe_strike_5", "pe_5",
        "highest_premium", "highest_strike", "highest_option_type", "previous_high", "breakout",
    ]
    
    minute_csv_path = day_folder / f"{csv_prefix}-{dd_mm_yyyy}.csv"
    _write_csv_fast(minute_csv_path, csv_header, csv_rows)

    # 2. Save Daily Trades CSV
    trades_header = [
        "date", "start_time", "end_time", "symbol", "expiry", "lots", "quantity",
        "buy_leg", "buy_entry", "buy_exit",
        "sell_leg", "sell_entry", "sell_exit",
        "exit_reason", "pnl",
    ]
    
    trades_csv_path = day_folder / f"trades_{csv_prefix}.csv"
    trades_rows = []
    for t in trades:
        start_time = t.get("start_time") or t.get("entry_time", "")
        end_time = t.get("end_time") or t.get("exit_time", "")
        trades_rows.append([
            t["date"], start_time, end_time, instrument, expiry_month, str(t.get("lots", 5)), str(t.get("quantity", 150)),
            f"{t['buy_strike']} {t['buy_opt_type']}", f"{t['buy_entry']:.2f}", f"{t['buy_exit']:.2f}",
            f"{t['sell_strike']} {t['sell_opt_type']}", f"{t['sell_entry']:.2f}", f"{t['sell_exit']:.2f}",
            t["exit_reason"], f"{t['pnl']:.2f}",
        ])
    
    _write_csv_fast(trades_csv_path, trades_header, trades_rows)


def _write_csv_fast(file_path, header, rows):
    """
    Optimized CSV writing with buffering for better performance
    """
    with open(file_path, "w", newline="", encoding="utf-8", buffering=8192) as f:
        w = csv.writer(f)
        w.writerow(header)
        w.writerows(rows)


def create_summary_tables(daily_results, instrument, num_days):
    """
    Creates and prints summary tables for backtest results
    
    Args:
        daily_results: List of daily result dictionaries
        instrument: Instrument name (e.g., "BANKNIFTY", "SENSEX")
        num_days: Number of days in backtest
    """
    print(f"\n{'='*95}")
    print(f"                          {instrument} {num_days}-DAY PERFORMANCE TABLE")
    print(f"{'='*95}")
    print(f"{'#':<3} | {'Date':<10} | {'Breakouts':<9} | {'Trades':<6} | {'W/L':<6} | {'Daily Net P&L':<15} | {'Cumulative P&L':<15}")
    print(f"{'-'*95}")

    cum_pnl = 0.0
    for i, r in enumerate(daily_results, 1):
        cum_pnl += r["pnl"]
        pnl_fmt = f"{r['pnl']:+,.2f}"
        cum_fmt = f"{cum_pnl:+,.2f}"
        wl = f"{r['wins']}/{r['losses']}"
        print(f"{i:<3} | {r['dd_mm']:<10} | {r['breakouts']:<9} | {r['trades']:<6} | {wl:<6} | Rs.{pnl_fmt:<11} | Rs.{cum_fmt:<11}")

    print(f"{'='*95}")


def create_overall_summary(daily_results, all_trades, num_days, instrument):
    """
    Creates and prints overall strategy summary
    
    Args:
        daily_results: List of daily result dictionaries
        all_trades: List of all trades
        num_days: Number of days in backtest
        instrument: Instrument name (e.g., "BANKNIFTY", "SENSEX")
    """
    total_trades = len(all_trades)
    total_wins = sum(1 for t in all_trades if t["pnl"] > 0)
    total_losses = sum(1 for t in all_trades if t["pnl"] <= 0)
    win_rate = (total_wins / total_trades * 100) if total_trades > 0 else 0.0
    total_pnl = sum(t["pnl"] for t in all_trades)
    gross_profit = sum(t["pnl"] for t in all_trades if t["pnl"] > 0)
    gross_loss = abs(sum(t["pnl"] for t in all_trades if t["pnl"] < 0))
    profit_factor = (gross_profit / gross_loss) if gross_loss > 0 else (gross_profit if gross_profit > 0 else 0.0)

    best_day = max(daily_results, key=lambda x: x["pnl"]) if daily_results else None
    worst_day = min(daily_results, key=lambda x: x["pnl"]) if daily_results else None

    print("\n" + "=" * 55)
    print("               OVERALL STRATEGY SUMMARY")
    print("=" * 55)
    print(f"Total Trading Days Analyzed : {len(daily_results)}")
    print(f"Total Trades Taken          : {total_trades}")
    print(f"Winning Trades              : {total_wins} ({win_rate:.1f}%)")
    print(f"Losing Trades               : {total_losses} ({100-win_rate:.1f}%)")
    print(f"Gross Profit                : Rs.+{gross_profit:,.2f}")
    print(f"Gross Loss                  : Rs.-{gross_loss:,.2f}")
    print(f"Profit Factor               : {profit_factor:.2f}")
    print(f"Average P&L per Trade       : Rs.{total_pnl/total_trades:+,.2f}" if total_trades else "N/A")
    if best_day:
        print(f"Best Day ({best_day['dd_mm']})       : Rs.{best_day['pnl']:+,.2f}")
    if worst_day:
        print(f"Worst Day ({worst_day['dd_mm']})      : Rs.{worst_day['pnl']:+,.2f}")
    print(f"{'-'*55}")
    print(f"TOTAL {num_days}-DAY REALIZED P&L   : Rs.{total_pnl:+,.2f}")
    print("=" * 55 + "\n")
