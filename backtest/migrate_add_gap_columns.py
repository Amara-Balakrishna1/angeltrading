"""
One-shot migration script: enriches the existing backtest CSV with gap columns.

For each row the gap is approximated as:
  prev_close  ≈ (range_high + range_low) / 2  of the prev_date range
                (we don't have exact daily close, but midpoint is a solid proxy)
  today_open  ≈ entry_spot of the FIRST trade on that date

This gives a consistent, deterministic gap classification for the historical data.
Run once:
  .venv\\Scripts\\python.exe backtest\\migrate_add_gap_columns.py --instrument SENSEX
"""

import sys
import csv
import argparse
from pathlib import Path
from collections import defaultdict

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

VENV = ROOT / ".venv" / "Lib" / "site-packages"
if VENV.exists() and str(VENV) not in sys.path:
    sys.path.insert(0, str(VENV))

from live.daybreakout.gap_analyzer import classify_gap
from live.daybreakout.config import DATA_DIR


def migrate(instrument: str) -> None:
    csv_path = DATA_DIR / f"backtest_daybreakout_15m_{instrument.upper()}.csv"
    if not csv_path.exists():
        print(f"[!] File not found: {csv_path}")
        return

    with open(csv_path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        rows = list(reader)

    if not rows:
        print("[!] CSV is empty.")
        return

    # Check if already migrated
    if "gap_size" in rows[0]:
        print("[*] Gap columns already present. Re-computing to refresh values...")

    # Determine first entry_spot per date (used as today_open proxy)
    first_spot_per_date: dict = {}
    for row in rows:
        d = row["date"]
        if d not in first_spot_per_date:
            first_spot_per_date[d] = float(row["entry_spot"])

    # Enrich rows
    for row in rows:
        date       = row["date"]
        range_high = float(row["range_high"])
        range_low  = float(row["range_low"])

        # Previous day midpoint as proxy for prev_close
        prev_close = (range_high + range_low) / 2.0
        today_open = first_spot_per_date.get(date, prev_close)

        gap_pct_frac = (today_open - prev_close) / prev_close
        info = classify_gap(gap_pct_frac)

        same_gap_dir = (
            (row["direction"] == "BULLISH" and info.direction == "UP") or
            (row["direction"] == "BEARISH" and info.direction == "DOWN")
        )

        row["gap_pct"]       = round(info.gap_pct * 100, 3)
        row["gap_direction"] = info.direction
        row["gap_size"]      = info.size
        row["caution_level"] = info.caution_level
        row["no_chase"]      = same_gap_dir and info.no_chase_same_direction

        # Recovery: trade was OPPOSITE to gap direction on a LARGE/VERY_LARGE day
        is_recovery = (
            info.caution_level >= 3
            and (
                (row["direction"] == "BULLISH" and info.direction == "DOWN") or
                (row["direction"] == "BEARISH" and info.direction == "UP")
            )
        )
        row["trade_type"] = "RECOVERY" if is_recovery else "BREAKOUT"

    # Build new fieldnames: insert gap columns after range_low
    base_fields = [
        "date", "prev_date", "range_high", "range_low",
        "gap_pct", "gap_direction", "gap_size", "caution_level", "no_chase", "trade_type",
        "direction", "opt_type", "entry_time", "entry_spot",
        "buy_strike", "sell_strike", "entry_premium",
        "exit_time", "exit_spot", "exit_reason", "pnl", "lots", "quantity",
    ]
    # Keep any extra columns that might already exist
    extra = [k for k in rows[0].keys() if k not in base_fields]
    fieldnames = base_fields + extra

    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)

    print(f"[+] Migrated {len(rows)} rows in {csv_path}")
    print(f"    Columns present: gap_pct, gap_direction, gap_size, caution_level, no_chase, trade_type")


def main():
    parser = argparse.ArgumentParser(description="Add gap columns to existing backtest CSV")
    parser.add_argument("--instrument", "-i", choices=["NIFTY", "SENSEX"], default="SENSEX")
    args = parser.parse_args()
    migrate(args.instrument)


if __name__ == "__main__":
    main()
