"""
Breakout Strategy Live Viewer (Light Theme with Separate BN & SENSEX Trades Tabs)
Run:  python breakoutstrategy/viewer.py
Open: http://localhost:8765
Auto-refreshes every 5 minutes in the browser.
"""

import collections
import csv
import json
import sys
from datetime import datetime, time as dt_time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs
from zoneinfo import ZoneInfo

try:
    IST = ZoneInfo("Asia/Kolkata")
except Exception:
    from datetime import timezone, timedelta
    IST = timezone(timedelta(hours=5, minutes=30))


ROOT = Path(__file__).resolve().parents[2]  # repository root (for shared data folder)
DATA_DIR = ROOT / "data"  # shared data folder
PORT = 8765
MIN_BREAKOUT_AMOUNT = 3.0
TRADES_FILE = DATA_DIR / "trades.csv"   # legacy fallback

INDEX_MAP = {
    "BANKNIFTY": "BN",
    "SENSEX": "SENSEX",
}

INSTR_BN = "BANKNIFTY"
INSTR_SX = "SENSEX"

INSTR_LABELS = {
    INSTR_BN: "BankNifty",
    INSTR_SX: "SENSEX",
}

TRADES_FILENAME = {
    INSTR_BN: "trades_BN.csv",
    INSTR_SX: "trades_SENSEX.csv",
}

# Column indices (0-based)
IDX_DATE   = 0
IDX_TIME   = 1
IDX_SPOT   = 2
IDX_ATM    = 3
IDX_CE_S1  = 4
IDX_CE_P1  = 5
IDX_BREAK  = -1
IDX_HP     = -5
IDX_HS     = -4
IDX_HOT    = -3
IDX_PREV   = -2

# Time-slot filter keys
SLOT_ALL     = "all"
SLOT_MORNING = "morning"
SLOT_SESSION = "session"
SLOT_BOTH    = "both"

SLOT_LABELS = {
    SLOT_ALL:     "All Times",
    SLOT_MORNING: "9:30 – 10:00",
    SLOT_SESSION: "10:00 – 15:00",
    SLOT_BOTH:    "9:30 – 15:00 (Both)",
}


def parse_row_time(row) -> dt_time | None:
    if len(row) <= IDX_TIME:
        return None
    t_str = row[IDX_TIME].strip()
    if not t_str:
        return None
    try:
        parts = t_str.split(":")
        h, m = int(parts[0]), int(parts[1])
        s = int(parts[2]) if len(parts) > 2 else 0
        return dt_time(h, m, s)
    except (ValueError, IndexError):
        return None


def row_in_time_slot(row, slot: str) -> bool:
    if slot == SLOT_ALL:
        return True
    t = parse_row_time(row)
    if t is None:
        return False
    in_morning = dt_time(9, 30) <= t < dt_time(10, 0)
    in_session = dt_time(10, 0) <= t <= dt_time(15, 0)
    if slot == SLOT_MORNING:
        return in_morning
    if slot == SLOT_SESSION:
        return in_session
    if slot == SLOT_BOTH:
        return in_morning or in_session
    return True


def filter_rows_by_slot(rows: list, slot: str) -> list:
    if slot == SLOT_ALL:
        return rows
    return [r for r in rows if row_in_time_slot(r, slot)]


def is_expiry_day(date_str: str, instrument: str = "NIFTY") -> bool:
    """Check if a given date is the weekly expiry day for the given instrument.
    NIFTY/BANKNIFTY: Tuesday (weekday 1)
    SENSEX: Thursday (weekday 3)
    """
    try:
        # Handle date format variations
        if "-" in date_str:
            # Try DD-MM-YYYY format
            parts = date_str.split("-")
            if len(parts) == 3:
                day, month, year = int(parts[0]), int(parts[1]), int(parts[2])
            else:
                # Try YYYY-MM-DD format
                year, month, day = int(parts[0]), int(parts[1]), int(parts[2])
        else:
            return False

        date_obj = datetime(year, month, day)
        # Monday=0, Tuesday=1, Wednesday=2, Thursday=3, Friday=4, Saturday=5, Sunday=6
        instr_upper = instrument.upper()
        if instr_upper in ("NIFTY", "BANKNIFTY"):
            return date_obj.weekday() == 1  # Tuesday
        elif instr_upper == "SENSEX":
            return date_obj.weekday() == 3  # Thursday
        else:
            return False
    except (ValueError, IndexError):
        return False


def get_available_dates() -> list:
    if not DATA_DIR.exists():
        return []
    folders = [
        d.name for d in DATA_DIR.iterdir()
        if d.is_dir() and len(d.name) == 10 and d.name[2] == "-" and d.name[5] == "-"
    ]
    folders.sort(key=lambda x: (x[6:], x[3:5], x[:2]), reverse=True)
    return folders


def today_csv(prefix: str, for_date: str = None) -> Path:
    date_key = for_date or datetime.now(IST).strftime("%d-%m-%Y")
    return DATA_DIR / date_key / f"{prefix}-{date_key}.csv"


def read_csv(prefix: str, for_date: str = None):
    path = today_csv(prefix, for_date)
    if not path.exists():
        return [], []
    with open(path, newline="", encoding="utf-8") as f:
        reader = csv.reader(f)
        rows = [r for r in reader if any(c.strip() for c in r)]
    if not rows:
        return [], []
    if rows[0][0].lower() == "date":
        headers = rows[0]
        data = rows[1:]
    else:
        headers = []
        data = rows
    return headers, data


def read_trades(for_date: str = None, instrument: str = INSTR_BN):
    """Read trades for a specific instrument from trades_BN.csv or trades_SENSEX.csv.
    Falls back to legacy trades.csv filtered by symbol for backward compatibility."""
    date_key = for_date or datetime.now(IST).strftime("%d-%m-%Y")
    fn = TRADES_FILENAME.get(instrument.upper(), "trades_BN.csv")
    target = DATA_DIR / date_key / fn

    if not target.exists():
        legacy = DATA_DIR / date_key / "trades.csv"
        if legacy.exists():
            with open(legacy, newline="", encoding="utf-8") as f:
                reader = csv.reader(f)
                rows = [r for r in reader if any(c.strip() for c in r)]
            if rows and rows[0][0].lower() == "date":
                headers = rows[0]
                data = rows[1:]
            else:
                headers = []
                data = rows
            sym = "BANKNIFTY" if instrument.upper() == INSTR_BN else "SENSEX"
            data = [r for r in data if len(r) > 2 and r[2].strip().upper() == sym]
            return headers, data
        if TRADES_FILE.exists():
            target = TRADES_FILE
        else:
            return [], []

    with open(target, newline="", encoding="utf-8") as f:
        reader = csv.reader(f)
        rows = [r for r in reader if any(c.strip() for c in r)]
    if not rows:
        return [], []
    if rows[0][0].lower() == "date":
        headers = rows[0]
        data = rows[1:]
    else:
        headers = []
        data = rows
    return headers, data


def read_all_trades(instrument: str = INSTR_BN) -> list:
    """Load every closed trade from all date folders for a given instrument."""
    all_trades = []
    for date_key in get_available_dates():
        headers, rows = read_trades(date_key, instrument)
        for r in rows:
            all_trades.append(parse_trade_row(r, headers))

    def sort_key(t):
        date_s = t.get("date", "")
        time_s = t.get("start_time") if t.get("start_time") != "—" else t.get("end_time", "")
        return (date_s, time_s)

    all_trades.sort(key=sort_key, reverse=True)
    return all_trades


def parse_trade_row(row, headers=None) -> dict:
    # If headers are provided and match our columns
    if headers and isinstance(headers, (list, tuple)) and len(headers) > 0 and headers[0].lower().strip() == "date":
        h_map = {col.lower().strip(): idx for idx, col in enumerate(headers)}
        def get_c(name, default="—"):
            idx = h_map.get(name.lower())
            if idx is not None and idx < len(row):
                val = row[idx].strip()
                return val if val else default
            return default

        try:
            pnl_val = float(get_c("pnl", "0.0").replace(",", ""))
        except ValueError:
            pnl_val = 0.0

        start_time = get_c("start_time", "")
        end_time = get_c("end_time", "")
        time_c = get_c("time", "")

        if not start_time and not end_time:
            start_time = "—"
            end_time = time_c if time_c else "—"
        elif not start_time:
            start_time = "—"
        elif not end_time:
            end_time = "—"

        return {
            "date": get_c("date"),
            "start_time": start_time,
            "end_time": end_time,
            "time": f"{start_time} &rarr; {end_time}" if start_time != "—" and end_time != "—" else (end_time if end_time != "—" else start_time),
            "symbol": get_c("symbol"),
            "expiry": get_c("expiry"),
            "structure": get_c("structure", "SPREAD"),
            "lots": get_c("lots"),
            "quantity": get_c("quantity"),
            "buy_leg": get_c("buy_leg"),
            "buy_entry": get_c("buy_entry"),
            "buy_exit": get_c("buy_exit"),
            "sell_leg": get_c("sell_leg"),
            "sell_entry": get_c("sell_entry"),
            "sell_exit": get_c("sell_exit"),
            "trade_reason": get_c("trade_reason", "—"),
            "exit_reason": get_c("exit_reason"),
            "pnl": pnl_val,
            "entry_premium": get_c("entry_premium", "—"),
            "exit_premium": get_c("exit_premium", "—"),
        }

    # Fallback to positional parsing:
    # 19 cols: date, start_time, end_time, symbol, expiry, structure, lots, quantity, buy_leg, buy_entry, buy_exit, sell_leg, sell_entry, sell_exit, trade_reason, exit_reason, pnl, entry_premium, exit_premium
    # 17 cols: date, start_time, end_time, symbol, expiry, structure, lots, quantity, buy_leg, buy_entry, buy_exit, sell_leg, sell_entry, sell_exit, trade_reason, exit_reason, pnl
    # 15 cols: date, start_time, end_time, symbol, expiry, lots, quantity, buy_leg, buy_entry, buy_exit, sell_leg, sell_entry, sell_exit, exit_reason, pnl
    # 14 cols: date, time, symbol, expiry, lots, quantity, buy_leg, buy_entry, buy_exit, sell_leg, sell_entry, sell_exit, exit_reason, pnl
    num_cols = len(row)
    if num_cols >= 19:
        try:
            pnl_val = float(row[16].strip().replace(",", "")) if row[16].strip() else 0.0
        except (ValueError, IndexError):
            pnl_val = 0.0
        start_time = row[1].strip() if num_cols > 1 else "—"
        end_time = row[2].strip() if num_cols > 2 else "—"
        return {
            "date": row[0].strip() if num_cols > 0 else "—",
            "start_time": start_time,
            "end_time": end_time,
            "time": f"{start_time} &rarr; {end_time}",
            "symbol": row[3].strip() if num_cols > 3 else "—",
            "expiry": row[4].strip() if num_cols > 4 else "—",
            "structure": row[5].strip() if num_cols > 5 else "SPREAD",
            "lots": row[6].strip() if num_cols > 6 else "—",
            "quantity": row[7].strip() if num_cols > 7 else "—",
            "buy_leg": row[8].strip() if num_cols > 8 else "—",
            "buy_entry": row[9].strip() if num_cols > 9 else "—",
            "buy_exit": row[10].strip() if num_cols > 10 else "—",
            "sell_leg": row[11].strip() if num_cols > 11 else "—",
            "sell_entry": row[12].strip() if num_cols > 12 else "—",
            "sell_exit": row[13].strip() if num_cols > 13 else "—",
            "trade_reason": row[14].strip() if num_cols > 14 else "—",
            "exit_reason": row[15].strip() if num_cols > 15 else "—",
            "pnl": pnl_val,
            "entry_premium": row[17].strip() if num_cols > 17 else "—",
            "exit_premium": row[18].strip() if num_cols > 18 else "—",
        }
    elif num_cols >= 17:
        try:
            pnl_val = float(row[16].strip().replace(",", "")) if row[16].strip() else 0.0
        except (ValueError, IndexError):
            pnl_val = 0.0
        start_time = row[1].strip() if num_cols > 1 else "—"
        end_time = row[2].strip() if num_cols > 2 else "—"
        return {
            "date": row[0].strip() if num_cols > 0 else "—",
            "start_time": start_time,
            "end_time": end_time,
            "time": f"{start_time} &rarr; {end_time}",
            "symbol": row[3].strip() if num_cols > 3 else "—",
            "expiry": row[4].strip() if num_cols > 4 else "—",
            "structure": row[5].strip() if num_cols > 5 else "SPREAD",
            "lots": row[6].strip() if num_cols > 6 else "—",
            "quantity": row[7].strip() if num_cols > 7 else "—",
            "buy_leg": row[8].strip() if num_cols > 8 else "—",
            "buy_entry": row[9].strip() if num_cols > 9 else "—",
            "buy_exit": row[10].strip() if num_cols > 10 else "—",
            "sell_leg": row[11].strip() if num_cols > 11 else "—",
            "sell_entry": row[12].strip() if num_cols > 12 else "—",
            "sell_exit": row[13].strip() if num_cols > 13 else "—",
            "trade_reason": row[14].strip() if num_cols > 14 else "—",
            "exit_reason": row[15].strip() if num_cols > 15 else "—",
            "pnl": pnl_val,
            "entry_premium": "—",
            "exit_premium": "—",
        }
    elif num_cols >= 15:
        try:
            pnl_val = float(row[14].strip().replace(",", "")) if row[14].strip() else 0.0
        except (ValueError, IndexError):
            pnl_val = 0.0
        start_time = row[1].strip() if num_cols > 1 else "—"
        end_time = row[2].strip() if num_cols > 2 else "—"
        return {
            "date": row[0].strip() if num_cols > 0 else "—",
            "start_time": start_time,
            "end_time": end_time,
            "time": f"{start_time} &rarr; {end_time}",
            "symbol": row[3].strip() if num_cols > 3 else "—",
            "expiry": row[4].strip() if num_cols > 4 else "—",
            "structure": "SPREAD",
            "lots": row[5].strip() if num_cols > 5 else "—",
            "quantity": row[6].strip() if num_cols > 6 else "—",
            "buy_leg": row[7].strip() if num_cols > 7 else "—",
            "buy_entry": row[8].strip() if num_cols > 8 else "—",
            "buy_exit": row[9].strip() if num_cols > 9 else "—",
            "sell_leg": row[10].strip() if num_cols > 10 else "—",
            "sell_entry": row[11].strip() if num_cols > 11 else "—",
            "sell_exit": row[12].strip() if num_cols > 12 else "—",
            "trade_reason": "—",
            "exit_reason": row[13].strip() if num_cols > 13 else "—",
            "pnl": pnl_val,
            "entry_premium": "—",
            "exit_premium": "—",
        }
    else:
        time_val = row[1].strip() if num_cols > 1 else "—"
        try:
            pnl_val = float(row[13].strip().replace(",", "")) if num_cols > 13 and row[13].strip() else 0.0
        except (ValueError, IndexError):
            pnl_val = 0.0
        return {
            "date": row[0].strip() if num_cols > 0 else "—",
            "start_time": "—",
            "end_time": time_val,
            "time": time_val,
            "symbol": row[2].strip() if num_cols > 2 else "—",
            "expiry": row[3].strip() if num_cols > 3 else "—",
            "structure": "SPREAD",
            "lots": row[4].strip() if num_cols > 4 else "—",
            "quantity": row[5].strip() if num_cols > 5 else "—",
            "buy_leg": row[6].strip() if num_cols > 6 else "—",
            "buy_entry": row[7].strip() if num_cols > 7 else "—",
            "buy_exit": row[8].strip() if num_cols > 8 else "—",
            "sell_leg": row[9].strip() if num_cols > 9 else "—",
            "sell_entry": row[10].strip() if num_cols > 10 else "—",
            "sell_exit": row[11].strip() if num_cols > 11 else "—",
            "trade_reason": "—",
            "exit_reason": row[12].strip() if num_cols > 12 else "—",
            "pnl": pnl_val,
            "entry_premium": "—",
            "exit_premium": "—",
        }



def derive_log_reason(row, prev_session_high_map: dict) -> str:
    if len(row) < 5:
        return "Insufficient data"

    is_breakout = row[IDX_BREAK].strip().lower() == "true"
    hp_raw  = row[IDX_HP].strip()
    hs_raw  = row[IDX_HS].strip()
    hot_raw = row[IDX_HOT].strip()
    prev_raw = row[IDX_PREV].strip()

    if is_breakout and hp_raw and hs_raw and hot_raw and prev_raw:
        try:
            hp   = float(hp_raw)
            hs   = hs_raw
            hot  = hot_raw
            prev = float(prev_raw)
            gain = hp - prev
            return (
                f"{hot} {hs} rose to {hp:.2f} "
                f"(prev high {prev:.2f}, gain +{gain:.2f}) "
                f"- exceeded Rs.{MIN_BREAKOUT_AMOUNT} threshold"
            )
        except ValueError:
            return "Breakout detected (parse error)"

    if not is_breakout:
        ce_prices = []
        pe_prices = []
        try:
            for i in range(5):
                base = 4 + i * 2
                if base + 1 < len(row):
                    strike = row[base].strip()
                    price_s = row[base + 1].strip()
                    if price_s:
                        price = float(price_s)
                        key = f"CE_{strike}"
                        prev_h = prev_session_high_map.get(key, price)
                        gain = price - prev_h
                        ce_prices.append((strike, price, prev_h, gain))
            for i in range(5):
                base = 14 + i * 2
                if base + 1 < len(row):
                    strike = row[base].strip()
                    price_s = row[base + 1].strip()
                    if price_s:
                        price = float(price_s)
                        key = f"PE_{strike}"
                        prev_h = prev_session_high_map.get(key, price)
                        gain = price - prev_h
                        pe_prices.append((strike, price, prev_h, gain))
        except (ValueError, IndexError):
            pass

        all_prices = ce_prices + pe_prices
        if all_prices:
            best = max(all_prices, key=lambda x: x[3])
            s, p, ph, g = best
            if g >= MIN_BREAKOUT_AMOUNT:
                return "Check not due yet (alert every 3-min interval)"
            elif g > 0:
                return (
                    f"Best gain: {s} +{g:.2f} (need >{MIN_BREAKOUT_AMOUNT:.0f}) - below threshold"
                )
            else:
                return "No premium exceeded previous session high"
        return "No check due this minute"

    return "—"


def update_session_highs(row, session_high_map: dict):
    try:
        for i in range(5):
            base = 4 + i * 2
            if base + 1 < len(row):
                strike = row[base].strip()
                price_s = row[base + 1].strip()
                if price_s:
                    key = f"CE_{strike}"
                    session_high_map[key] = max(
                        session_high_map.get(key, 0), float(price_s)
                    )
        for i in range(5):
            base = 14 + i * 2
            if base + 1 < len(row):
                strike = row[base].strip()
                price_s = row[base + 1].strip()
                if price_s:
                    key = f"PE_{strike}"
                    session_high_map[key] = max(
                        session_high_map.get(key, 0), float(price_s)
                    )
    except (ValueError, IndexError):
        pass


def build_data_tab(label: str, prefix: str, for_date: str = None, slot: str = SLOT_ALL) -> tuple:
    headers, rows = read_csv(prefix, for_date)
    rows = filter_rows_by_slot(rows, slot)

    if not rows:
        table_html = f'<p class="no-data">No data for {for_date or "today"} ({label}).</p>'
        return table_html, 0, 0

    table_html = '<div class="table-wrap"><table><thead><tr>'
    if headers:
        nice_headers = []
        for h in headers:
            h2 = h.replace("_", " ").title()
            nice_headers.append(h2)
        for h in nice_headers:
            table_html += f"<th>{h}</th>"
    else:
        for i in range(len(rows[0])):
            table_html += f"<th>Col {i+1}</th>"
    table_html += "</tr></thead><tbody>"

    breakout_count = 0
    for row in rows:
        is_breakout = len(row) > 0 and row[-1].strip().lower() == "true"
        if is_breakout:
            breakout_count += 1
        row_class = "breakout-row" if is_breakout else ""
        table_html += f'<tr class="{row_class}">'
        for i, cell in enumerate(row):
            cell = cell.strip()
            if headers and i < len(headers) and headers[i] == "breakout":
                badge = (
                    '<span class="badge-yes">BREAKOUT</span>'
                    if is_breakout
                    else '<span class="badge-no">-</span>'
                )
                table_html += f"<td>{badge}</td>"
            elif cell == "":
                table_html += "<td>—</td>"
            else:
                table_html += f"<td>{cell}</td>"
        table_html += "</tr>"
    table_html += "</tbody></table></div>"

    return table_html, len(rows), breakout_count


def build_log_tab(label: str, prefix: str, for_date: str = None, slot: str = SLOT_ALL) -> tuple:
    headers, rows = read_csv(prefix, for_date)
    rows = filter_rows_by_slot(rows, slot)
    if not rows:
        return f'<p class="no-data">No log data for {for_date or "today"} ({label}).</p>', 0, 0

    log_html = """
    <div class="table-wrap">
    <table>
      <thead>
        <tr>
          <th>Date</th>
          <th>Time</th>
          <th>Spot</th>
          <th>ATM</th>
          <th>Breakout?</th>
          <th class="reason-col">Reason / Analysis</th>
        </tr>
      </thead>
      <tbody>
    """

    session_high_map = {}
    breakout_count = 0
    last_spot_val = None

    for row in rows:
        if len(row) < 5:
            continue
        spot_str = row[IDX_SPOT].strip() if len(row) > IDX_SPOT else ""
        if last_spot_val is not None and spot_str and spot_str != last_spot_val:
            session_high_map = {}
        if spot_str:
            last_spot_val = spot_str

        is_breakout = row[-1].strip().lower() == "true"
        if is_breakout:
            breakout_count += 1

        reason = derive_log_reason(row, session_high_map)
        update_session_highs(row, session_high_map)

        row_class = "breakout-row" if is_breakout else ""
        badge = (
            '<span class="badge-yes">YES</span>'
            if is_breakout
            else '<span class="badge-no">NO</span>'
        )
        date  = row[IDX_DATE].strip() if len(row) > IDX_DATE else "—"
        time_ = row[IDX_TIME].strip() if len(row) > IDX_TIME else "—"
        spot  = row[IDX_SPOT].strip() if len(row) > IDX_SPOT else "—"
        atm   = row[IDX_ATM].strip() if len(row) > IDX_ATM else "—"

        log_html += f"""
        <tr class="{row_class}">
          <td>{date}</td>
          <td class="mono">{time_}</td>
          <td class="mono">{spot}</td>
          <td class="mono">{atm}</td>
          <td>{badge}</td>
          <td class="reason-cell">{reason}</td>
        </tr>"""

    log_html += "</tbody></table></div>"
    return log_html, len(rows), breakout_count


def build_trades_tab_content(instrument: str = INSTR_BN, for_date: str = None, slot: str = SLOT_ALL) -> tuple:
    """Builds table and summary for trades_<INSTR>.csv for a single date."""
    headers, rows = read_trades(for_date, instrument)
    rows = filter_rows_by_slot(rows, slot)
    label = INSTR_LABELS.get(instrument.upper(), instrument.upper())
    fn = TRADES_FILENAME.get(instrument.upper(), "trades_BN.csv")

    if not rows:
        empty_html = f"""
        <div class="summary-bar">
          <span class="s-item"><span class="s-lbl">Status</span><strong>No {label} Trades Yet</strong></span>
          <span class="s-item"><span class="s-lbl">Date</span><strong>{for_date or 'Today'}</strong></span>
          <span class="s-item"><span class="s-lbl">Total Trades</span><strong>0</strong></span>
          <span class="s-item"><span class="s-lbl">Total Net P&amp;L</span><strong>Rs. 0.00</strong></span>
          <span class="s-item"><span class="s-lbl">File</span><strong>{fn}</strong></span>
        </div>
        <p class="no-data">No closed {label} trade positions recorded for {for_date or 'today'}.</p>
        """
        return empty_html, 0, 0.0

    total_trades = len(rows)
    total_pnl = 0.0
    wins = 0
    losses = 0
    table_rows_html = ""

    for row in rows:
        trade = parse_trade_row(row, headers)
        pnl_val = trade["pnl"]
        date_val = trade["date"]
        start_time_val = trade["start_time"]
        end_time_val = trade["end_time"]
        symbol_val = trade["symbol"]
        expiry_val = trade["expiry"]
        lots_val = trade["lots"]
        qty_val = trade["quantity"]
        buy_leg = trade["buy_leg"]
        buy_entry = trade["buy_entry"]
        buy_exit = trade["buy_exit"]
        sell_leg = trade["sell_leg"]
        sell_entry = trade["sell_entry"]
        sell_exit = trade["sell_exit"]
        exit_reason = trade["exit_reason"]
        entry_premium = trade["entry_premium"]
        exit_premium = trade["exit_premium"]

        total_pnl += pnl_val
        if pnl_val > 0:
            wins += 1
        elif pnl_val < 0:
            losses += 1

        pnl_class = "pnl-positive" if pnl_val >= 0 else "pnl-negative"
        pnl_badge = f"+Rs. {pnl_val:,.2f}" if pnl_val >= 0 else f"-Rs. {abs(pnl_val):,.2f}"
        reason_badge_class = (
            "badge-success" if "PROFIT" in exit_reason.upper()
            else ("badge-danger" if "LOSS" in exit_reason.upper() else "badge-neutral")
        )
        reason_label = exit_reason.replace("EXIT_", "").replace("_", " ").title()

        # Format premium values if they're not the default "—"
        entry_premium_display = f"Rs.{entry_premium}" if entry_premium != "—" else "—"
        exit_premium_display = f"Rs.{exit_premium}" if exit_premium != "—" else "—"

        # Check if it's expiry day and format date accordingly (from CSV data)
        date_display = date_val
        # Use the expiry field from CSV if available, otherwise check if symbol indicates expiry
        expiry_field = trade.get("expiry", "")
        if expiry_field and expiry_field.upper() in ("YES", "TRUE", "EXPIRY"):
            date_display = f'<span class="expiry-date">{date_display}</span>'

        table_rows_html += f"""
        <tr>
          <td>{date_display}</td>
          <td class="mono">{start_time_val}</td>
          <td class="mono">{end_time_val}</td>
          <td><strong>{symbol_val}</strong></td>
          <td class="mono">{expiry_val}</td>
          <td class="mono">{lots_val} lots ({qty_val} qty)</td>
          <td style="text-align:left;">
            <span class="tag-buy">BUY</span> <strong>{buy_leg}</strong>
            <span class="price-sub">Rs.{buy_entry} &rarr; Rs.{buy_exit}</span>
          </td>
          <td style="text-align:left;">
            <span class="tag-sell">SELL</span> <strong>{sell_leg}</strong>
            <span class="price-sub">Rs.{sell_entry} &rarr; Rs.{sell_exit}</span>
          </td>
          <td><span class="status-badge {reason_badge_class}">{reason_label}</span></td>
          <td class="mono pnl-cell {pnl_class}"><strong>{pnl_badge}</strong></td>
          <td class="mono premium-cell">{entry_premium_display}</td>
          <td class="mono premium-cell">{exit_premium_display}</td>
        </tr>
        """

    total_pnl_class = "pnl-positive" if total_pnl >= 0 else "pnl-negative"
    total_pnl_formatted = f"+Rs. {total_pnl:,.2f}" if total_pnl >= 0 else f"-Rs. {abs(total_pnl):,.2f}"

    summary = f"""
    <div class="summary-bar">
      <span class="s-item"><span class="s-lbl">Instrument</span><strong>{label}</strong></span>
      <span class="s-item"><span class="s-lbl">Date</span><strong>{for_date or 'Today'}</strong></span>
      <span class="s-item"><span class="s-lbl">Closed Trades</span><strong>{total_trades}</strong></span>
      <span class="s-item"><span class="s-lbl">Winning Trades</span><strong style="color:var(--green);">{wins}</strong></span>
      <span class="s-item"><span class="s-lbl">Losing Trades</span><strong style="color:var(--red);">{losses}</strong></span>
      <span class="s-item"><span class="s-lbl">Total Realized P&amp;L</span><strong class="{total_pnl_class}" style="font-size:16px;">{total_pnl_formatted}</strong></span>
      <span class="s-item"><span class="s-lbl">Source File</span><strong>{fn}</strong></span>
    </div>"""

    table_html = f"""
    <div class="table-wrap">
      <table>
        <thead>
          <tr>
            <th>Date</th>
            <th>Start Time</th>
            <th>End Time</th>
            <th>Index</th>
            <th>Expiry</th>
            <th>Quantity</th>
            <th style="text-align:left;">Buy Leg (Entry &rarr; Exit)</th>
            <th style="text-align:left;">Sell Leg (Entry &rarr; Exit)</th>
            <th>Exit Reason</th>
            <th>Profit / Loss</th>
            <th>Entry Premium</th>
            <th>Exit Premium</th>
          </tr>
        </thead>
        <tbody>
          {table_rows_html}
        </tbody>
        <tfoot>
          <tr class="tfoot-total">
            <td colspan="11" style="text-align:right; font-weight:700; font-size:13px; padding:12px 14px; background:var(--surface2);">
              SUM OF TOTAL PROFIT / LOSS ({total_trades} trades):
            </td>
            <td class="mono pnl-cell {total_pnl_class}" style="font-size:15px; font-weight:700; padding:12px 14px; background:var(--surface2);">
              {total_pnl_formatted}
            </td>
          </tr>
        </tfoot>
      </table>
    </div>
    """
    return summary + table_html, total_trades, total_pnl


def build_trades_summary_content(instrument: str = INSTR_BN) -> tuple:
    """All trades across every available date folder for modal."""
    trades = read_all_trades(instrument)
    label = INSTR_LABELS.get(instrument.upper(), instrument.upper())
    fn = TRADES_FILENAME.get(instrument.upper(), "trades_BN.csv")

    if not trades:
        empty_html = f"""
        <div class="summary-bar">
          <span class="s-item"><span class="s-lbl">Status</span><strong>No {label} Trades Found</strong></span>
          <span class="s-item"><span class="s-lbl">Total Trades</span><strong>0</strong></span>
          <span class="s-item"><span class="s-lbl">Total Net P&amp;L</span><strong>Rs. 0.00</strong></span>
        </div>
        <p class="no-data">No closed {label} trades recorded across date folders ({fn}).</p>
        """
        return empty_html, 0, 0.0

    total_trades = len(trades)
    total_pnl = 0.0
    wins = 0
    losses = 0
    trading_days = set()

    for trade in trades:
        pnl_val = trade["pnl"]
        total_pnl += pnl_val
        trading_days.add(trade["date"])
        if pnl_val > 0:
            wins += 1
        elif pnl_val < 0:
            losses += 1

    table_rows_html = ""
    i = 0
    while i < len(trades):
        date = trades[i]["date"]
        span = 1
        while i + span < len(trades) and trades[i + span]["date"] == date:
            span += 1

        for j in range(span):
            trade = trades[i + j]
            pnl_val = trade["pnl"]
            pnl_class = "pnl-positive" if pnl_val >= 0 else "pnl-negative"
            pnl_badge = f"+Rs. {pnl_val:,.2f}" if pnl_val >= 0 else f"-Rs. {abs(pnl_val):,.2f}"
            # Check if it's expiry day and format date accordingly (from CSV data)
            date_display = date
            expiry_field = trade.get("expiry", "")
            if expiry_field and expiry_field.upper() in ("YES", "TRUE", "EXPIRY"):
                date_display = f'<span class="expiry-date">{date_display}</span>'
            
            date_cell = (
                f'<td rowspan="{span}" class="date-group-cell">{date_display}</td>'
                if j == 0
                else ""
            )
            table_rows_html += f"""
        <tr>
          {date_cell}
          <td class="mono">{trade["start_time"]}</td>
          <td class="mono">{trade["end_time"]}</td>
          <td style="text-align:left;">
            <span class="tag-buy">BUY</span> <strong>{trade["buy_leg"]}</strong>
            <span class="price-sub">Rs.{trade["buy_entry"]} &rarr; Rs.{trade["buy_exit"]}</span>
          </td>
          <td style="text-align:left;">
            <span class="tag-sell">SELL</span> <strong>{trade["sell_leg"]}</strong>
            <span class="price-sub">Rs.{trade["sell_entry"]} &rarr; Rs.{trade["sell_exit"]}</span>
          </td>
          <td class="mono pnl-cell {pnl_class}"><strong>{pnl_badge}</strong></td>
        </tr>"""
        i += span

    total_pnl_class = "pnl-positive" if total_pnl >= 0 else "pnl-negative"
    total_pnl_formatted = f"+Rs. {total_pnl:,.2f}" if total_pnl >= 0 else f"-Rs. {abs(total_pnl):,.2f}"
    win_rate = (wins / total_trades * 100) if total_trades else 0.0

    summary = f"""
    <div class="summary-bar">
      <span class="s-item"><span class="s-lbl">Instrument</span><strong>{label}</strong></span>
      <span class="s-item"><span class="s-lbl">Scope</span><strong>All Available Dates</strong></span>
      <span class="s-item"><span class="s-lbl">Trading Days</span><strong>{len(trading_days)}</strong></span>
      <span class="s-item"><span class="s-lbl">Total Trades</span><strong>{total_trades}</strong></span>
      <span class="s-item"><span class="s-lbl">Winning Trades</span><strong style="color:var(--green);">{wins}</strong></span>
      <span class="s-item"><span class="s-lbl">Losing Trades</span><strong style="color:var(--red);">{losses}</strong></span>
      <span class="s-item"><span class="s-lbl">Win Rate</span><strong>{win_rate:.1f}%</strong></span>
      <span class="s-item"><span class="s-lbl">Total Realized P&amp;L</span><strong class="{total_pnl_class}" style="font-size:16px;">{total_pnl_formatted}</strong></span>
    </div>"""

    table_html = f"""
    <div class="table-wrap">
      <table>
        <thead>
          <tr>
            <th>Date</th>
            <th>Start Time</th>
            <th>End Time</th>
            <th style="text-align:left;">Buy Leg (Entry &rarr; Exit)</th>
            <th style="text-align:left;">Sell Leg (Entry &rarr; Exit)</th>
            <th>Profit / Loss</th>
          </tr>
        </thead>
        <tbody>
          {table_rows_html}
        </tbody>
        <tfoot>
          <tr class="tfoot-total">
            <td colspan="5" style="text-align:right; font-weight:700; font-size:13px; padding:12px 14px; background:var(--surface2);">
              SUM OF TOTAL PROFIT / LOSS ({total_trades} trades):
            </td>
            <td class="mono pnl-cell {total_pnl_class}" style="font-size:15px; font-weight:700; padding:12px 14px; background:var(--surface2);">
              {total_pnl_formatted}
            </td>
          </tr>
        </tfoot>
      </table>
    </div>
    """
    return summary + table_html, total_trades, total_pnl


def read_3leg_backtest_summary() -> tuple:
    """Read BANKNIFTY 3-leg backtest CSV data."""
    candidates = list(DATA_DIR.glob("backtest_3leg_BANKNIFTY_*.csv"))
    if not candidates:
        return [], None

    # Get the most recent file
    csv_file = sorted(candidates)[-1]
    records = []

    with open(csv_file, mode="r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            try:
                # Safely convert P&L values, handling empty strings and None
                final_pnl_str = row.get("final_pnl", "").strip()

                # Handle both old (pe_pnl) and new (hedge_pnl) column names
                pe_pnl_str = row.get("pe_pnl", row.get("hedge_pnl", "")).strip()

                # Handle both old (pe_bought) and new (hedge_bought) column names
                pe_bought_str = row.get("pe_bought", row.get("hedge_bought", "False")).strip()

                row["final_pnl"] = float(final_pnl_str) if final_pnl_str and final_pnl_str not in ("", "None", "N/A") else 0.0
                row["pe_pnl"] = float(pe_pnl_str) if pe_pnl_str and pe_pnl_str not in ("", "None", "N/A") else 0.0
                row["pe_bought"] = pe_bought_str.lower() == "true"
            except (ValueError, TypeError):
                row["final_pnl"] = 0.0
                row["pe_pnl"] = 0.0
                row["pe_bought"] = False
            records.append(row)

    return records, csv_file.name


def read_daybreakout_summary(instrument: str) -> tuple:
    instr = instrument.upper()
    candidates = [
        DATA_DIR / f"backtest_daybreakout_15m_{instr}_spread.csv",
        DATA_DIR / f"backtest_daybreakout_15m_{instr}_naked.csv",
        DATA_DIR / f"backtest_daybreakout_15m_{instr}.csv",
    ]
    # Dynamically find any non-dual daybreakout backtest files for this instrument
    extra_matches = [
        p for p in DATA_DIR.glob(f"backtest_daybreakout_15m_{instr}*.csv")
        if not any(k in p.name.lower() for k in ("_dual", "_reverse", "_no-reverse", "comparison"))
    ]
    all_candidates = []
    seen = set()
    for c in candidates + extra_matches:
        if c not in seen:
            seen.add(c)
            all_candidates.append(c)

    # Sort candidates by modification time (most recent first)
    all_candidates.sort(key=lambda x: x.stat().st_mtime if x.exists() else 0, reverse=True)

    records = []
    found_file = f"backtest_daybreakout_15m_{instr}_spread.csv"
    for csv_file in all_candidates:
        if csv_file.is_file():
            found_file = csv_file.name
            with open(csv_file, mode="r", encoding="utf-8") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    try:
                        row["pnl"] = float(row.get("pnl", 0.0))
                    except ValueError:
                        row["pnl"] = 0.0
                    if "structure" not in row:
                        row["structure"] = "NAKED" if "_naked" in csv_file.name else "SPREAD"
                    records.append(row)
            break
    return records, found_file


def read_dual_mode_trades(instrument: str = "NIFTY", for_date: str = None) -> tuple:
    """Read dual-mode trades (REVERSE and NO-REVERSE) for a specific date."""
    instr = instrument.upper()
    date_key = for_date or datetime.now(IST).strftime("%Y-%m-%d")

    # Look for dual-mode trade files in the trades/<date>/ directory
    trades_dir = DATA_DIR / "trades" / date_key

    reverse_records = []
    no_reverse_records = []

    # Try to read REVERSE mode trades
    reverse_file = trades_dir / f"trades_{instr}_reverse.csv"
    if reverse_file.exists():
        with open(reverse_file, mode="r", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                try:
                    row["pnl"] = float(row.get("pnl", 0.0))
                    row["mode"] = "REVERSE"
                    reverse_records.append(row)
                except ValueError:
                    row["pnl"] = 0.0
                    row["mode"] = "REVERSE"
                    reverse_records.append(row)

    # Try to read NO-REVERSE mode trades
    no_reverse_file = trades_dir / f"trades_{instr}_no-reverse.csv"
    if no_reverse_file.exists():
        with open(no_reverse_file, mode="r", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                try:
                    row["pnl"] = float(row.get("pnl", 0.0))
                    row["mode"] = "NO-REVERSE"
                    no_reverse_records.append(row)
                except ValueError:
                    row["pnl"] = 0.0
                    row["mode"] = "NO-REVERSE"
                    no_reverse_records.append(row)

    return reverse_records, no_reverse_records


def build_3leg_summary_content() -> tuple:
    """Build 3-leg strategy summary with P&L breakdown and monthly analysis."""
    records, fn = read_3leg_backtest_summary()

    if not records:
        empty_html = f"""
        <div class="summary-bar">
          <span class="s-item"><span class="s-lbl">Status</span><strong>No 3-Leg Backtest Data Found</strong></span>
          <span class="s-item"><span class="s-lbl">Total Days</span><strong>0</strong></span>
          <span class="s-item"><span class="s-lbl">Total Net P&amp;L</span><strong>Rs. 0.00</strong></span>
        </div>
        <p class="no-data">No 3-leg backtest records found in {fn or 'data directory'}.</p>
        """
        return empty_html, 0, 0.0

    # Filter out rows with no data (empty final_pnl or all columns empty)
    valid_records = []
    for r in records:
        # Check if the row has actual data (atm_strike or entry prices are good indicators)
        atm_strike = r.get("atm_strike", "").strip()
        entry_atm = r.get("entry_atm", r.get("entry_atm_ce", "")).strip()
        # Include if there's an ATM strike or entry price (meaning a trade was entered)
        if atm_strike or entry_atm:
            valid_records.append(r)
    total_days = len(valid_records)

    # Calculate totals
    total_pnl = sum(r["final_pnl"] for r in valid_records)
    total_hedge_pnl = sum(r["pe_pnl"] for r in valid_records)
    total_trade_pnl = total_pnl - total_hedge_pnl

    wins = sum(1 for r in valid_records if r["final_pnl"] > 0)
    losses = sum(1 for r in valid_records if r["final_pnl"] < 0)
    pe_hedge_days = sum(1 for r in valid_records if r["pe_bought"])

    # Monthly aggregation
    monthly_data = {}
    for r in valid_records:
        date_str = r.get("date", "")
        if date_str:
            try:
                # Parse date (YYYY-MM-DD format)
                parts = date_str.split("-")
                if len(parts) == 3:
                    year_month = f"{parts[0]}-{parts[1]}"
                    if year_month not in monthly_data:
                        monthly_data[year_month] = {"days": 0, "pnl": 0.0, "trade_pnl": 0.0, "hedge_pnl": 0.0, "wins": 0, "losses": 0}
                    monthly_data[year_month]["days"] += 1
                    monthly_data[year_month]["pnl"] += r["final_pnl"]
                    monthly_data[year_month]["trade_pnl"] += (r["final_pnl"] - r["pe_pnl"])
                    monthly_data[year_month]["hedge_pnl"] += r["pe_pnl"]
                    if r["final_pnl"] > 0:
                        monthly_data[year_month]["wins"] += 1
                    elif r["final_pnl"] < 0:
                        monthly_data[year_month]["losses"] += 1
            except (ValueError, IndexError):
                pass

    # Build daily table
    table_rows_html = ""
    for r in valid_records:
        pnl_val = r["final_pnl"]
        hedge_pnl_val = r["pe_pnl"]
        trade_pnl_val = pnl_val - hedge_pnl_val
        hedge_bought = r["pe_bought"]
        leg = r.get("side", "CE")  # Get the leg type (CE or PE)

        pnl_class = "pnl-positive" if pnl_val >= 0 else "pnl-negative"
        pnl_badge = f"+Rs. {pnl_val:,.2f}" if pnl_val >= 0 else f"-Rs. {abs(pnl_val):,.2f}"

        hedge_pnl_class = "pnl-positive" if hedge_pnl_val >= 0 else "pnl-negative"
        hedge_pnl_badge = f"+Rs. {hedge_pnl_val:,.2f}" if hedge_pnl_val >= 0 else f"-Rs. {abs(hedge_pnl_val):,.2f}"

        trade_pnl_class = "pnl-positive" if trade_pnl_val >= 0 else "pnl-negative"
        trade_pnl_badge = f"+Rs. {trade_pnl_val:,.2f}" if trade_pnl_val >= 0 else f"-Rs. {abs(trade_pnl_val):,.2f}"

        hedge_badge = '<span class="badge-success">YES</span>' if hedge_bought else '<span class="badge-neutral">NO</span>'

        # Leg badge color
        leg_class = "badge-success" if leg == "CE" else "badge-danger"
        leg_badge = f'<span class="{leg_class}">{leg}</span>'

        table_rows_html += f"""
        <tr>
          <td>{r.get('date', '')}</td>
          <td>{leg_badge}</td>
          <td class="mono pnl-cell {pnl_class}"><strong>{pnl_badge}</strong></td>
          <td class="mono pnl-cell {trade_pnl_class}">{trade_pnl_badge}</td>
          <td class="mono pnl-cell {hedge_pnl_class}">{hedge_pnl_badge}</td>
          <td>{hedge_badge}</td>
        </tr>"""

    # Build monthly table
    monthly_rows_html = ""
    for month in sorted(monthly_data.keys()):
        m_data = monthly_data[month]
        m_pnl = m_data["pnl"]
        m_trade_pnl = m_data["trade_pnl"]
        m_hedge_pnl = m_data["hedge_pnl"]

        m_pnl_class = "pnl-positive" if m_pnl >= 0 else "pnl-negative"
        m_pnl_badge = f"+Rs. {m_pnl:,.2f}" if m_pnl >= 0 else f"-Rs. {abs(m_pnl):,.2f}"

        m_trade_pnl_class = "pnl-positive" if m_trade_pnl >= 0 else "pnl-negative"
        m_trade_pnl_badge = f"+Rs. {m_trade_pnl:,.2f}" if m_trade_pnl >= 0 else f"-Rs. {abs(m_trade_pnl):,.2f}"

        m_hedge_pnl_class = "pnl-positive" if m_hedge_pnl >= 0 else "pnl-negative"
        m_hedge_pnl_badge = f"+Rs. {m_hedge_pnl:,.2f}" if m_hedge_pnl >= 0 else f"-Rs. {abs(m_hedge_pnl):,.2f}"

        monthly_rows_html += f"""
        <tr>
          <td><strong>{month}</strong></td>
          <td class="mono">{m_data['days']}</td>
          <td class="mono pnl-cell {m_pnl_class}"><strong>{m_pnl_badge}</strong></td>
          <td class="mono pnl-cell {m_trade_pnl_class}">{m_trade_pnl_badge}</td>
          <td class="mono pnl-cell {m_hedge_pnl_class}">{m_hedge_pnl_badge}</td>
          <td class="mono"><span style="color:var(--green);">{m_data['wins']}</span></td>
          <td class="mono"><span style="color:var(--red);">{m_data['losses']}</span></td>
        </tr>"""

    total_pnl_class = "pnl-positive" if total_pnl >= 0 else "pnl-negative"
    total_pnl_formatted = f"+Rs. {total_pnl:,.2f}" if total_pnl >= 0 else f"-Rs. {abs(total_pnl):,.2f}"

    total_trade_pnl_class = "pnl-positive" if total_trade_pnl >= 0 else "pnl-negative"
    total_trade_pnl_formatted = f"+Rs. {total_trade_pnl:,.2f}" if total_trade_pnl >= 0 else f"-Rs. {abs(total_trade_pnl):,.2f}"

    total_hedge_pnl_class = "pnl-positive" if total_hedge_pnl >= 0 else "pnl-negative"
    total_hedge_pnl_formatted = f"+Rs. {total_hedge_pnl:,.2f}" if total_hedge_pnl >= 0 else f"-Rs. {abs(total_hedge_pnl):,.2f}"

    win_rate = (wins / total_days * 100) if total_days else 0.0

    summary = f"""
    <div class="summary-bar">
      <span class="s-item"><span class="s-lbl">Strategy</span><strong>3-Leg BANKNIFTY with Hedge</strong></span>
      <span class="s-item"><span class="s-lbl">Total Days</span><strong>{total_days}</strong></span>
      <span class="s-item"><span class="s-lbl">Winning Days</span><strong style="color:var(--green);">{wins}</strong></span>
      <span class="s-item"><span class="s-lbl">Losing Days</span><strong style="color:var(--red);">{losses}</strong></span>
      <span class="s-item"><span class="s-lbl">Hedge Days</span><strong>{pe_hedge_days}</strong></span>
      <span class="s-item"><span class="s-lbl">Win Rate</span><strong>{win_rate:.1f}%</strong></span>
      <span class="s-item"><span class="s-lbl">Total Trade P&amp;L</span><strong class="{total_trade_pnl_class}">{total_trade_pnl_formatted}</strong></span>
      <span class="s-item"><span class="s-lbl">Total Hedge P&amp;L</span><strong class="{total_hedge_pnl_class}">{total_hedge_pnl_formatted}</strong></span>
      <span class="s-item"><span class="s-lbl">Total Cumulative P&amp;L</span><strong class="{total_pnl_class}" style="font-size:16px;">{total_pnl_formatted}</strong></span>
      <span class="s-item"><span class="s-lbl">Source File</span><strong>{fn}</strong></span>
    </div>"""

    daily_table_html = f"""
    <div class="table-wrap">
      <table>
        <thead>
          <tr>
            <th>Date</th>
            <th>Leg</th>
            <th>Total P&amp;L</th>
            <th>Trade P&amp;L</th>
            <th>Hedge P&amp;L</th>
            <th>Hedge</th>
          </tr>
        </thead>
        <tbody>
          {table_rows_html}
        </tbody>
        <tfoot>
          <tr class="tfoot-total">
            <td colspan="2" style="text-align:right; font-weight:700; font-size:13px; padding:12px 14px; background:var(--surface2);">
              TOTAL ({total_days} days):
            </td>
            <td class="mono pnl-cell {total_pnl_class}" style="font-size:15px; font-weight:700; padding:12px 14px; background:var(--surface2);">
              {total_pnl_formatted}
            </td>
            <td class="mono pnl-cell {total_trade_pnl_class}" style="font-size:15px; font-weight:700; padding:12px 14px; background:var(--surface2);">
              {total_trade_pnl_formatted}
            </td>
            <td class="mono pnl-cell {total_hedge_pnl_class}" style="font-size:15px; font-weight:700; padding:12px 14px; background:var(--surface2);">
              {total_hedge_pnl_formatted}
            </td>
            <td style="padding:12px 14px; background:var(--surface2); text-align:center;">
              {pe_hedge_days}/{total_days}
            </td>
          </tr>
        </tfoot>
      </table>
    </div>"""

    monthly_table_html = f"""
    <div class="table-wrap" style="margin-top:20px;">
      <table>
        <thead>
          <tr>
            <th>Month</th>
            <th>Days</th>
            <th>Total P&amp;L</th>
            <th>Trade P&amp;L</th>
            <th>Hedge P&amp;L</th>
            <th>Wins</th>
            <th>Losses</th>
          </tr>
        </thead>
        <tbody>
          {monthly_rows_html}
        </tbody>
      </table>
    </div>"""

    return summary + daily_table_html + monthly_table_html, total_days, total_pnl


def compute_daily_pnl_metrics(records: list) -> dict:
    daily_pnl = collections.defaultdict(float)
    daily_trades = collections.defaultdict(int)
    for r in records:
        d = r.get("date", "").strip()
        if d:
            try:
                pnl_val = float(r.get("pnl", 0.0))
            except (ValueError, TypeError):
                pnl_val = 0.0
            daily_pnl[d] += pnl_val
            daily_trades[d] += 1

    total_days = len(daily_pnl)
    if total_days == 0:
        return {
            "total_days": 0, "net_profit_days": 0, "net_loss_days": 0, "breakeven_days": 0,
            "profit_days_pct": 0.0, "loss_days_pct": 0.0,
            "max_net_profit": 0.0, "max_profit_day": "—",
            "max_net_loss": 0.0, "max_loss_day": "—",
            "avg_profit": 0.0, "avg_loss": 0.0,
            "max_p_fmt": "Rs. 0.00", "max_l_fmt": "Rs. 0.00",
            "avg_p_fmt": "Rs. 0.00", "avg_l_fmt": "Rs. 0.00",
        }

    profit_days = [p for p in daily_pnl.values() if p > 0]
    loss_days = [p for p in daily_pnl.values() if p < 0]
    be_days = [p for p in daily_pnl.values() if p == 0]

    net_profit_days = len(profit_days)
    net_loss_days = len(loss_days)
    profit_days_pct = (net_profit_days / total_days * 100) if total_days > 0 else 0.0
    loss_days_pct = (net_loss_days / total_days * 100) if total_days > 0 else 0.0

    max_profit_item = max(daily_pnl.items(), key=lambda x: x[1])
    max_net_profit = max_profit_item[1]
    max_profit_day = max_profit_item[0]

    min_loss_item = min(daily_pnl.items(), key=lambda x: x[1])
    max_net_loss = min_loss_item[1]
    max_loss_day = min_loss_item[0]

    avg_profit = (sum(profit_days) / len(profit_days)) if profit_days else 0.0
    avg_loss = (sum(loss_days) / len(loss_days)) if loss_days else 0.0

    max_p_fmt = f"+Rs. {max_net_profit:,.2f}" if max_net_profit >= 0 else f"-Rs. {abs(max_net_profit):,.2f}"
    max_l_fmt = f"-Rs. {abs(max_net_loss):,.2f}" if max_net_loss <= 0 else f"+Rs. {max_net_loss:,.2f}"
    avg_p_fmt = f"+Rs. {avg_profit:,.2f}" if avg_profit >= 0 else f"-Rs. {abs(avg_profit):,.2f}"
    avg_l_fmt = f"-Rs. {abs(avg_loss):,.2f}" if avg_loss <= 0 else f"+Rs. {avg_loss:,.2f}"

    return {
        "total_days": total_days,
        "net_profit_days": net_profit_days,
        "net_loss_days": net_loss_days,
        "breakeven_days": len(be_days),
        "profit_days_pct": profit_days_pct,
        "loss_days_pct": loss_days_pct,
        "max_net_profit": max_net_profit,
        "max_profit_day": max_profit_day,
        "max_net_loss": max_net_loss,
        "max_loss_day": max_loss_day,
        "avg_profit": avg_profit,
        "avg_loss": avg_loss,
        "max_p_fmt": max_p_fmt,
        "max_l_fmt": max_l_fmt,
        "avg_p_fmt": avg_p_fmt,
        "avg_l_fmt": avg_l_fmt,
    }


def render_daily_metrics_cards(metrics: dict) -> str:
    if not metrics or metrics.get("total_days", 0) == 0:
        return ""
    total_days = metrics["total_days"]
    net_profit_days = metrics["net_profit_days"]
    net_loss_days = metrics["net_loss_days"]
    p_pct = metrics["profit_days_pct"]
    l_pct = metrics["loss_days_pct"]
    max_p_fmt = metrics["max_p_fmt"]
    max_p_day = metrics["max_profit_day"]
    max_l_fmt = metrics["max_l_fmt"]
    max_l_day = metrics["max_loss_day"]
    avg_p_fmt = metrics["avg_p_fmt"]
    avg_l_fmt = metrics["avg_l_fmt"]

    return f"""
    <div class="day-stats-bar" style="margin-top:20px; background:#ffffff; border:1px solid var(--border); border-radius:10px; padding:18px 22px; box-shadow:var(--shadow);">
      <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:14px; border-bottom:1px solid var(--border-light); padding-bottom:10px;">
        <span style="font-size:15px; font-weight:700; color:var(--text);">&#128197; DAILY NET P&amp;L PERFORMANCE METRICS</span>
        <span style="font-size:12px; font-weight:600; color:var(--text-muted); background:var(--surface2); padding:3px 10px; border-radius:12px; border:1px solid var(--border-light);">
          {total_days} Trading Days Analyzed
        </span>
      </div>
      <div style="display:grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap:14px;">
        <div style="background:var(--green-bg); border:1px solid var(--green-border); border-radius:8px; padding:12px 16px;">
          <div style="font-size:11px; font-weight:700; text-transform:uppercase; color:var(--green); letter-spacing:0.5px;">Net Profit Days</div>
          <div style="font-size:22px; font-weight:800; color:var(--green); margin-top:4px;">{net_profit_days} <span style="font-size:13px; font-weight:600;">({p_pct:.1f}%)</span></div>
          <div style="font-size:11px; color:var(--text-muted); margin-top:2px;">Out of {total_days} total days</div>
        </div>
        <div style="background:var(--red-bg); border:1px solid var(--red-border); border-radius:8px; padding:12px 16px;">
          <div style="font-size:11px; font-weight:700; text-transform:uppercase; color:var(--red); letter-spacing:0.5px;">Net Loss Days</div>
          <div style="font-size:22px; font-weight:800; color:var(--red); margin-top:4px;">{net_loss_days} <span style="font-size:13px; font-weight:600;">({l_pct:.1f}%)</span></div>
          <div style="font-size:11px; color:var(--text-muted); margin-top:2px;">Out of {total_days} total days</div>
        </div>
        <div style="background:var(--green-bg); border:1px solid var(--green-border); border-radius:8px; padding:12px 16px;">
          <div style="font-size:11px; font-weight:700; text-transform:uppercase; color:var(--green); letter-spacing:0.5px;">Max Net Profit (Single Day)</div>
          <div style="font-size:18px; font-weight:800; color:var(--green); margin-top:6px;">{max_p_fmt}</div>
          <div style="font-size:11px; color:var(--text-muted); margin-top:2px;">Peak: {max_p_day}</div>
        </div>
        <div style="background:var(--red-bg); border:1px solid var(--red-border); border-radius:8px; padding:12px 16px;">
          <div style="font-size:11px; font-weight:700; text-transform:uppercase; color:var(--red); letter-spacing:0.5px;">Max Net Loss (Single Day)</div>
          <div style="font-size:18px; font-weight:800; color:var(--red); margin-top:6px;">{max_l_fmt}</div>
          <div style="font-size:11px; color:var(--text-muted); margin-top:2px;">Worst: {max_l_day}</div>
        </div>
        <div style="background:var(--surface2); border:1px solid var(--border); border-radius:8px; padding:12px 16px;">
          <div style="font-size:11px; font-weight:700; text-transform:uppercase; color:var(--text-muted); letter-spacing:0.5px;">Avg Profit (Winning Days)</div>
          <div style="font-size:18px; font-weight:800; color:var(--green); margin-top:6px;">{avg_p_fmt}</div>
          <div style="font-size:11px; color:var(--text-muted); margin-top:2px;">Avg on {net_profit_days} profit days</div>
        </div>
        <div style="background:var(--surface2); border:1px solid var(--border); border-radius:8px; padding:12px 16px;">
          <div style="font-size:11px; font-weight:700; text-transform:uppercase; color:var(--text-muted); letter-spacing:0.5px;">Avg Loss (Losing Days)</div>
          <div style="font-size:18px; font-weight:800; color:var(--red); margin-top:6px;">{avg_l_fmt}</div>
          <div style="font-size:11px; color:var(--text-muted); margin-top:2px;">Avg on {net_loss_days} loss days</div>
        </div>
      </div>
    </div>
    """


def build_daybreakout_summary_content(instrument: str = "NIFTY") -> tuple:
    records, fn = read_daybreakout_summary(instrument)
    label = instrument.upper()

    if not records:
        empty_html = f"""
        <div class="summary-bar">
          <span class="s-item"><span class="s-lbl">Status</span><strong>No {label} Day-Breakout Data Found</strong></span>
          <span class="s-item"><span class="s-lbl">Total Trades</span><strong>0</strong></span>
          <span class="s-item"><span class="s-lbl">Total Net P&amp;L</span><strong>Rs. 0.00</strong></span>
        </div>
        <p class="no-data">No Day-Breakout records found in {fn}.</p>
        """
        return empty_html, 0, 0.0

    total_trades = len(records)
    total_pnl = sum(r["pnl"] for r in records)
    wins = sum(1 for r in records if r["pnl"] > 0)
    losses = sum(1 for r in records if r["pnl"] < 0)

    table_rows_html = ""
    for r in records:
        pnl_val = r["pnl"]
        pnl_class = "pnl-positive" if pnl_val >= 0 else "pnl-negative"
        pnl_badge = f"+Rs. {pnl_val:,.2f}" if pnl_val >= 0 else f"-Rs. {abs(pnl_val):,.2f}"
        try:
            min_unrealized_display = f"-Rs. {abs(float(r.get('min_unrealized_pnl'))):,.2f}"
        except (TypeError, ValueError):
            min_unrealized_display = "—"
        dir_val = r.get('direction', '')
        tag_class = "tag-buy" if dir_val == "BULLISH" else ("tag-sell" if dir_val == "BEARISH" else "badge-neutral")
        struct_val = r.get('structure', 'SPREAD').upper()
        struct_clean = struct_val.lower()
        sell_st = r.get('sell_strike', '')
        if struct_val == 'NAKED' or sell_st in ('N/A', '', None):
            strikes_display = f"{r.get('buy_strike', '')} {r.get('opt_type', '')}"
        else:
            strikes_display = f"{r.get('buy_strike', '')}/{sell_st} {r.get('opt_type', '')}"

        exit_rsn = r.get('exit_reason', '')
        reason_badge_class = (
            "badge-success" if "PROFIT" in exit_rsn.upper()
            else ("badge-danger" if "LOSS" in exit_rsn.upper() else "badge-neutral")
        )
        reason_label = exit_rsn.replace("EXIT_", "").replace("_", " ").title()
        trade_rsn = r.get('trade_reason', '—')

        # Format premium values
        entry_premium = r.get('entry_premium', '—')
        exit_premium = r.get('exit_premium', '—')
        entry_premium_display = f"Rs.{entry_premium}" if entry_premium != '—' else "—"
        exit_premium_display = f"Rs.{exit_premium}" if exit_premium != '—' else "—"
        
        # Check if it's expiry day and format date accordingly (from exchange data)
        expiry_val = r.get('expiry', 'NO')
        date_display = r.get('date', '')
        if expiry_val == 'YES':
            date_display = f'<span class="expiry-date">{date_display}</span>'

        trade_instr = r.get('instrument', instrument).upper()
        instr_td = f'<td class="mono"><strong>{trade_instr}</strong></td>' if instrument.upper() == "COMBINATION" else ""

        table_rows_html += f"""
        <tr>
          <td>{date_display}</td>
          {instr_td}
          <td class="mono"><strong>{struct_val}</strong></td>
          <td class="mono">{r.get('gap_direction', '')} ({r.get('gap_size', '')})</td>
          <td class="mono">{r.get('trade_type', '')}</td>
          <td><span class="{tag_class}">{dir_val}</span></td>
          <td class="mono">{r.get('entry_time', '')} @ {r.get('entry_spot', '')}</td>
          <td class="mono">{strikes_display}</td>
          <td class="mono">{r.get('exit_time', '')} @ {r.get('exit_spot', '')}</td>
          <td class="reason-cell">{trade_rsn}</td>
          <td><span class="status-badge {reason_badge_class}">{reason_label}</span></td>
          <td class="mono pnl-cell {pnl_class}" style="white-space:nowrap;"><strong>{pnl_badge}</strong>&nbsp;<button class="pnl-icon-btn" title="30-min P/L breakdown" onclick="open30MinModal('{r.get('date','')}', '{trade_instr}', '{struct_clean}')">&#128202;</button></td>
          <td class="mono pnl-cell pnl-negative" style="white-space:nowrap;">{min_unrealized_display}</td>
          <td class="mono premium-cell">{entry_premium_display}</td>
          <td class="mono premium-cell">{exit_premium_display}</td>
        </tr>"""

    total_pnl_class = "pnl-positive" if total_pnl >= 0 else "pnl-negative"
    total_pnl_formatted = f"+Rs. {total_pnl:,.2f}" if total_pnl >= 0 else f"-Rs. {abs(total_pnl):,.2f}"

    instr_th = "<th>Instrument</th>" if instrument.upper() == "COMBINATION" else ""
    colspan_val = 13 if instrument.upper() == "COMBINATION" else 12

    summary = f"""
    <div class="summary-bar">
      <span class="s-item"><span class="s-lbl">Instrument</span><strong>{label} Day-Breakout</strong></span>
      <span class="s-item"><span class="s-lbl">Total Trades</span><strong>{total_trades}</strong></span>
      <span class="s-item"><span class="s-lbl">Wins</span><strong style="color:var(--green);">{wins}</strong></span>
      <span class="s-item"><span class="s-lbl">Losses</span><strong style="color:var(--red);">{losses}</strong></span>
      <span class="s-item"><span class="s-lbl">Total P&amp;L</span><strong class="{total_pnl_class}" style="font-size:16px;">{total_pnl_formatted}</strong></span>
      <span class="s-item"><span class="s-lbl">Source File</span><strong>{fn}</strong></span>
    </div>"""

    table_html = f"""
    <div class="table-wrap">
      <table>
        <thead>
          <tr>
            <th>Date</th>
            {instr_th}
            <th>Structure</th>
            <th>Gap</th>
            <th>Trade Type</th>
            <th>Direction</th>
            <th>Entry</th>
            <th>Strikes</th>
            <th>Exit</th>
            <th class="reason-col">Trade Reason</th>
            <th>Exit Reason</th>
            <th>P&amp;L</th>
            <th>min_unrealized_pnl</th>
            <th>Entry Premium</th>
            <th>Exit Premium</th>
          </tr>
        </thead>
        <tbody>
          {table_rows_html}
        </tbody>
        <tfoot>
          <tr class="tfoot-total">
            <td colspan="{colspan_val}" style="text-align:right; font-weight:700; font-size:13px; padding:12px 14px; background:var(--surface2);">
              TOTAL P&amp;L ({total_trades} trades):
            </td>
            <td class="mono pnl-cell {total_pnl_class}" style="font-size:15px; font-weight:700; padding:12px 14px; background:var(--surface2);">
              {total_pnl_formatted}
            </td>
          </tr>
        </tfoot>
      </table>
    </div>
    """
    return summary + table_html, total_trades, total_pnl


def build_strategy_pnl_data() -> dict:
    """Aggregate net P&L by instrument and direction from default backtests.

    Position-size variants (for example ``_x0.3``) are skipped to prevent
    counting the same historical trades multiple times.
    """
    result = {}
    for instrument in ("NIFTY", "SENSEX", "COMBINATION"):
        candidates = [
            path for path in DATA_DIR.glob(f"backtest_daybreakout_15m_{instrument}_*_dual.csv")
            if "_x" not in path.stem.lower()
        ]
        candidates.sort(key=lambda path: path.stat().st_mtime, reverse=True)
        totals = {
            "REVERSE": {"pnl": 0.0, "trades": 0, "daily": {}},
            "NO-REVERSE": {"pnl": 0.0, "trades": 0, "daily": {}},
        }

        def add_file(path):
            try:
                with path.open(newline="", encoding="utf-8-sig") as stream:
                    for row in csv.DictReader(stream):
                        mode = str(row.get("mode", "")).strip().upper()
                        if mode not in totals:
                            continue
                        try:
                            pnl = float(row.get("pnl", 0) or 0)
                        except (TypeError, ValueError):
                            continue
                        totals[mode]["pnl"] += pnl
                        totals[mode]["trades"] += 1
                        trade_date = str(row.get("date", "")).strip()
                        if trade_date:
                            daily = totals[mode]["daily"]
                            daily[trade_date] = daily.get(trade_date, 0.0) + pnl
            except (OSError, csv.Error):
                return False
            return True

        source = None
        if candidates:
            chosen = candidates[0]
            source = chosen.name if add_file(chosen) else None
        else:
            # Use separately exported mode files if a merged dual file is absent.
            mode_files = []
            for mode_suffix in ("reverse", "no-reverse"):
                options = [p for p in DATA_DIR.glob(f"backtest_daybreakout_15m_{instrument}_*_{mode_suffix}.csv") if "_x" not in p.stem.lower()]
                options.sort(key=lambda path: path.stat().st_mtime, reverse=True)
                if options and add_file(options[0]):
                    mode_files.append(options[0].name)
            if not mode_files:
                # A conventional instrument file may contain only REVERSE results.
                options = [p for p in DATA_DIR.glob(f"backtest_daybreakout_15m_{instrument}_*.csv") if not any(tag in p.stem.lower() for tag in ("_dual", "_reverse", "_no-reverse", "_x", "comparison"))]
                options.sort(key=lambda path: path.stat().st_mtime, reverse=True)
                if options and add_file(options[0]):
                    mode_files.append(options[0].name)
            source = ", ".join(mode_files) if mode_files else None
        result[instrument] = {"source": source, **totals}
    return result


def read_dual_backtest_summary(instrument: str = "COMBINATION") -> tuple:
    instr = instrument.upper()
    candidates = [
        DATA_DIR / f"backtest_daybreakout_15m_{instr}_spread_dual.csv",
        DATA_DIR / f"backtest_daybreakout_15m_{instr}_naked_dual.csv",
        DATA_DIR / f"backtest_daybreakout_15m_{instr}_dual.csv",
    ]
    extra_matches = [
        p for p in DATA_DIR.glob(f"backtest_daybreakout_15m_{instr}*dual*.csv")
    ]
    all_candidates = []
    seen = set()
    for c in candidates + extra_matches:
        if c not in seen:
            seen.add(c)
            all_candidates.append(c)

    # Sort candidates by modification time (most recent first)
    all_candidates.sort(key=lambda x: x.stat().st_mtime if x.exists() else 0, reverse=True)
    
    records = []
    found_file = f"backtest_daybreakout_15m_{instr}_spread_dual.csv"
    for csv_file in all_candidates:
        if csv_file.is_file():
            found_file = csv_file.name
            with open(csv_file, mode="r", encoding="utf-8") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    try:
                        row["pnl"] = float(row.get("pnl", 0.0))
                    except ValueError:
                        row["pnl"] = 0.0
                    if "structure" not in row:
                        row["structure"] = "NAKED" if "_naked" in csv_file.name else "SPREAD"
                    records.append(row)
            break
    return records, found_file


def build_dual_backtest_summary_content(instrument: str = "COMBINATION") -> tuple:
    records, fn = read_dual_backtest_summary(instrument)
    label = instrument.upper()

    if not records:
        empty_html = f"""
        <div class="summary-bar">
          <span class="s-item"><span class="s-lbl">Status</span><strong>No {label} Dual Backtest Data Found</strong></span>
          <span class="s-item"><span class="s-lbl">Total Trades</span><strong>0</strong></span>
          <span class="s-item"><span class="s-lbl">Total Net P&amp;L</span><strong>Rs. 0.00</strong></span>
        </div>
        <p class="no-data">No Dual Backtest records found in {fn}. Run backtest_daybreakout_dual.py to generate data.</p>
        """
        return empty_html, 0, 0.0

    records.sort(key=lambda row: (
        str(row.get("date", "")),
        str(row.get("entry_time", "")),
        str(row.get("instrument", "")),
        str(row.get("mode", "")),
    ))

    total_trades = len(records)
    total_pnl = sum(r["pnl"] for r in records)
    wins = sum(1 for r in records if r["pnl"] > 0)
    losses = sum(1 for r in records if r["pnl"] < 0)
    win_rate = (wins / total_trades * 100) if total_trades > 0 else 0.0
    gross_profit = sum(r["pnl"] for r in records if r["pnl"] > 0)
    gross_loss = sum(abs(r["pnl"]) for r in records if r["pnl"] < 0)
    profit_factor = (gross_profit / gross_loss) if gross_loss > 0 else 0.0
    avg_pnl = (total_pnl / total_trades) if total_trades > 0 else 0.0
    avg_win = (gross_profit / wins) if wins > 0 else 0.0
    avg_loss = (gross_loss / losses) if losses > 0 else 0.0
    max_profit = max(r["pnl"] for r in records) if records else 0.0
    max_loss = min(r["pnl"] for r in records) if records else 0.0

    # Calculate daily P&L to get net profit/loss days
    daily_pnl = {}
    daily_rev_pnl = {}
    daily_no_rev_pnl = {}
    daily_instrument_pnl = {"NIFTY": {}, "SENSEX": {}}
    for r in records:
        date = r.get("date", "")
        mode = r.get("mode", "").upper()
        pnl = r["pnl"]
        trade_instrument = str(r.get("instrument", "")).strip().upper()
        
        if date not in daily_pnl:
            daily_pnl[date] = 0.0
        daily_pnl[date] += pnl
        
        if mode == "REVERSE":
            if date not in daily_rev_pnl:
                daily_rev_pnl[date] = 0.0
            daily_rev_pnl[date] += pnl
        elif mode == "NO-REVERSE":
            if date not in daily_no_rev_pnl:
                daily_no_rev_pnl[date] = 0.0
            daily_no_rev_pnl[date] += pnl

        if trade_instrument in daily_instrument_pnl:
            instrument_daily = daily_instrument_pnl[trade_instrument]
            instrument_daily[date] = instrument_daily.get(date, 0.0) + pnl

    instrument_loss_stats = {}
    instrument_profit_stats = {}
    for trade_instrument, by_date in daily_instrument_pnl.items():
        losing_day_values = [pnl for pnl in by_date.values() if pnl < 0]
        profitable_day_values = [pnl for pnl in by_date.values() if pnl > 0]
        instrument_loss_stats[trade_instrument] = {
            "days": len(losing_day_values),
            "pnl": sum(losing_day_values),
        }
        instrument_profit_stats[trade_instrument] = {
            "days": len(profitable_day_values),
            "pnl": sum(profitable_day_values),
        }
    
    net_profit_days = sum(1 for pnl in daily_pnl.values() if pnl > 0)
    net_loss_days = sum(1 for pnl in daily_pnl.values() if pnl < 0)
    total_days = len(daily_pnl)
    
    # Calculate combined daily statistics
    both_profit_days = 0
    both_loss_days = 0
    mixed_profit_days = 0
    mixed_loss_days = 0
    both_profit_pnl = 0.0
    both_loss_pnl = 0.0
    mixed_profit_pnl = 0.0
    mixed_loss_pnl = 0.0
    
    for date in daily_pnl:
        rev_pnl = daily_rev_pnl.get(date, 0.0)
        no_rev_pnl = daily_no_rev_pnl.get(date, 0.0)
        combined_pnl = daily_pnl[date]
        
        if rev_pnl > 0 and no_rev_pnl > 0:
            both_profit_days += 1
            both_profit_pnl += combined_pnl
        elif rev_pnl < 0 and no_rev_pnl < 0:
            both_loss_days += 1
            both_loss_pnl += combined_pnl
        elif combined_pnl > 0:
            mixed_profit_days += 1
            mixed_profit_pnl += combined_pnl
        elif combined_pnl < 0:
            mixed_loss_days += 1
            mixed_loss_pnl += combined_pnl

    rev_records = [r for r in records if r.get("mode", "").upper() == "REVERSE"]
    no_rev_records = [r for r in records if r.get("mode", "").upper() == "NO-REVERSE"]
    rev_pnl = sum(r["pnl"] for r in rev_records)
    no_rev_pnl = sum(r["pnl"] for r in no_rev_records)
    rev_wins = sum(1 for r in rev_records if r["pnl"] > 0)
    no_rev_wins = sum(1 for r in no_rev_records if r["pnl"] > 0)
    rev_losses = sum(1 for r in rev_records if r["pnl"] < 0)
    no_rev_losses = sum(1 for r in no_rev_records if r["pnl"] < 0)
    rev_avg_pnl = (rev_pnl / len(rev_records)) if rev_records else 0.0
    no_rev_avg_pnl = (no_rev_pnl / len(no_rev_records)) if no_rev_records else 0.0

    table_rows_html = ""
    table_day = None

    def append_day_total(day):
        day_pnl = daily_pnl.get(day, 0.0)
        day_class = "pnl-positive" if day_pnl >= 0 else "pnl-negative"
        day_amount = f"+Rs. {day_pnl:,.2f}" if day_pnl >= 0 else f"-Rs. {abs(day_pnl):,.2f}"
        return f"""
        <tr class="tfoot-total">
          <td colspan="12" style="text-align:right; font-weight:700; background:var(--surface2);">DAY TOTAL — {day}</td>
          <td class="mono pnl-cell {day_class}" style="font-weight:700; background:var(--surface2);">{day_amount}</td>
          <td style="background:var(--surface2);"></td>
          <td colspan="2" style="background:var(--surface2);"></td>
        </tr>"""

    for r in records:
        record_day = str(r.get("date", ""))
        if table_day is not None and record_day != table_day:
            table_rows_html += append_day_total(table_day)
        table_day = record_day

        pnl_val = r["pnl"]
        pnl_class = "pnl-positive" if pnl_val >= 0 else "pnl-negative"
        pnl_badge = f"+Rs. {pnl_val:,.2f}" if pnl_val >= 0 else f"-Rs. {abs(pnl_val):,.2f}"
        dir_val = r.get('direction', '')
        tag_class = "tag-buy" if dir_val == "BULLISH" else ("tag-sell" if dir_val == "BEARISH" else "badge-neutral")
        struct_val = r.get('structure', 'NAKED').upper()
        struct_clean = struct_val.lower()
        sell_st = r.get('sell_strike', '')
        if struct_val == 'NAKED' or sell_st in ('N/A', '', None):
            strikes_display = f"{r.get('buy_strike', '')} {r.get('opt_type', '')}"
        else:
            strikes_display = f"{r.get('buy_strike', '')}/{sell_st} {r.get('opt_type', '')}"

        mode_val = r.get('mode', 'DUAL').upper()
        try:
            min_unrealized_display = f"-Rs. {abs(float(r.get('min_unrealized_pnl'))):,.2f}"
        except (TypeError, ValueError):
            min_unrealized_display = "—"
        if mode_val == "REVERSE":
            mode_badge = '<span style="background:var(--purple-bg); color:var(--purple); border:1px solid var(--purple-border); padding:2px 8px; border-radius:4px; font-size:11px; font-weight:700;">REVERSE</span>'
        elif mode_val == "NO-REVERSE":
            mode_badge = '<span style="background:var(--accent-bg); color:var(--accent); border:1px solid var(--border); padding:2px 8px; border-radius:4px; font-size:11px; font-weight:700;">NO-REVERSE</span>'
        else:
            mode_badge = f'<span class="badge-neutral">{mode_val}</span>'

        exit_rsn = r.get('exit_reason', '')
        reason_badge_class = (
            "badge-success" if "PROFIT" in exit_rsn.upper()
            else ("badge-danger" if "LOSS" in exit_rsn.upper() else "badge-neutral")
        )
        reason_label = exit_rsn.replace("EXIT_", "").replace("_", " ").title()
        trade_rsn = r.get('trade_reason', '—')

        entry_premium = r.get('entry_premium', '—')
        exit_premium = r.get('exit_premium', '—')
        entry_premium_display = f"Rs.{entry_premium}" if entry_premium != '—' else "—"
        exit_premium_display = f"Rs.{exit_premium}" if exit_premium != '—' else "—"

        expiry_val = r.get('expiry', 'NO')
        date_display = r.get('date', '')
        if expiry_val == 'YES':
            date_display = f'<span class="expiry-date">{date_display}</span>'

        trade_instr = r.get('instrument', 'NIFTY').upper()

        table_rows_html += f"""
        <tr>
          <td>{date_display}</td>
          <td class="mono"><strong>{trade_instr}</strong></td>
          <td>{mode_badge}</td>
          <td class="mono"><strong>{struct_val}</strong></td>
          <td class="mono">{r.get('gap_direction', '')} ({r.get('gap_size', '')})</td>
          <td class="mono">{r.get('trade_type', '')}</td>
          <td><span class="{tag_class}">{dir_val}</span></td>
          <td class="mono">{r.get('entry_time', '')} @ {r.get('entry_spot', '')}</td>
          <td class="mono">{strikes_display}</td>
          <td class="mono">{r.get('exit_time', '')} @ {r.get('exit_spot', '')}</td>
          <td class="reason-cell">{trade_rsn}</td>
          <td><span class="status-badge {reason_badge_class}">{reason_label}</span></td>
          <td class="mono pnl-cell {pnl_class}" style="white-space:nowrap;"><strong>{pnl_badge}</strong>&nbsp;<button class="pnl-icon-btn" title="30-min P/L breakdown" onclick="open30MinModal('{r.get('date','')}', '{trade_instr}', '{struct_clean}')">&#128202;</button></td>
          <td class="mono pnl-cell pnl-negative" style="white-space:nowrap;">{min_unrealized_display}</td>
          <td class="mono premium-cell">{entry_premium_display}</td>
          <td class="mono premium-cell">{exit_premium_display}</td>
        </tr>"""

    if table_day is not None:
        table_rows_html += append_day_total(table_day)

    total_pnl_class = "pnl-positive" if total_pnl >= 0 else "pnl-negative"
    total_pnl_formatted = f"+Rs. {total_pnl:,.2f}" if total_pnl >= 0 else f"-Rs. {abs(total_pnl):,.2f}"
    rev_pnl_formatted = f"+Rs. {rev_pnl:,.2f}" if rev_pnl >= 0 else f"-Rs. {abs(rev_pnl):,.2f}"
    no_rev_pnl_formatted = f"+Rs. {no_rev_pnl:,.2f}" if no_rev_pnl >= 0 else f"-Rs. {abs(no_rev_pnl):,.2f}"
    rev_avg_formatted = f"+Rs. {rev_avg_pnl:,.2f}" if rev_avg_pnl >= 0 else f"-Rs. {abs(rev_avg_pnl):,.2f}"
    no_rev_avg_formatted = f"+Rs. {no_rev_avg_pnl:,.2f}" if no_rev_avg_pnl >= 0 else f"-Rs. {abs(no_rev_avg_pnl):,.2f}"
    
    avg_win_formatted = f"+Rs. {avg_win:,.2f}" if avg_win >= 0 else f"-Rs. {abs(avg_win):,.2f}"
    avg_loss_formatted = f"-Rs. {abs(avg_loss):,.2f}"
    max_profit_formatted = f"+Rs. {max_profit:,.2f}" if max_profit >= 0 else f"-Rs. {abs(max_profit):,.2f}"
    max_loss_formatted = f"-Rs. {abs(max_loss):,.2f}"
    daily_avg_pnl = (total_pnl / total_days) if total_days > 0 else 0.0
    daily_avg_formatted = f"+Rs. {daily_avg_pnl:,.2f}" if daily_avg_pnl >= 0 else f"-Rs. {abs(daily_avg_pnl):,.2f}"
    
    both_profit_pnl_formatted = f"+Rs. {both_profit_pnl:,.2f}" if both_profit_pnl >= 0 else f"-Rs. {abs(both_profit_pnl):,.2f}"
    both_loss_pnl_formatted = f"-Rs. {abs(both_loss_pnl):,.2f}"
    mixed_profit_pnl_formatted = f"+Rs. {mixed_profit_pnl:,.2f}" if mixed_profit_pnl >= 0 else f"-Rs. {abs(mixed_profit_pnl):,.2f}"
    mixed_loss_pnl_formatted = f"-Rs. {abs(mixed_loss_pnl):,.2f}"
    nifty_loss_days = instrument_loss_stats["NIFTY"]["days"]
    nifty_loss_pnl = instrument_loss_stats["NIFTY"]["pnl"]
    sensex_loss_days = instrument_loss_stats["SENSEX"]["days"]
    sensex_loss_pnl = instrument_loss_stats["SENSEX"]["pnl"]
    nifty_profit_days = instrument_profit_stats["NIFTY"]["days"]
    nifty_profit_pnl = instrument_profit_stats["NIFTY"]["pnl"]
    sensex_profit_days = instrument_profit_stats["SENSEX"]["days"]
    sensex_profit_pnl = instrument_profit_stats["SENSEX"]["pnl"]
    nifty_loss_pnl_formatted = f"-Rs. {abs(nifty_loss_pnl):,.2f}" if nifty_loss_pnl < 0 else "Rs. 0.00"
    sensex_loss_pnl_formatted = f"-Rs. {abs(sensex_loss_pnl):,.2f}" if sensex_loss_pnl < 0 else "Rs. 0.00"
    nifty_profit_pnl_formatted = f"+Rs. {nifty_profit_pnl:,.2f}" if nifty_profit_pnl > 0 else "Rs. 0.00"
    sensex_profit_pnl_formatted = f"+Rs. {sensex_profit_pnl:,.2f}" if sensex_profit_pnl > 0 else "Rs. 0.00"

    summary = f"""
    <div style="background:#ffffff; border:1px solid var(--border); border-radius:8px; padding:16px 20px; margin-bottom:16px; box-shadow:var(--shadow);">
      <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:12px; border-bottom:1px solid var(--border-light); padding-bottom:10px;">
        <div>
          <span style="font-size:17px; font-weight:700; color:var(--text);">&#127919; DUAL STRATEGY OVERALL NET RESULT</span>
          <span style="font-size:12px; color:var(--text-muted); margin-left:8px;">(Executing Both REVERSE &amp; NO-REVERSE Trades at a Time)</span>
        </div>
        <div style="font-size:12px; color:var(--text-muted);">Source: <strong>{fn}</strong></div>
      </div>
      
      <!-- Primary Metrics -->
      <div class="summary-bar" style="margin-bottom:10px; background:var(--surface2);">
        <span class="s-item"><span class="s-lbl">Total Trades</span><strong>{total_trades}</strong></span>
        <span class="s-item"><span class="s-lbl">Win Rate</span><strong>{win_rate:.1f}% ({wins}W / {losses}L)</strong></span>
        <span class="s-item"><span class="s-lbl">Gross Profit</span><strong style="color:var(--green);">+Rs. {gross_profit:,.2f}</strong></span>
        <span class="s-item"><span class="s-lbl">Gross Loss</span><strong style="color:var(--red);">-Rs. {gross_loss:,.2f}</strong></span>
        <span class="s-item"><span class="s-lbl">Profit Factor</span><strong>{profit_factor:.2f}</strong></span>
        <span class="s-item"><span class="s-lbl">Avg / Trade</span><strong>Rs. {'+' if avg_pnl >= 0 else '-'}{abs(avg_pnl):,.2f}</strong></span>
        <span class="s-item"><span class="s-lbl">OVERALL NET P&amp;L</span><strong class="{total_pnl_class}" style="font-size:18px;">{total_pnl_formatted}</strong></span>
      </div>
      
      <!-- Detailed Statistics -->
      <div class="summary-bar" style="margin-bottom:10px; background:var(--bg);">
        <span class="s-item"><span class="s-lbl">Avg Win</span><strong style="color:var(--green);">{avg_win_formatted}</strong></span>
        <span class="s-item"><span class="s-lbl">Avg Loss</span><strong style="color:var(--red);">{avg_loss_formatted}</strong></span>
        <span class="s-item"><span class="s-lbl">Max Profit</span><strong style="color:var(--green);">{max_profit_formatted}</strong></span>
        <span class="s-item"><span class="s-lbl">Max Loss</span><strong style="color:var(--red);">{max_loss_formatted}</strong></span>
        <span class="s-item"><span class="s-lbl">Total Days</span><strong>{total_days}</strong></span>
        <span class="s-item"><span class="s-lbl">Net Profit Days</span><strong style="color:var(--green);">{net_profit_days}</strong></span>
        <span class="s-item"><span class="s-lbl">Net Loss Days</span><strong style="color:var(--red);">{net_loss_days}</strong></span>
        <span class="s-item"><span class="s-lbl">Daily Avg P&amp;L</span><strong>{daily_avg_formatted}</strong></span>
      </div>
      
      <!-- Combined Strategy Day Analysis -->
      <div style="background:var(--surface2); border:1px solid var(--border); border-radius:8px; padding:12px 16px; margin-bottom:10px;">
        <div style="font-size:13px; font-weight:700; color:var(--text); margin-bottom:10px; border-bottom:1px solid var(--border-light); padding-bottom:8px;">
          &#128200; COMBINED STRATEGY DAY ANALYSIS (Both Legs Together)
        </div>
        <div style="display:grid; grid-template-columns: repeat(4, 1fr); gap:12px; font-size:12px;">
          <div style="background:var(--green-bg); border:1px solid var(--green-border); padding:8px 12px; border-radius:6px;">
            <div style="color:var(--green); font-weight:700; font-size:11px; margin-bottom:4px;">BOTH PROFIT DAYS</div>
            <div style="font-size:18px; font-weight:700; color:var(--text);">{both_profit_days}</div>
            <div style="font-size:11px; color:var(--text-muted); margin-top:2px;">{both_profit_pnl_formatted}</div>
          </div>
          <div style="background:var(--red-bg); border:1px solid var(--red-border); padding:8px 12px; border-radius:6px;">
            <div style="color:var(--red); font-weight:700; font-size:11px; margin-bottom:4px;">BOTH LOSS DAYS</div>
            <div style="font-size:18px; font-weight:700; color:var(--text);">{both_loss_days}</div>
            <div style="font-size:11px; color:var(--text-muted); margin-top:2px;">{both_loss_pnl_formatted}</div>
          </div>
          <div style="background:var(--accent-bg); border:1px solid var(--border); padding:8px 12px; border-radius:6px;">
            <div style="color:var(--accent); font-weight:700; font-size:11px; margin-bottom:4px;">MIXED PROFIT DAYS</div>
            <div style="font-size:18px; font-weight:700; color:var(--text);">{mixed_profit_days}</div>
            <div style="font-size:11px; color:var(--text-muted); margin-top:2px;">{mixed_profit_pnl_formatted}</div>
          </div>
          <div style="background:var(--purple-bg); border:1px solid var(--purple-border); padding:8px 12px; border-radius:6px;">
            <div style="color:var(--purple); font-weight:700; font-size:11px; margin-bottom:4px;">MIXED LOSS DAYS</div>
            <div style="font-size:18px; font-weight:700; color:var(--text);">{mixed_loss_days}</div>
            <div style="font-size:11px; color:var(--text-muted); margin-top:2px;">{mixed_loss_pnl_formatted}</div>
          </div>
          <div style="background:var(--red-bg); border:1px solid var(--red-border); padding:8px 12px; border-radius:6px;">
            <div style="color:var(--red); font-weight:700; font-size:11px; margin-bottom:4px;">NIFTY LOSS DAYS</div>
            <div style="font-size:18px; font-weight:700; color:var(--text);">{nifty_loss_days}</div>
            <div style="font-size:11px; color:var(--text-muted); margin-top:2px;">{nifty_loss_pnl_formatted}</div>
          </div>
          <div style="background:var(--red-bg); border:1px solid var(--red-border); padding:8px 12px; border-radius:6px;">
            <div style="color:var(--red); font-weight:700; font-size:11px; margin-bottom:4px;">SENSEX LOSS DAYS</div>
            <div style="font-size:18px; font-weight:700; color:var(--text);">{sensex_loss_days}</div>
            <div style="font-size:11px; color:var(--text-muted); margin-top:2px;">{sensex_loss_pnl_formatted}</div>
          </div>
          <div style="background:var(--green-bg); border:1px solid var(--green-border); padding:8px 12px; border-radius:6px;">
            <div style="color:var(--green); font-weight:700; font-size:11px; margin-bottom:4px;">NIFTY PROFIT DAYS</div>
            <div style="font-size:18px; font-weight:700; color:var(--text);">{nifty_profit_days}</div>
            <div style="font-size:11px; color:var(--text-muted); margin-top:2px;">{nifty_profit_pnl_formatted}</div>
          </div>
          <div style="background:var(--green-bg); border:1px solid var(--green-border); padding:8px 12px; border-radius:6px;">
            <div style="color:var(--green); font-weight:700; font-size:11px; margin-bottom:4px;">SENSEX PROFIT DAYS</div>
            <div style="font-size:18px; font-weight:700; color:var(--text);">{sensex_profit_days}</div>
            <div style="font-size:11px; color:var(--text-muted); margin-top:2px;">{sensex_profit_pnl_formatted}</div>
          </div>
        </div>
        <div style="font-size:11px; color:var(--text-muted); margin-top:8px; text-align:center;">
          Instrument loss days count a day when that instrument’s NIFTY or SENSEX trades combined across both modes have negative net P&amp;L. <strong>BOTH PROFIT:</strong> Both strategies profitable | <strong>BOTH LOSS:</strong> Both strategies loss-making | <strong>MIXED:</strong> One profit, one loss (combined net shown)
        </div>
      </div>
      
      <!-- Mode Breakdown -->
      <div style="display:flex; gap:16px; font-size:12px;">
        <div style="flex:1; background:var(--purple-bg); border:1px solid var(--purple-border); padding:8px 14px; border-radius:6px;">
          <strong style="color:var(--purple); font-size:13px;">REVERSE Leg (Fade):</strong>
          <div style="margin-top:4px;">
            <span style="display:block;">{len(rev_records)} trades ({rev_wins}W / {rev_losses}L)</span>
            <span style="display:block;">Net P&amp;L: <strong>{rev_pnl_formatted}</strong></span>
            <span style="display:block;">Avg / Trade: <strong>{rev_avg_formatted}</strong></span>
          </div>
        </div>
        <div style="flex:1; background:var(--accent-bg); border:1px solid var(--border); padding:8px 14px; border-radius:6px;">
          <strong style="color:var(--accent); font-size:13px;">NO-REVERSE Leg (Follow):</strong>
          <div style="margin-top:4px;">
            <span style="display:block;">{len(no_rev_records)} trades ({no_rev_wins}W / {no_rev_losses}L)</span>
            <span style="display:block;">Net P&amp;L: <strong>{no_rev_pnl_formatted}</strong></span>
            <span style="display:block;">Avg / Trade: <strong>{no_rev_avg_formatted}</strong></span>
          </div>
        </div>
      </div>
    </div>"""

    table_html = f"""
    <div class="dual-backtest-scroll-top" aria-label="Scroll dual backtest table horizontally"><div></div></div>
    <div class="table-wrap dual-backtest-table-wrap">
      <table>
        <thead>
          <tr>
            <th>Date</th>
            <th>Instrument</th>
            <th>Mode</th>
            <th>Structure</th>
            <th>Gap</th>
            <th>Trade Type</th>
            <th>Direction</th>
            <th>Entry</th>
            <th>Strikes</th>
            <th>Exit</th>
            <th class="reason-col">Trade Reason</th>
            <th>Exit Reason</th>
            <th>P&amp;L</th>
            <th>min_unrealized_pnl</th>
            <th>Entry Premium</th>
            <th>Exit Premium</th>
          </tr>
        </thead>
        <tbody>
          {table_rows_html}
        </tbody>
        <tfoot>
          <tr class="tfoot-total">
            <td colspan="12" style="text-align:right; font-weight:700; font-size:13px; padding:12px 14px; background:var(--surface2);">
              OVERALL DUAL NET P&amp;L ({total_trades} trades from both legs):
            </td>
            <td class="mono pnl-cell {total_pnl_class}" style="font-size:16px; font-weight:700; padding:12px 14px; background:var(--surface2);">
              {total_pnl_formatted}
            </td>
            <td style="background:var(--surface2);"></td>
            <td colspan="2" style="background:var(--surface2);"></td>
          </tr>
        </tfoot>
      </table>
    </div>
    """
    return summary + table_html, total_trades, total_pnl



def build_page(selected_date: str = None, selected_slot: str = SLOT_ALL,
               selected_instrument: str = INSTR_BN) -> str:
    now_str = datetime.now(IST).strftime("%d-%b-%Y %H:%M:%S IST")
    today   = datetime.now(IST).strftime("%d-%m-%Y")
    if not selected_date:
        selected_date = today
    if selected_slot not in SLOT_LABELS:
        selected_slot = SLOT_ALL
    if selected_instrument.upper() not in INSTR_LABELS:
        selected_instrument = INSTR_BN
    selected_instrument = selected_instrument.upper()

    available_dates = get_available_dates()
    if selected_date not in available_dates and available_dates:
        selected_date = available_dates[0]

    slot_label = SLOT_LABELS[selected_slot]
    instr_label = INSTR_LABELS[selected_instrument]

    # Date dropdown
    date_options = ""
    for d in available_dates:
        sel = ' selected' if d == selected_date else ''
        date_options += f'<option value="{d}"{sel}>{d}</option>'
    if not available_dates:
        date_options = f'<option value="{today}" selected>{today} (no data)</option>'

    # Slot dropdown
    slot_options = ""
    for key, label in SLOT_LABELS.items():
        sel = ' selected' if key == selected_slot else ''
        slot_options += f'<option value="{key}"{sel}>{label}</option>'

    # Instrument dropdown
    instr_options = ""
    for key, label in INSTR_LABELS.items():
        sel = ' selected' if key == selected_instrument else ''
        instr_options += f'<option value="{key}"{sel}>{label}</option>'

    all_tabs_btn  = ""
    all_tabs_pane = ""

    # ── 1. Active Data Tab (BankNifty or SENSEX based on dropdown selection) ──
    for label, prefix in INDEX_MAP.items():
        tab_id = f"data-{label.lower()}"
        is_active = (label == selected_instrument)
        active_tab  = "active" if is_active else ""
        active_pane = "show"   if is_active else ""

        table_html, total, breakouts = build_data_tab(label, prefix, selected_date, selected_slot)

        summary = f"""
        <div class="summary-bar">
          <span class="s-item"><span class="s-lbl">Index Data</span><strong>{label}</strong></span>
          <span class="s-item"><span class="s-lbl">Date</span><strong>{selected_date}</strong></span>
          <span class="s-item"><span class="s-lbl">Time Slot</span><strong>{slot_label}</strong></span>
          <span class="s-item"><span class="s-lbl">Records</span><strong>{total}</strong></span>
          <span class="s-item"><span class="s-lbl">Breakouts</span><strong class="text-alert">{breakouts}</strong></span>
          <span class="s-item"><span class="s-lbl">CSV</span><strong>{prefix}-{selected_date}.csv</strong></span>
        </div>"""

        all_tabs_btn += f"""
        <button class="tab-btn {active_tab}" data-target="{tab_id}" onclick="switchTab(this)">
          {label} Data
          <span class="tab-pill">{total}</span>
        </button>"""

        all_tabs_pane += f"""
        <div id="{tab_id}" class="tab-pane {active_pane}">
          {summary}
          {table_html}
        </div>"""

    # ── 2. Bank Nifty Trades Tab (trades_BN.csv) ──
    bn_trades_html, bn_trades_count, _ = build_trades_tab_content(INSTR_BN, selected_date, selected_slot)
    all_tabs_btn += f"""
    <button class="tab-btn" data-target="bn-trades-tab" onclick="switchTab(this)">
      Bank Nifty Trades
      <span class="tab-pill pos-pill">{bn_trades_count}</span>
    </button>"""

    all_tabs_pane += f"""
    <div id="bn-trades-tab" class="tab-pane">
      {bn_trades_html}
    </div>"""

    # ── 3. SENSEX Trades Tab (trades_SENSEX.csv) ──
    sx_trades_html, sx_trades_count, _ = build_trades_tab_content(INSTR_SX, selected_date, selected_slot)
    all_tabs_btn += f"""
    <button class="tab-btn" data-target="sx-trades-tab" onclick="switchTab(this)">
      SENSEX Trades
      <span class="tab-pill pos-pill-sx">{sx_trades_count}</span>
    </button>"""

    all_tabs_pane += f"""
    <div id="sx-trades-tab" class="tab-pane">
      {sx_trades_html}
    </div>"""

    # ── 4. Activity Log Tab (Combined or instrument specific) ──
    all_tabs_btn += """
    <button class="tab-btn" data-target="log-tab" onclick="switchTab(this)">
      Activity Log
      <span class="tab-pill log-pill">LOG</span>
    </button>"""

    log_pane_html = ""
    for label, prefix in INDEX_MAP.items():
        log_html, total, breakouts = build_log_tab(label, prefix, selected_date, selected_slot)
        log_pane_html += f"""
        <div class="log-section">
          <div class="log-section-header">
            {label}
            <span class="s-lbl">&nbsp;|&nbsp; {total} records, {breakouts} breakouts</span>
          </div>
          {log_html}
        </div>"""

    all_tabs_pane += f"""
    <div id="log-tab" class="tab-pane">
      <div class="summary-bar">
        <span class="s-item"><span class="s-lbl">View</span><strong>Activity Log</strong></span>
        <span class="s-item"><span class="s-lbl">Date</span><strong>{selected_date}</strong></span>
        <span class="s-item"><span class="s-lbl">Time Slot</span><strong>{slot_label}</strong></span>
        <span class="s-item"><span class="s-lbl">Breakout threshold</span><strong>Rs.{MIN_BREAKOUT_AMOUNT:.0f}+ move</strong></span>
      </div>
      {log_pane_html}
    </div>"""

    # ── 5. Dual Backtest Tab (Both Trades at a Time) ──
    dual_backtest_html, dual_backtest_count, _ = build_dual_backtest_summary_content("COMBINATION")
    strategy_pnl_json = json.dumps(build_strategy_pnl_data(), ensure_ascii=False).replace("</", "<\\/")
    all_tabs_btn += f"""
    <button class="tab-btn" data-target="dual-backtest-tab" onclick="switchTab(this)">
      🎯 Dual Backtest
      <span class="tab-pill" style="background:var(--purple-bg); border-color:var(--purple-border); color:var(--purple); font-weight:700;">{dual_backtest_count}</span>
    </button>"""

    all_tabs_pane += f"""
    <div id="dual-backtest-tab" class="tab-pane">
      {dual_backtest_html}
    </div>"""

    # Modal summaries for instruments
    bn_summary_html, bn_summary_count, _ = build_trades_summary_content(INSTR_BN)
    sx_summary_html, sx_summary_count, _ = build_trades_summary_content(INSTR_SX)

    bn_daybreakout_html, bn_daybreakout_count, _ = build_daybreakout_summary_content("NIFTY")
    sx_daybreakout_html, sx_daybreakout_count, _ = build_daybreakout_summary_content("SENSEX")
    comb_daybreakout_html, comb_daybreakout_count, _ = build_daybreakout_summary_content("COMBINATION")

    # 3-Leg strategy summary
    threeleg_html, threeleg_count, _ = build_3leg_summary_content()

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1.0"/>
<title>Breakout Dashboard &amp; Trades — {selected_date}</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700&family=JetBrains+Mono:wght@400;500;600;700&display=swap" rel="stylesheet">
<style>
  :root {{
    --bg:          #f5f7fa;
    --surface:     #ffffff;
    --surface2:    #f0f2f5;
    --border:      #d0d7de;
    --border-light:#e4e8ed;
    --accent:      #0969da;
    --accent-bg:   #dbeafe;
    --green:       #1a7f37;
    --green-bg:    #dcfce7;
    --green-border:#86efac;
    --red:         #cf222e;
    --red-bg:      #ffe4e6;
    --red-border:  #fca5a5;
    --purple:      #7e22ce;
    --purple-bg:   #f3e8ff;
    --purple-border:#d8b4fe;
    --text:        #1f2328;
    --text-muted:  #656d76;
    --text-light:  #8c959f;
    --shadow:      0 1px 3px rgba(0,0,0,0.08), 0 1px 2px rgba(0,0,0,0.04);
    --radius:      8px;
    --radius-sm:   5px;
    --header-h:    57px;
    --bar-h:       34px;
  }}

  * {{ box-sizing: border-box; margin: 0; padding: 0; }}

  body {{
    font-family: 'Inter', system-ui, sans-serif;
    background: var(--bg);
    color: var(--text);
    min-height: 100vh;
  }}

  /* ── TOP HEADER ── */
  .header {{
    background: var(--surface);
    border-bottom: 1px solid var(--border);
    padding: 0 24px;
    height: var(--header-h);
    display: flex;
    align-items: center;
    justify-content: space-between;
    position: sticky;
    top: 0;
    z-index: 200;
    box-shadow: var(--shadow);
  }}
  .header-left {{ display: flex; align-items: center; gap: 12px; }}
  .logo {{
    width: 32px; height: 32px; border-radius: 7px;
    background: linear-gradient(135deg, #0969da, #388bfd);
    display: flex; align-items: center; justify-content: center;
    font-size: 15px; font-weight: 700; color: #fff;
    box-shadow: 0 2px 4px rgba(9,105,218,0.3);
  }}
  .app-title  {{ font-size: 15px; font-weight: 600; color: var(--text); }}
  .app-sub    {{ font-size: 11px; color: var(--text-muted); margin-top: 1px; }}
  .header-right {{ display: flex; align-items: center; gap: 14px; }}
  .live-indicator {{
    display: flex; align-items: center; gap: 6px;
    font-size: 12px; color: var(--green); font-weight: 500;
  }}
  .live-dot {{
    width: 7px; height: 7px; border-radius: 50%;
    background: var(--green);
    animation: blink 2s infinite;
  }}
  @keyframes blink {{ 0%,100%{{opacity:1}} 50%{{opacity:0.3}} }}
  .updated-label {{ font-size: 12px; color: var(--text-muted); }}
  .btn-refresh {{
    background: var(--surface2);
    border: 1px solid var(--border);
    color: var(--text);
    padding: 5px 13px;
    border-radius: var(--radius-sm);
    font-size: 12px; font-weight: 500;
    cursor: pointer; font-family: 'Inter', sans-serif;
    transition: all 0.15s;
  }}
  .btn-refresh:hover {{ background: var(--accent); color: #fff; border-color: var(--accent); }}
  .btn-summary {{
    background: var(--purple-bg);
    border: 1px solid var(--purple-border);
    color: var(--purple);
    padding: 5px 13px;
    border-radius: var(--radius-sm);
    font-size: 12px; font-weight: 600;
    cursor: pointer; font-family: 'Inter', sans-serif;
    transition: all 0.15s;
    margin-right: 8px;
  }}
  .btn-summary:last-child {{ margin-right: 0; }}
  .btn-summary:hover {{ background: var(--purple); color: #fff; border-color: var(--purple); }}

  /* ── FILTER BAR (Date, Time Slot, Instrument) ── */
  .date-bar {{
    background: var(--surface);
    border-bottom: 1px solid var(--border-light);
    padding: 6px 24px;
    display: flex; align-items: center; gap: 12px;
    font-size: 12px; color: var(--text-muted);
    position: sticky;
    top: var(--header-h);
    z-index: 195;
    box-shadow: 0 1px 3px rgba(0,0,0,0.04);
  }}
  .date-bar label {{
    font-weight: 600; font-size: 11px;
    text-transform: uppercase; letter-spacing: 0.5px;
    color: var(--text-muted);
  }}
  .date-select {{
    border: 1px solid var(--border);
    border-radius: var(--radius-sm);
    padding: 4px 10px;
    font-size: 12px; font-family: 'JetBrains Mono', monospace;
    font-weight: 500; color: var(--text);
    background: var(--surface2);
    cursor: pointer;
    transition: border-color 0.15s;
  }}
  .date-select:focus {{ outline: none; border-color: var(--accent); box-shadow: 0 0 0 2px rgba(9,105,218,0.15); }}

  .instr-select {{
    border: 1px solid var(--purple-border);
    border-radius: var(--radius-sm);
    padding: 4px 12px;
    font-size: 12px; font-family: 'Inter', sans-serif;
    font-weight: 700; color: var(--purple);
    background: var(--purple-bg);
    cursor: pointer;
    transition: border-color 0.15s;
  }}
  .instr-select:focus {{ outline: none; border-color: var(--purple); box-shadow: 0 0 0 2px rgba(126,34,206,0.15); }}

  .date-today-badge {{
    background: var(--accent-bg);
    color: var(--accent);
    border: 1px solid #93c5fd;
    padding: 2px 8px; border-radius: 10px;
    font-size: 10px; font-weight: 600;
    font-family: 'Inter', sans-serif;
    display: {{'inline' if selected_date == today else 'none'}};
  }}
  .date-bar-divider {{
    width: 1px; height: 20px;
    background: var(--border-light);
    margin: 0 4px;
  }}

  /* ── COUNTDOWN BAR ── */
  .cbar {{
    background: var(--surface2);
    border-bottom: 1px solid var(--border-light);
    height: var(--bar-h);
    padding: 0 24px;
    display: flex; align-items: center; gap: 10px;
    font-size: 12px; color: var(--text-muted);
    position: sticky;
    top: calc(var(--header-h) + 37px);
    z-index: 190;
  }}
  .cbar progress {{
    flex: 1; height: 4px; border: none;
    border-radius: 2px; background: var(--border-light);
  }}
  .cbar progress::-webkit-progress-bar   {{ background: var(--border-light); border-radius:2px; }}
  .cbar progress::-webkit-progress-value {{ background: var(--accent); border-radius:2px; transition: width 1s linear; }}
  .cbar progress::-moz-progress-bar      {{ background: var(--accent); }}
  #timer-text {{
    font-family: 'JetBrains Mono', monospace; font-size: 12px;
    color: var(--accent); min-width: 38px; font-weight: 500;
  }}

  /* ── TABS ROW ── */
  .tabs-row {{
    display: flex; gap: 0; padding: 12px 24px 0;
    background: var(--bg);
    border-bottom: 2px solid var(--border-light);
    position: sticky;
    top: calc(var(--header-h) + 37px + var(--bar-h));
    z-index: 180;
  }}
  .tab-btn {{
    background: transparent; border: none;
    border-bottom: 2px solid transparent; margin-bottom: -2px;
    color: var(--text-muted); padding: 7px 18px;
    font-size: 13px; font-weight: 500; cursor: pointer;
    display: flex; align-items: center; gap: 7px;
    transition: all 0.15s; font-family: 'Inter', sans-serif;
  }}
  .tab-btn:hover {{ color: var(--text); }}
  .tab-btn.active {{ color: var(--accent); border-bottom-color: var(--accent); font-weight: 600; }}
  .tab-pill {{
    background: var(--surface2); border: 1px solid var(--border);
    color: var(--text-muted); padding: 1px 7px; border-radius: 10px;
    font-size: 10px; font-weight: 500; font-family: 'JetBrains Mono', monospace;
  }}
  .log-pill {{ background: var(--accent-bg); border-color: #93c5fd; color: var(--accent); }}
  .pos-pill {{ background: var(--green-bg); border-color: var(--green-border); color: var(--green); }}
  .pos-pill-sx {{ background: var(--purple-bg); border-color: var(--purple-border); color: var(--purple); }}
  .tab-btn.active .tab-pill {{ background: var(--accent-bg); border-color: #93c5fd; color: var(--accent); }}
  .tab-btn.active .pos-pill {{ background: var(--green-bg); border-color: var(--green-border); color: var(--green); }}
  .tab-btn.active .pos-pill-sx {{ background: var(--purple-bg); border-color: var(--purple-border); color: var(--purple); }}

  /* ── TRADES SUMMARY MODAL ── */
  .modal-overlay {{
    display: none; position: fixed; inset: 0;
    background: rgba(15,23,42,0.45); z-index: 1000;
    align-items: stretch; justify-content: stretch;
    padding: 0;
  }}
  .modal-overlay.show {{ display: flex; }}
  .modal-box {{
    width: 100vw; height: 100vh;
    max-width: 100vw; max-height: 100vh;
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: 0;
    box-shadow: 0 20px 50px rgba(0,0,0,0.22);
    display: flex;
    flex-direction: column;
    overflow: hidden;
  }}
  .modal-header {{
    display: flex; align-items: center; justify-content: space-between;
    padding: 14px 20px; border-bottom: 1px solid var(--border-light);
    background: var(--surface2); flex-shrink: 0;
  }}
  .modal-header h2 {{
    font-size: 16px; font-weight: 600; color: var(--text);
    display: flex; align-items: center; gap: 10px;
  }}
  .modal-badge {{
    background: var(--purple-bg); color: var(--purple); border: 1px solid var(--purple-border);
    padding: 2px 9px; border-radius: 10px;
    font-size: 11px; font-weight: 600; font-family: 'JetBrains Mono', monospace;
  }}
  .modal-close {{
    background: transparent; border: none; font-size: 26px; line-height: 1;
    color: var(--text-muted); cursor: pointer; padding: 0 4px; transition: color 0.15s;
  }}
  .modal-close:hover {{ color: var(--red); }}
  .modal-body {{
    flex: 1; min-height: 0; overflow-x: auto; overflow-y: auto; padding: 20px; background: var(--bg);
  }}
  #dual-backtest-summary-modal {{ padding: 0; align-items: stretch; justify-content: stretch; }}
  #dual-backtest-summary-modal .modal-box {{
    width: 100vw; height: 100vh; max-width: 100vw; max-height: 100vh;
    border-radius: 0; display: flex; flex-direction: column;
  }}
  #dual-backtest-summary-modal .modal-body {{ min-height: 0; padding: 12px; }}
  .modal-scroll-top, .dual-backtest-scroll-top {{
    position: sticky; top: 0; z-index: 20;
    height: 16px; overflow-x: auto; overflow-y: hidden; margin: 0 0 4px;
    background: var(--surface); scrollbar-width: auto;
  }}
  .modal-scroll-top > div, .dual-backtest-scroll-top > div {{ height: 1px; }}
  .modal-body .summary-bar {{ margin-bottom: 16px; }}
  .date-group-cell {{
    vertical-align: middle; font-weight: 600;
    background: var(--surface2); border-right: 1px solid var(--border-light);
  }}
  .pnl-icon-btn {{
    background: transparent; border: 1px solid var(--border);
    border-radius: 4px; padding: 1px 4px; font-size: 11px;
    cursor: pointer; vertical-align: middle; transition: background 0.15s, border-color 0.15s;
  }}
  .pnl-icon-btn:hover {{ background: var(--accent-bg); border-color: var(--accent); }}

  /* ── CONTENT ── */
  .content {{ padding: 0 24px 32px; overflow-x: auto; }}
  .tab-pane {{ display: none; padding-top: 16px; }}
  .tab-pane.show {{ display: block; }}

  /* ── SUMMARY BAR ── */
  .summary-bar {{
    display: flex; flex-wrap: wrap; gap: 24px;
    background: var(--surface); border: 1px solid var(--border);
    border-radius: var(--radius); padding: 12px 20px;
    margin-bottom: 16px; box-shadow: var(--shadow);
  }}
  .s-item {{ display: flex; flex-direction: column; gap: 2px; }}
  .s-lbl {{ font-size: 10px; color: var(--text-muted); text-transform: uppercase; letter-spacing: 0.5px; }}
  .s-item strong {{ font-size: 14px; font-weight: 600; }}
  .text-alert {{ color: var(--red); }}

  /* ── TABLE WRAPPER ── */
  .table-wrap {{
    border: 1px solid var(--border); border-radius: var(--radius);
    box-shadow: var(--shadow); overflow-x: auto; overflow-y: visible; background: var(--surface);
  }}
  .table-wrap table {{
    width: 100%; border-collapse: separate; border-spacing: 0;
    font-size: 12.5px; white-space: nowrap;
  }}
  .table-wrap thead tr th {{
    position: sticky; top: 0; z-index: 10;
    background: var(--surface2); border-bottom: 2px solid var(--border);
    padding: 9px 14px; text-align: right;
    font-size: 11px; font-weight: 600; color: var(--text-muted);
    text-transform: uppercase; letter-spacing: 0.4px;
  }}
  .table-wrap thead tr th:first-child,
  .table-wrap thead tr th:nth-child(2) {{ text-align: left; }}
  .reason-col {{ min-width: 340px; text-align: left !important; }}

  tbody tr td {{
    padding: 8px 14px; border-bottom: 1px solid var(--border-light);
    text-align: right; color: var(--text);
    font-family: 'JetBrains Mono', monospace; font-size: 12px;
  }}
  tbody tr td:first-child, tbody tr td:nth-child(2) {{
    text-align: left; font-family: 'Inter', sans-serif;
  }}
  .reason-cell {{
    text-align: left !important; font-family: 'Inter', sans-serif !important;
    font-size: 12px !important; color: var(--text-muted);
    max-width: 440px; white-space: normal; line-height: 1.4;
  }}
  .mono {{ font-family: 'JetBrains Mono', monospace !important; }}
  tbody tr:last-child td {{ border-bottom: none; }}
  tbody tr:hover td {{ background: #f0f6ff; }}
  .breakout-row td {{ background: #fff5f5; border-bottom: 1px solid var(--red-border) !important; }}
  .breakout-row:hover td {{ background: #fee2e2; }}

  .tag-buy {{
    background: #dbeafe; color: #1e40af; border: 1px solid #93c5fd;
    padding: 1px 6px; border-radius: 3px; font-size: 10px; font-weight: 700; margin-right: 4px;
  }}
  .tag-sell {{
    background: #fef3c7; color: #92400e; border: 1px solid #fde68a;
    padding: 1px 6px; border-radius: 3px; font-size: 10px; font-weight: 700; margin-right: 4px;
  }}
  .price-sub {{ display: block; font-size: 11px; color: var(--text-muted); margin-top: 2px; }}

  .pnl-positive {{ color: var(--green) !important; }}
  .pnl-negative {{ color: var(--red) !important; }}
  .pnl-cell {{ font-weight: 600; font-size: 13px !important; }}
  .premium-cell {{ font-weight: 500; font-size: 12px !important; color: var(--text-muted); }}

  .status-badge {{
    padding: 3px 8px; border-radius: 4px; font-size: 11px; font-weight: 600;
    display: inline-block; font-family: 'Inter', sans-serif;
  }}
  .badge-success {{ background: var(--green-bg); color: var(--green); border: 1px solid var(--green-border); }}
  .badge-danger  {{ background: var(--red-bg); color: var(--red); border: 1px solid var(--red-border); }}
  .badge-neutral {{ background: var(--surface2); color: var(--text-muted); border: 1px solid var(--border); }}
  .expiry-badge {{ background: var(--red-bg); color: var(--red); border: 1px solid var(--red-border); padding: 2px 8px; border-radius: 4px; font-size: 11px; font-weight: 600; font-family: 'Inter', sans-serif; }}
  .expiry-date {{ color: var(--red); font-weight: 600; }}

  .badge-yes {{
    background: var(--red-bg); color: var(--red); border: 1px solid var(--red-border);
    padding: 2px 8px; border-radius: 4px; font-size: 11px; font-weight: 600;
    font-family: 'Inter', sans-serif;
  }}
  .badge-no {{ color: var(--text-light); font-size: 12px; }}
  .no-data {{ color: var(--text-muted); padding: 48px; text-align: center; font-size: 14px; }}

  .log-section {{ margin-bottom: 24px; }}
  .log-section-header {{
    font-size: 13px; font-weight: 600; color: var(--text);
    padding: 10px 4px; border-bottom: 1px solid var(--border-light); margin-bottom: 10px;
  }}
</style>
</head>
<body>

<div class="header">
  <div class="header-left">
    <div class="logo">B</div>
    <div>
      <div class="app-title">Breakout Strategy Dashboard &amp; Trades</div>
      <div class="app-sub">Angel One SmartAPI &mdash; Live Monitor &amp; Trade Records</div>
    </div>
  </div>
  <div class="header-right">
    <div class="live-indicator"><div class="live-dot"></div> Live</div>
    <div class="updated-label">Updated: <strong>{now_str}</strong></div>
    <button class="btn-refresh" onclick="location.reload()">&#8635; Refresh Now</button>
    <button class="btn-summary" onclick="openDualBacktestSummary()" style="background:#7e22ce; color:#ffffff; border-color:#d8b4fe; font-weight:700; box-shadow:0 1px 4px rgba(126,34,206,0.35);">&#127919; Dual Backtest Summary</button>
    <button class="btn-summary" onclick="openStrategyPnlGraph()" style="background:#0f766e; color:#ffffff; border-color:#5eead4; font-weight:700;">&#128200; Strategy P&amp;L Graph</button>
    <!-- Day‑Breakout summary buttons -->
    <button class="btn-summary" onclick="openDayBreakoutSummary('NIFTY')">&#128202; Day‑Breakout NIFTY</button>
    <button class="btn-summary" onclick="openDayBreakoutSummary('SENSEX')">&#128202; Day‑Breakout SENSEX</button>
    <button class="btn-summary" onclick="openDayBreakoutSummary('COMBINATION')">&#128202; Day‑Breakout Combination</button>
    <!-- 3-Leg Strategy summary button -->
    <button class="btn-summary" onclick="open3LegSummary()">&#128200; 3-Leg Strategy</button>
  </div>
</div>

<div class="date-bar">
  <label>&#128197; Date:</label>
  <select id="date-select" class="date-select" onchange="applyFilters()">
    {date_options}
  </select>
  <span class="date-today-badge">&#10003; Today</span>
  <span class="date-bar-divider"></span>
  <label>&#9201; Time Slot:</label>
  <select id="slot-select" class="date-select" onchange="applyFilters()">
    {slot_options}
  </select>
  <span class="date-bar-divider"></span>
  <label>&#128200; Instrument:</label>
  <select id="instr-select" class="instr-select" onchange="applyFilters()">
    {instr_options}
  </select>
  <span style="margin-left:auto; font-size:11px; color:var(--text-light);">{len(available_dates)} date folder(s) available</span>
</div>

<div class="cbar">
  <span>Next auto-refresh in</span>
  <span id="timer-text">5:00</span>
  <progress id="prog" value="300" max="300"></progress>
</div>

<div class="tabs-row">
  {all_tabs_btn}
</div>

<div class="content">
  {all_tabs_pane}
</div>

<div id="trades-summary-modal-bn" class="modal-overlay" onclick="closeTradesSummaryOnBackdrop(event, 'BANKNIFTY')">
  <div class="modal-box" role="dialog" aria-modal="true" aria-labelledby="trades-summary-title-bn">
    <div class="modal-header">
      <h2 id="trades-summary-title-bn">
        BankNifty Multi-Day Trades Summary
        <span class="modal-badge">{bn_summary_count} trades</span>
      </h2>
      <button type="button" class="modal-close" onclick="closeTradesSummary('BANKNIFTY')" aria-label="Close">&times;</button>
    </div>
    <div class="modal-body">
      {bn_summary_html}
    </div>
  </div>
</div>

<div id="trades-summary-modal-sx" class="modal-overlay" onclick="closeTradesSummaryOnBackdrop(event, 'SENSEX')">
  <div class="modal-box" role="dialog" aria-modal="true" aria-labelledby="trades-summary-title-sx">
    <div class="modal-header">
      <h2 id="trades-summary-title-sx">
        SENSEX Multi-Day Trades Summary
        <span class="modal-badge">{sx_summary_count} trades</span>
      </h2>
      <button type="button" class="modal-close" onclick="closeTradesSummary('SENSEX')" aria-label="Close">&times;</button>
    </div>
    <div class="modal-body">
      {sx_summary_html}
    </div>
  </div>
</div>

    <!-- Day‑Breakout Summary Modals -->
    <div id="daybreakout-summary-modal-bn" class="modal-overlay" onclick="closeDayBreakoutSummaryOnBackdrop(event, 'NIFTY')">
      <div class="modal-box" role="dialog" aria-modal="true" aria-labelledby="daybreakout-summary-title-bn">
        <div class="modal-header">
          <h2 id="daybreakout-summary-title-bn">
            NIFTY Day‑Breakout Summary
            <span class="modal-badge">{bn_daybreakout_count} entries</span>
          </h2>
          <button type="button" class="modal-close" onclick="closeDayBreakoutSummary('NIFTY')" aria-label="Close">&times;</button>
        </div>
        <div class="modal-body">
          {bn_daybreakout_html}
        </div>
      </div>
    </div>

    <div id="daybreakout-summary-modal-sx" class="modal-overlay" onclick="closeDayBreakoutSummaryOnBackdrop(event, 'SENSEX')">
      <div class="modal-box" role="dialog" aria-modal="true" aria-labelledby="daybreakout-summary-title-sx">
        <div class="modal-header">
          <h2 id="daybreakout-summary-title-sx">
            SENSEX Day‑Breakout Summary
            <span class="modal-badge">{sx_daybreakout_count} entries</span>
          </h2>
          <button type="button" class="modal-close" onclick="closeDayBreakoutSummary('SENSEX')" aria-label="Close">&times;</button>
        </div>
        <div class="modal-body">
          {sx_daybreakout_html}
        </div>
      </div>
    </div>

    <div id="daybreakout-summary-modal-comb" class="modal-overlay" onclick="closeDayBreakoutSummaryOnBackdrop(event, 'COMBINATION')">
      <div class="modal-box" role="dialog" aria-modal="true" aria-labelledby="daybreakout-summary-title-comb">
        <div class="modal-header">
          <h2 id="daybreakout-summary-title-comb">
            Combination Day‑Breakout Summary (Nearest Expiry)
            <span class="modal-badge">{comb_daybreakout_count} entries</span>
          </h2>
          <button type="button" class="modal-close" onclick="closeDayBreakoutSummary('COMBINATION')" aria-label="Close">&times;</button>
        </div>
        <div class="modal-body">
          {comb_daybreakout_html}
        </div>
      </div>
    </div>

    <!-- 3-Leg Strategy Summary Modal -->
    <div id="threeleg-summary-modal" class="modal-overlay" onclick="close3LegSummaryOnBackdrop(event)">
      <div class="modal-box" role="dialog" aria-modal="true" aria-labelledby="threeleg-summary-title">
        <div class="modal-header">
          <h2 id="threeleg-summary-title">
            3-Leg Strategy P&amp;L Analysis
            <span class="modal-badge">{threeleg_count} days</span>
          </h2>
          <button type="button" class="modal-close" onclick="close3LegSummary()" aria-label="Close">&times;</button>
        </div>
        <div class="modal-body">
          {threeleg_html}
        </div>
      </div>
    </div>

    <!-- Dual Backtest Summary Modal -->
    <div id="dual-backtest-summary-modal" class="modal-overlay" onclick="closeDualBacktestSummaryOnBackdrop(event)">
      <div class="modal-box" role="dialog" aria-modal="true" aria-labelledby="dual-backtest-summary-title" style="max-width:100vw; width:100vw;">
        <div class="modal-header">
          <h2 id="dual-backtest-summary-title">
            🎯 Dual Strategy Backtest (Both Trades at a Time &mdash; Nearest Expiry Combination)
            <span class="modal-badge">{dual_backtest_count} trades</span>
          </h2>
          <button type="button" class="modal-close" onclick="closeDualBacktestSummary()" aria-label="Close">&times;</button>
        </div>
        <div class="modal-body">
          {dual_backtest_html}
        </div>
      </div>
    </div>

    <div id="strategy-pnl-modal" class="modal-overlay" onclick="closeStrategyPnlGraphOnBackdrop(event)" style="padding:0;">
      <div class="modal-box" role="dialog" aria-modal="true" aria-labelledby="strategy-pnl-title" style="width:100vw;height:100vh;max-width:none;max-height:none;border-radius:0;">
        <div class="modal-header">
          <h2 id="strategy-pnl-title">Strategy P&amp;L Comparison · Backtest</h2>
          <button type="button" class="modal-close" onclick="closeStrategyPnlGraph()" aria-label="Close">&times;</button>
        </div>
        <div class="modal-body">
          <p class="no-data" style="margin-top:0;">Cumulative realized P&amp;L by trade date. Each line aggregates that strategy’s P&amp;L from the first backtest date onward. Solid = REVERSE; dashed = NO-REVERSE.</p>
          <div id="strategy-pnl-chart-wrap" style="position:relative;width:100%;height:calc(100vh - 230px);min-height:280px;background:var(--surface);border:1px solid var(--border);border-radius:10px;padding:12px;"><canvas id="strategy-pnl-canvas" style="width:100%;height:100%;"></canvas><div id="strategy-pnl-tooltip" style="display:none;position:absolute;z-index:5;pointer-events:none;padding:9px 11px;border:1px solid var(--border);border-radius:8px;background:var(--surface2);box-shadow:0 8px 24px rgba(0,0,0,.35);font:12px system-ui;white-space:nowrap;"></div></div>
          <div id="strategy-pnl-sources" class="summary-bar" style="margin-top:14px;"></div>
        </div>
      </div>
    </div>

    <!-- 30-Min Breakdown Modal -->
    <div id="breakdown-30min-modal" class="modal-overlay" onclick="close30MinModalOnBackdrop(event)">
      <div class="modal-box" role="dialog" aria-modal="true" aria-labelledby="breakdown-30min-title" style="max-width:100vw; width:100vw;">
        <div class="modal-header">
          <h2 id="breakdown-30min-title">30-Min P/L Breakdown</h2>
          <button type="button" class="modal-close" onclick="close30MinModal()" aria-label="Close">&times;</button>
        </div>
        <div class="modal-body" id="breakdown-30min-body">
          <p class="no-data">Loading...</p>
        </div>
      </div>
    </div>

<script>
  const strategyPnlData = {strategy_pnl_json};
  let strategyPnlSegments = [];

  function drawStrategyPnlGraph() {{
    const canvas = document.getElementById('strategy-pnl-canvas');
    if (!canvas) return;
    const rect = canvas.getBoundingClientRect();
    if (!rect.width || !rect.height) return;
    const dpr = window.devicePixelRatio || 1;
    canvas.width = rect.width * dpr; canvas.height = rect.height * dpr;
    const ctx = canvas.getContext('2d'); ctx.scale(dpr, dpr);
    strategyPnlSegments = [];
    const w = rect.width, h = rect.height;
    ctx.clearRect(0, 0, w, h);
    const palette = {{
      'NIFTY REVERSE': '#a855f7', 'NIFTY NO-REVERSE': '#d8b4fe',
      'SENSEX REVERSE': '#06b6d4', 'SENSEX NO-REVERSE': '#67e8f9',
      'COMBINATION REVERSE': '#f59e0b', 'COMBINATION NO-REVERSE': '#ec4899'
    }};
    const instruments = ['NIFTY', 'SENSEX', 'COMBINATION'];
    const modes = ['REVERSE', 'NO-REVERSE'];
    const dates = [...new Set(instruments.flatMap(i => modes.flatMap(m => Object.keys(strategyPnlData[i][m].daily))))].sort();
    const series = [];
    instruments.forEach(instrument => modes.forEach(mode => {{
      const raw = strategyPnlData[instrument][mode].daily;
      let runningTotal = 0, started = false;
      const cumulative = {{}};
      dates.forEach(date => {{
        if (date in raw) {{ runningTotal += Number(raw[date]); started = true; }}
        if (started) cumulative[date] = runningTotal;
      }});
      if (started) series.push({{
        label: `${{instrument}} ${{mode}}`, color: palette[`${{instrument}} ${{mode}}`], mode,
        values: cumulative, source: strategyPnlData[instrument].source
      }});
    }}));
    const sources = document.getElementById('strategy-pnl-sources');
    if (!dates.length) {{
      ctx.fillStyle = '#94a3b8'; ctx.font = '14px system-ui'; ctx.textAlign = 'center';
      ctx.fillText('No dated backtest P&L data found', w / 2, h / 2);
      sources.innerHTML = '<span class="muted">No matching backtest CSV files.</span>';
      return;
    }}
    const allValues = series.flatMap(s => Object.values(s.values).map(Number));
    let minY = Math.min(0, ...allValues), maxY = Math.max(0, ...allValues);
    const padding = (maxY - minY) * 0.10 || 1; minY -= padding; maxY += padding;
    const pad = {{ left: 76, right: 20, top: 24, bottom: 54 }};
    const plotW = w - pad.left - pad.right, plotH = h - pad.top - pad.bottom;
    const x = i => pad.left + (dates.length === 1 ? plotW / 2 : i * plotW / (dates.length - 1));
    const y = value => pad.top + (maxY - value) * plotH / (maxY - minY);
    ctx.font = '10px system-ui'; ctx.textAlign = 'right'; ctx.fillStyle = '#94a3b8';
    for (let i = 0; i <= 4; i++) {{
      const value = maxY - i * (maxY - minY) / 4, yy = y(value);
      ctx.strokeStyle = value === 0 ? '#94a3b8' : '#334155'; ctx.lineWidth = value === 0 ? 1.5 : 1;
      ctx.beginPath(); ctx.moveTo(pad.left, yy); ctx.lineTo(w - pad.right, yy); ctx.stroke();
      ctx.fillText(value.toLocaleString('en-IN', {{ maximumFractionDigits: 0 }}), pad.left - 8, yy + 3);
    }}
    const tickCount = Math.min(10, dates.length), tickIndices = new Set(Array.from({{length: tickCount}}, (_, i) => Math.round(i * (dates.length - 1) / Math.max(1, tickCount - 1))));
    ctx.textAlign = 'center';
    tickIndices.forEach(i => ctx.fillText(dates[i].slice(5), x(i), h - pad.bottom + 18));
    series.forEach(s => {{
      ctx.strokeStyle = s.color; ctx.fillStyle = s.color;
      ctx.lineWidth = s.label.startsWith('COMBINATION') ? 3.5 : 2;
      ctx.setLineDash(s.mode === 'NO-REVERSE' ? [6, 4] : []);
      let started = false;
      let previous = null;
      ctx.beginPath();
      dates.forEach((date, i) => {{
        if (!(date in s.values)) {{ if (started) ctx.stroke(); started = false; return; }}
        const px = x(i), py = y(Number(s.values[date]));
        const point = {{ x: px, y: py, value: Number(s.values[date]), date }};
        if (previous) strategyPnlSegments.push({{ x1: previous.x, y1: previous.y, v1: previous.value, d1: previous.date, x2: px, y2: py, v2: point.value, d2: date, label: s.label, color: s.color }});
        else if (dates.length === 1) strategyPnlSegments.push({{ x1: px, y1: py, v1: point.value, d1: date, x2: px, y2: py, v2: point.value, d2: date, label: s.label, color: s.color }});
        if (!started) {{ ctx.beginPath(); ctx.moveTo(px, py); started = true; }}
        else ctx.lineTo(px, py);
        previous = point;
      }});
      ctx.stroke();
      ctx.setLineDash([]);
    }});
    sources.innerHTML = series.map(s => {{
      const lastDate = dates[dates.length - 1], lastValue = s.values[lastDate];
      return `<span class="s-item"><strong style="color:${{s.color}}">${{s.label}}</strong><span class="s-lbl">Latest cumulative: Rs. ${{Number(lastValue).toLocaleString('en-IN', {{minimumFractionDigits:2, maximumFractionDigits:2}})}}</span><span class="s-lbl">${{s.source || 'No source file'}}</span></span>`;
    }}).join('');
    canvas.onmousemove = event => {{
      const bounds = canvas.getBoundingClientRect(), mx = event.clientX - bounds.left, my = event.clientY - bounds.top;
      let closest = null, closestDistance = Infinity;
      strategyPnlSegments.forEach(segment => {{
        const dx = segment.x2 - segment.x1, dy = segment.y2 - segment.y1;
        const lengthSq = dx * dx + dy * dy;
        const t = lengthSq ? Math.max(0, Math.min(1, ((mx - segment.x1) * dx + (my - segment.y1) * dy) / lengthSq)) : 0;
        const px = segment.x1 + t * dx, py = segment.y1 + t * dy;
        const distance = Math.hypot(mx - px, my - py);
        if (distance < closestDistance) {{ closestDistance = distance; closest = {{ segment, t, px, py }}; }}
      }});
      const tooltip = document.getElementById('strategy-pnl-tooltip');
      if (!closest || closestDistance > 16) {{ tooltip.style.display = 'none'; return; }}
      const {{ segment, t }} = closest;
      const value = segment.v1 + (segment.v2 - segment.v1) * t;
      const dateLabel = segment.d1 === segment.d2 ? segment.d1 : `${{segment.d1}} → ${{segment.d2}}`;
      tooltip.innerHTML = `<strong style="color:${{segment.color}}">${{segment.label}}</strong><br>${{dateLabel}}<br>Cumulative P&amp;L: Rs. ${{value.toLocaleString('en-IN', {{minimumFractionDigits:2, maximumFractionDigits:2}})}}`;
      tooltip.style.display = 'block';
      tooltip.style.left = Math.min(mx + 14, bounds.width - tooltip.offsetWidth - 8) + 'px';
      tooltip.style.top = Math.max(8, Math.min(my + 14, bounds.height - tooltip.offsetHeight - 8)) + 'px';
    }};
    canvas.onmouseleave = () => {{ document.getElementById('strategy-pnl-tooltip').style.display = 'none'; }};
  }}

  function openStrategyPnlGraph() {{
    const modal = document.getElementById('strategy-pnl-modal');
    if (modal) modal.classList.add('show');
    document.body.style.overflow = 'hidden';
    const url = new URL(window.location.href);
    url.searchParams.set('tab', 'strategy-pnl');
    history.replaceState(null, '', url.toString());
    requestAnimationFrame(drawStrategyPnlGraph);
  }}
  function closeStrategyPnlGraph() {{
    const modal = document.getElementById('strategy-pnl-modal');
    if (modal) modal.classList.remove('show');
    document.body.style.overflow = '';
    const url = new URL(window.location.href); url.searchParams.delete('tab');
    history.replaceState(null, '', url.toString());
  }}
  function closeStrategyPnlGraphOnBackdrop(event) {{
    if (event.target.id === 'strategy-pnl-modal') closeStrategyPnlGraph();
  }}
  window.addEventListener('resize', () => {{
    if (document.getElementById('strategy-pnl-modal').classList.contains('show')) drawStrategyPnlGraph();
  }});

  function syncModalTableScrollbars(modal) {{
    if (!modal) return;
    modal.querySelectorAll('.modal-body table').forEach(table => {{
      const wrap = table.closest('.table-wrap');
      if (!wrap) return;
      let top = wrap.previousElementSibling;
      if (!top || (!top.classList.contains('modal-scroll-top') && !top.classList.contains('dual-backtest-scroll-top'))) {{
        top = document.createElement('div');
        top.className = 'modal-scroll-top';
        top.setAttribute('aria-label', 'Scroll table horizontally');
        top.innerHTML = '<div></div>';
        wrap.parentNode.insertBefore(top, wrap);
      }}
      const spacer = top.firstElementChild;
      if (!spacer) return;
      const updateWidth = () => {{ spacer.style.width = `${{wrap.scrollWidth}}px`; }};
      updateWidth();
      if (top.dataset.synced === 'true') return;
      top.dataset.synced = 'true';
      let syncing = false;
      top.addEventListener('scroll', () => {{
        if (syncing) return;
        syncing = true; wrap.scrollLeft = top.scrollLeft; syncing = false;
      }});
      wrap.addEventListener('scroll', () => {{
        if (syncing) return;
        syncing = true; top.scrollLeft = wrap.scrollLeft; syncing = false;
      }});
      window.addEventListener('resize', updateWidth);
    }});
  }}

  function open30MinModal(date, instrument, structure) {{
    const modal = document.getElementById('breakdown-30min-modal');
    const titleEl = document.getElementById('breakdown-30min-title');
    const bodyEl  = document.getElementById('breakdown-30min-body');
    titleEl.textContent = '\u2248 30-Min P/L Breakdown \u2014 ' + date + ' (' + instrument + ' ' + structure.toUpperCase() + ')';
    bodyEl.innerHTML = '<p style="color:var(--muted);padding:20px;">Loading...</p>';
    modal.classList.add('show');
    document.body.style.overflow = 'hidden';
    const url = '/api/30min?date=' + date + '&instrument=' + instrument + '&structure=' + structure;
    fetch(url)
      .then(r => r.ok ? r.text() : Promise.reject('No data (' + r.status + ')'))
      .then(csv => {{
        const rows = csv.trim().split(/\\r?\\n/).map(l => l.split(','));
        if (rows.length < 2) {{ bodyEl.innerHTML = '<p class="no-data">No 30-min data for this date yet.</p>'; return; }}
        const headers = rows[0];
        let total = 0;
        let tableHTML = '<table style="width:100%;border-collapse:collapse;">';
        tableHTML += '<thead><tr>';
        for (const h of headers) {{
          tableHTML += '<th style="padding:8px 14px;border-bottom:1px solid var(--border);text-align:' + (h==='pnl'?'right':'left') + ';font-size:12px;color:var(--muted);text-transform:uppercase;">' + h + '</th>';
        }}
        tableHTML += '</tr></thead><tbody>';
        for (let i = 1; i < rows.length; i++) {{
          const cols = rows[i];
          if (cols.length < 3) continue;
          const pnl = parseFloat(cols[2]);
          total += pnl;
          const pnlClass = pnl >= 0 ? 'pnl-positive' : 'pnl-negative';
          const pnlFmt = (pnl >= 0 ? '+' : '') + 'Rs. ' + Math.abs(pnl).toLocaleString('en-IN', {{minimumFractionDigits:2}});
          tableHTML += '<tr style="border-bottom:1px solid var(--border);">';
          tableHTML += '<td style="padding:8px 14px;font-family:monospace;">' + cols[0] + '</td>';
          tableHTML += '<td style="padding:8px 14px;text-align:center;">' + cols[1] + '</td>';
          tableHTML += '<td class="' + pnlClass + '" style="padding:8px 14px;text-align:right;font-family:monospace;font-weight:700;">' + pnlFmt + '</td>';
          tableHTML += '</tr>';
        }}
        const totClass = total >= 0 ? 'pnl-positive' : 'pnl-negative';
        const totFmt = (total >= 0 ? '+' : '') + 'Rs. ' + Math.abs(total).toLocaleString('en-IN', {{minimumFractionDigits:2}});
        tableHTML += '<tr style="border-top:2px solid var(--border);background:var(--surface2);">';
        tableHTML += '<td style="padding:10px 14px;font-weight:700;">TOTAL</td>';
        tableHTML += '<td></td>';
        tableHTML += '<td class="' + totClass + '" style="padding:10px 14px;text-align:right;font-family:monospace;font-weight:700;font-size:15px;">' + totFmt + '</td>';
        tableHTML += '</tr>';
        tableHTML += '</tbody></table>';
        bodyEl.innerHTML = '<div class="table-wrap">' + tableHTML + '</div>';
        requestAnimationFrame(() => syncModalTableScrollbars(modal));
      }})
      .catch(err => {{ bodyEl.innerHTML = '<p class="no-data" style="color:var(--red);">&#9888; ' + err + '</p>'; }});
  }}

  function close30MinModal() {{
    const modal = document.getElementById('breakdown-30min-modal');
    if (modal) modal.classList.remove('show');
    document.body.style.overflow = '';
  }}

  function close30MinModalOnBackdrop(event) {{
    if (event.target.id === 'breakdown-30min-modal') close30MinModal();
  }}

  function switchTab(btn) {{
    document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
    document.querySelectorAll('.tab-pane').forEach(p => p.classList.remove('show'));
    btn.classList.add('active');
    const pane = document.getElementById(btn.dataset.target);
    if (pane) pane.classList.add('show');
  }}

  function openTradesSummary(instrument) {{
    const modalId = instrument === 'SENSEX' ? 'trades-summary-modal-sx' : 'trades-summary-modal-bn';
    const modal = document.getElementById(modalId);
    if (modal) modal.classList.add('show');
    requestAnimationFrame(() => syncModalTableScrollbars(modal));
    document.body.style.overflow = 'hidden';
    const url = new URL(window.location.href);
    url.searchParams.set('tab', 'summary');
    url.searchParams.set('instrument', instrument);
    history.replaceState(null, '', url.toString());
  }}

  function closeTradesSummary(instrument) {{
    const modalId = instrument === 'SENSEX' ? 'trades-summary-modal-sx' : 'trades-summary-modal-bn';
    const modal = document.getElementById(modalId);
    if (modal) modal.classList.remove('show');
    document.body.style.overflow = '';
    const url = new URL(window.location.href);
    url.searchParams.delete('tab');
    url.searchParams.delete('instrument');
    history.replaceState(null, '', url.toString());
  }}

  function closeTradesSummaryOnBackdrop(event, instrument) {{
    const modalId = instrument === 'SENSEX' ? 'trades-summary-modal-sx' : 'trades-summary-modal-bn';
    if (event.target.id === modalId) closeTradesSummary(instrument);
  }}

  function openDayBreakoutSummary(instrument) {{
    let modalId = 'daybreakout-summary-modal-bn';
    if (instrument === 'SENSEX') modalId = 'daybreakout-summary-modal-sx';
    else if (instrument === 'COMBINATION') modalId = 'daybreakout-summary-modal-comb';
    const modal = document.getElementById(modalId);
    if (modal) modal.classList.add('show');
    requestAnimationFrame(() => syncModalTableScrollbars(modal));
    document.body.style.overflow = 'hidden';
    const url = new URL(window.location.href);
    url.searchParams.set('tab', 'daybreakout');
    url.searchParams.set('instrument', instrument);
    history.replaceState(null, '', url.toString());
  }}

  function closeDayBreakoutSummary(instrument) {{
    let modalId = 'daybreakout-summary-modal-bn';
    if (instrument === 'SENSEX') modalId = 'daybreakout-summary-modal-sx';
    else if (instrument === 'COMBINATION') modalId = 'daybreakout-summary-modal-comb';
    const modal = document.getElementById(modalId);
    if (modal) modal.classList.remove('show');
    document.body.style.overflow = '';
    const url = new URL(window.location.href);
    url.searchParams.delete('tab');
    url.searchParams.delete('instrument');
    history.replaceState(null, '', url.toString());
  }}

  function closeDayBreakoutSummaryOnBackdrop(event, instrument) {{
    let modalId = 'daybreakout-summary-modal-bn';
    if (instrument === 'SENSEX') modalId = 'daybreakout-summary-modal-sx';
    else if (instrument === 'COMBINATION') modalId = 'daybreakout-summary-modal-comb';
    if (event.target.id === modalId) closeDayBreakoutSummary(instrument);
  }}

  function open3LegSummary() {{
    const modal = document.getElementById('threeleg-summary-modal');
    if (modal) modal.classList.add('show');
    requestAnimationFrame(() => syncModalTableScrollbars(modal));
    document.body.style.overflow = 'hidden';
    const url = new URL(window.location.href);
    url.searchParams.set('tab', 'threeleg');
    history.replaceState(null, '', url.toString());
  }}

  function close3LegSummary() {{
    const modal = document.getElementById('threeleg-summary-modal');
    if (modal) modal.classList.remove('show');
    document.body.style.overflow = '';
    const url = new URL(window.location.href);
    url.searchParams.delete('tab');
    history.replaceState(null, '', url.toString());
  }}

  function close3LegSummaryOnBackdrop(event) {{
    if (event.target.id === 'threeleg-summary-modal') close3LegSummary();
  }}

  function openDualBacktestSummary() {{
    const modal = document.getElementById('dual-backtest-summary-modal');
    if (modal) modal.classList.add('show');
    requestAnimationFrame(() => syncModalTableScrollbars(modal));
    document.body.style.overflow = 'hidden';
    const url = new URL(window.location.href);
    url.searchParams.set('tab', 'dual-backtest');
    history.replaceState(null, '', url.toString());
  }}

  function closeDualBacktestSummary() {{
    const modal = document.getElementById('dual-backtest-summary-modal');
    if (modal) modal.classList.remove('show');
    document.body.style.overflow = '';
    const url = new URL(window.location.href);
    url.searchParams.delete('tab');
    history.replaceState(null, '', url.toString());
  }}

  function closeDualBacktestSummaryOnBackdrop(event) {{
    if (event.target.id === 'dual-backtest-summary-modal') closeDualBacktestSummary();
  }}

  document.addEventListener('keydown', (e) => {{
    if (e.key === 'Escape') {{
      closeTradesSummary('BANKNIFTY');
      closeTradesSummary('SENSEX');
      closeDayBreakoutSummary('NIFTY');
      closeDayBreakoutSummary('SENSEX');
      closeDayBreakoutSummary('COMBINATION');
      close3LegSummary();
      closeDualBacktestSummary();
      closeStrategyPnlGraph();
      close30MinModal();
    }}
  }});

  function applyFilters() {{
    const url = new URL(window.location.href);
    const date  = document.getElementById('date-select').value;
    const slot  = document.getElementById('slot-select').value;
    const instr = document.getElementById('instr-select').value;
    url.searchParams.set('date', date);
    url.searchParams.set('instrument', instr);
    if (slot && slot !== 'all') {{
      url.searchParams.set('slot', slot);
    }} else {{
      url.searchParams.delete('slot');
    }}
    window.location.href = url.toString();
  }}

  let rem = 300;
  const timerEl = document.getElementById('timer-text');
  const progEl  = document.getElementById('prog');
  setInterval(() => {{
    if (--rem <= 0) {{ location.reload(); return; }}
    const m = Math.floor(rem / 60), s = rem % 60;
    timerEl.textContent = m + ':' + String(s).padStart(2,'0');
    progEl.value = rem;
  }}, 1000);

  (function restoreModalState() {{
    const params = new URLSearchParams(window.location.search);
    if (params.get('tab') === 'summary') {{
      openTradesSummary(params.get('instrument') || 'BANKNIFTY');
    }} else if (params.get('tab') === 'daybreakout') {{
      openDayBreakoutSummary(params.get('instrument') || 'NIFTY');
    }} else if (params.get('tab') === 'threeleg') {{
      open3LegSummary();
    }} else if (params.get('tab') === 'dual' || params.get('tab') === 'dual-backtest') {{
      openDualBacktestSummary();
    }} else if (params.get('tab') === 'strategy-pnl') {{
      openStrategyPnlGraph();
    }}
  }})();
</script>
</body>
</html>"""


def build_dual_mode_comparison(instrument: str = "NIFTY", for_date: str = None) -> tuple:
    """Build dual-mode comparison table showing REVERSE vs NO-REVERSE side by side."""
    reverse_records, no_reverse_records = read_dual_mode_trades(instrument, for_date)
    label = instrument.upper()
    date_key = for_date or datetime.now(IST).strftime("%Y-%m-%d")

    # Calculate statistics for REVERSE mode
    reverse_trades = len(reverse_records)
    reverse_pnl = sum(r["pnl"] for r in reverse_records)
    reverse_wins = sum(1 for r in reverse_records if r["pnl"] > 0)
    reverse_losses = sum(1 for r in reverse_records if r["pnl"] < 0)

    # Calculate statistics for NO-REVERSE mode
    no_reverse_trades = len(no_reverse_records)
    no_reverse_pnl = sum(r["pnl"] for r in no_reverse_records)
    no_reverse_wins = sum(1 for r in no_reverse_records if r["pnl"] > 0)
    no_reverse_losses = sum(1 for r in no_reverse_records if r["pnl"] < 0)

    # Combined statistics
    total_trades = reverse_trades + no_reverse_trades
    total_pnl = reverse_pnl + no_reverse_pnl

    reverse_pnl_class = "pnl-positive" if reverse_pnl >= 0 else "pnl-negative"
    reverse_pnl_formatted = f"+Rs. {reverse_pnl:,.2f}" if reverse_pnl >= 0 else f"-Rs. {abs(reverse_pnl):,.2f}"

    no_reverse_pnl_class = "pnl-positive" if no_reverse_pnl >= 0 else "pnl-negative"
    no_reverse_pnl_formatted = f"+Rs. {no_reverse_pnl:,.2f}" if no_reverse_pnl >= 0 else f"-Rs. {abs(no_reverse_pnl):,.2f}"

    total_pnl_class = "pnl-positive" if total_pnl >= 0 else "pnl-negative"
    total_pnl_formatted = f"+Rs. {total_pnl:,.2f}" if total_pnl >= 0 else f"-Rs. {abs(total_pnl):,.2f}"

    if reverse_trades == 0 and no_reverse_trades == 0:
        empty_html = f"""
        <div class="summary-bar">
          <span class="s-item"><span class="s-lbl">Instrument</span><strong>{label} Dual-Mode</strong></span>
          <span class="s-item"><span class="s-lbl">Date</span><strong>{date_key}</strong></span>
          <span class="s-item"><span class="s-lbl">Status</span><strong>No Dual-Mode Trades Yet</strong></span>
        </div>
        <p class="no-data">No dual-mode trades recorded for {date_key}. Run main_dual.py to generate dual-mode data.</p>
        """
        return empty_html, 0, 0.0

    summary = f"""
    <div class="summary-bar">
      <span class="s-item"><span class="s-lbl">Instrument</span><strong>{label} Dual-Mode</strong></span>
      <span class="s-item"><span class="s-lbl">Date</span><strong>{date_key}</strong></span>
      <span class="s-item"><span class="s-lbl">REVERSE</span><strong style="color:var(--purple);">{reverse_trades} trades | {reverse_pnl_formatted}</strong></span>
      <span class="s-item"><span class="s-lbl">NO-REVERSE</span><strong style="color:var(--accent);">{no_reverse_trades} trades | {no_reverse_pnl_formatted}</strong></span>
      <span class="s-item"><span class="s-lbl">COMBINED</span><strong class="{total_pnl_class}" style="font-size:16px;">{total_trades} trades | {total_pnl_formatted}</strong></span>
    </div>"""

    # Build comparison table
    table_html = f"""
    <div class="table-wrap">
      <table>
        <thead>
          <tr>
            <th colspan="4" style="text-align:center; background:var(--purple); color:white;">REVERSE MODE (Fade Breakouts)</th>
            <th colspan="4" style="text-align:center; background:var(--accent); color:white;">NO-REVERSE MODE (Follow Breakouts)</th>
          </tr>
          <tr>
            <th>Trade #</th>
            <th>Time</th>
            <th>Direction</th>
            <th>P&amp;L</th>
            <th>Trade #</th>
            <th>Time</th>
            <th>Direction</th>
            <th>P&amp;L</th>
          </tr>
        </thead>
        <tbody>"""

    # Determine maximum number of trades across both modes
    max_trades = max(reverse_trades, no_reverse_trades)

    for i in range(max_trades):
        # REVERSE mode row
        if i < reverse_trades:
            r = reverse_records[i]
            pnl_val = r["pnl"]
            pnl_class = "pnl-positive" if pnl_val >= 0 else "pnl-negative"
            pnl_badge = f"+Rs. {pnl_val:,.2f}" if pnl_val >= 0 else f"-Rs. {abs(pnl_val):,.2f}"
            time_val = r.get("entry_time", r.get("time", "—"))
            direction = r.get("direction", "—")
            table_html += f"""
        <tr>
          <td style="background:var(--purple-bg);">{i + 1}</td>
          <td class="mono" style="background:var(--purple-bg);">{time_val}</td>
          <td style="background:var(--purple-bg);">{direction}</td>
          <td class="mono pnl-cell {pnl_class}" style="background:var(--purple-bg);"><strong>{pnl_badge}</strong></td>"""
        else:
            table_html += """
        <tr>
          <td style="background:var(--purple-bg);">—</td>
          <td style="background:var(--purple-bg);">—</td>
          <td style="background:var(--purple-bg);">—</td>
          <td style="background:var(--purple-bg);">—</td>"""

        # NO-REVERSE mode row
        if i < no_reverse_trades:
            r = no_reverse_records[i]
            pnl_val = r["pnl"]
            pnl_class = "pnl-positive" if pnl_val >= 0 else "pnl-negative"
            pnl_badge = f"+Rs. {pnl_val:,.2f}" if pnl_val >= 0 else f"-Rs. {abs(pnl_val):,.2f}"
            time_val = r.get("entry_time", r.get("time", "—"))
            direction = r.get("direction", "—")
            table_html += f"""
          <td style="background:var(--accent-bg);">{i + 1}</td>
          <td class="mono" style="background:var(--accent-bg);">{time_val}</td>
          <td style="background:var(--accent-bg);">{direction}</td>
          <td class="mono pnl-cell {pnl_class}" style="background:var(--accent-bg);"><strong>{pnl_badge}</strong></td>
        </tr>"""
        else:
            table_html += """
          <td style="background:var(--accent-bg);">—</td>
          <td style="background:var(--accent-bg);">—</td>
          <td style="background:var(--accent-bg);">—</td>
          <td style="background:var(--accent-bg);">—</td>
        </tr>"""

    table_html += f"""
        </tbody>
        <tfoot>
          <tr class="tfoot-total">
            <td colspan="3" style="text-align:right; font-weight:700; font-size:13px; padding:12px 14px; background:var(--purple); color:white;">
              REVERSE TOTAL ({reverse_trades} trades):
            </td>
            <td class="mono pnl-cell {reverse_pnl_class}" style="font-size:15px; font-weight:700; padding:12px 14px; background:var(--purple); color:white;">
              {reverse_pnl_formatted}
            </td>
            <td colspan="3" style="text-align:right; font-weight:700; font-size:13px; padding:12px 14px; background:var(--accent); color:white;">
              NO-REVERSE TOTAL ({no_reverse_trades} trades):
            </td>
            <td class="mono pnl-cell {no_reverse_pnl_class}" style="font-size:15px; font-weight:700; padding:12px 14px; background:var(--accent); color:white;">
              {no_reverse_pnl_formatted}
            </td>
          </tr>
          <tr class="tfoot-total">
            <td colspan="7" style="text-align:right; font-weight:700; font-size:14px; padding:12px 14px; background:var(--surface2);">
              COMBINED TOTAL ({total_trades} trades):
            </td>
            <td class="mono pnl-cell {total_pnl_class}" style="font-size:16px; font-weight:700; padding:12px 14px; background:var(--surface2);">
              {total_pnl_formatted}
            </td>
          </tr>
        </tfoot>
      </table>
    </div>"""

    return summary + table_html, total_trades, total_pnl


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        path = args[0].split(" ")[1] if " " in args[0] else args[0]
        if path.startswith("/"):
            print(f"[{datetime.now(IST).strftime('%H:%M:%S')}] Page served  {path}")

    def do_GET(self):
        parsed = urlparse(self.path)
        params = parse_qs(parsed.query)

        # ── API: 30-min breakdown CSV ──────────────────────────────
        if parsed.path == "/api/30min":
            date       = params.get("date",       [None])[0]
            instrument = (params.get("instrument", ["NIFTY"])[0]).upper()
            structure  = (params.get("structure",  ["spread"])[0]).lower()
            csv_path = None
            if date:
                if instrument == "COMBINATION":
                    matches = sorted(DATA_DIR.glob(f"backtest_30min_*_{date}.csv"))
                    if matches:
                        csv_path = matches[-1]
                else:
                    # Exact match first
                    candidate = DATA_DIR / f"backtest_30min_{instrument}_{structure}_{date}.csv"
                    if candidate.is_file():
                        csv_path = candidate
                    else:
                        # Glob fallback (any structure)
                        matches = sorted(DATA_DIR.glob(f"backtest_30min_{instrument}_*_{date}.csv"))
                        if matches:
                            csv_path = matches[-1]
            if csv_path and csv_path.is_file():
                data = csv_path.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "text/csv; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(data)
            else:
                self.send_response(404)
                self.send_header("Content-Type", "text/plain")
                self.end_headers()
                self.wfile.write(b"No 30-min data found for requested date/instrument.")
            return

        selected_date       = params.get("date",       [None])[0]
        selected_slot       = params.get("slot",       [SLOT_ALL])[0]
        selected_instrument = params.get("instrument", [INSTR_BN])[0]
        html = build_page(selected_date, selected_slot, selected_instrument).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(html)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(html)


def main():
    server = HTTPServer(("127.0.0.1", PORT), Handler)
    url = f"http://localhost:{PORT}"
    dates = get_available_dates()
    print(f"\n{'='*55}")
    print(f"  Breakout Dashboard & Positions  ->  {url}")
    print(f"  Data directory: {DATA_DIR}")
    print(f"  Available dates: {', '.join(dates[:5]) if dates else 'none yet'}{'...' if len(dates)>5 else ''}")
    print(f"  Auto-refreshes every 5 minutes in browser")
    print(f"{'='*55}")
    print("  Press Ctrl+C to stop.\n")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[Viewer stopped]")
        sys.exit(0)


if __name__ == "__main__":
    main()
