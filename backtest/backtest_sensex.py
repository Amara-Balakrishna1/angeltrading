"""
Multi-Day Backtest Engine for SENSEX Breakout Strategy
Fetches historical candle data from Angel One SmartAPI for the last N trading days,
runs the exact Breakout & Broken Spread Strategy simulation,
and generates logs in data/<DD-MM-YYYY>/ for the web dashboard viewer.
"""

import sys
import io
import json
import time
from pathlib import Path
from datetime import datetime, timedelta
from api_helper import RateLimitedSmartApi
from backtest_utils import save_day_results, create_summary_tables, create_overall_summary

# Configurable number of days for backtest (default 20)
NUM_DAYS = 15
# Set to True to run a single-day backtest for today only
RUN_TODAY_ONLY = False

# Ensure UTF-8 output on Windows
if sys.platform == "win32":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

ROOT = Path(__file__).resolve().parent.parent
VENV_SITE_PACKAGES = ROOT / ".venv" / "Lib" / "site-packages"
if VENV_SITE_PACKAGES.exists() and str(VENV_SITE_PACKAGES) not in sys.path:
    sys.path.insert(0, str(VENV_SITE_PACKAGES))
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import csv
import requests
import pyotp
from SmartApi.smartConnect import SmartConnect
import config

# ==== SENSEX Configuration (hardcoded) ====
INSTRUMENT = "SENSEX"
EXCHANGE = "BSE"
SYMBOLTOKEN = "99919000"
OPTION_EXCH_SEG = "BFO"        # SENSEX options are on BFO
# NOTE: Expiry is calculated dynamically based on nearest expiry to analysis date
CSV_PREFIX = "SENSEX"           # e.g. SENSEX-26-08-2026.csv

DATA_ROOT = ROOT / "data"
DATA_ROOT.mkdir(parents=True, exist_ok=True)
GLOBAL_TRADES_FILE = DATA_ROOT / "trades.csv"

# Strategy Parameters
ALERT_INTERVAL_MINUTES = 3
MIN_BREAKOUT_AMOUNT = 3.0
REQUIRED_CONSECUTIVE_CHECKS = 2
DEFAULT_LOTS = 5
LOT_SIZE = 20
TOTAL_QTY = DEFAULT_LOTS * LOT_SIZE  # 100 qty
MAX_LOSS = -5000.0
MAX_PROFIT = 50000.0
PROFIT_THRESHOLD = 3000.0
MIN_PROFIT_STEP = 2000.0
MAX_DAILY_TRADES = 4
DAILY_MAX_LOSS = -10000.0
SPREAD_WIDTH = 1000  # 1000 points OTM sell leg


def get_smartapi_session():
    """Initializes and authenticates SmartConnect session."""
    api_key = config.api_key.strip()
    smart_api = SmartConnect(api_key, disable_ssl=True)
    smart_api = RateLimitedSmartApi(smart_api_instance=smart_api, max_requests_per_sec=0.5)
    totp = pyotp.TOTP(config.token).now()
    session = smart_api.generateSession(config.username, config.pwd, totp)
    if not session.get("status"):
        raise RuntimeError(f"SmartAPI login failed: {session}")
    return smart_api


def get_last_n_trading_days(smart_api, n_days=20):
    """Fetches daily candles for SENSEX to determine the last N active market trading days."""
    res = smart_api.getCandleData({
        "exchange": EXCHANGE,
        "symboltoken": SYMBOLTOKEN,
        "interval": "ONE_DAY",
        "fromdate": (datetime.now() - timedelta(days=60)).strftime("%Y-%m-%d 09:15"),
        "todate": datetime.now().strftime("%Y-%m-%d 15:30"),
    })
    if not res.get("status") or not res.get("data"):
        raise RuntimeError(f"Failed to fetch trading days: {res}")

    trading_dates = [c[0][:10] for c in res["data"]]
    return trading_dates[-n_days:]


def load_instrument_map(instruments=None, for_date_str=None):
    """Loads OpenAPIScripMaster and dynamically selects the nearest non-expired option contracts relative to for_date_str."""
    if instruments is None:
        url = "https://margincalculator.angelbroking.com/OpenAPI_File/files/OpenAPIScripMaster.json"
        resp = requests.get(url, timeout=30, verify=False)
        instruments = resp.json()

    target_date = datetime.strptime(for_date_str, "%Y-%m-%d").date() if for_date_str else datetime.now().date()

    matching = [
        item for item in instruments
        if item.get("name") == INSTRUMENT
        and item.get("exch_seg") == OPTION_EXCH_SEG
        and item.get("instrumenttype") == "OPTIDX"
        and item.get("expiry")
    ]

    valid_expiries = []
    for item in matching:
        try:
            exp_date = datetime.strptime(item["expiry"], "%d%b%Y").date()
            if exp_date >= target_date:
                valid_expiries.append(exp_date)
        except ValueError:
            pass

    if valid_expiries:
        selected_expiry_date = min(valid_expiries)
        selected_expiry_str = selected_expiry_date.strftime("%d%b%Y").upper()
    else:
        all_expiries = []
        for item in matching:
            try:
                all_expiries.append(datetime.strptime(item["expiry"], "%d%b%Y").date())
            except ValueError:
                pass
        selected_expiry_date = min(all_expiries) if all_expiries else target_date
        selected_expiry_str = selected_expiry_date.strftime("%d%b%Y").upper()

    option_map = {}
    for item in matching:
        if item.get("expiry") == selected_expiry_str:
            strike = float(item["strike"]) / 100.0
            opt_type = item["symbol"][-2:]
            option_map[(strike, opt_type)] = {
                "token": str(item["token"]),
                "symbol": item["symbol"],
                "strike": strike,
                "option_type": opt_type,
                "expiry": selected_expiry_str,
            }
    return option_map, selected_expiry_str


def fetch_day_candles(smart_api, date_str, option_map, cache_dir):
    """
    Fetches spot and required option candles for a given date (YYYY-MM-DD),
    caching them locally.
    """
    cache_file = cache_dir / f"candles_{date_str}.json"
    if cache_file.exists():
        with open(cache_file, "r", encoding="utf-8") as f:
            print(f"    📂 Loading from cache...")
            return json.load(f)
    
    print(f"    📡 Fetching data from API...")

    # 1. Fetch SENSEX Spot
    spot_res = smart_api.getCandleData({
        "exchange": EXCHANGE,
        "symboltoken": SYMBOLTOKEN,
        "interval": "ONE_MINUTE",
        "fromdate": f"{date_str} 09:15",
        "todate": f"{date_str} 15:30",
    })
    if not spot_res.get("status") or not spot_res.get("data"):
        print(f"[!] No spot data for {date_str}, skipping.")
        return None

    spot_candles = {c[0][:16]: c[4] for c in spot_res["data"]}
    min_spot = min(spot_candles.values())
    max_spot = max(spot_candles.values())

    needed_strikes = range(
        int(round(min_spot / 100) * 100) - 1500,
        int(round(max_spot / 100) * 100) + 1600,
        100,
    )

    day_data = {"spot": spot_candles, "options": {}}

    for st in needed_strikes:
        # Fetch CE and PE options for the selected strike
        for ot in ["CE", "PE"]:
            key = f"{st}_{ot}"

            opt_info = option_map.get((float(st), ot))

            if not opt_info:
                print(f"    ⚠️ Option {key} not found in instrument map")
                continue

            # Store all chunks for this option
            option_data = {}

            # 2-hour chunks
            chunks = [
                ("09:15", "11:15"),
                ("11:15", "13:15"),
                ("13:15", "15:00"),
             #   ("15:00", "15:15"),
            ]

            for chunk_start, chunk_end in chunks:

                success = False

                for attempt in range(3):
                    try:
                        p = {
                            "exchange": OPTION_EXCH_SEG,
                            "symboltoken": opt_info["token"],
                            "interval": "ONE_MINUTE",
                            "fromdate": f"{date_str} {chunk_start}",
                            "todate": f"{date_str} {chunk_end}",
                        }

                        res = smart_api.getCandleData(p)

                        if res.get("status") and res.get("data"):
                            # Check if data is not empty
                            if len(res["data"]) > 0:
                                for c in res["data"]:
                                    option_data[c[0][:16]] = c[4]
                                success = True
                                break
                            else:
                                # Empty data - option contract might not exist or have no trading data
                                print(f"    ⚠️ No data available for {key} {chunk_start}-{chunk_end} (token: {opt_info['token']})")
                                break  # Don't retry for empty data
                        else:
                            print(
                                f"    Retry {attempt + 1}/3: "
                                f"{key} {chunk_start}-{chunk_end} - API response: {res}"
                            )
                            time.sleep(5.0 * (attempt + 1))

                    except Exception as e:
                        print(
                            f"    API error {key} "
                            f"{chunk_start}-{chunk_end}: {e}"
                        )
                        time.sleep(5.0 * (attempt + 1))

                if not success:
                    print(
                        f"    ❌ Failed: {key} "
                        f"{chunk_start}-{chunk_end}"
                    )
                
                # Add delay between chunks to avoid rate limiting
                time.sleep(1.0)

            # Save all chunks together
            if option_data:
                day_data["options"][key] = option_data

    # Log summary of fetched options
    successful_options = len(day_data["options"])
    print(f"    ✅ Successfully fetched {successful_options} option contracts")

    cache_dir.mkdir(parents=True, exist_ok=True)
    with open(cache_file, "w", encoding="utf-8") as f:
        json.dump(day_data, f)

    return day_data


def nearest_strike(spot, strike_step=100):
    return int(round(spot / strike_step) * strike_step)


def get_ce_strikes(spot, strike_step=100, number_of_strikes=5):
    atm = nearest_strike(spot, strike_step)
    return [atm + (i * strike_step) for i in range(number_of_strikes)]


def get_pe_strikes(spot, strike_step=100, number_of_strikes=5):
    atm = nearest_strike(spot, strike_step)
    return [atm - ((i + 1) * strike_step) for i in range(number_of_strikes)]


def run_day_simulation(date_str, day_data):
    """
    Executes the Breakout Strategy on 1-minute historical data for a single day.
    Returns: (trades_list, minute_csv_rows, total_breakouts)
    """
    spot_candles = day_data["spot"]
    option_candles = day_data["options"]

    def get_spot_p(time_str):
        return spot_candles.get(time_str)

    def get_option_p(strike, opt_type, time_str):
        key = f"{int(strike)}_{opt_type}"
        return option_candles.get(key, {}).get(time_str)

    dt_obj = datetime.strptime(date_str, "%Y-%m-%d")
    current_dt = dt_obj.replace(hour=9, minute=15)
    end_dt = dt_obj.replace(hour=15, minute=0)

    minute_list = []
    while current_dt <= end_dt:
        minute_list.append(current_dt)
        current_dt += timedelta(minutes=1)

    session_highs = {}
    processed_minute_count = 0
    consecutive_direction = None
    consecutive_count = 0
    last_breakout_candidate = None
    last_spot_refresh_minute = None
    spot = None
    atm = None
    ce_strikes = []
    pe_strikes = []

    active_position = None
    trades = []
    daily_pnl = 0.0
    total_breakouts_detected = 0

    csv_rows = []

    for dt in minute_list:
        time_str = dt.strftime("%Y-%m-%dT%H:%M")
        display_time = dt.strftime("%H:%M:%S")

        # 1. Update active trade position if open
        if active_position is not None:
            buy_p = get_option_p(active_position["buy_strike"], active_position["buy_opt_type"], time_str)
            sell_p = get_option_p(active_position["sell_strike"], active_position["sell_opt_type"], time_str)

            if buy_p is not None:
                active_position["last_buy_price"] = buy_p
            else:
                buy_p = active_position["last_buy_price"]

            if sell_p is not None:
                active_position["last_sell_price"] = sell_p
            else:
                sell_p = active_position["last_sell_price"]

            buy_pnl = (buy_p - active_position["buy_entry"]) * TOTAL_QTY
            sell_pnl = (active_position["sell_entry"] - sell_p) * TOTAL_QTY
            total_pnl = buy_pnl + sell_pnl

            # Trailing Profit Floor
            if total_pnl > active_position["max_pnl"]:
                active_position["max_pnl"] = total_pnl
                if active_position["max_pnl"] >= PROFIT_THRESHOLD:
                    new_min = ((active_position["max_pnl"] - PROFIT_THRESHOLD) // MIN_PROFIT_STEP) * MIN_PROFIT_STEP + PROFIT_THRESHOLD
                    if new_min > active_position["min_profit"]:
                        active_position["min_profit"] = new_min

            # Check Exit Conditions
            exit_triggered = False
            exit_reason = ""

            if total_pnl <= MAX_LOSS:
                exit_triggered = True
                exit_reason = f"EXIT_MAX_LOSS (Rs.{total_pnl:,.2f} <= Rs.{MAX_LOSS:,.2f})"
            elif active_position["max_pnl"] >= PROFIT_THRESHOLD and total_pnl < active_position["min_profit"]:
                exit_triggered = True
                exit_reason = f"EXIT_TRAILING_PROFIT (Rs.{total_pnl:,.2f} < locked Rs.{active_position['min_profit']:,.2f})"
            elif total_pnl >= MAX_PROFIT:
                exit_triggered = True
                exit_reason = f"EXIT_MAX_PROFIT (+Rs.{MAX_PROFIT:,.2f})"
            elif dt == end_dt:
                exit_triggered = True
                exit_reason = "EXIT_MARKET_CLOSE (15:00)"

            if exit_triggered:
                active_position["exit_time"] = display_time
                active_position["end_time"] = display_time
                active_position["buy_exit"] = buy_p
                active_position["sell_exit"] = sell_p
                active_position["pnl"] = total_pnl
                active_position["exit_reason"] = exit_reason
                trades.append(active_position)
                daily_pnl += total_pnl
                active_position = None

        # 2. Minute processing for breakout scanner
        processed_minute_count += 1
        should_check_breakout = (processed_minute_count % ALERT_INTERVAL_MINUTES == 0)

        current_30min_bucket = dt.strftime("%Y-%m-%d %H:") + ("00" if dt.minute < 30 else "30")
        need_spot_refresh = (spot is None or last_spot_refresh_minute != current_30min_bucket)

        if need_spot_refresh:
            new_spot = get_spot_p(time_str)
            if new_spot is not None:
                if spot is not None and new_spot != spot:
                    session_highs = {}
                    consecutive_direction = None
                    consecutive_count = 0
                    last_breakout_candidate = None
                spot = new_spot
                atm = nearest_strike(spot, 100)
                ce_strikes = get_ce_strikes(spot, 100)
                pe_strikes = get_pe_strikes(spot, 100)
                last_spot_refresh_minute = current_30min_bucket
        else:
            curr_spot = get_spot_p(time_str)
            if curr_spot:
                spot = curr_spot

        breakouts_this_minute = []

        ce_prices = [get_option_p(st, "CE", time_str) for st in ce_strikes]
        pe_prices = [get_option_p(st, "PE", time_str) for st in pe_strikes]

        for strike in ce_strikes:
            price = get_option_p(strike, "CE", time_str)
            if price is not None:
                key = (strike, "CE")
                prev_high = session_highs.get(key)
                if prev_high is None:
                    session_highs[key] = price
                else:
                    gain = price - prev_high
                    if should_check_breakout and gain >= MIN_BREAKOUT_AMOUNT:
                        breakouts_this_minute.append({
                            "strike": strike, "option_type": "CE", "price": price, "previous_high": prev_high
                        })
                        session_highs[key] = price
                    elif price > prev_high:
                        session_highs[key] = price

        for strike in pe_strikes:
            price = get_option_p(strike, "PE", time_str)
            if price is not None:
                key = (strike, "PE")
                prev_high = session_highs.get(key)
                if prev_high is None:
                    session_highs[key] = price
                else:
                    gain = price - prev_high
                    if should_check_breakout and gain >= MIN_BREAKOUT_AMOUNT:
                        breakouts_this_minute.append({
                            "strike": strike, "option_type": "PE", "price": price, "previous_high": prev_high
                        })
                        session_highs[key] = price
                    elif price > prev_high:
                        session_highs[key] = price

        alerted_breakout = None
        is_breakout_flag = False
        highest_premium = ""
        highest_strike = ""
        highest_opt_type = ""
        prev_high_str = ""

        if should_check_breakout:
            if not breakouts_this_minute:
                consecutive_direction = None
                consecutive_count = 0
                last_breakout_candidate = None
            else:
                is_breakout_flag = True
                total_breakouts_detected += 1
                ce_b = [b for b in breakouts_this_minute if b["option_type"] == "CE"]
                pe_b = [b for b in breakouts_this_minute if b["option_type"] == "PE"]
                if ce_b and not pe_b:
                    check_dir = "CE"
                    sel = min(ce_b, key=lambda x: x["price"])
                elif pe_b and not ce_b:
                    check_dir = "PE"
                    sel = min(pe_b, key=lambda x: x["price"])
                else:
                    sel = min(breakouts_this_minute, key=lambda x: x["price"])
                    check_dir = sel["option_type"]

                highest_premium = f"{sel['price']:.2f}"
                highest_strike = str(sel['strike'])
                highest_opt_type = sel['option_type']
                prev_high_str = f"{sel['previous_high']:.2f}"

                if consecutive_count == 0 or last_breakout_candidate is None or check_dir != consecutive_direction:
                    consecutive_direction = check_dir
                    consecutive_count = 1
                    last_breakout_candidate = {
                        "strike": sel["strike"],
                        "option_type": sel["option_type"],
                        "price": sel["price"],
                        "previous_high": sel["previous_high"],
                        "spot": spot,
                    }
                else:
                    cand_strike = last_breakout_candidate["strike"]
                    cand_opt = last_breakout_candidate["option_type"]
                    cand_spot = last_breakout_candidate["spot"]
                    cand_prev_high = last_breakout_candidate["previous_high"]

                    underlying_confirmed = (spot > cand_spot) if cand_opt == "CE" else (spot < cand_spot)
                    curr_opt_p = get_option_p(cand_strike, cand_opt, time_str)
                    if curr_opt_p is None:
                        curr_opt_p = sel["price"]
                    premium_retained = (curr_opt_p >= cand_prev_high)

                    if underlying_confirmed and premium_retained:
                        consecutive_count += 1
                        if consecutive_count >= REQUIRED_CONSECUTIVE_CHECKS:
                            alerted_breakout = sel
                    else:
                        consecutive_direction = None
                        consecutive_count = 0
                        last_breakout_candidate = None

        # Build CSV Row
        ce_pairs = []
        for i in range(5):
            st = ce_strikes[i] if i < len(ce_strikes) else ""
            p = f"{ce_prices[i]:.2f}" if i < len(ce_prices) and ce_prices[i] is not None else ""
            ce_pairs.extend([st, p])

        pe_pairs = []
        for i in range(5):
            st = pe_strikes[i] if i < len(pe_strikes) else ""
            p = f"{pe_prices[i]:.2f}" if i < len(pe_prices) and pe_prices[i] is not None else ""
            pe_pairs.extend([st, p])

        csv_row = [
            date_str, display_time, f"{spot:.2f}" if spot else "", atm or "",
            *ce_pairs,
            *pe_pairs,
            highest_premium, highest_strike, highest_opt_type, prev_high_str,
            str(is_breakout_flag),
        ]
        csv_rows.append(csv_row)

        # 3. Enter new position if signal validated and no active position
        if alerted_breakout and active_position is None:
            if len(trades) < MAX_DAILY_TRADES and daily_pnl > DAILY_MAX_LOSS:
                b_strike = int(alerted_breakout["strike"])
                b_opt = alerted_breakout["option_type"]
                s_strike = b_strike + SPREAD_WIDTH if b_opt == "CE" else b_strike - SPREAD_WIDTH
                s_opt = b_opt

                b_entry = get_option_p(b_strike, b_opt, time_str)
                s_entry = get_option_p(s_strike, s_opt, time_str)

                if b_entry and s_entry:
                    active_position = {
                        "date": date_str,
                        "entry_time": display_time,
                        "start_time": display_time,
                        "buy_strike": b_strike,
                        "buy_opt_type": b_opt,
                        "buy_entry": b_entry,
                        "sell_strike": s_strike,
                        "sell_opt_type": s_opt,
                        "sell_entry": s_entry,
                        "last_buy_price": b_entry,
                        "last_sell_price": s_entry,
                        "max_pnl": 0.0,
                        "min_profit": PROFIT_THRESHOLD,
                        "lots": DEFAULT_LOTS,
                        "quantity": TOTAL_QTY,
                    }

    return trades, csv_rows, total_breakouts_detected


def main():
    print(f"\n{'='*95}")
    print(f"  SENSEX {NUM_DAYS}-DAY HISTORICAL BACKTEST (ANGEL ONE SMARTAPI)")
    print(f"  Strategy: 3-Min Breakout Scanner + 2-Step Validation + Broken Spread Execution")
    print(f"{'='*95}\n")

    print("[1/4] Authenticating with Angel One SmartAPI...")
    smart_api = get_smartapi_session()
    print("      ✓ Connected successfully.")

    print("[2/4] Determining trading dates...")
    if RUN_TODAY_ONLY:
        trading_days = [datetime(2026, 8, 28).strftime("%Y-%m-%d")]
            # [datetime(2026, 7, 8).strftime("%Y-%m-%d")] # [datetime.now().strftime("%Y-%m-%d")]
        print(f"      ✓ Running backtest for today only: {trading_days[0]}")
    else:
        trading_days = get_last_n_trading_days(smart_api, n_days=NUM_DAYS)
        print(f"      ✓ Selected {len(trading_days)} trading days:")
        print(f"        {trading_days[0]}  --->  {trading_days[-1]}")

    print("[3/4] Downloading instrument master...")
    url = "https://margincalculator.angelbroking.com/OpenAPI_File/files/OpenAPIScripMaster.json"
    resp = requests.get(url, timeout=30, verify=False)
    master_instruments = resp.json()
    print(f"      ✓ Loaded instrument master ({len(master_instruments)} total records).")

    # Separate cache for SENSEX
    cache_dir = ROOT / "backtest" / "cache_sensex"
    cache_dir.mkdir(parents=True, exist_ok=True)

    print(f"\n[4/4] Executing multi-day simulation...\n")

    daily_results = []
    all_trades = []

    for idx, day_str in enumerate(trading_days, 1):
        dd_mm = datetime.strptime(day_str, "%Y-%m-%d").strftime("%d-%m-%Y")
        option_map, day_expiry = load_instrument_map(master_instruments, for_date_str=day_str)
        print(f"[{idx:02d}/{len(trading_days)}] Processing {day_str} ({dd_mm}) [Expiry: {day_expiry}] ({len(option_map)} options loaded)...", end=" ", flush=True)

        day_data = fetch_day_candles(smart_api, day_str, option_map, cache_dir)
        if not day_data:
            print("❌ No data.")
            continue

        trades, csv_rows, breakouts = run_day_simulation(day_str, day_data)
        save_day_results(day_str, trades, csv_rows, day_expiry, INSTRUMENT, CSV_PREFIX, DATA_ROOT)

        day_pnl = sum(t["pnl"] for t in trades)
        wins = sum(1 for t in trades if t["pnl"] > 0)
        losses = sum(1 for t in trades if t["pnl"] <= 0)

        all_trades.extend(trades)
        daily_results.append({
            "date": day_str,
            "dd_mm": dd_mm,
            "breakouts": breakouts,
            "trades": len(trades),
            "wins": wins,
            "losses": losses,
            "pnl": day_pnl,
        })

        pnl_str = f"+Rs.{day_pnl:,.2f}" if day_pnl >= 0 else f"-Rs.{abs(day_pnl):,.2f}"
        status = "🟢" if day_pnl > 0 else ("🔴" if day_pnl < 0 else "⚪")
        print(f"{status} Trades: {len(trades)} (W:{wins}/L:{losses}) | Breakouts: {breakouts:2d} | Daily P&L: {pnl_str}")

    # =========================================================================
    # PRINT SUMMARY TABLE
    # =========================================================================
    create_summary_tables(daily_results, INSTRUMENT, NUM_DAYS)

    # =========================================================================
    # AGGREGATE METRICS
    # =========================================================================
    create_overall_summary(daily_results, all_trades, NUM_DAYS, INSTRUMENT)


if __name__ == "__main__":
    main()
