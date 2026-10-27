"""Read-only live dashboard for Day Breakout dual-mode runs.

Run with ``python live/daybreakout/viewer.py`` and open http://localhost:8766.
The viewer reads the daily logger output and mode-specific trade CSVs; it does
not connect to SmartAPI or place orders.
"""

import csv
import json
import re
import sys
from datetime import datetime, timezone, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

# Allow both ``python live/daybreakout/viewer.py`` and package execution.
ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Direct paths to avoid package imports and dependencies
DATA_DIR = ROOT / "data"
LOGS_DIR = DATA_DIR / "daybreakout-logs"

PORT = 8766
# IST is UTC+5:30
IST = timezone(timedelta(hours=5, minutes=30))
CANDLE_RE = re.compile(
    r"15m Candle @ (?P<candle>\d{2}:\d{2}) - O:(?P<open>[\d.]+) "
    r"H:(?P<high>[\d.]+) L:(?P<low>[\d.]+) C:(?P<close>[\d.]+) "
    r"Range:\[(?P<range_low>[\d.]+)-(?P<range_high>[\d.]+)\]"
)
SPOT_RE = re.compile(r"Current .* Spot: (?P<spot>[\d.]+) \| Range High: (?P<high>[\d.]+) \| Range Low: (?P<low>[\d.]+)")
PNL_RE = re.compile(r"(?:\[Consolidated Monitor #\d+\] )?\[(?P<trade_id>[^\]]+)\].*?P&L: Rs\.(?P<pnl>[+-]?[\d,.]+) \| Peak: Rs\.(?P<peak>[+-]?[\d,.]+)")
TRADE_ID_TIME_RE = re.compile(r"_(?P<date>\d{8})_(?P<time>\d{6})(?:_|$)")
MODE_RE = re.compile(r"Strategy Mode: (?P<mode>REVERSE|NO-REVERSE)")
TRADE_RESULT_RE = re.compile(
    r"(?:Logged trade completion - Mode: (?P<mode>REVERSE|NO-REVERSE), "
    r"Symbol: (?P<symbol>[^,]+), P&L: Rs\.(?P<pnl>[+-]?[\d,.]+), "
    r"Reason: (?P<reason>.*)|Trade logged - Symbol: (?P<symbol2>[^,]+), "
    r"Start: (?P<start>[^,]+), End: (?P<end>[^,]+), "
    r"P&L: Rs\.(?P<pnl2>[+-]?[\d,.]+), Exit Reason: (?P<reason2>.*))"
)
CANDLE_SKIP_RE = re.compile(r"Candle (?P<candle>\d{2}:\d{2}) skipped: (?P<reason>.*)")
CANDLE_CONFIRMED_RE = re.compile(
    r"\[CONFIRMED\] 15m (?P<trade_type>BREAKOUT|RECOVERY): "
    r"(?P<direction>BULLISH|BEARISH) close (?P<close>[\d.]+)"
)
CANDLE_EXECUTION_RE = re.compile(
    r"Executing .*?\[(?P<mode>REVERSE|NO-REVERSE)\].*? - Reason: (?P<reason>.*?), Direction:"
)


def _summarize_candle_log(lines):
    """Join each 15-minute candle with its logged skip or trade decision."""
    by_time = {}
    current = None
    for line in lines:
        candle_match = CANDLE_RE.search(line)
        if candle_match:
            values = candle_match.groupdict()
            current = by_time.setdefault(values["candle"], {
                "candle": values["candle"],
                "open": float(values["open"]), "high": float(values["high"]),
                "low": float(values["low"]), "close": float(values["close"]),
                "range_low": float(values["range_low"]),
                "range_high": float(values["range_high"]),
                "reasons": [], "trades": [], "last_update": line[:19],
            })
            continue
        skip_match = CANDLE_SKIP_RE.search(line)
        if skip_match:
            item = by_time.get(skip_match.group("candle"))
            if item and skip_match.group("reason") not in item["reasons"]:
                item["reasons"].append(skip_match.group("reason"))
                item["last_update"] = line[:19]
            continue
        confirmed = CANDLE_CONFIRMED_RE.search(line)
        if confirmed and current:
            detail = confirmed.groupdict()
            message = f"{detail['trade_type'].title()} {detail['direction'].lower()} signal confirmed at close {detail['close']}"
            if message not in current["trades"]:
                current["trades"].append(message)
            current["last_update"] = line[:19]
            continue
        execution = CANDLE_EXECUTION_RE.search(line)
        if execution and current:
            detail = execution.groupdict()
            message = f"{detail['mode']}: {detail['reason']}"
            if message not in current["trades"]:
                current["trades"].append(message)
            current["last_update"] = line[:19]

    summaries = []
    for item in by_time.values():
        if item["trades"]:
            item["summary"] = "Trade signal confirmed."
        elif item["reasons"]:
            item["summary"] = "; ".join(item["reasons"])
        elif item["close"] > item["range_high"]:
            item["summary"] = "Candle closed above the range; no skip or execution reason was logged."
        elif item["close"] < item["range_low"]:
            item["summary"] = "Candle closed below the range; no skip or execution reason was logged."
        else:
            item["summary"] = "Close stayed inside the range; no breakout signal."
        summaries.append(item)
    return summaries[-30:]


def _read_csv(path):
    if not path.is_file():
        return []
    try:
        with path.open(newline="", encoding="utf-8-sig") as stream:
            return list(csv.DictReader(stream))
    except (OSError, csv.Error):
        return []


def _normalize_trade_row(row, mode):
    """Normalize completed trade CSV variants for the dashboard."""
    normalized = dict(row)
    normalized.update({
        "mode": row.get("mode") or mode,
        "entry_time": row.get("entry_time") or row.get("start_time") or "",
        "exit_time": row.get("exit_time") or row.get("end_time") or "",
        "buy_symbol": row.get("buy_symbol") or row.get("buy_leg") or "",
        "sell_symbol": row.get("sell_symbol") or row.get("sell_leg") or "N/A",
        "trade_reason": row.get("trade_reason") or "",
        "exit_reason": row.get("exit_reason") or "",
        "pnl": row.get("pnl") or "0",
        "lots": row.get("lots") or "",
        "structure": row.get("structure") or "",
    })
    return normalized


def _get_backtest_daily_summary():
    """Aggregate the latest dual backtest CSV into per-day, per-mode P&L."""
    files = list(DATA_DIR.glob("backtest_daybreakout_15m_*_dual.csv"))
    if not files:
        return {"source": "", "days": []}

    source = max(files, key=lambda path: path.stat().st_mtime)
    totals = {}
    trades = _read_csv(source)
    for row in trades:
        day = (row.get("date") or "").strip()
        mode = (row.get("mode") or "").strip().upper()
        if not day or mode not in ("REVERSE", "NO-REVERSE"):
            continue
        try:
            pnl = float((row.get("pnl") or "0").replace(",", ""))
        except (TypeError, ValueError):
            continue
        try:
            min_unrealized_pnl = float((row.get("min_unrealized_pnl") or "").replace(",", ""))
        except (AttributeError, TypeError, ValueError):
            min_unrealized_pnl = None
        item = totals.setdefault(day, {
            "date": day,
            "REVERSE": 0.0,
            "NO-REVERSE": 0.0,
            "REVERSE_min_unrealized_pnl": None,
            "NO-REVERSE_min_unrealized_pnl": None,
            "trades": 0,
        })
        item[mode] += pnl
        min_key = f"{mode}_min_unrealized_pnl"
        if min_unrealized_pnl is not None:
            previous_min = item[min_key]
            item[min_key] = min(min_unrealized_pnl, previous_min) if previous_min is not None else min_unrealized_pnl
        item["trades"] += 1

    days = []
    for day in sorted(totals):
        item = totals[day]
        item["total"] = item["REVERSE"] + item["NO-REVERSE"]
        days.append(item)
    days.reverse()
    latest_days = days[:3]
    return {"source": source.name, "days": latest_days}


def get_state():
    now = datetime.now(IST)
    day_key = now.strftime("%Y-%m-%d")
    log_path = LOGS_DIR / f"daybreakout-{day_key}.log"
    candles, positions, trade_results = [], {}, []
    lines = []
    current_instrument, range_high, range_low, last_spot = "Unknown", None, None, None
    last_update = None

    if log_path.is_file():
        try:
            lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            lines = []
        for line in lines:
            stamp = line[:19]
            last_update = stamp if line else last_update
            if "DAY BREAKOUT STRATEGY INITIALIZED:" in line:
                current_instrument = line.split("DAY BREAKOUT STRATEGY INITIALIZED:", 1)[1].strip()
            spot_match = SPOT_RE.search(line)
            if spot_match:
                last_spot = float(spot_match.group("spot"))
                range_high = float(spot_match.group("high"))
                range_low = float(spot_match.group("low"))
            candle_match = CANDLE_RE.search(line)
            if candle_match:
                candle = candle_match.groupdict()
                parsed = {key: (value if key == "candle" else float(value)) for key, value in candle.items()}
                if candles and candles[-1]["candle"] == parsed["candle"]:
                    candles[-1] = parsed  # Both mode threads may log the same completed candle.
                else:
                    candles.append(parsed)
                last_spot = parsed["close"]
            pnl_match = PNL_RE.search(line)
            if pnl_match:
                mode_match = MODE_RE.search(line)
                trade_id = pnl_match.group("trade_id")
                pnl = float(pnl_match.group("pnl").replace(",", ""))
                reported_peak = float(pnl_match.group("peak").replace(",", ""))
                previous = positions.get(trade_id, {})
                positions[trade_id] = {
                    "trade_id": trade_id,
                    "mode": mode_match.group("mode") if mode_match else "Unknown",
                    "pnl": pnl,
                    "peak": max(reported_peak, pnl, previous.get("peak", float("-inf"))),
                    "time": stamp[11:] if len(stamp) >= 19 else "",
                }
            result_match = TRADE_RESULT_RE.search(line)
            if result_match:
                result = result_match.groupdict()
                trade_results.append({
                    "mode": result.get("mode"),
                    "symbol": (result.get("symbol") or result.get("symbol2") or "").strip(),
                    "pnl": float((result.get("pnl") or result.get("pnl2")).replace(",", "")),
                    "exit_reason": (result.get("reason") or result.get("reason2") or "").strip(),
                    "time": result.get("end") or stamp[11:19],
                    "logged_at": stamp,
                })

    # Live order logs are stored by local trading date (DD-MM-YYYY). Keep the
    # viewer on this date only; historical/backtest CSVs are never consulted.
    folder = DATA_DIR / now.strftime("%d-%m-%Y")
    modes = {}
    symbol = "SENSEX" if "SENSEX" in current_instrument.upper() else "NIFTY"
    consolidated_triggered = _read_csv(folder / f"triggered_trades_{symbol}.csv")
    consolidated_completed = _read_csv(folder / f"trades_{symbol}.csv")
    for mode in ("reverse", "no-reverse"):
        prefix = f"{symbol}_{mode}"
        # Optional live strategy CSVs, when present, are also date-scoped.
        triggered = _read_csv(DATA_DIR / "trades" / day_key / f"triggered_trades_{prefix}.csv")
        completed = _read_csv(DATA_DIR / "trades" / day_key / f"trades_{prefix}.csv")
        mode_name = mode.upper()
        for row in consolidated_triggered:
            if (row.get("mode") or "").strip().upper() != mode_name:
                continue
            key = (row.get("date"), row.get("time"), row.get("buy_symbol"), row.get("trade_reason"))
            if not any((item.get("date"), item.get("time"), item.get("buy_symbol"), item.get("trade_reason")) == key for item in triggered):
                triggered.append(row)
        for row in consolidated_completed:
            if (row.get("mode") or "").strip().upper() != mode_name:
                continue
            key = (row.get("trade_id"), row.get("date"), row.get("entry_time"), row.get("pnl"))
            if not any((item.get("trade_id"), item.get("date"), item.get("entry_time"), item.get("pnl")) == key for item in completed):
                completed.append(row)
        modes[mode.upper()] = {
            "triggered": [dict(row, strategy_mode=mode.upper()) for row in triggered],
            "completed": [_normalize_trade_row(row, mode.upper()) for row in completed],
        }

    # Use each trade's entry timestamp to find its mode/reason in today's
    # logger, then add the actual FILLED order from today's dated order file.
    mode_events = []
    for line in lines if log_path.is_file() else []:
        mode_match = re.search(r"Executing .*?\[(REVERSE|NO-REVERSE)\].*?Reason: (.*)", line)
        if mode_match:
            try:
                event_time = datetime.strptime(line[:19], "%Y-%m-%d %H:%M:%S")
                mode_events.append((event_time, mode_match.group(1), mode_match.group(2)))
            except ValueError:
                pass
    fills = {}
    entry_orders = {}
    for row in _read_csv(folder / f"orders_{symbol}.csv"):
        if row.get("date") != day_key or row.get("symbol_base", "").upper() != symbol:
            continue
        if row.get("status", "").upper() not in {"FILLED", "FILLED_POLL", "FILLED_WS"} or row.get("order_type", "").upper() not in {"ENTRY_BUY", "ENTRY_SELL"}:
            continue
        try:
            filled_at = datetime.strptime(f"{row['date']} {row['time'][:8]}", "%Y-%m-%d %H:%M:%S")
        except (KeyError, ValueError):
            continue
        closest = min(mode_events, key=lambda event: abs((event[0] - filled_at).total_seconds()), default=None)
        if not closest or abs((closest[0] - filled_at).total_seconds()) > 120:
            continue
        mode, reason = closest[1], closest[2]
        trade_id = row.get("trade_id", "")
        entry_orders.setdefault(trade_id, []).append(row)
        fills[trade_id] = {
            "trade_id": trade_id, "date": row["date"], "time": row["time"],
            "entry_time": row["time"], "buy_symbol": row.get("symbol", ""),
            "sell_symbol": "N/A", "lots": "", "structure": "Live order",
            "trade_reason": reason, "status": "Filled entry · exit/P&L not recorded",
            "execution_price": row.get("execution_price", ""),
            "quantity": row.get("quantity", ""), "pnl_known": False,
            "strategy_mode": mode,
        }
    # Completed-trade log messages carry realized P&L. Some logger versions
    # include the mode and exit reason; older ones include start/end times.
    for result in trade_results:
        if result["symbol"].upper() != symbol:
            continue
        try:
            exit_at = datetime.strptime(f"{day_key} {result['time'][:8]}", "%Y-%m-%d %H:%M:%S")
        except ValueError:
            try:
                exit_at = datetime.strptime(result["logged_at"][:19], "%Y-%m-%d %H:%M:%S")
            except ValueError:
                continue
        candidates = []
        for trade_id, item in fills.items():
            if item["pnl_known"] or (result["mode"] and item["strategy_mode"] != result["mode"]):
                continue
            try:
                entry_at = datetime.strptime(f"{item['date']} {item['time'][:8]}", "%Y-%m-%d %H:%M:%S")
            except ValueError:
                continue
            if entry_at <= exit_at:
                candidates.append((abs((exit_at - entry_at).total_seconds()), trade_id, item))
        if candidates:
            _, trade_id, item = min(candidates, key=lambda value: value[0])
            item.update({
                "exit_time": result["time"], "exit_reason": result["exit_reason"],
                "pnl": result["pnl"], "pnl_known": True,
                "status": "Completed (P&L from live log)",
            })
    existing_ids = {row.get("trade_id") for data in modes.values() for row in data["triggered"]}
    existing_ids.update(row.get("trade_id") for data in modes.values() for row in data["completed"])
    for item in fills.values():
        if item["trade_id"] not in existing_ids:
            if item["pnl_known"]:
                modes[item["strategy_mode"]]["completed"].append(_normalize_trade_row(item, item["strategy_mode"]))
            else:
                modes[item["strategy_mode"]]["triggered"].append(item)

    trade_source = f"Today's live files · {folder.name}"

    # The current logger's periodic P&L line omits mode. Match its timestamp
    # against a mode-tagged triggered-trade record from the same session.
    for trade_id, position in positions.items():
        trade_time_match = TRADE_ID_TIME_RE.search(trade_id)
        if not trade_time_match:
            continue
        trade_time = datetime.strptime(
            f"{trade_time_match.group('date')}_{trade_time_match.group('time')}",
            "%Y%m%d_%H%M%S",
        )
        closest = None
        for mode_name, data in modes.items():
            for row in data["triggered"]:
                try:
                    row_time = datetime.strptime(f"{row['date']} {row['time']}", "%Y-%m-%d %H:%M:%S")
                except (KeyError, ValueError):
                    continue
                delta = abs((trade_time - row_time).total_seconds())
                if delta <= 300 and (closest is None or delta < closest[0]):
                    closest = (delta, mode_name, row)
        if closest:
            position["mode"] = closest[1]
            position["entry_time"] = closest[2].get("time", "")
            position.update({
                key: closest[2].get(key, "")
                for key in ("buy_symbol", "sell_symbol", "trade_reason", "lots", "structure", "execution_price", "quantity")
            })
            matching_orders = [
                order for order in entry_orders.get(trade_id, [])
                if order.get("symbol", "").upper() == str(closest[2].get("buy_symbol", "")).upper()
                and order.get("execution_price")
            ]
            if matching_orders:
                fill = matching_orders[-1]
                position["execution_price"] = fill["execution_price"]
                position["quantity"] = fill.get("quantity", position.get("quantity", ""))
            matched_modes = {
                mode for event_time, mode, _reason in mode_events
                if abs((event_time - trade_time).total_seconds()) <= 2
            }
            order_symbols = list(dict.fromkeys(
                order.get("symbol", "") for order in entry_orders.get(trade_id, []) if order.get("symbol")
            ))
            if len(matched_modes) > 1 and len(order_symbols) > 1:
                position["mode"] = "Shared ID: " + " + ".join(sorted(matched_modes))
                position["buy_symbol"] = " / ".join(order_symbols)
                position["execution_price"] = " / ".join(
                    order.get("execution_price", "")
                    for order in entry_orders.get(trade_id, [])
                    if order.get("symbol") in order_symbols and order.get("execution_price")
                )
                position["quantity"] = " / ".join(
                    order.get("quantity", "")
                    for order in entry_orders.get(trade_id, [])
                    if order.get("symbol") in order_symbols and order.get("quantity")
                )
                position["monitor_note"] = "Shared trade ID; only one P&L monitor registered, so this sample is not combined across both modes."

    # Closed trades are authoritative in CSV; hide their last periodic P&L sample.
    completed_ids = {
        row.get("trade_id")
        for mode_data in modes.values()
        for row in mode_data["completed"]
        if row.get("trade_id")
    }
    active = [item for trade_id, item in positions.items() if trade_id not in completed_ids]
    return {
        "date": now.strftime("%d %b %Y"), "instrument": current_instrument,
        "updated": last_update or "No log data yet", "spot": last_spot,
        "range_high": range_high, "range_low": range_low,
        "candles": candles[-120:], "candle_log": _summarize_candle_log(lines),
        "modes": modes, "positions": active,
        "trade_source": trade_source,
        "backtest_daily": _get_backtest_daily_summary(),
    }


PAGE = r"""<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Day Breakout Dual Viewer</title>
<style>
:root{color-scheme:dark;--bg:#0b1020;--card:#131b2d;--line:#26334c;--text:#e7edf8;--muted:#94a3b8;--green:#37d39a;--red:#ff657a;--purple:#bf91ff;--blue:#63b3ed;--grid:#26334c;--axis:#94a3b8;--ink:#e2e8f0;--zero:#cbd5e1;--run:#f8fafc}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--text);font:14px system-ui,Segoe UI,sans-serif}
main{width:100%;max-width:none;margin:0;padding:20px 24px}
.head,.grid,.modes{display:grid;gap:14px}
.head{grid-template-columns:1fr auto;align-items:center;margin-bottom:18px}
.grid{grid-template-columns:repeat(4,1fr);margin-bottom:14px}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:16px;margin-bottom:14px;overflow-x:auto}
.label{color:var(--muted);font-size:12px;text-transform:uppercase;letter-spacing:.08em}
.value{font-size:23px;font-weight:700;margin-top:7px}
.charts-row{display:grid;grid-template-columns:1fr 1fr;gap:14px;margin-bottom:14px}
.chart-card{display:flex;flex-direction:column;padding:14px 16px;margin-bottom:0;min-height:310px}
.pnl-card{min-height:360px}
.chart-header{display:flex;justify-content:space-between;align-items:center;margin-bottom:8px}
.pnl-head{align-items:flex-start;flex-wrap:wrap;gap:8px}
.chart-sub{color:var(--muted);font-size:11px;font-weight:500;letter-spacing:0;text-transform:none;margin-top:3px;line-height:1.35;max-width:420px}
.pnl-switch{display:inline-flex;border:1px solid var(--line);border-radius:8px;overflow:hidden;flex:0 0 auto}
.pnl-switch button{background:transparent;color:var(--muted);border:0;padding:4px 8px;font:600 11px system-ui,Segoe UI,sans-serif;cursor:pointer}
.pnl-switch button.on{background:#243049;color:#f8fafc}
.mode-filter button.on.mf-rev{background:#7c3aed;color:#fff}
.mode-filter button.on.mf-follow{background:#2563eb;color:#fff}
.chart-wrap{flex:1;min-height:250px;position:relative}
.chart-wrap svg{width:100%;height:100%;display:block}
.expand-btn{background:none;border:1px solid var(--line);color:var(--muted);border-radius:6px;padding:3px 8px;cursor:pointer;font-size:13px;transition:all .15s ease}
.expand-btn:hover{color:var(--text);border-color:var(--text)}
.chart-badges{display:flex;flex-wrap:wrap;gap:6px;margin-bottom:12px;align-items:center}
.p-pill{display:inline-flex;align-items:center;padding:4px 10px;border-radius:12px;font-size:11px;font-weight:600;background:#243049;color:#94a3b8;white-space:nowrap}
.p-pill-rev{background:#4c1d95;color:#f8fafc}
.p-pill-follow{background:#0369a1;color:#f8fafc}
.p-pill-realized{background:#0f766e;color:#f8fafc}
.p-pill-realized.pos{background:#059669;color:#f8fafc}
.p-pill-open{background:#be185d;color:#f8fafc}
.chart-tooltip{position:fixed;display:none;background:#1e293b;border:1px solid #334155;border-radius:8px;padding:12px 14px;box-shadow:0 10px 30px rgba(0,0,0,0.5);pointer-events:none;z-index:10000;max-width:300px;color:#e2e8f0;font-size:12px;line-height:1.4}
.tt-head{display:flex;justify-content:space-between;align-items:center;margin-bottom:8px;padding-bottom:8px;border-bottom:1px solid #334155}
.tt-title{font-weight:600;font-size:13px}
.tt-badge{padding:2px 6px;border-radius:4px;font-size:10px;font-weight:600}
.tt-pnl{font-size:15px;font-weight:700;margin:6px 0}
.tt-row{display:flex;justify-content:space-between;margin:4px 0;color:#94a3b8}
.tt-val{color:#e2e8f0;font-weight:500}
.tt-desc{margin-top:8px;padding-top:8px;border-top:1px solid #334155;color:#cbd5e1;font-size:11px}
.rev{color:#c084fc}
.follow{color:#38bdf8}
.p-pill-rev{background:#7c3aed}
.p-pill-follow{background:#2563eb}
.chart-header{display:flex;justify-content:space-between;align-items:flex-start;margin-bottom:12px}
.chart-header-left{flex:1}
.chart-sub{color:#94a3b8;font-size:12px;margin-top:4px}
.chart-legend{display:flex;align-items:center;gap:12px}
.legend-item{display:flex;align-items:center;gap:6px;font-size:12px;color:#94a3b8}
.legend-dot{width:10px;height:10px;border-radius:50%}
.dot-rev{background:#7c3aed}
.dot-follow{background:#2563eb}
.line-open{width:20px;height:2px;background:#fbbf24}
.page-columns{display:grid;grid-template-columns:minmax(0,3fr) minmax(280px,1fr);gap:14px;align-items:start}
.dashboard-main{min-width:0;width:100%}
.candle-log-card{position:sticky;top:12px;height:calc(100vh - 40px);overflow:hidden}
.candle-log-list{height:calc(100vh - 150px);overflow-y:auto;margin-top:10px}
.candle-log-item{padding:10px 0;border-top:1px solid var(--line)}
.candle-log-item h3{font-size:14px;margin:0 0 5px}
.candle-log-item p{margin:4px 0}
.candle-log-meta{color:var(--muted);font-size:12px}
.modes{grid-template-columns:1fr 1fr}
.mode-title{font-size:18px;font-weight:700;margin:0 0 12px}
.rev{color:var(--purple)}
.follow{color:var(--blue)}
table{width:100%;border-collapse:collapse;font-size:13px;min-width:600px}
th,td{text-align:left;padding:9px 7px;border-bottom:1px solid var(--line);vertical-align:top}
th{color:var(--muted);font-weight:600}
.pos{color:var(--green)}
.neg{color:var(--red)}
.muted{color:var(--muted)}
.pill{display:inline-block;border:1px solid var(--line);border-radius:20px;padding:5px 9px;margin:3px}
.refresh{color:var(--muted);font-size:12px}
.chart-header-left{display:flex;align-items:center;gap:10px;flex-wrap:wrap}
.chart-badges{display:flex;align-items:center;gap:6px;flex-wrap:wrap}
.p-pill{display:inline-flex;align-items:center;gap:4px;padding:3px 9px;border-radius:12px;font-size:11px;font-weight:600;border:1px solid transparent}
.p-pill-rev{background:rgba(191,145,255,0.14);color:var(--purple);border-color:rgba(191,145,255,0.35)}
.p-pill-follow{background:rgba(56,189,248,0.14);color:var(--blue);border-color:rgba(56,189,248,0.35)}
.p-pill-realized{background:rgba(255,101,122,0.14);color:var(--red);border-color:rgba(255,101,122,0.35)}
.p-pill-realized.pos{background:rgba(55,211,154,0.14);color:var(--green);border-color:rgba(55,211,154,0.35)}
.p-pill-open{background:rgba(55,211,154,0.14);color:var(--green);border-color:rgba(55,211,154,0.35)}
.p-pill-open.neg{background:rgba(255,101,122,0.14);color:var(--red);border-color:rgba(255,101,122,0.35)}
.chart-legend{display:flex;align-items:center;gap:10px;font-size:11px;color:var(--muted);flex-wrap:wrap}
.legend-item{display:inline-flex;align-items:center;gap:5px}
.legend-dot{width:8px;height:8px;border-radius:50%}
.dot-rev{background:var(--purple)}
.dot-follow{background:var(--blue)}
.legend-line{width:14px;height:2.5px;border-radius:1px}
.legend-line.line-open{height:0;border-radius:0;background:none;border-top:2px dashed var(--muted)}
.chart-tooltip{position:fixed;display:none;pointer-events:none;z-index:99999;background:rgba(15,23,42,0.96);border:1px solid rgba(148,163,184,0.3);box-shadow:0 16px 36px rgba(0,0,0,0.65),0 0 0 1px rgba(255,255,255,0.06);border-radius:10px;padding:12px 14px;max-width:340px;font-size:12px;color:#e2e8f0;line-height:1.45;backdrop-filter:blur(10px)}
.tt-head{display:flex;justify-content:space-between;align-items:center;border-bottom:1px solid rgba(148,163,184,0.2);padding-bottom:6px;margin-bottom:6px;gap:8px}
.tt-title{font-weight:700;font-size:13px;color:#f8fafc}
.tt-badge{font-size:10px;font-weight:700;padding:2px 6px;border-radius:8px;text-transform:uppercase}
.tt-pnl{font-size:17px;font-weight:800;margin:4px 0 6px 0}
.tt-row{display:flex;justify-content:space-between;margin:2.5px 0;font-size:11px;color:#94a3b8}
.tt-val{color:#f1f5f9;font-weight:500;text-align:right;margin-left:8px}
.tt-desc{margin-top:6px;padding-top:6px;border-top:1px dashed rgba(148,163,184,0.2);font-size:11px;color:#94a3b8;word-break:break-word}
h2{margin:18px 0 12px 0}
@media(max-width:960px){.charts-row{grid-template-columns:1fr}}
@media(max-width:800px){main{padding:12px}.page-columns{grid-template-columns:1fr}.candle-log-card{position:static;height:auto}.candle-log-list{height:auto}.modes{grid-template-columns:1fr}}
@media(prefers-color-scheme:light){
:root{color-scheme:light;--bg:#f2f5fb;--card:#ffffff;--line:#dce4f0;--text:#17243b;--muted:#65748b;--green:#087f5b;--red:#d6455d;--purple:#7551c8;--blue:#1674b8;--grid:#d5deea;--axis:#64748b;--ink:#1e293b;--zero:#334155;--run:#1e293b}
body{background:radial-gradient(ellipse at 12% 0%,#e6efff 0,transparent 38%),linear-gradient(180deg,#f5f7fc 0%,#edf2f9 100%);color:var(--text)}
main{padding:22px 28px}
.head{padding:16px 20px;margin-bottom:18px;border:1px solid rgba(208,220,238,.9);border-radius:18px;background:rgba(255,255,255,.78);box-shadow:0 10px 28px rgba(35,57,89,.07);backdrop-filter:blur(10px)}
h1{font-size:clamp(25px,2.2vw,34px);letter-spacing:-.035em;color:#16243b}
h2{margin:18px 0 12px 0;font-size:19px;letter-spacing:-.02em;color:#233652}
.card{border-color:rgba(210,221,237,.95);border-radius:16px;background:rgba(255,255,255,.94);box-shadow:0 8px 24px rgba(37,58,89,.055);margin-bottom:14px}
.grid>.card{position:relative;overflow:hidden;background:linear-gradient(145deg,#fff,#f8faff)}
.grid>.card:after{content:"";position:absolute;left:0;top:0;bottom:0;width:4px;background:linear-gradient(180deg,#75a8f8,#587de0)}
.value{color:#1c3150;font-size:clamp(20px,1.7vw,27px);letter-spacing:-.025em}
.label{color:#75849a}
.pill{border-color:#d6e1f0;background:#f6f9fe;color:#425775}
.candle-log-card{border-color:#cddbf0;background:linear-gradient(160deg,#fff 0%,#f7faff 100%);box-shadow:0 12px 30px rgba(36,64,105,.11)}
.candle-log-card>.label{font-weight:700;color:#476287}
.candle-log-item{padding:13px 11px;border-top:1px solid #e5ebf4;border-radius:9px}
.candle-log-item:first-child{margin-top:4px;border:1px solid #dce8fa;background:linear-gradient(135deg,#f5f9ff,#fff)}
.candle-log-item h3{color:#263d5e}
.candle-log-item p{line-height:1.45;color:#334760}
.candle-log-meta{color:#71829a}
.mode-title{letter-spacing:-.015em}
.modes>.card{background:linear-gradient(150deg,#fff,#fbfcff)}
table{border-radius:10px;overflow:hidden}
th{background:#f4f7fb;color:#63748c}
td{color:#2c3d56}
tr:hover td{background:#f8faff}
.pos{color:#087f5b}
.neg{color:#c53b54}
.refresh{color:#6b7b91}
}
@media(min-width:1000px) and (min-height:760px){
body{height:100vh;overflow:hidden}
main{height:100vh;padding:12px 18px;display:flex;flex-direction:column}
.head{flex:0 0 auto;margin-bottom:10px;padding:10px 16px;border-radius:13px}
.head h1{font-size:24px;margin:2px 0!important}
.page-columns{flex:1;min-height:0;height:auto;grid-template-columns:minmax(0,3fr) minmax(280px,1fr);gap:12px}
.dashboard-main{height:100%;overflow-y:auto;padding-right:6px;scrollbar-width:thin}
.dashboard-main .grid{margin-bottom:10px}
.charts-row{gap:10px;margin-bottom:10px}
.chart-card{min-height:280px;padding:12px}
.pnl-card{min-height:320px}
.chart-wrap{min-height:230px}
.dashboard-main h2{font-size:16px;margin:12px 0 6px 0}
.card{margin-bottom:10px}
.candle-log-card{position:sticky;top:0;height:100%}
.candle-log-list{height:calc(100vh - 120px);overflow-y:auto}
}
</style>
</head><body><main><div class="head"><div><div class="label">Day Breakout &middot; Live Dual Mode</div><h1 id="title">Loading&hellip;</h1></div><div style="text-align:right"><div class="pill" id="trade-source">Live trade CSVs</div><div class="refresh" id="updated"></div></div></div>
<section class="page-columns"><div class="dashboard-main"><section class="grid"><div class="card"><div class="label">Latest underlying spot</div><div class="value" id="spot">&mdash;</div></div><div class="card"><div class="label">Range high</div><div class="value" id="high">&mdash;</div></div><div class="card"><div class="label">Range low</div><div class="value" id="low">&mdash;</div></div><div class="card"><div class="label">Latest candle</div><div class="value" id="lastcandle">&mdash;</div></div></section>
<div class="charts-row">
<section class="card chart-card"><div class="chart-header"><div class="label">Underlying movement &middot; 15 minute OHLC</div></div><div class="chart-wrap"><svg id="chart" viewBox="0 0 1000 270" preserveAspectRatio="none"></svg></div></section>
<section class="card chart-card pnl-card">
<div class="chart-header pnl-head">
<div class="chart-header-left">
<div>
<div class="label">Trade profit / loss (Rs.)</div>
<div class="chart-sub pnl-chart-sub" id="pnl-chart-sub">Each trade: All trades (closed + open) as bars · Day total: Realized + floating P&amp;L combined over time</div>
</div>
<div id="pnl-summary-pills" class="chart-badges"></div>
</div>
<div class="chart-legend">
<div class="pnl-switch" id="pnl-switch">
<button type="button" data-view="trades" class="on">Each trade</button>
<button type="button" data-view="total">Day total</button>
</div>
<div class="pnl-switch mode-filter" id="mode-filter" style="display:none" title="Filter Day total line by mode">
<button type="button" data-mode="REVERSE" class="on mf-rev">REVERSE</button>
<button type="button" data-mode="NO-REVERSE" class="on mf-follow">NO-REVERSE</button>
</div>
<span class="legend-item"><span class="legend-dot dot-rev"></span>REVERSE</span>
<span class="legend-item"><span class="legend-dot dot-follow"></span>NO-REVERSE</span>
<span class="legend-item"><span class="legend-line line-open"></span>Open</span>
<span class="legend-item" style="color:#38bdf8;font-size:10px;">— Cumulative</span>
<button id="expand-pnl" class="expand-btn" title="Expand trade P&amp;L chart">&#x26F6;</button>
</div>
<div id="pnl-summary-pills" class="chart-badges"></div>
</div>
<div class="chart-wrap"><svg id="pnl-chart" viewBox="0 0 640 250" preserveAspectRatio="xMidYMid meet"></svg></div>
</section>
</div>
<h2>Strategy outcomes</h2><section class="modes"><div class="card"><h3 class="mode-title rev">REVERSE &middot; Fade breakout</h3><div id="reverse"></div></div><div class="card"><h3 class="mode-title follow">NO-REVERSE &middot; Follow breakout</h3><div id="no-reverse"></div></div></section>
<h2>Backtest daily profit / loss</h2><section class="card"><div class="label" id="backtest-source">Latest dual backtest CSV</div><div id="backtest-daily"></div></section>
<h2>Live position P&amp;L</h2><section class="card" id="positions"></section></div>
<aside class="card candle-log-card"><div class="label">15-minute candle decisions</div><div id="candle-log" class="candle-log-list"><p class="muted">Waiting for candle log entries.</p></div></aside></section></main>
<div id="pnl-modal" style="display:none;position:fixed;top:0;left:0;right:0;bottom:0;background:rgba(0,0,0,0.8);z-index:1000;align-items:center;justify-content:center;padding:20px;"><div style="background:var(--card);border-radius:16px;padding:24px;width:95vw;max-width:1200px;max-height:95vh;overflow:auto;position:relative;border:1px solid var(--line);box-shadow:0 20px 60px rgba(0,0,0,0.4);"><button id="close-pnl-modal" style="position:absolute;top:16px;right:16px;background:none;border:none;cursor:pointer;font-size:24px;color:var(--muted);padding:8px;border-radius:6px;">&times;</button><div class="chart-header" style="margin-bottom:14px;"><div class="chart-header-left"><div><h2 style="margin:0;">Trade profit / loss</h2><div class="chart-sub pnl-chart-sub" id="pnl-chart-sub-expanded">Each trade: All trades (closed + open) as bars · Day total: Realized + floating P&amp;L combined over time</div></div><div id="pnl-summary-pills-expanded" class="chart-badges"></div></div><div class="chart-legend" style="margin-right:32px;"><div class="pnl-switch"><button type="button" data-view="trades" class="on">Each trade</button><button type="button" data-view="total">Day total</button></div><div class="pnl-switch mode-filter" id="mode-filter-expanded" style="display:none" title="Filter Day total line by mode"><button type="button" data-mode="REVERSE" class="on mf-rev">REVERSE</button><button type="button" data-mode="NO-REVERSE" class="on mf-follow">NO-REVERSE</button></div><span class="legend-item"><span class="legend-dot dot-rev"></span>REVERSE</span><span class="legend-item"><span class="legend-dot dot-follow"></span>NO-REVERSE</span><span class="legend-item"><span class="legend-line line-open"></span>Open</span><span class="legend-item" style="color:#38bdf8;font-size:10px;">— Cumulative</span></div></div><div class="chart-wrap" style="height:500px;"><svg id="pnl-chart-expanded" viewBox="0 0 1000 500" preserveAspectRatio="xMidYMid meet" style="width:100%;height:100%;"></svg></div></div></div>
<div id="chart-tooltip" class="chart-tooltip"></div>
<script>
const fmt=n=>n==null?'—':Number(n).toLocaleString('en-IN',{minimumFractionDigits:2,maximumFractionDigits:2});
function showTradeTooltip(e,t,isClosed){const tip=document.getElementById('chart-tooltip');if(!tip)return;const isProfit=t.pnl>=0,pnlClass=isProfit?'pos':'neg',stratColor=t.mode==='REVERSE'?'p-pill-rev':'p-pill-follow';let html='';if(isClosed){const contract=(t.buy_symbol||t.symbol||'')+(t.sell_symbol&&t.sell_symbol!=='N/A'?' / '+t.sell_symbol:'');const fillStr=[t.lots?t.lots+' lots':'',t.quantity?t.quantity+' qty':''].filter(Boolean).join(' · ');const premiumStr=(t.entry_premium?'Rs. '+t.entry_premium:'—')+' → '+(t.exit_premium?'Rs. '+t.exit_premium:'—');html=`<div class="tt-head"><div class="tt-title">Trade #${t.tradeIndex} &middot; <span class="${t.mode==='REVERSE'?'rev':'follow'}">${t.mode}</span></div><div class="tt-badge ${stratColor}">CLOSED</div></div><div class="tt-pnl ${pnlClass}">Rs. ${fmt(t.pnl)}</div><div class="tt-row"><span>Account Realized at Exit:</span><span class="tt-val ${t.cumPnl>=0?'pos':'neg'}">Rs. ${fmt(t.cumPnl)}</span></div><div class="tt-row"><span>Mode Realized (${t.mode}):</span><span class="tt-val ${t.modeCum>=0?'pos':'neg'}">Rs. ${fmt(t.modeCum)}</span></div>${t.max_pnl!=null?`<div class="tt-row"><span>Peak Unrealized P&amp;L:</span><span class="tt-val pos">Rs. ${fmt(t.max_pnl)}</span></div>`:''}<div class="tt-row"><span>Timing:</span><span class="tt-val">${t.entry_time||'—'} → ${t.exit_time||'—'}</span></div><div class="tt-row"><span>Contract:</span><span class="tt-val">${contract||'—'}</span></div><div class="tt-row"><span>Position Size:</span><span class="tt-val">${fillStr||'—'}</span></div><div class="tt-row"><span>Premiums:</span><span class="tt-val">${premiumStr}</span></div><div class="tt-desc"><strong>Trigger:</strong> ${t.trade_reason||'—'}<br><strong>Exit Reason:</strong> <span style="color:#f87171">${t.exit_reason||'Closed'}</span></div>`}else{const contract=(t.buy_symbol||'')+(t.sell_symbol&&t.sell_symbol!=='N/A'?' / '+t.sell_symbol:'');const fillStr=[t.quantity?t.quantity+' qty':'',t.execution_price?'@ Rs. '+t.execution_price:''].filter(Boolean).join(' · ');html=`<div class="tt-head"><div class="tt-title">Live Position #${t.posIndex} &middot; <span class="${t.mode==='REVERSE'?'rev':'follow'}">${t.mode}</span></div><div class="tt-badge p-pill-open">ACTIVE</div></div><div class="tt-pnl ${pnlClass}">Rs. ${fmt(t.pnl)}</div>${t.peak!=null?`<div class="tt-row"><span>Peak Observed P&amp;L:</span><span class="tt-val pos">Rs. ${fmt(t.peak)}</span></div>`:''}<div class="tt-row"><span>Entry Time:</span><span class="tt-val">${t.entry_time||'—'}</span></div><div class="tt-row"><span>Last Monitored:</span><span class="tt-val">${t.time||'—'}</span></div><div class="tt-row"><span>Contract:</span><span class="tt-val">${contract||'—'}</span></div><div class="tt-row"><span>Fill:</span><span class="tt-val">${fillStr||'Market'}</span></div><div class="tt-desc"><strong>Trigger:</strong> ${t.trade_reason||'—'}<br>Still open. Included in the day total line as floating P&amp;L.</div>`}tip.innerHTML=html;tip.style.display='block';moveTradeTooltip(e)}
function showGenericTooltip(e,html){const tip=document.getElementById('chart-tooltip');if(!tip)return;tip.innerHTML=html;tip.style.display='block';moveTradeTooltip(e)}
function moveTradeTooltip(e){const tip=document.getElementById('chart-tooltip');if(!tip||tip.style.display==='none')return;const rect=tip.getBoundingClientRect();let left=e.clientX+14,top=e.clientY+14;if(left+rect.width>window.innerWidth-12)left=e.clientX-rect.width-14;if(top+rect.height>window.innerHeight-12)top=e.clientY-rect.height-14;tip.style.left=Math.max(8,left)+'px';tip.style.top=Math.max(8,top)+'px'}
function hideTradeTooltip(){const tip=document.getElementById('chart-tooltip');if(tip)tip.style.display='none'}
function renderBacktestDaily(data){const el=document.getElementById('backtest-daily'),days=(data||{}).days||[];document.getElementById('backtest-source').textContent=data&&data.source?'Source: '+data.source:'No dual backtest CSV found';if(!days.length){el.innerHTML='<p class="muted">Run a dual backtest to see daily realized P&amp;L here.</p>';return}const money=v=>v==null?'—':'Rs. '+fmt(v);el.innerHTML='<table><thead><tr><th>Date</th><th>Trades</th><th>REVERSE P&amp;L</th><th>REVERSE worst min unrealized</th><th>NO-REVERSE P&amp;L</th><th>NO-REVERSE worst min unrealized</th><th>Day total</th></tr></thead><tbody>'+days.map(d=>`<tr><td>${d.date}</td><td>${d.trades}</td><td class="${d.REVERSE>=0?'pos':'neg'}">Rs. ${fmt(d.REVERSE)}</td><td class="neg">${money(d.REVERSE_min_unrealized_pnl)}</td><td class="${d['NO-REVERSE']>=0?'pos':'neg'}">Rs. ${fmt(d['NO-REVERSE'])}</td><td class="neg">${money(d['NO-REVERSE_min_unrealized_pnl'])}</td><td class="${d.total>=0?'pos':'neg'}"><strong>Rs. ${fmt(d.total)}</strong></td></tr>`).join('')+'</tbody></table>'}
function renderCandleLog(items){const el=document.getElementById('candle-log');el.replaceChildren();const rows=(items||[]).slice().reverse();if(!rows.length){const p=document.createElement('p');p.className='muted';p.textContent='No completed 15-minute candle decisions logged yet.';el.append(p);return}for(const item of rows){const card=document.createElement('article');card.className='candle-log-item';const title=document.createElement('h3');title.textContent=`${item.candle} candle`;const meta=document.createElement('div');meta.className='candle-log-meta';meta.textContent=`O ${fmt(item.open)} · H ${fmt(item.high)} · L ${fmt(item.low)} · C ${fmt(item.close)}`;const range=document.createElement('div');range.className='candle-log-meta';range.textContent=`Range ${fmt(item.range_low)}–${fmt(item.range_high)}`;const summary=document.createElement('p');summary.textContent=item.summary;card.append(title,meta,range,summary);for(const decision of (item.trades||[])){const detail=document.createElement('p');detail.className='muted';detail.textContent=decision;card.append(detail)}el.append(card)}}
function drawChart(s){const el=document.getElementById('chart'),a=s.candles||[];if(!a.length){el.innerHTML='<text x="500" y="135" fill="#94a3b8" text-anchor="middle" font-size="13">Waiting for completed 15-minute candles in today&apos;s log</text>';return}let vals=a.flatMap(x=>[x.low,x.high]);if(s.range_low!=null)vals.push(s.range_low,s.range_high);if(s.spot!=null)vals.push(s.spot);let lo=Math.min(...vals),hi=Math.max(...vals),pad=(hi-lo)*0.08||5;lo-=pad;hi+=pad;const W=1000,H=270,padL=65,padR=25,padT=22,padB=32;const chartW=W-padL-padR,chartH=H-padT-padB,bottom=H-padB;const X=i=>padL+(i+0.5)*(chartW/Math.max(1,a.length));const Y=v=>padT+(hi-v)/(hi-lo)*chartH;const candleW=Math.min(26,Math.max(8,(chartW/Math.max(1,a.length))*0.65));let out='';for(let i=0;i<=4;i++){const v=lo+(hi-lo)*i/4,y=Y(v);out+=`<line x1="${padL}" x2="${W-padR}" y1="${y}" y2="${y}" stroke="#26334c" stroke-dasharray="3 3"/><text x="${padL-8}" y="${y+4}" fill="#94a3b8" font-size="11" text-anchor="end">${fmt(v)}</text>`}if(s.range_high!=null){const yHigh=Y(s.range_high);out+=`<line x1="${padL}" x2="${W-padR}" y1="${yHigh}" y2="${yHigh}" stroke="#37d39a" stroke-width="1.5" stroke-dasharray="6 4"/><text x="${W-padR-5}" y="${yHigh-5}" fill="#37d39a" font-size="11" font-weight="600" text-anchor="end">Range High ${fmt(s.range_high)}</text>`}if(s.range_low!=null){const yLow=Y(s.range_low);out+=`<line x1="${padL}" x2="${W-padR}" y1="${yLow}" y2="${yLow}" stroke="#ff657a" stroke-width="1.5" stroke-dasharray="6 4"/><text x="${W-padR-5}" y="${yLow+13}" fill="#ff657a" font-size="11" font-weight="600" text-anchor="end">Range Low ${fmt(s.range_low)}</text>`}a.forEach((c,i)=>{const x=X(i),up=c.close>=c.open,color=up?'#37d39a':'#ff657a';const yH=Y(c.high),yL=Y(c.low),yO=Y(c.open),yC=Y(c.close);const top=Math.min(yO,yC),height=Math.max(2,Math.abs(yC-yO));const tip=`${c.candle} | O: ${fmt(c.open)} H: ${fmt(c.high)} L: ${fmt(c.low)} C: ${fmt(c.close)}`;out+=`<g data-tip="${tip}"><line x1="${x}" x2="${x}" y1="${yH}" y2="${yL}" stroke="${color}" stroke-width="1.5"/><rect x="${x-candleW/2}" y="${top}" width="${candleW}" height="${height}" fill="${color}" stroke="${color}" rx="1"/><text x="${x}" y="${bottom+16}" fill="#94a3b8" font-size="11" text-anchor="middle">${c.candle}</text></g>`});el.innerHTML=out;el.querySelectorAll('[data-tip]').forEach(n=>{n.addEventListener('mouseenter',e=>{const tipText=e.currentTarget.getAttribute('data-tip'),p=tipText.split(' | ');showGenericTooltip(e,`<div class="tt-head"><div class="tt-title">15m Candle &middot; ${p[0]}</div></div><div class="tt-desc" style="margin:0;padding:0;border:none;font-size:12px;color:#e2e8f0;">${p[1]}</div>`)});n.addEventListener('mousemove',e=>moveTradeTooltip(e));n.addEventListener('mouseleave',()=>hideTradeTooltip())})}
let view='trades';function drawPnlChartCore(s,svgId,pillsId,isExpanded){const el=document.getElementById(svgId);if(!el)return;const pillsEl=document.getElementById(pillsId),W=1000,H=isExpanded?500:280;view=currentPnlView||'trades';const completed=[],positions=[];let revTotal=0,followTotal=0,openTotal=0;['REVERSE','NO-REVERSE'].forEach(mode=>{const list=((s.modes&&s.modes[mode]&&s.modes[mode].completed)||[]);list.forEach(t=>{const p=Number(t.pnl||0);if(mode==='REVERSE')revTotal+=p;else followTotal+=p;completed.push({...t,mode,closed:true,pnl:p,when:t.exit_time||t.entry_time})})});(s.positions||[]).forEach((p,idx)=>{const pnlVal=Number(p.pnl||0);openTotal+=pnlVal;positions.push({...p,posIndex:idx+1,closed:false,pnl:pnlVal,when:p.time||p.entry_time})});const netRealized=revTotal+followTotal;if(pillsEl){pillsEl.innerHTML=`<span class="p-pill p-pill-rev" title="REVERSE Mode Realized P&amp;L">&bull; REVERSE: Rs. ${fmt(revTotal)} (${completed.filter(t=>t.mode==='REVERSE').length})</span><span class="p-pill p-pill-follow" title="NO-REVERSE Mode Realized P&amp;L">&bull; NO-REVERSE: Rs. ${fmt(followTotal)} (${completed.filter(t=>t.mode==='NO-REVERSE').length})</span><span class="p-pill p-pill-realized ${netRealized>=0?'pos':''}" title="Net Realized P&amp;L across all closed trades">Net: Rs. ${fmt(netRealized)}</span>${positions.length?`<span class="p-pill p-pill-open" title="Open Positions Floating P&amp;L">Live Open: Rs. ${fmt(openTotal)} (${positions.length})</span>`:''}`; }const toMin=t=>{const m=/^(\d{1,2}):(\d{2})/.exec(t||'');return m?Number(m[1])*60+Number(m[2]):null};completed.forEach(t=>{t.exitMin=toMin(t.exit_time||t.when);t.entryMin=toMin(t.entry_time)||t.exitMin});positions.forEach(p=>{p.posMin=toMin(p.time||p.when);p.entryMin=toMin(p.entry_time)||p.posMin});const validCompleted=completed.filter(t=>t.exitMin!==null&&t.exitMin>=540&&t.exitMin<=930);validCompleted.sort((a,b)=>(a.exitMin||0)-(b.exitMin||0));let cum=0,revCum=0,followCum=0;validCompleted.forEach((t,i)=>{cum+=t.pnl;t.cumPnl=cum;t.tradeIndex=i+1;t.label='T'+(i+1);if(t.mode==='REVERSE')revCum+=t.pnl;else followCum+=t.pnl;t.modeCum=t.mode==='REVERSE'?revCum:followCum});const validPositions=positions.filter(p=>p.posMin!==null&&p.posMin>=540&&p.posMin<=930);if(!validCompleted.length&&!validPositions.length){el.innerHTML=`<text x="${W/2}" y="${H/2}" fill="#94a3b8" text-anchor="middle" font-size="${isExpanded?15:13}">No live trades or positions recorded today</text>`;return}const padL=isExpanded?80:70,padR=30,padT=isExpanded?30:25,padB=isExpanded?40:34;const chartW=W-padL-padR,chartH=H-padT-padB,bottom=H-padB;const posStart=p=>p.entryMin!=null?p.entryMin:p.posMin;document.querySelectorAll('.mode-filter').forEach(x=>x.style.display=view==='total'?'inline-flex':'none');const modeMatch=m=>{const s=String(m||'');if(s==='REVERSE')return !!pnlModes.REVERSE;if(s==='NO-REVERSE')return !!pnlModes['NO-REVERSE'];return ['REVERSE','NO-REVERSE'].some(md=>pnlModes[md]&&s.includes(md))};const totalCompleted=view==='total'?validCompleted.filter(t=>modeMatch(t.mode)):validCompleted;const totalPositions=view==='total'?validPositions.filter(p=>modeMatch(p.mode)):validPositions;const totalTimes=[...new Set([...totalCompleted.map(t=>t.exitMin),...totalPositions.flatMap(p=>[posStart(p),p.posMin])])].filter(m=>m!=null).sort((a,b)=>a-b);const totalValAt=m=>totalCompleted.reduce((v,t)=>v+(t.exitMin<=m?t.pnl:0),0)+totalPositions.reduce((v,p)=>v+(posStart(p)<=m?p.pnl:0),0);const totalSeries=totalTimes.map(m=>({m,v:totalValAt(m)}));const realizedEnd=totalCompleted.reduce((v,t)=>v+t.pnl,0),floatEnd=totalPositions.reduce((v,p)=>v+p.pnl,0);const allVals=[0,...validCompleted.map(t=>t.pnl),...validCompleted.map(t=>t.cumPnl),...validPositions.map(p=>p.pnl),...totalSeries.map(pt=>pt.v)];let lo,hi;if(view==='trades'){const m=Math.max(1,...validCompleted.map(t=>Math.abs(t.pnl)),...validPositions.map(p=>Math.abs(p.pnl)))*1.18;lo=-m;hi=m}else{lo=Math.min(...allVals);hi=Math.max(...allVals);let pad=(hi-lo)*0.16||500;lo-=pad;hi+=pad}const X=m=>padL+(Math.max(540,Math.min(930,m))-540)/390*chartW;const Y=v=>padT+(hi-v)/(hi-lo)*chartH;const zeroY=Y(0);let out='';out+=`<defs><linearGradient id="grad-profit-${svgId}" x1="0" y1="0" x2="0" y2="1"><stop offset="0%" stop-color="#38bdf8" stop-opacity="0.25"/><stop offset="100%" stop-color="#38bdf8" stop-opacity="0.0"/></linearGradient></defs>`;for(let i=0;i<=4;i++){const v=lo+(hi-lo)*i/4,y=Y(v);out+=`<line x1="${padL}" x2="${W-padR}" y1="${y}" y2="${y}" stroke="#26334c" stroke-dasharray="3 3"/><text x="${padL-8}" y="${y+4}" fill="#94a3b8" font-size="${isExpanded?11:10}" text-anchor="end">Rs. ${fmt(v)}</text>`}out+=`<line x1="${padL}" x2="${W-padR}" y1="${zeroY}" y2="${zeroY}" stroke="#64748b" stroke-width="1.5" stroke-dasharray="4 4"/><text x="${W-padR-5}" y="${zeroY-5}" fill="#64748b" font-size="10" font-weight="600" text-anchor="end">Rs. 0.00</text>`;for(let m=540;m<=930;m+=30){const x=X(m),label=`${String(Math.floor(m/60)).padStart(2,'0')}:${String(m%60).padStart(2,'0')}`;out+=`<line x1="${x}" x2="${x}" y1="${padT}" y2="${bottom}" stroke="#1e293b"/><text x="${x}" y="${bottom+(isExpanded?18:16)}" fill="#94a3b8" font-size="${isExpanded?11:10}" text-anchor="middle">${label}</text>`}if(view==='trades'&&(validCompleted.length||validPositions.length)){const barItems=[...validCompleted.map(t=>({...t,isOpen:false})),...validPositions.map(p=>({...p,isOpen:true,label:'L'+p.posIndex}))];const barWidth=Math.min(40,Math.max(12,(chartW/Math.max(1,barItems.length))*0.6));const gap=8;const totalWidth=barItems.length*(barWidth+gap)-gap;const startX=padL+(chartW-totalWidth)/2;barItems.forEach((t,i)=>{const x=startX+i*(barWidth+gap);const y=Y(t.pnl);const h=zeroY-y;const isProfit=t.pnl>=0;const stratColor=t.isOpen?'#0f766e':(t.mode==='REVERSE'?'#7c3aed':'#2563eb');const pnlColor=isProfit?'#37d39a':'#ff657a';out+=`<rect x="${x}" y="${isProfit?y:zeroY}" width="${barWidth}" height="${Math.abs(h)}" fill="${stratColor}" opacity="0.8" rx="2"${t.isOpen?' stroke="#34d399" stroke-width="1.5" stroke-dasharray="4 3"':''}/>`;out+=`<rect x="${x}" y="${isProfit?y:zeroY}" width="${barWidth}" height="${Math.abs(h)}" fill="${pnlColor}" opacity="0.3" rx="2"/>`;out+=`<rect x="${x}" y="${y-22}" width="${barWidth}" height="20" rx="3" fill="#1e293b" stroke="${stratColor}" stroke-width="1.5"/>`;out+=`<text x="${x+barWidth/2}" y="${y-8}" fill="${t.isOpen?'#34d399':'#e2e8f0'}" font-size="10" font-weight="700" text-anchor="middle">${t.label}</text>`;const textY=isProfit?y+16:zeroY-6;const shortPnl=(t.pnl>=0?'+':'')+(Math.abs(t.pnl)>=1000?(t.pnl/1000).toFixed(1)+'k':t.pnl.toFixed(0));out+=`<text x="${x+barWidth/2}" y="${textY}" fill="${pnlColor}" font-size="10" font-weight="600" text-anchor="middle">${shortPnl}</text>`;out+=`<rect x="${Math.min(x,x)}" y="${Math.min(y,zeroY)}" width="${barWidth}" height="${Math.max(4,Math.abs(h))}" fill="transparent" class="${t.isOpen?'pos-hit':'trade-hit'}" data-${t.isOpen?'pos':'trade'}-idx="${t.isOpen?t.posIndex-1:t.tradeIndex-1}" style="cursor:pointer;"/>`;});}if(view==='total'&&(validCompleted.length||validPositions.length)){const firstMin=Math.min(totalSeries.length?totalSeries[0].m:555,...totalCompleted.map(t=>t.entryMin||1e9),...totalPositions.map(p=>posStart(p)||1e9),555);const startX=X(firstMin);let pathD=`M ${startX} ${Y(0)}`;totalSeries.forEach(pt=>{pathD+=` L ${X(pt.m)} ${Y(pt.v)}`});const lastM=totalSeries.length?totalSeries[totalSeries.length-1].m:firstMin;const lastX=X(lastM),lastY=Y(totalSeries.length?totalSeries[totalSeries.length-1].v:0);const areaD=`${pathD} L ${lastX} ${zeroY} L ${startX} ${zeroY} Z`;out+=`<path d="${areaD}" fill="url(#grad-profit-${svgId})"/>`;out+=`<path d="${pathD}" fill="none" stroke="#38bdf8" stroke-width="${isExpanded?3:2.5}" stroke-linecap="round" stroke-linejoin="round"/>`;out+=`<circle cx="${lastX}" cy="${lastY}" r="${isExpanded?6:5}" fill="#38bdf8" stroke="#0f172a" stroke-width="2"/>`;out+=`<text x="${lastX}" y="${lastY-(isExpanded?14:11)}" fill="#38bdf8" font-size="${isExpanded?12:10}" font-weight="700" text-anchor="middle">Rs. ${fmt(realizedEnd+floatEnd)}</text>`;out+=`<path d="${pathD}" fill="none" stroke="transparent" stroke-width="16" class="pnl-cum-hover" data-cum-info="Day Total (Realized + Floating) over time [${Object.keys(pnlModes).filter(m=>pnlModes[m]).join(' + ')||'no mode selected'}]: Realized Rs. ${fmt(realizedEnd)} (${totalCompleted.length} closed) + Floating Rs. ${fmt(floatEnd)} (${totalPositions.length} open) = Rs. ${fmt(realizedEnd+floatEnd)}." style="cursor:pointer;"/>`;totalCompleted.forEach(t=>{const m0=t.entryMin!=null?t.entryMin:t.exitMin;const xS=X(m0),yS=Y(totalValAt(m0)-(t.exitMin<=m0?t.pnl:0)),xE=X(t.exitMin),yE=Y(totalValAt(t.exitMin));const color=t.mode==='REVERSE'?'#7c3aed':'#2563eb';out+=`<line x1="${xS}" x2="${xE}" y1="${yS}" y2="${yE}" stroke="${color}" stroke-width="2" opacity="0.9"/>`;out+=`<circle cx="${xS}" cy="${yS}" r="4" fill="#0f172a" stroke="${color}" stroke-width="2"/>`;out+=`<circle cx="${xE}" cy="${yE}" r="4.5" fill="${color}" stroke="#0f172a" stroke-width="1.5"/>`;out+=`<rect x="${Math.min(xS,xE)}" y="${padT}" width="${Math.max(14,Math.abs(xE-xS))}" height="${chartH}" fill="transparent" class="trade-hit" data-trade-idx="${t.tradeIndex-1}" style="cursor:pointer;"/>`;})}if(view==='total')totalPositions.forEach(p=>{const m0=posStart(p);const xS=X(m0),yS=Y(totalValAt(m0)-p.pnl),xE=X(p.posMin),yE=Y(totalValAt(p.posMin));out+=`<line x1="${xS}" x2="${xE}" y1="${yS}" y2="${yE}" stroke="#34d399" stroke-width="2" stroke-dasharray="5 4" opacity="0.9"/>`;out+=`<circle cx="${xS}" cy="${yS}" r="4" fill="#0f172a" stroke="#34d399" stroke-width="2"/>`;out+=`<rect x="${xE-4.5}" y="${yE-4.5}" width="9" height="9" fill="#34d399" stroke="#0f172a" stroke-width="1.5" rx="1.5" transform="rotate(45 ${xE} ${yE})"/>`;out+=`<rect x="${Math.min(xS,xE)}" y="${padT}" width="${Math.max(14,Math.abs(xE-xS))}" height="${chartH}" fill="transparent" class="pos-hit" data-pos-idx="${p.posIndex-1}" style="cursor:pointer;"/>`;});el.innerHTML=out;el.querySelectorAll('.trade-hit').forEach(hit=>{const idx=Number(hit.getAttribute('data-trade-idx')),t=validCompleted[idx];if(!t)return;hit.addEventListener('mouseenter',e=>showTradeTooltip(e,t,true));hit.addEventListener('mousemove',e=>moveTradeTooltip(e));hit.addEventListener('mouseleave',()=>hideTradeTooltip())});el.querySelectorAll('.pos-hit').forEach(hit=>{const idx=Number(hit.getAttribute('data-pos-idx')),p=validPositions[idx];if(!p)return;hit.addEventListener('mouseenter',e=>showTradeTooltip(e,p,false));hit.addEventListener('mousemove',e=>moveTradeTooltip(e));hit.addEventListener('mouseleave',()=>hideTradeTooltip())});el.querySelectorAll('.pnl-cum-hover').forEach(line=>{line.addEventListener('mouseenter',e=>{const info=line.getAttribute('data-cum-info');showGenericTooltip(e,`<div class="tt-head"><div class="tt-title">Day Total Movement</div><div class="tt-badge p-pill-follow">REALIZED + FLOATING</div></div><div class="tt-desc" style="font-size:12px;color:#e2e8f0;margin:0;padding:0;border:none;">${info}</div>`)});line.addEventListener('mousemove',e=>moveTradeTooltip(e));line.addEventListener('mouseleave',()=>hideTradeTooltip())});}
function drawPnlChart(s){drawPnlChartCore(s,'pnl-chart','pnl-summary-pills',false)}
function drawPnlChartExpanded(s){drawPnlChartCore(s,'pnl-chart-expanded','pnl-summary-pills-expanded',true)}
function rows(mode){let d=mode||{},tr=(d.triggered||[]),done=(d.completed||[]);let html='<div class="label">Triggered: '+tr.length+' · Completed: '+done.length+'</div>';if(!tr.length&&!done.length)return html+'<p class="muted">No trades recorded for this mode today.</p>';const minPnl=x=>x.min_unrealized_pnl==null||String(x.min_unrealized_pnl).trim()===''?'—':'Rs. '+fmt(x.min_unrealized_pnl);html+='<table><thead><tr><th>Status / Time</th><th>Structure</th><th>Contracts / reason</th><th>P&amp;L</th><th>Min unrealized P&amp;L</th></tr></thead><tbody>';tr.slice().reverse().forEach(x=>html+=`<tr><td>Triggered<br>${x.time||''}</td><td>${x.structure||''} · ${x.lots||''} lots</td><td>${x.buy_symbol||''}${x.sell_symbol&&x.sell_symbol!=='N/A'?' / '+x.sell_symbol:''}<br><span class="muted">${x.trade_reason||''}</span></td><td class="muted">Awaiting completion</td><td>${minPnl(x)}</td></tr>`);done.slice().reverse().forEach(x=>{let p=Number(x.pnl||0);html+=`<tr><td>${x.exit_time||x.entry_time||''}<br>${x.exit_reason||x.status||'Closed'}</td><td>${x.structure||''} · ${x.lots||''} lots</td><td>${x.buy_symbol||''}${x.sell_symbol&&x.sell_symbol!=='N/A'?' / '+x.sell_symbol:''}<br><span class="muted">${x.trade_reason||''}</span></td><td class="${p>=0?'pos':'neg'}">Rs. ${fmt(p)}</td><td class="neg">${minPnl(x)}</td></tr>`});return html+'</tbody></table>'}
function renderPositions(items){const el=document.getElementById('positions');el.replaceChildren();if(!items.length){const empty=document.createElement('span');empty.className='muted';empty.textContent='No live P&L samples found in the logger yet. Open-position P&L appears when the strategy monitor writes it.';el.append(empty);return}const table=document.createElement('table');table.innerHTML='<thead><tr><th>Trade</th><th>Mode</th><th>Contract / fill</th><th>Entry</th><th>Updated</th><th>Current P&amp;L</th><th>Peak P&amp;L</th><th>Reason</th></tr></thead>';const body=document.createElement('tbody');for(const p of items){const row=document.createElement('tr');const contract=document.createElement('td');contract.textContent=(p.buy_symbol||'—')+(p.sell_symbol&&p.sell_symbol!=='N/A'?' / '+p.sell_symbol:'');const fill=document.createElement('small');fill.className='muted';fill.textContent=[p.quantity?p.quantity+' qty':'',p.execution_price?'@ Rs. '+p.execution_price:''].filter(Boolean).join(' · ');contract.append(document.createElement('br'),fill);const cells=[p.trade_id,p.mode,null,p.entry_time||'—',p.time,null,'Rs. '+fmt(p.peak),null];for(let i=0;i<cells.length;i++){if(i===2){row.append(contract);continue}const cell=document.createElement('td');if(i===5){cell.className=Number(p.pnl)>=0?'pos':'neg';cell.textContent='Rs. '+fmt(p.pnl)}else if(i===7){cell.textContent=p.trade_reason||'—';if(p.monitor_note){const note=document.createElement('small');note.className='muted';note.textContent=p.monitor_note;cell.append(document.createElement('br'),note)}}else{cell.textContent=cells[i]??''}row.append(cell)}body.append(row)}table.append(body);el.append(table)}
let currentPnlState=null;
let currentPnlView='trades';
let pnlModes={REVERSE:true,'NO-REVERSE':true};
document.getElementById('expand-pnl').addEventListener('click',()=>{const modal=document.getElementById('pnl-modal');modal.style.display='flex';if(currentPnlState)drawPnlChartExpanded(currentPnlState);const modalSwitches=modal.querySelectorAll('.pnl-switch:not(.mode-filter) button');modalSwitches.forEach(btn=>{if(btn.getAttribute('data-view')===currentPnlView)btn.classList.add('on');else btn.classList.remove('on')})});
document.getElementById('close-pnl-modal').addEventListener('click',()=>{document.getElementById('pnl-modal').style.display='none'});
document.getElementById('pnl-modal').addEventListener('click',e=>{if(e.target.id==='pnl-modal')e.target.style.display='none'});
document.querySelectorAll('.pnl-switch:not(.mode-filter) button').forEach(btn=>{btn.addEventListener('click',e=>{document.querySelectorAll('.pnl-switch:not(.mode-filter) button').forEach(b=>b.classList.remove('on'));e.target.classList.add('on');currentPnlView=e.target.getAttribute('data-view');if(currentPnlState){drawPnlChart(currentPnlState);drawPnlChartExpanded(currentPnlState)}})});
document.querySelectorAll('.mode-filter button').forEach(btn=>{btn.addEventListener('click',e=>{const mode=e.target.getAttribute('data-mode');pnlModes[mode]=!pnlModes[mode];document.querySelectorAll('.mode-filter button').forEach(b=>b.classList.toggle('on',!!pnlModes[b.getAttribute('data-mode')]));if(currentPnlState){drawPnlChart(currentPnlState);drawPnlChartExpanded(currentPnlState)}})});
async function update(){try{let s=await fetch('/api/state',{cache:'no-store'}).then(r=>r.json());currentPnlState=s;document.getElementById('title').textContent=s.instrument+' · '+s.date;document.getElementById('updated').textContent='Last log update: '+s.updated+' · Refreshes every 5 seconds';const ts=document.getElementById('trade-source');if(ts)ts.textContent=s.trade_source;document.getElementById('spot').textContent=s.spot==null?'—':fmt(s.spot);document.getElementById('high').textContent=s.range_high==null?'—':fmt(s.range_high);document.getElementById('low').textContent=s.range_low==null?'—':fmt(s.range_low);document.getElementById('lastcandle').textContent=s.candles.length?s.candles[s.candles.length-1].candle:'—';drawChart(s);drawPnlChart(s);renderCandleLog(s.candle_log);document.getElementById('reverse').innerHTML=rows(s.modes.REVERSE);document.getElementById('no-reverse').innerHTML=rows(s.modes['NO-REVERSE']);renderBacktestDaily(s.backtest_daily);renderPositions(s.positions)}catch(e){console.error(e)}}update();setInterval(update,5000);
</script></body></html>"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        pass

    def do_GET(self):
        if urlparse(self.path).path == "/api/state":
            body = json.dumps(get_state(), ensure_ascii=False).encode("utf-8")
            kind = "application/json; charset=utf-8"
        else:
            try:
                src = Path(__file__).read_text(encoding="utf-8", errors="replace")
                m = re.search(r'PAGE\s*=\s*r?"""(.*?)"""', src, re.DOTALL)
                content = m.group(1) if m else PAGE
            except Exception:
                content = PAGE
            body = content.encode("utf-8")
            kind = "text/html; charset=utf-8"
        self.send_response(200)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)


def main():
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    print(f"Day Breakout Dual Viewer: http://localhost:{PORT}")
    print(f"Reading daily logs from {LOGS_DIR} and trades from {DATA_DIR / 'trades'}")
    print("Read-only dashboard. Press Ctrl+C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nViewer stopped.")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
