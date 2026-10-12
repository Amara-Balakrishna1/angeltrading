import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Auto-detect and include .venv site-packages if running with global Python
VENV_SITE_PACKAGES = ROOT / ".venv" / "Lib" / "site-packages"
if VENV_SITE_PACKAGES.exists() and str(VENV_SITE_PACKAGES) not in sys.path:
    sys.path.insert(0, str(VENV_SITE_PACKAGES))

import csv
import json
import math
import time
import requests
from datetime import datetime, time as dt_time
from zoneinfo import ZoneInfo

import pyotp
from logzero import logger
from SmartApi.smartConnect import SmartConnect
import config
from live.breakoutstrategy.broken_call import (
    execute_spread,
    get_daily_trade_stats,
    DAILY_MAX_LOSS,
    MAX_DAILY_TRADES
)

# ============================================================
# API LOGIN
# ============================================================

api_key = config.api_key.strip()
username = config.username
pwd = config.pwd
token = config.token
disable_ssl = getattr(config, "disable_ssl", True)

smartApi = SmartConnect(api_key, disable_ssl=disable_ssl)

try:
    totp = pyotp.TOTP(token).now()
except Exception as e:
    logger.error("Invalid Token: The provided token is not valid.")
    raise e

data = smartApi.generateSession(username, pwd, totp)


# ============================================================
# CONFIGURATION
# ============================================================

IST = ZoneInfo("Asia/Kolkata")

DATA_DIR = ROOT / "data"
DATA_DIR.mkdir(parents=True, exist_ok=True)

MARKET_START = dt_time(9, 15)
MARKET_END = dt_time(15, 0)

# Supported Index Configurations
INDEX_CONFIGS = {
    "SENSEX": {
        "name": "SENSEX",
        "spot_exchange": "BSE",
        "spot_symbol": "SENSEX",
        "spot_token": "99919000",
        "option_exchange": "BFO",
        "strike_step": 100,
        "csv_prefix": "SENSEX",
    },
    "BANKNIFTY": {
        "name": "BANKNIFTY",
        "spot_exchange": "NSE",
        "spot_symbol": "Nifty Bank",
        "spot_token": "99926009",
        "option_exchange": "NFO",
        "strike_step": 100,
        "csv_prefix": "BN",
    }
}

# Number of strikes to monitor on each side
# CE: ATM, ATM+100, ATM+200, ATM+300, ATM+400
# PE: ATM-100, ATM-200, ATM-300, ATM-400, ATM-500
NUMBER_OF_STRIKES = 5

# Alert only when premium crosses previous high
ALERT_ON_BREAKOUT = True

# Compare prices and alert every N minutes (CSV still records every minute)
ALERT_INTERVAL_MINUTES = 3
# Number of consecutive checks in the same direction (CE/PE) required to trigger an alert
REQUIRED_CONSECUTIVE_CHECKS = 2

# Minimum price increase (in Rs) over the previous session high to qualify as a breakout
MIN_BREAKOUT_AMOUNT = 3.0

# Default number of lots to trade
DEFAULT_LOTS = 5

# Daily Risk Management Limits
# DAILY_MAX_LOSS = -10000.0  (Maximum allowable daily loss before halting trades)
# MAX_DAILY_TRADES = 4       (Maximum allowable executed trades per day)


# ============================================================
# INDEX SELECTION
# ============================================================

def select_index():
    """
    Prompts the user to select SENSEX or BANKNIFTY. Defaults to SENSEX if no input is given.
    """
    print("\n" + "=" * 50)
    print("Select Index to Monitor / Trade:")
    print("1. SENSEX (Default)")
    print("2. BANKNIFTY")
    print("=" * 50)
    try:
        choice = input("Enter choice [1/2 or SENSEX/BANKNIFTY] (default: SENSEX): ").strip().upper()
    except (EOFError, KeyboardInterrupt):
        choice = "1"

    if choice in ("2", "BANKNIFTY", "BN"):
        selected = "BANKNIFTY"
    else:
        selected = "SENSEX"

    print(f"[+] Selected Index: {selected}\n")
    return selected


# ============================================================
# DOWNLOAD ANGEL ONE INSTRUMENT MASTER
# ============================================================

def load_instrument_master():
    url = (
        "https://margincalculator.angelone.in/"
        "OpenAPI_File/files/OpenAPIScripMaster.json"
    )

    print("Downloading instrument master...")

    response = requests.get(url, timeout=30, verify=not disable_ssl)
    response.raise_for_status()

    instruments = response.json()

    print(f"Loaded {len(instruments)} instruments")

    return instruments


# ============================================================
# FIND NEAREST INDEX EXPIRY OPTIONS
# ============================================================

def get_index_options(instruments, index_name, option_exchange, for_date=None):
    today = for_date if for_date is not None else datetime.now(IST).date()
    options = []

    for item in instruments:
        if item.get("exch_seg") != option_exchange:
            continue

        if item.get("name") != index_name:
            continue

        if item.get("instrumenttype") != "OPTIDX":
            continue

        expiry_string = item.get("expiry")
        if not expiry_string:
            continue

        try:
            expiry = datetime.strptime(expiry_string, "%d%b%Y").date()
        except ValueError:
            continue

        if expiry < today:
            continue

        option_type = item.get("symbol", "")[-2:]
        if option_type not in ("CE", "PE"):
            continue

        try:
            strike = float(item["strike"]) / 100
        except Exception:
            continue

        try:
            lotsize = int(item.get("lotsize", 0))
        except (ValueError, TypeError):
            lotsize = 0

        options.append({
            "token": str(item["token"]),
            "symbol": item["symbol"],
            "expiry": expiry,
            "strike": strike,
            "option_type": option_type,
            "lotsize": lotsize
        })

    if not options:
        raise RuntimeError(
            f"No {index_name} option contracts found on {option_exchange} in instrument master for {today}."
        )

    nearest_expiry = min(x["expiry"] for x in options)

    expiry_options = [x for x in options if x["expiry"] == nearest_expiry]

    print(
        f"{index_name} dynamic expiry selected for {today.strftime('%d-%b-%Y')}: "
        f"{nearest_expiry.strftime('%d-%b-%Y')} ({len(expiry_options)} contracts)"
    )

    return expiry_options, nearest_expiry


# ============================================================
# ROUND TO NEAREST STRIKE
# ============================================================

def nearest_strike(spot, strike_step=100):
    return int(
        round(spot / strike_step)
        * strike_step
    )


# ============================================================
# GET CE / PE STRIKE LISTS
# ============================================================

def get_ce_strikes(spot, strike_step=100, number_of_strikes=NUMBER_OF_STRIKES):
    atm = nearest_strike(spot, strike_step)
    return [
        atm + (i * strike_step)
        for i in range(number_of_strikes)
    ]


def get_pe_strikes(spot, strike_step=100, number_of_strikes=NUMBER_OF_STRIKES):
    atm = nearest_strike(spot, strike_step)
    return [
        atm - ((i + 1) * strike_step)
        for i in range(number_of_strikes)
    ]


# ============================================================
# FIND OPTION CONTRACTS
# ============================================================

def build_option_map(options):
    option_map = {}
    for option in options:
        key = (
            float(option["strike"]),
            option["option_type"]
        )
        option_map[key] = option
    return option_map


def get_option_price(strike, option_type, option_map, exchange):
    contract = option_map.get(
        (float(strike), option_type)
    )
    if not contract:
        return None

    return get_ltp(
        contract["token"],
        contract["symbol"],
        exchange
    )


# ============================================================
# GET LTP
# ============================================================

def get_ltp(token, symbol, exchange):
    try:
        response = smartApi.ltpData(
            exchange,
            symbol,
            str(token)
        )

        if not response:
            return None

        if response.get("status") is not True:
            print(f"LTP error for {symbol}: {response}")
            return None

        data = response.get("data")
        if not data:
            return None
        return float(data["ltp"])

    except Exception as e:
        print(f"Error getting LTP for {symbol}: {e}")
        return None


# ============================================================
# GET SPOT PRICE
# ============================================================

def get_spot_price(spot_exchange, spot_symbol, spot_token):
    try:
        response = smartApi.ltpData(
            spot_exchange,
            spot_symbol,
            str(spot_token)
        )

        if response.get("status") is not True:
            print(f"Spot LTP error for {spot_symbol}: {response}")
            return None

        return float(response["data"]["ltp"])

    except Exception as e:
        print(f"Spot error for {spot_symbol}: {e}")
        return None


# ============================================================
# CSV HELPERS
# ============================================================

CSV_HEADER = [
    "date",
    "time",
    "spot_price",
    "atm",

    "ce_strike_1",
    "ce_1",

    "ce_strike_2",
    "ce_2",

    "ce_strike_3",
    "ce_3",

    "ce_strike_4",
    "ce_4",

    "ce_strike_5",
    "ce_5",

    "pe_strike_1",
    "pe_1",

    "pe_strike_2",
    "pe_2",

    "pe_strike_3",
    "pe_3",

    "pe_strike_4",
    "pe_4",

    "pe_strike_5",
    "pe_5",

    "highest_premium",
    "highest_strike",
    "highest_option_type",

    "previous_high",
    "breakout"
]


def get_csv_filename(csv_prefix, for_date):
    date_folder = DATA_DIR / for_date.strftime("%d-%m-%Y")
    date_folder.mkdir(parents=True, exist_ok=True)
    return date_folder / f"{csv_prefix}-{for_date.strftime('%d-%m-%Y')}.csv"


def ensure_csv_file(csv_prefix, for_date, index_name="INDEX"):
    csv_file = get_csv_filename(csv_prefix, for_date)

    if not csv_file.exists():
        header = list(CSV_HEADER)
        header[2] = f"{index_name.lower()}_spot"
        with open(csv_file, "w", newline="") as file:
            writer = csv.writer(file)
            writer.writerow(header)
        print(f"Created CSV file: {csv_file}")
    else:
        print(f"Appending to CSV file: {csv_file}")

    return csv_file


def initialize_csv(csv_prefix, for_date, index_name="INDEX"):
    return ensure_csv_file(csv_prefix, for_date, index_name)


# ============================================================
# WRITE CSV
# ============================================================

def write_csv(
    csv_file,
    now,
    spot,
    atm,
    ce_data,
    pe_data,
    highest_premium,
    highest_strike,
    highest_option_type,
    previous_high,
    breakout
):
    row = [
        now.strftime("%Y-%m-%d"),
        now.strftime("%H:%M:%S"),
        spot,
        atm
    ]

    for item in ce_data:
        row.extend([
            item["strike"],
            item["price"]
        ])

    for item in pe_data:
        row.extend([
            item["strike"],
            item["price"]
        ])

    row.extend([
        highest_premium,
        highest_strike,
        highest_option_type,
        previous_high,
        breakout
    ])

    with open(csv_file, "a", newline="") as file:
        writer = csv.writer(file)
        writer.writerow(row)


def check_strike_breakout(
    strike,
    option_type,
    price,
    session_highs,
    breakouts_this_minute,
    check_breakout=False
):
    if price is None:
        return

    key = (strike, option_type)
    previous_high = session_highs.get(key)

    if previous_high is None:
        session_highs[key] = price
        return

    if not check_breakout:
        return

    gain = price - previous_high
    if price > 300:
        MIN_BREAKOUT_AMOUNT = round(price * 0.02)
    else:
        MIN_BREAKOUT_AMOUNT = 3

    if gain >= MIN_BREAKOUT_AMOUNT:
        breakouts_this_minute.append({
            "strike": strike,
            "option_type": option_type,
            "price": price,
            "previous_high": previous_high,
        })
        session_highs[key] = price
    elif price > previous_high:
        # Price is higher but below MIN_BREAKOUT_AMOUNT threshold — update high silently
        session_highs[key] = price


def pick_lowest_premium_breakout(breakouts):
    if not breakouts:
        return None
    return min(breakouts, key=lambda item: item["price"])


def process_consecutive_breakout(
    breakouts,
    consecutive_direction,
    consecutive_count,
    last_breakout_candidate,
    current_spot,
    option_map,
    option_exchange,
    index_name="INDEX",
    required_consecutive=REQUIRED_CONSECUTIVE_CHECKS
):
    """
    Evaluates breakouts for the current check and updates consecutive check tracking.
    
    CONFIRMATION RULES:
    1. Check 1 (Initial Breakout): Detects surge >= MIN_BREAKOUT_AMOUNT. Records candidate strike and spot.
    2. Check 2 (Confirmation Check):
       - Underlying spot MUST move in the same direction (Spot UP for CE, Spot DOWN for PE).
       - Option premium of candidate MUST remain at or above the breakout level.
    3. If both pass -> Confirm breakout!
    4. If either fails -> Reject breakout and reset consecutive streak to 0.
    """
    if not breakouts:
        if consecutive_count > 0:
            print(
                f"\n[INFO] No breakout this check. "
                f"Resetting consecutive {consecutive_direction} streak from {consecutive_count} to 0."
            )
        return None, None, 0, None

    ce_breakouts = [b for b in breakouts if b["option_type"] == "CE"]
    pe_breakouts = [b for b in breakouts if b["option_type"] == "PE"]

    # Determine direction of breakout in this check
    if ce_breakouts and not pe_breakouts:
        check_direction = "CE"
        selected = pick_lowest_premium_breakout(ce_breakouts)
    elif pe_breakouts and not ce_breakouts:
        check_direction = "PE"
        selected = pick_lowest_premium_breakout(pe_breakouts)
    else:
        # Both CE and PE broke out in the same check -> pick lowest premium breakout overall
        selected = pick_lowest_premium_breakout(breakouts)
        check_direction = selected["option_type"]

    if consecutive_count == 0 or last_breakout_candidate is None or check_direction != consecutive_direction:
        # First check in this direction (Check 1)
        # Evaluate immediate alert criteria: price > 150 and premium gain > 3%
        price = selected["price"]
        previous_high = selected["previous_high"]
        premium_gain_percent = ((price - previous_high) / previous_high) * 100 if previous_high != 0 else 0
        if price > 150 and premium_gain_percent > 5:
            # Immediate breakout alert, ignore consecutive checks
            if ALERT_ON_BREAKOUT:
                alert(
                    selected["option_type"],
                    selected["strike"],
                    selected["price"],
                    selected["previous_high"],
                    index_name=index_name,
                    consecutive_count=1,
                )
            print(
                f"\n[ALERT] Immediate breakout: {selected['strike']} {selected['option_type']} @ Rs.{price:.2f} "
                f"(Prev High: {previous_high:.2f}, Gain: {premium_gain_percent:.2f}%)."
            )
            # Return as alerted breakout
            return selected, check_direction, 1, None
        # Otherwise, set up for confirmation on next check
        consecutive_direction = check_direction
        consecutive_count = 1
        last_breakout_candidate = {
            "strike": selected["strike"],
            "option_type": selected["option_type"],
            "price": selected["price"],
            "previous_high": selected["previous_high"],
            "spot": current_spot,
        }
        print(
            f"\n[INFO] {check_direction} breakout candidate detected at Check 1/{required_consecutive}: "
            f"{selected['strike']} {selected['option_type']} @ Rs.{selected['price']:.2f} "
            f"(Spot: {current_spot:.2f}). Waiting for next check to confirm underlying direction & premium retention."
        )
        return None, consecutive_direction, consecutive_count, last_breakout_candidate

    else:
        # Confirmation check (e.g. Check 2) -> Validate underlying direction & option premium retention
        cand_strike = last_breakout_candidate["strike"]
        cand_opt_type = last_breakout_candidate["option_type"]
        cand_spot = last_breakout_candidate["spot"]
        cand_breakout_level = last_breakout_candidate["previous_high"]

        # 1. Check underlying direction
        spot_change = current_spot - cand_spot
        if cand_opt_type == "CE":
            underlying_confirmed = (current_spot > cand_spot)
            dir_expected = "UP (+)"
        else:
            underlying_confirmed = (current_spot < cand_spot)
            dir_expected = "DOWN (-)"

        # 2. Check candidate option current premium
        current_opt_price = get_option_price(cand_strike, cand_opt_type, option_map, option_exchange)
        if current_opt_price is None:
            current_opt_price = selected["price"]

        premium_retained = (current_opt_price is not None and current_opt_price >= cand_breakout_level)

        if not underlying_confirmed:
            print(
                f"\n[FILTERED] ❌ Breakout confirmation REJECTED: Underlying spot did not move in {cand_opt_type} direction! "
                f"(Expected spot to move {dir_expected}, but spot moved from {cand_spot:.2f} -> {current_spot:.2f} [{spot_change:+.2f}]). "
                f"Resetting {cand_opt_type} streak to 0."
            )
            return None, None, 0, None

        if not premium_retained:
            print(
                f"\n[FILTERED] ❌ Breakout confirmation REJECTED: Option premium dropped below breakout level! "
                f"({cand_strike} {cand_opt_type} breakout level was Rs.{cand_breakout_level:.2f}, but current price is Rs.{current_opt_price:.2f}). "
                f"Resetting {cand_opt_type} streak to 0."
            )
            return None, None, 0, None

        # Both conditions validated!
        consecutive_count += 1
        print(
            f"\n[CONFIRMED] ✅ {cand_opt_type} Breakout VALIDATED! "
            f"Underlying spot confirmed (Spot: {cand_spot:.2f} -> {current_spot:.2f} [{spot_change:+.2f}]) & "
            f"Option premium sustained ({cand_strike} {cand_opt_type}: Rs.{current_opt_price:.2f} >= Rs.{cand_breakout_level:.2f})."
        )

        if consecutive_count >= required_consecutive:
            # This path is now only reached for traditional consecutive alerts (if any remain)
            if ALERT_ON_BREAKOUT:
                alert(
                    selected["option_type"],
                    selected["strike"],
                    selected["price"],
                    selected["previous_high"],
                    index_name=index_name,
                    consecutive_count=consecutive_count,
                )
            return selected, consecutive_direction, consecutive_count, last_breakout_candidate
        else:
            # Not enough consecutive checks yet; continue tracking
            return None, consecutive_direction, consecutive_count, last_breakout_candidate


# ============================================================
# ALERT
# ============================================================

def alert(
    option_type,
    strike,
    current_price,
    previous_high,
    index_name="INDEX",
    consecutive_count=2
):
    message = (
        "\n"
        "================================================\n"
        f"🚨 {index_name} OPTION BREAKOUT ({consecutive_count} CONSECUTIVE {option_type} CHECKS)\n"
        "================================================\n"
        f"Index              : {index_name}\n"
        f"Option             : {option_type}\n"
        f"Strike             : {strike}\n"
        f"Previous High      : {previous_high:.2f}\n"
        f"Current Price      : {current_price:.2f}\n"
        f"Breakout           : +{current_price - previous_high:.2f}\n"
        f"Consecutive Checks : {consecutive_count} in same direction ({option_type})\n"
        "================================================\n"
    )
    print(message)


# ============================================================
# MAIN MONITOR
# ============================================================

def monitor_breakout(index_choice="SENSEX"):
    if index_choice not in INDEX_CONFIGS:
        index_choice = "SENSEX"

    index_cfg = INDEX_CONFIGS[index_choice]
    index_name = index_cfg["name"]
    spot_exchange = index_cfg["spot_exchange"]
    spot_symbol = index_cfg["spot_symbol"]
    spot_token = index_cfg["spot_token"]
    option_exchange = index_cfg["option_exchange"]
    strike_step = index_cfg["strike_step"]
    csv_prefix = index_cfg["csv_prefix"]

    print(f"\nStarting {index_name} monitor ({spot_exchange} spot, {option_exchange} options)...")

    # Load instrument master ONCE
    instruments = load_instrument_master()

    # Find current nearest expiry options
    options, nearest_expiry = get_index_options(
        instruments,
        index_name,
        option_exchange
    )

    # Derive lot size from API data - must be present in at least some contracts
    if not options:
        raise RuntimeError(f"No options data found for {index_name}")
    
    # Try to get lot size from first option, fall back to common lot size
    lot_size = None
    for opt in options:
        if opt.get("lotsize"):
            lot_size = opt["lotsize"]
            break
    
    if not lot_size:
        # Try to find common lot size across all options
        lot_sizes = [opt.get("lotsize") for opt in options if opt.get("lotsize")]
        if lot_sizes:
            # Use most common lot size
            from collections import Counter
            lot_size = Counter(lot_sizes).most_common(1)[0][0]
        else:
            raise RuntimeError(f"Lot size not found in any API data for {index_name}")
    
    print(f"[+] Lot size: {lot_size}")

    option_map = build_option_map(options)

    # Per-strike session highs: (strike, CE/PE) -> highest premium seen
    session_highs = {}
    processed_minute_count = 0

    # Consecutive breakout tracking
    consecutive_direction = None  # "CE", "PE", or None
    consecutive_count = 0
    last_breakout_candidate = None

    # Previous session high is reset each day
    current_session_date = None
    current_csv_file = None

    last_processed_minute = None

    # Spot refresh control: recalculate spot+strikes every 30 minutes only
    SPOT_REFRESH_INTERVAL_MINUTES = 50
    last_spot_refresh_minute = None   # tracks the HH:MM0 of last refresh
    spot = None
    atm = None
    ce_strikes = []
    pe_strikes = []

    while True:
        now = datetime.now(IST)

        current_date = now.date()
        current_time = now.time()

        # ====================================================
        # OUTSIDE MARKET HOURS
        # ====================================================
        if not (
            MARKET_START
            <= current_time
            <= MARKET_END
        ):
            # Reset when a new day starts
            if current_session_date != current_date:
                session_highs = {}
                processed_minute_count = 0
                consecutive_direction = None
                consecutive_count = 0
                last_breakout_candidate = None
                last_processed_minute = None
                current_csv_file = None
                current_session_date = current_date
                try:
                    options, nearest_expiry = get_index_options(instruments, index_name, option_exchange, for_date=current_date)
                    if not options:
                        raise RuntimeError(f"No options data found for {index_name}")
                    
                    # Try to get lot size from first option, fall back to common lot size
                    lot_size = None
                    for opt in options:
                        if opt.get("lotsize"):
                            lot_size = opt["lotsize"]
                            break
                    
                    if not lot_size:
                        # Try to find common lot size across all options
                        lot_sizes = [opt.get("lotsize") for opt in options if opt.get("lotsize")]
                        if lot_sizes:
                            from collections import Counter
                            lot_size = Counter(lot_sizes).most_common(1)[0][0]
                        else:
                            raise RuntimeError(f"Lot size not found in any API data for {index_name}")
                    
                    option_map = build_option_map(options)
                except Exception as e:
                    print(f"[!] Warning updating expiry for {current_date}: {e}")

            print(
                f"[{now.strftime('%H:%M:%S')}] "
                f"Market closed. Waiting..."
            )
            time.sleep(10)
            continue

        # ====================================================
        # ONLY PROCESS ONCE PER MINUTE
        # ====================================================
        # Check day transition during market hours as well
        if current_session_date != current_date:
            session_highs = {}
            processed_minute_count = 0
            consecutive_direction = None
            consecutive_count = 0
            last_breakout_candidate = None
            last_processed_minute = None
            current_csv_file = None
            current_session_date = current_date
            try:
                options, nearest_expiry = get_index_options(instruments, index_name, option_exchange, for_date=current_date)
                option_map = build_option_map(options)
                lot_size = options[0]["lotsize"] if options and options[0].get("lotsize") else index_cfg["default_lot_size"]
            except Exception as e:
                print(f"[!] Warning updating expiry for {current_date}: {e}")

        minute_key = now.strftime("%Y-%m-%d %H:%M")

        if minute_key == last_processed_minute:
            time.sleep(1)
            continue

        last_processed_minute = minute_key
        processed_minute_count += 1

        should_check_breakout = (
            processed_minute_count
            % ALERT_INTERVAL_MINUTES
            == 0
        )

        # ====================================================
        # ENSURE TODAY'S CSV FILE (create or append)
        # ====================================================
        csv_file = get_csv_filename(csv_prefix, current_date)

        if csv_file != current_csv_file:
            current_csv_file = initialize_csv(csv_prefix, current_date, index_name)

        # ====================================================
        # GET SPOT PRICE (refresh every 30 minutes only)
        # ====================================================
        # Determine the current 30-min bucket (e.g. 09:00, 09:30, 10:00 ...)
        current_30min_bucket = now.strftime("%Y-%m-%d %H:") + (
            "00" if now.minute < 30 else "30"
        )

        need_spot_refresh = (
            spot is None
            or last_spot_refresh_minute != current_30min_bucket
        )

        if need_spot_refresh:
            new_spot = get_spot_price(spot_exchange, spot_symbol, spot_token)
            if new_spot is None:
                if spot is None:
                    print(f"Could not get {index_name} spot price. Retrying...")
                    time.sleep(2)
                    continue
            else:
                spot_changed = (spot is not None and new_spot != spot)
                if spot_changed:
                    print(
                        f"\n[SPOT CHANGED] Underlying spot moved: {spot:.2f} -> {new_spot:.2f}. "
                        f"Resetting previous session highs & tracking to evaluate fresh breakout baseline."
                    )
                    session_highs = {}
                    consecutive_direction = None
                    consecutive_count = 0
                    last_breakout_candidate = None

                spot = new_spot
                atm = nearest_strike(spot, strike_step)
                ce_strikes = get_ce_strikes(spot, strike_step)
                pe_strikes = get_pe_strikes(spot, strike_step)
                last_spot_refresh_minute = current_30min_bucket
                print(
                    f"[SPOT REFRESH] {now.strftime('%H:%M')} "
                    f"| {index_name}: {spot:.2f} | ATM: {atm} "
                    f"(next refresh at ~{current_30min_bucket[-5:]} + 30 min)"
                )

        print("\n")
        print("=" * 70)
        print(
            f"{now.strftime('%Y-%m-%d %H:%M:%S')} "
            f"| {index_name}: {spot:.2f} "
            f"| ATM: {atm}"
        )
        print(f"CE strikes: {ce_strikes}")
        print(f"PE strikes: {pe_strikes}")

        if should_check_breakout:
            print(f"Alert check: YES (every {ALERT_INTERVAL_MINUTES} min)")
        else:
            print("Alert check: NO (record only)")
        print("=" * 70)

        # ====================================================
        # GET CE / PE PRICES
        # ====================================================
        ce_data = []
        pe_data = []
        breakouts_this_minute = []

        for strike in ce_strikes:
            ce_price = get_option_price(
                strike,
                "CE",
                option_map,
                option_exchange
            )

            print(
                f"CE {strike:>6} : "
                f"{str(ce_price):>8}"
            )

            ce_data.append({
                "strike": strike,
                "price": ce_price
            })

            check_strike_breakout(
                strike,
                "CE",
                ce_price,
                session_highs,
                breakouts_this_minute,
                check_breakout=should_check_breakout
            )

        for strike in pe_strikes:
            pe_price = get_option_price(
                strike,
                "PE",
                option_map,
                option_exchange
            )

            print(
                f"PE {strike:>6} : "
                f"{str(pe_price):>8}"
            )

            pe_data.append({
                "strike": strike,
                "price": pe_price
            })

            check_strike_breakout(
                strike,
                "PE",
                pe_price,
                session_highs,
                breakouts_this_minute,
                check_breakout=should_check_breakout
            )

        # ====================================================
        # BREAKOUT SUMMARY FOR CSV / CONSOLE
        # ====================================================
        alerted_breakout = None

        if should_check_breakout:
            (
                alerted_breakout,
                consecutive_direction,
                consecutive_count,
                last_breakout_candidate
            ) = process_consecutive_breakout(
                breakouts_this_minute,
                consecutive_direction,
                consecutive_count,
                last_breakout_candidate,
                spot,
                option_map,
                option_exchange,
                index_name=index_name,
                required_consecutive=REQUIRED_CONSECUTIVE_CHECKS
            )

        breakout = alerted_breakout is not None

        if alerted_breakout:
            highest_premium = alerted_breakout["price"]
            highest_strike = alerted_breakout["strike"]
            highest_option_type = alerted_breakout["option_type"]
            previous_high = alerted_breakout["previous_high"]
        else:
            highest_premium = None
            highest_strike = None
            highest_option_type = None
            previous_high = None

        # ====================================================
        # SAVE CSV (every minute, regardless of breakout)
        # ====================================================
        write_csv(
            current_csv_file,
            now,
            spot,
            atm,
            ce_data,
            pe_data,
            highest_premium,
            highest_strike,
            highest_option_type,
            previous_high,
            breakout
        )

        if should_check_breakout:
            print(f"\nBreakouts this check : {len(breakouts_this_minute)}")

            for item in breakouts_this_minute:
                marker = "  "
                if (
                    alerted_breakout
                    and item["strike"] == alerted_breakout["strike"]
                    and item["option_type"] == alerted_breakout["option_type"]
                ):
                    marker = "* "

                print(
                    f"{marker}{item['strike']} {item['option_type']} "
                    f"{item['previous_high']:.2f} -> "
                    f"{item['price']:.2f}"
                )

            if alerted_breakout:
                print(
                    f"Alerted (lowest premium): "
                    f"{alerted_breakout['strike']} "
                    f"{alerted_breakout['option_type']} "
                    f"@ {alerted_breakout['price']:.2f} "
                    f"({consecutive_count} consecutive {consecutive_direction} checks confirmed)"
                )
            elif breakouts_this_minute:
                print(
                    f"Consecutive status   : "
                    f"{consecutive_count}/{REQUIRED_CONSECUTIVE_CHECKS} consecutive {consecutive_direction} checks "
                    f"(Requires underlying direction confirmation & premium retention on next check)"
                )
            elif not breakouts_this_minute:
                print(f"Breakout             : {breakout}")

        # ====================================================
        # TRIGGER SPREAD STRATEGY ON ALERT & EXIT ON COMPLETION
        # ====================================================
        if alerted_breakout:
            # Sound alert on Windows
            try:
                import winsound
                winsound.Beep(1500, 1000)
            except Exception:
                pass

            # Check daily risk & trade count limits before entering order
            daily_trades, daily_pnl = get_daily_trade_stats(current_date, symbol_base=index_name)
            print(f"\n[*] Pre-trade Daily Risk Check ({index_name}): {daily_trades}/{MAX_DAILY_TRADES} trades completed today | Realized P&L: Rs.{daily_pnl:.2f}")

            if daily_trades >= MAX_DAILY_TRADES:
                print(f"\n[🛑 RISK LIMIT] Maximum daily trades limit reached ({daily_trades}/{MAX_DAILY_TRADES}). Trade execution skipped.")
                # Reset consecutive count so we don't spam
                consecutive_count = 0
                consecutive_direction = None
                last_breakout_candidate = None
            elif daily_pnl <= DAILY_MAX_LOSS:
                print(f"\n[🛑 RISK LIMIT] Maximum daily loss limit reached (Daily P&L: Rs.{daily_pnl:.2f} <= Rs.{DAILY_MAX_LOSS:.2f}). Trade execution halted.")
                consecutive_count = 0
                consecutive_direction = None
                last_breakout_candidate = None
            else:
                buy_strike = int(alerted_breakout["strike"])
                buy_option_type = alerted_breakout["option_type"]

                # Buy leg is the lower price option that broke out
                # Sell leg is +1000 for CE and -1000 for PE
                if buy_option_type == "CE":
                    sell_strike = buy_strike + 1000
                    sell_option_type = "CE"
                else:
                    sell_strike = buy_strike - 1000
                    sell_option_type = "PE"

                # Dynamically identify unit lot size at the time of trade
                buy_contract = option_map.get((float(buy_strike), buy_option_type))
                sell_contract = option_map.get((float(sell_strike), sell_option_type))

                # Pick lot size dynamically from contract or master or config
                unit_lot_size = (
                    (buy_contract.get("lotsize") if buy_contract else None)
                    or (sell_contract.get("lotsize") if sell_contract else None)
                    or (options[0].get("lotsize") if options else None)
                    or index_cfg["default_lot_size"]
                )
                total_quantity = DEFAULT_LOTS * unit_lot_size

                buy_leg = {
                    "strike": buy_strike,
                    "option_type": buy_option_type,
                    "quantity": total_quantity,
                    "lots": DEFAULT_LOTS,
                    "lot_size": unit_lot_size,
                    "token": buy_contract.get("token") if buy_contract else None,
                    "symbol": buy_contract.get("symbol") if buy_contract else None
                }
                sell_leg = {
                    "strike": sell_strike,
                    "option_type": sell_option_type,
                    "quantity": total_quantity,
                    "lots": DEFAULT_LOTS,
                    "lot_size": unit_lot_size,
                    "token": sell_contract.get("token") if sell_contract else None,
                    "symbol": sell_contract.get("symbol") if sell_contract else None
                }

                print("\n" + "=" * 70)
                print(f"🚨 TRIGGERING SPREAD STRATEGY (BROKEN CALL/SPREAD) ON {index_name}")
                print(f"   BUY Leg   : {buy_strike} {buy_option_type} @ Rs.{alerted_breakout['price']:.2f} (Broke out)")
                print(f"   SELL Leg  : {sell_strike} {sell_option_type} ({'+1000' if buy_option_type == 'CE' else '-1000'})")
                print(f"   Order Qty : {total_quantity} ({DEFAULT_LOTS} lots x {unit_lot_size} lot size)")
                print(f"   Expiry    : {nearest_expiry.strftime('%d-%b-%Y')}")
                print("=" * 70 + "\n")

                expiry_str = nearest_expiry.strftime("%d%b%y").upper()

                trade_result = execute_spread(
                    buy_leg=buy_leg,
                    sell_leg=sell_leg,
                    smart_api=smartApi,
                    expiry=expiry_str,
                    symbol_base=index_name,
                    exchange=option_exchange,
                    lots=DEFAULT_LOTS,
                    lot_size=unit_lot_size,
                    option_type=buy_option_type,
                    check_existing=True
                )

                print(f"\n[+] Strategy completed with status: {trade_result.get('status')}")
                if "pnl" in trade_result:
                    print(f"[+] Final Strategy P&L: Rs.{trade_result.get('pnl', 0.0):.2f}")
                print("[+] Strategy execution completed. Continuing monitoring and CSV logging.\n")
                # Reset candidate after trade
                last_breakout_candidate = None
                consecutive_count = 0
                consecutive_direction = None

        # ====================================================
        # WAIT UNTIL NEXT MINUTE
        # ====================================================
        now = datetime.now(IST)
        seconds_to_next_minute = (
            60
            - now.second
            - now.microsecond / 1_000_000
        )
        time.sleep(max(1, seconds_to_next_minute))


# ============================================================
# START
# ============================================================

if __name__ == "__main__":
    if data.get("status"):
        refresh_token = data["data"]["refreshToken"]
        smartApi.generateToken(refresh_token)
        profile = smartApi.getProfile(refresh_token)

        print("[+] Connected successfully")
        print("User Name:", profile["data"]["name"])
        print("Client ID:", profile["data"]["clientcode"])

        selected_index = select_index()
        monitor_breakout(selected_index)
    else:
        print("[!] Login failed:", data)
        sys.exit(1)
