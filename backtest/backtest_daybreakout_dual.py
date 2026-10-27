"""
Dual-Mode Backtest Wrapper for Day Breakout Strategy.
Runs both REVERSE and NO-REVERSE backtests and compares results.
"""

import sys
import io
import argparse
from pathlib import Path
from datetime import datetime
from typing import Dict, List, Tuple
import csv

# Ensure UTF-8 output on Windows
if sys.platform == "win32":
    try:
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    except Exception:
        pass

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from live.daybreakout.config import RISK_CONFIG

import subprocess

# Direct path to DATA_DIR to avoid import issues
DATA_DIR = Path(__file__).resolve().parent.parent / "data"


def parse_backtest_summary(output: str) -> Dict[str, any]:
    """Parse backtest output to extract key metrics."""
    metrics = {
        "total_trades": 0,
        "winning_trades": 0,
        "losing_trades": 0,
        "gross_profit": 0.0,
        "gross_loss": 0.0,
        "net_pnl": 0.0,
        "profit_factor": 0.0,
        "avg_profit_per_trade": 0.0,
    }

    lines = output.split("\n")
    for line in lines:
        if "Total Trades Executed" in line:
            try:
                metrics["total_trades"] = int(line.split(":")[-1].strip())
            except:
                pass
        elif "Winning Trades" in line:
            try:
                # Format: "Winning Trades              : 115 (49.6%)"
                metrics["winning_trades"] = int(line.split(":")[1].split("(")[0].strip())
            except:
                pass
        elif "Gross Profit" in line:
            try:
                metrics["gross_profit"] = float(line.split(":")[-1].replace("Rs.", "").replace("+", "").replace(",", "").strip())
            except:
                pass
        elif "Gross Loss" in line:
            try:
                loss_str = line.split(":")[-1].replace("Rs.", "").replace(",", "").strip()
                # Gross loss should be negative
                metrics["gross_loss"] = -float(loss_str.replace("-", "").replace("+", "").strip())
            except:
                pass
        elif "Net Realized P&L" in line:
            try:
                pnl_str = line.split(":")[-1].replace("Rs.", "").replace(",", "").strip()
                # Handle negative values
                if "-" in pnl_str:
                    metrics["net_pnl"] = -float(pnl_str.replace("-", "").strip())
                else:
                    metrics["net_pnl"] = float(pnl_str.replace("+", "").strip())
            except:
                pass
        elif "Profit Factor" in line:
            try:
                metrics["profit_factor"] = float(line.split(":")[-1].strip())
            except:
                pass
        elif "Average Profit / Trade" in line:
            try:
                avg_str = line.split(":")[-1].replace("Rs.", "").replace(",", "").strip()
                if "-" in avg_str:
                    metrics["avg_profit_per_trade"] = -float(avg_str.replace("-", "").strip())
                else:
                    metrics["avg_profit_per_trade"] = float(avg_str.replace("+", "").strip())
            except:
                pass

    metrics["losing_trades"] = metrics["total_trades"] - metrics["winning_trades"]
    return metrics


def print_comparison(reverse_metrics: Dict, no_reverse_metrics: Dict, instrument: str) -> Dict[str, any]:
    """Print independent results for REVERSE and NO-REVERSE strategies (NO combining of P&L)."""
    rev_trades = reverse_metrics["total_trades"]
    no_rev_trades = no_reverse_metrics["total_trades"]
    rev_wins = reverse_metrics["winning_trades"]
    no_rev_wins = no_reverse_metrics["winning_trades"]
    rev_losses = reverse_metrics["losing_trades"]
    no_rev_losses = no_reverse_metrics["losing_trades"]
    rev_win_rate = (rev_wins / rev_trades * 100) if rev_trades > 0 else 0.0
    no_rev_win_rate = (no_rev_wins / no_rev_trades * 100) if no_rev_trades > 0 else 0.0
    rev_gp = reverse_metrics["gross_profit"]
    no_rev_gp = no_reverse_metrics["gross_profit"]
    rev_gl = reverse_metrics["gross_loss"]
    no_rev_gl = no_reverse_metrics["gross_loss"]
    rev_pnl = reverse_metrics["net_pnl"]
    no_rev_pnl = no_reverse_metrics["net_pnl"]
    rev_pf = reverse_metrics["profit_factor"]
    no_rev_pf = no_reverse_metrics["profit_factor"]
    rev_avg = reverse_metrics["avg_profit_per_trade"]
    no_rev_avg = no_reverse_metrics["avg_profit_per_trade"]

    # Print each strategy independently - NO COMBINED P&L
    print("\n" + "=" * 90)
    print("  DUAL-MODE STRATEGY COMPARISON (Independent Tracking)")
    print("  Each strategy has its own 4-trade limit per day")
    print("=" * 90)
    print(f"  Target Instrument: {instrument}")
    print("=" * 90)
    print(f"{'Metric':<25} {'REVERSE Mode (Fade)':<30} {'NO-REVERSE Mode (Follow)':<30}")
    print("-" * 90)
    print(f"{'Total Trades':<25} {rev_trades:<30} {no_rev_trades:<30}")
    print(f"{'Winning Trades':<25} {rev_wins:<30} {no_rev_wins:<30}")
    print(f"{'Losing Trades':<25} {rev_losses:<30} {no_rev_losses:<30}")
    print(f"{'Win Rate (%)':<25} {f'{rev_win_rate:.1f}%':<30} {f'{no_rev_win_rate:.1f}%':<30}")
    print(f"{'Gross Profit':<25} Rs. {rev_gp:<26,.2f} Rs. {no_rev_gp:<26,.2f}")
    print(f"{'Gross Loss':<25} -Rs. {abs(rev_gl):<25,.2f} -Rs. {abs(no_rev_gl):<25,.2f}")
    
    rev_pnl_str = f"{'+' if rev_pnl >= 0 else '-'}Rs. {abs(rev_pnl):,.2f}"
    no_rev_pnl_str = f"{'+' if no_rev_pnl >= 0 else '-'}Rs. {abs(no_rev_pnl):,.2f}"
    print(f"{'Net Realized P&L':<25} {rev_pnl_str:<30} {no_rev_pnl_str:<30}")
    print(f"{'Profit Factor':<25} {rev_pf:<30.2f} {no_rev_pf:<30.2f}")
    
    rev_avg_str = f"{'+' if rev_avg >= 0 else '-'}Rs. {abs(rev_avg):,.2f}"
    no_rev_avg_str = f"{'+' if no_rev_avg >= 0 else '-'}Rs. {abs(no_rev_avg):,.2f}"
    print(f"{'Avg Profit/Trade':<25} {rev_avg_str:<30} {no_rev_avg_str:<30}")
    print("=" * 90)
    print("\n  NOTE: Strategies are tracked independently. Each has its own:")
    print("        - 4-trade limit per day")
    print("        - Risk management (max loss, profit targets)")
    print("        - P&L tracking (NOT combined)")
    print("=" * 90)

    return {
        "reverse": reverse_metrics,
        "no_reverse": no_reverse_metrics,
    }


def str2bool(v):
    if isinstance(v, bool):
        return v
    if str(v).strip().lower() in ("yes", "true", "t", "y", "1"):
        return True
    elif str(v).strip().lower() in ("no", "false", "f", "n", "0"):
        return False
    else:
        raise argparse.ArgumentTypeError("Boolean value expected (true/false).")


def main():
    parser = argparse.ArgumentParser(description="Dual-Mode Backtest for Day Breakout Strategy")
    parser.add_argument("--instrument", "-i", choices=["NIFTY", "SENSEX"], default="NIFTY", help="Instrument to backtest")
    parser.add_argument("--combination", type=str2bool, nargs="?", const=True, default=False, help="Combination mode (true/false): test nearest expiry instrument per day (default: false)")
    parser.add_argument("--structure", "-s", choices=["spread", "naked"], default="spread", help="Option structure")
    parser.add_argument("--lots", "-l", type=int, default=10, help="Lots to trade")
    parser.add_argument("--x-factor", "-x", type=float, default=RISK_CONFIG.get("x_factor", 1.0), help="Position sizing multiplier (default: from RISK_CONFIG)")
    parser.add_argument("--days", "-d", type=int, default=185, help="Number of days to backtest")
    parser.add_argument("--end-date", type=str, default=None, help="End date (YYYY-MM-DD)")
    parser.add_argument("--first-entry", default="09:30", help="First entry time")
    parser.add_argument("--vix-bearish-thresh", type=float, default=8.0, help="VIX bearish threshold")
    parser.add_argument("--vix-bullish-thresh", type=float, default=-8.0, help="VIX bullish threshold")
    args = parser.parse_args()

    print("\n" + "=" * 100)
    print("  DUAL-MODE BACKTEST: INDEPENDENT STRATEGY TRACKING")
    print("  REVERSE and NO-REVERSE strategies run separately with their own 4-trade limits")
    print("=" * 100)
    print(f"  Instrument: {args.instrument}")
    print(f"  Combination Mode: {args.combination}")
    print(f"  Structure: {args.structure.upper()}")
    effective_lots = max(1, int(args.lots * args.x_factor))
    print(f"  Lots: {args.lots} (raw) × {args.x_factor}x (x_factor) = {effective_lots} effective lots")
    print(f"  X-Factor: {args.x_factor}x")
    print("=" * 100)

    # Build command for REVERSE mode
    cmd_reverse = [
        "py", "backtest\\backtest_daybreakout.py",
        "-i", args.instrument,
        "-s", args.structure,
        "-l", str(args.lots),
        "-x", str(args.x_factor),
        "-d", str(args.days),
        "--reverse",
        "--tag", "reverse"
    ]
    if args.combination:
        cmd_reverse.append("--combination")
    if args.end_date:
        cmd_reverse.extend(["--end-date", args.end_date])
    if args.first_entry:
        cmd_reverse.extend(["--first-entry", args.first_entry])
    if args.vix_bearish_thresh:
        cmd_reverse.extend(["--vix-bearish-thresh", str(args.vix_bearish_thresh)])
    if args.vix_bullish_thresh:
        cmd_reverse.extend(["--vix-bullish-thresh", str(args.vix_bullish_thresh)])

    # Build command for NO-REVERSE mode
    cmd_no_reverse = [
        "py", "backtest\\backtest_daybreakout.py",
        "-i", args.instrument,
        "-s", args.structure,
        "-l", str(args.lots),
        "-x", str(args.x_factor),
        "-d", str(args.days),
        "--no-reverse",
        "--tag", "no-reverse"
    ]
    if args.combination:
        cmd_no_reverse.append("--combination")
    if args.end_date:
        cmd_no_reverse.extend(["--end-date", args.end_date])
    if args.first_entry:
        cmd_no_reverse.extend(["--first-entry", args.first_entry])
    if args.vix_bearish_thresh:
        cmd_no_reverse.extend(["--vix-bearish-thresh", str(args.vix_bearish_thresh)])
    if args.vix_bullish_thresh:
        cmd_no_reverse.extend(["--vix-bullish-thresh", str(args.vix_bullish_thresh)])

    # Run REVERSE mode backtest
    print("\n[*] Running REVERSE mode backtest...")
    print("-" * 100)
    result_reverse = subprocess.run(cmd_reverse, capture_output=True, text=True, encoding="utf-8")
    print(result_reverse.stdout)
    if result_reverse.stderr:
        print("STDERR:", result_reverse.stderr)
    reverse_metrics = parse_backtest_summary(result_reverse.stdout)

    # Run NO-REVERSE mode backtest
    print("\n[*] Running NO-REVERSE mode backtest...")
    print("-" * 100)
    result_no_reverse = subprocess.run(cmd_no_reverse, capture_output=True, text=True, encoding="utf-8")
    print(result_no_reverse.stdout)
    if result_no_reverse.stderr:
        print("STDERR:", result_no_reverse.stderr)
    no_reverse_metrics = parse_backtest_summary(result_no_reverse.stdout)

    # Print comparison (independent tracking, no combined P&L)
    inst_display = "COMBINATION" if args.combination else args.instrument
    comparison_metrics = print_comparison(reverse_metrics, no_reverse_metrics, inst_display)

    # Save comparison to CSV (only independent strategies, no combined P&L)
    date_str = args.end_date if args.end_date else datetime.now().strftime("%Y-%m-%d")
    comparison_file = DATA_DIR / f"backtest_dual_comparison_{inst_display}_{date_str}.csv"

    with open(comparison_file, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow([
            "Strategy", "Total Trades", "Winning Trades", "Losing Trades", "Win Rate %",
            "Gross Profit", "Gross Loss", "Net P&L", "Profit Factor", "Avg Profit/Trade"
        ])
        writer.writerow([
            "REVERSE (Fade Breakouts)",
            reverse_metrics["total_trades"],
            reverse_metrics["winning_trades"],
            reverse_metrics["losing_trades"],
            f"{(reverse_metrics['winning_trades'] / reverse_metrics['total_trades'] * 100) if reverse_metrics['total_trades'] > 0 else 0:.1f}",
            round(reverse_metrics["gross_profit"], 2),
            round(reverse_metrics["gross_loss"], 2),
            round(reverse_metrics["net_pnl"], 2),
            round(reverse_metrics["profit_factor"], 2),
            round(reverse_metrics["avg_profit_per_trade"], 2),
        ])
        writer.writerow([
            "NO-REVERSE (Follow Breakouts)",
            no_reverse_metrics["total_trades"],
            no_reverse_metrics["winning_trades"],
            no_reverse_metrics["losing_trades"],
            f"{(no_reverse_metrics['winning_trades'] / no_reverse_metrics['total_trades'] * 100) if no_reverse_metrics['total_trades'] > 0 else 0:.1f}",
            round(no_reverse_metrics["gross_profit"], 2),
            round(no_reverse_metrics["gross_loss"], 2),
            round(no_reverse_metrics["net_pnl"], 2),
            round(no_reverse_metrics["profit_factor"], 2),
            round(no_reverse_metrics["avg_profit_per_trade"], 2),
        ])

    print(f"\n[+] Dual summary saved to: {comparison_file}")

    # Merge individual reverse and no-reverse trade logs into a combined dual trade log
    struct_clean = args.structure.lower().strip()
    x_factor_suffix = f"_x{args.x_factor}" if args.x_factor != 1.0 else ""
    rev_csv = DATA_DIR / f"backtest_daybreakout_15m_{inst_display}_{struct_clean}{x_factor_suffix}_reverse.csv"
    no_rev_csv = DATA_DIR / f"backtest_daybreakout_15m_{inst_display}_{struct_clean}{x_factor_suffix}_no-reverse.csv"
    dual_csv = DATA_DIR / f"backtest_daybreakout_15m_{inst_display}_{struct_clean}{x_factor_suffix}_dual.csv"

    all_dual_trades = []
    fieldnames = []
    if rev_csv.exists():
        with open(rev_csv, "r", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            fieldnames = reader.fieldnames or []
            all_dual_trades.extend(list(reader))
    if no_rev_csv.exists():
        with open(no_rev_csv, "r", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for field in reader.fieldnames or []:
                if field not in fieldnames:
                    fieldnames.append(field)
            all_dual_trades.extend(list(reader))

    if all_dual_trades:
        # Keep the combined log compatible with older individual logs while
        # carrying the per-trade drawdown column from current backtests.
        if "min_unrealized_pnl" not in fieldnames:
            fieldnames.append("min_unrealized_pnl")
        for trade in all_dual_trades:
            trade.setdefault("min_unrealized_pnl", "")
        all_dual_trades.sort(key=lambda t: (t.get("date", ""), t.get("entry_time", "")))
        with open(dual_csv, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(all_dual_trades)
        print(f"[+] Combined dual trade log saved to: {dual_csv}")


if __name__ == "__main__":
    main()
