"""
Backtest Engine for 3-Leg Options Strategy with Dynamic Spread Adjustment & Hedging
- Entry: 9:30 AM | Exit: 3:10 PM
- Decision Modes:
    * AUTO: Decides CE or PE at 9:30 AM (Spot 9:30 >= Open 9:15 -> CE, else PE)
    * CE (Bullish): BUY ATM CE, SELL ATM+1000 CE, SELL ATM+2500 CE | Hedge: BUY ATM-1000 PE
    * PE (Bearish): BUY ATM PE, SELL ATM-1000 PE, SELL ATM-2500 PE | Hedge: BUY ATM+1000 CE
- Dynamic Spread Adjustment: If loss > 2000, move spread 2 closer by 500 points towards ATM
- Exit Condition: If spread 2 crosses or reaches spread 1, exit position
- Hedging: After 11:00, if position is negative, buy hedge contract (2x multiplier)
- Main-Only P&L: For hours 12, 13, 14, calculate P&L for main 3 legs only if hedge was bought
- Hourly P&L monitoring at 10:00, 11:00, 12:00, 13:00, 14:00, 15:00, 15:10
- Uses 15-minute option OHLC data with caching
- BANKNIFTY
"""

import sys
import io
import json
import time
import csv
import argparse
from pathlib import Path
from datetime import datetime, timedelta
from typing import Dict, Any, List, Optional, Tuple
from collections import defaultdict

# Ensure UTF-8 output on Windows
if sys.platform == "win32":
    try:
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    except Exception:
        pass

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

VENV_SITE_PACKAGES = ROOT / ".venv" / "Lib" / "site-packages"
if VENV_SITE_PACKAGES.exists() and str(VENV_SITE_PACKAGES) not in sys.path:
    sys.path.insert(0, str(VENV_SITE_PACKAGES))

import requests
import pyotp
from SmartApi.smartConnect import SmartConnect
import config
from backtest.api_helper import RateLimitedSmartApi

# ============================================================
# CONFIGURATION
# ============================================================

INSTRUMENTS_CONFIG = {
    "BANKNIFTY": {
        "name": "BANKNIFTY",
        "display_name": "BANKNIFTY",
        "spot_symbol": "BANKNIFTY",
        "spot_token": "99926009",
        "spot_exchange": "NSE",
        "option_exchange": "NFO",
        "default_lot_size": 15,  # Fallback if dynamic fetch fails
        "spread_1": 1000,
        "spread_2": 2500,
        "loss_threshold": 2000,  # Loss threshold to trigger spread adjustment
        "spread_adjustment": 500,  # Points to move spread 2 when threshold hit
        "stop_loss_threshold": 10000,  # Hard stop-loss - exit immediately if loss exceeds this
        "hedge_multiplier": 2,  # Hedge contract quantity multiplier (e.g., 2 = double lots)
    }
}

MONITORING_TIMES = ["10:00", "11:00", "12:00", "13:00", "14:00", "15:00", "15:10"]
ENTRY_TIME = "09:30"
EXIT_TIME = "15:10"

DATA_ROOT = ROOT / "data"
DATA_ROOT.mkdir(parents=True, exist_ok=True)

# ============================================================
# API HELPERS
# ============================================================

def get_lot_size_from_angel_api(symbol_name, month_year):
    """Fetch lot size from Angel One API using symbol name and expiry month."""
    try:
        url = "https://margincalculator.angelone.in/OpenAPI_File/files/OpenAPIScripMaster.json"
        response = requests.get(url, timeout=30)
        instruments = response.json()
        
        # Example format for month_year: "OCT2025"
        for instrument in instruments:
            if instrument.get('name') == symbol_name and month_year in instrument.get('expiry', ''):
                return {
                    "symbol": instrument.get("symbol"),
                    "expiry": instrument.get("expiry"),
                    "lot_size": instrument.get("lotsize")
                }
        return None
    except Exception as e:
        print(f"    ⚠️ Error fetching lot size from Angel API: {e}")
        return None


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


def get_trading_days(smart_api, custom_date: Optional[datetime] = None, days_back: int = 30):
    """Fetches trading days for the specified period."""
    if custom_date:
        end_date = custom_date
    else:
        end_date = datetime.now()
    
    start_date = end_date - timedelta(days=days_back + 10)
    
    res = smart_api.getCandleData({
        "exchange": "NSE",  # Use NSE for trading calendar
        "symboltoken": "99926000",  # NIFTY token for calendar
        "interval": "ONE_DAY",
        "fromdate": start_date.strftime("%Y-%m-%d 09:15"),
        "todate": end_date.strftime("%Y-%m-%d 15:30"),
    })
    
    if not res.get("status") or not res.get("data"):
        raise RuntimeError(f"Failed to fetch trading days: {res}")
    
    trading_dates = [c[0][:10] for c in res["data"]]
    
    # Filter to get completed trading days before custom_date
    if custom_date:
        trading_dates = [d for d in trading_dates if d < custom_date.strftime("%Y-%m-%d")]
    
    return trading_dates[-days_back:] if len(trading_dates) >= days_back else trading_dates


def load_instrument_map(instrument_name: str, instruments=None, for_date_str=None, cache_dir=None, verbose=True):
    """Loads OpenAPIScripMaster and dynamically selects the nearest weekly expiry."""
    # Cache the instrument master file to avoid repeated downloads
    if cache_dir is None:
        cache_dir = ROOT / "backtest" / "cache_3leg"
    
    cache_dir.mkdir(parents=True, exist_ok=True)
    instrument_cache_file = cache_dir / "instrument_master.json"
    
    if instruments is None:
        if instrument_cache_file.exists():
            if verbose:
                print(f"    📂 Loading instrument master from cache...")
            with open(instrument_cache_file, "r", encoding="utf-8") as f:
                instruments = json.load(f)
        else:
            if verbose:
                print(f"    📡 Downloading instrument master (this may take a moment)...")
            url = "https://margincalculator.angelone.in/OpenAPI_File/files/OpenAPIScripMaster.json"
            resp = requests.get(url, timeout=30, verify=False)
            instruments = resp.json()
            
            # Cache the downloaded file
            with open(instrument_cache_file, "w", encoding="utf-8") as f:
                json.dump(instruments, f)
            if verbose:
                print(f"    ✅ Instrument master cached ({len(instruments):,} records)")
    
    inst_config = INSTRUMENTS_CONFIG[instrument_name]
    target_date = datetime.strptime(for_date_str, "%Y-%m-%d").date() if for_date_str else datetime.now().date()
    
    # Pre-filter to only relevant instruments to speed up processing
    matching = [
        item for item in instruments
        if item.get("name") == instrument_name
        and item.get("exch_seg") == inst_config["option_exchange"]
        and item.get("instrumenttype") == "OPTIDX"
        and item.get("expiry")
    ]
    
    if verbose:
        print(f"    🔍 Filtering for {instrument_name} {inst_config['option_exchange']} options...")
        print(f"    ✅ Found {len(matching)} {instrument_name} option contracts")
    
    valid_expiries = []
    for item in matching:
        try:
            expiry_str = item.get("expiry")
            if expiry_str:
                exp_date = datetime.strptime(expiry_str, "%d%b%Y").date()
                if exp_date >= target_date:
                    valid_expiries.append(exp_date)
        except (ValueError, TypeError):
            pass
    
    if valid_expiries:
        # Select nearest weekly expiry (prefer Thursday expiry for weekly)
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
    lot_size = inst_config["default_lot_size"]  # Default fallback
    
    for item in matching:
        if item.get("expiry") == selected_expiry_str:
            strike = float(item["strike"]) / 100.0
            opt_type = item["symbol"][-2:]
            item_lot_size = item.get("lotsize", inst_config["default_lot_size"])
            option_map[(strike, opt_type)] = {
                "token": str(item["token"]),
                "symbol": item["symbol"],
                "strike": strike,
                "option_type": opt_type,
                "expiry": selected_expiry_str,
                "lot_size": item_lot_size
            }
            # Update lot size from actual instrument data
            if item_lot_size and isinstance(item_lot_size, (int, float)):
                lot_size = int(item_lot_size)  # Convert to int
    
    # If lot size is still default, try to fetch from Angel One API
    if lot_size == inst_config["default_lot_size"]:
        month_year = selected_expiry_date.strftime("%b%Y").upper()
        api_result = get_lot_size_from_angel_api(instrument_name, month_year)
        if api_result and api_result.get("lot_size"):
            lot_size = int(api_result["lot_size"])
            if verbose:
                print(f"    📊 Lot size fetched from Angel API for {month_year}: {lot_size}")
    
    if verbose:
        print(f"    📊 Lot size for {selected_expiry_str}: {lot_size}")
    
    return option_map, selected_expiry_str, lot_size


def fetch_single_contract_candles(smart_api, inst_config: Dict, opt_info: Dict, date_str: str, key: str) -> Dict[str, Dict[str, float]]:
    """Fetches 15-minute candle data for a single option contract in chunks with retries."""
    option_data = {}
    chunks = [
        ("09:15", "11:15"),
        ("11:15", "13:15"),
        ("13:15", "15:30"),
    ]
    for chunk_start, chunk_end in chunks:
        success = False
        for attempt in range(3):
            try:
                params = {
                    "exchange": inst_config["option_exchange"],
                    "symboltoken": opt_info["token"],
                    "interval": "FIFTEEN_MINUTE",
                    "fromdate": f"{date_str} {chunk_start}",
                    "todate": f"{date_str} {chunk_end}",
                }
                res = smart_api.getCandleData(params)
                if res.get("status") and res.get("data") and len(res["data"]) > 0:
                    for candle in res["data"]:
                        timestamp = candle[0][:16]
                        option_data[timestamp] = {
                            "open": float(candle[1]),
                            "high": float(candle[2]),
                            "low": float(candle[3]),
                            "close": float(candle[4])
                        }
                    success = True
                    break
                else:
                    time.sleep(1.5 * (attempt + 1))
            except Exception as e:
                time.sleep(1.5 * (attempt + 1))
        if not success:
            print(f"    ❌ Failed fetching {key} {chunk_start}-{chunk_end}")
        time.sleep(0.3)
    return option_data


def fetch_option_data(smart_api, date_str: str, option_map: Dict, instrument_name: str, cache_dir: Path, lot_size: int, side: str = "AUTO"):
    """Fetches 15-minute OHLC data for spot and required options for chosen side (CE, PE, or AUTO)."""
    cache_file = cache_dir / f"options_{instrument_name}_{date_str}.json"
    cached_data = None
    if cache_file.exists():
        try:
            with open(cache_file, "r", encoding="utf-8") as f:
                cached_data = json.load(f)
        except Exception:
            cached_data = None

    inst_config = INSTRUMENTS_CONFIG[instrument_name]

    # Spot data: load from cache if available, else fetch from API
    if cached_data and "spot" in cached_data and cached_data["spot"]:
        spot_data = cached_data["spot"]
    else:
        print(f"    📡 Fetching spot data from API...")
        spot_res = smart_api.getCandleData({
            "exchange": inst_config["spot_exchange"],
            "symboltoken": inst_config["spot_token"],
            "interval": "FIFTEEN_MINUTE",
            "fromdate": f"{date_str} 09:15",
            "todate": f"{date_str} 15:30",
        })
        if not spot_res.get("status") or not spot_res.get("data"):
            print(f"[!] No spot data for {date_str}, skipping.")
            return None

        spot_data = {}
        for candle in spot_res["data"]:
            timestamp = candle[0][:16]
            spot_data[timestamp] = {
                "open": float(candle[1]),
                "high": float(candle[2]),
                "low": float(candle[3]),
                "close": float(candle[4])
            }

    # Extract 9:15 Open and 9:30 Close
    spot_915_open = None
    spot_930 = None
    for timestamp, data in spot_data.items():
        if "09:15" in timestamp and spot_915_open is None:
            spot_915_open = data["open"]
        if "09:30" in timestamp:
            spot_930 = data["close"]

    if spot_930 is None:
        print(f"[!] No 9:30 data for {date_str}, skipping.")
        return None
    if spot_915_open is None:
        spot_915_open = spot_930

    # Determine strategy side: CE or PE
    if side.upper() == "AUTO":
        chosen_side = "CE" if spot_930 >= spot_915_open else "PE"
        decision_reason = f"Spot 9:30 ({spot_930:.2f}) >= Open 9:15 ({spot_915_open:.2f})" if chosen_side == "CE" else f"Spot 9:30 ({spot_930:.2f}) < Open 9:15 ({spot_915_open:.2f})"
    elif side.upper() == "PE":
        chosen_side = "PE"
        decision_reason = "Manual PE mode"
    else:
        chosen_side = "CE"
        decision_reason = "Manual CE mode"

    # Base ATM strike (nearest 500-divisible)
    atm_strike = int(round(spot_930 / 500) * 500)

    # Determine required strikes and option types based on chosen_side
    if chosen_side == "CE":
        main_opt_type = "CE"
        strike_1 = atm_strike + inst_config["spread_1"]  # ATM + 1000
        strike_2 = atm_strike + inst_config["spread_2"]  # ATM + 2500
        # Include intermediate strikes for dynamic spread adjustment (-500 steps down to strike_1)
        main_strikes = [atm_strike, strike_1, atm_strike + 1500, atm_strike + 2000, strike_2]
        hedge_opt_type = "PE"
        hedge_strike = int(round((atm_strike - 1000) / 500) * 500)
    else:
        main_opt_type = "PE"
        strike_1 = atm_strike - inst_config["spread_1"]  # ATM - 1000
        strike_2 = atm_strike - inst_config["spread_2"]  # ATM - 2500
        # Include intermediate strikes for dynamic spread adjustment (+500 steps up to strike_1)
        main_strikes = [atm_strike, strike_1, atm_strike - 1500, atm_strike - 2000, strike_2]
        hedge_opt_type = "CE"
        hedge_strike = int(round((atm_strike + 1000) / 500) * 500)

    # Required contract keys to check in cache
    needed_contracts = [(s, main_opt_type) for s in main_strikes] + [(hedge_strike, hedge_opt_type)]
    needed_keys = [f"{s}_{t}" for s, t in needed_contracts]

    # Initialize day_data
    options_dict = cached_data.get("options", {}) if cached_data else {}
    day_data = {
        "spot": spot_data,
        "spot_915_open": spot_915_open,
        "spot_930": spot_930,
        "chosen_side": chosen_side,
        "decision_reason": decision_reason,
        "atm_strike": atm_strike,
        "strike_1": strike_1,
        "strike_2": strike_2,
        "hedge_strike": hedge_strike,
        "lot_size": int(lot_size) if isinstance(lot_size, (int, float)) else inst_config["default_lot_size"],
        "options": options_dict
    }

    # Check if all needed contracts are already in options_dict
    missing_contracts = [(s, t) for (s, t) in needed_contracts if f"{s}_{t}" not in day_data["options"]]

    if not missing_contracts and cached_data:
        print(f"    📂 Loading from cache... ({chosen_side} side: {decision_reason})")
        return day_data

    print(f"    📡 Fetching option data for {chosen_side} side ({decision_reason})...")
    print(f"       Strikes: ATM={atm_strike} {main_opt_type}, S1={strike_1}, S2={strike_2}, Hedge={hedge_strike} {hedge_opt_type}")

    # Fetch missing contracts
    fetched_count = 0
    for strike, opt_type in missing_contracts:
        key = f"{strike}_{opt_type}"
        opt_info = option_map.get((float(strike), opt_type))
        if not opt_info:
            print(f"    ⚠️ Option {key} not found in instrument map")
            continue
        
        opt_candles = fetch_single_contract_candles(smart_api, inst_config, opt_info, date_str, key)
        if opt_candles:
            day_data["options"][key] = opt_candles
            fetched_count += 1

    # Save / update cache
    cache_dir.mkdir(parents=True, exist_ok=True)
    with open(cache_file, "w", encoding="utf-8") as f:
        json.dump(day_data, f)

    if fetched_count > 0:
        print(f"    ✅ Successfully fetched and cached {fetched_count} new option contract(s)")
    return day_data


def _find_closest_option_timestamp(option_timestamps, target_ts: str) -> str:
    """Find the closest available option timestamp to the target.
    Option data may have 15-min candles while spot data has 1-min candles.
    Returns the exact match if available, otherwise the closest timestamp.
    """
    if target_ts in option_timestamps:
        return target_ts
    
    # Sort available timestamps and find closest
    sorted_ts = sorted(option_timestamps)
    if not sorted_ts:
        return None
    
    # Find closest by string comparison (ISO format sorts correctly)
    best = None
    best_diff = float('inf')
    for ts in sorted_ts:
        # Compare just the time portion for proximity
        try:
            target_minutes = int(target_ts[-5:-3]) * 60 + int(target_ts[-2:])
            ts_minutes = int(ts[-5:-3]) * 60 + int(ts[-2:])
            diff = abs(target_minutes - ts_minutes)
            if diff < best_diff:
                best_diff = diff
                best = ts
        except (ValueError, IndexError):
            continue
    
    return best


def _get_option_price(option_data: Dict, key: str, target_timestamp: str) -> float:
    """Get option close price at or near a target timestamp.
    Falls back to nearest available timestamp if exact match is missing.
    """
    if key not in option_data:
        return None
    contract_data = option_data[key]
    if target_timestamp in contract_data:
        return contract_data[target_timestamp]["close"]
    # Fallback: find closest available timestamp
    closest = _find_closest_option_timestamp(contract_data, target_timestamp)
    if closest and closest in contract_data:
        return contract_data[closest]["close"]
    return None


# ============================================================
# STRATEGY IMPLEMENTATION
# ============================================================

def run_daily_strategy(date_str: str, day_data: Dict, instrument_name: str, lots: int = 5, side: str = "AUTO") -> Dict[str, Any]:
    """Runs the 3-leg strategy for a single day for either CE or PE side."""
    inst_config = INSTRUMENTS_CONFIG[instrument_name]
    spot_data = day_data["spot"]
    option_data = day_data["options"]
    spot_930 = day_data["spot_930"]
    spot_915_open = day_data.get("spot_915_open", spot_930)
    atm_strike = day_data["atm_strike"]
    lot_size = day_data["lot_size"]

    # Ensure lot_size is an integer
    if isinstance(lot_size, (list, tuple)):
        lot_size = int(lot_size[0]) if lot_size else inst_config["default_lot_size"]
    elif not isinstance(lot_size, int):
        lot_size = int(lot_size) if lot_size else inst_config["default_lot_size"]

    # Determine strategy side: CE or PE
    if side.upper() == "AUTO":
        chosen_side = day_data.get("chosen_side", "CE" if spot_930 >= spot_915_open else "PE")
    else:
        chosen_side = side.upper()

    total_quantity = lots * lot_size
    loss_threshold = inst_config["loss_threshold"]
    spread_adjustment = inst_config["spread_adjustment"]
    stop_loss_threshold = inst_config.get("stop_loss_threshold", 10000)  # Hard stop-loss

    # Symmetric strike setup
    if chosen_side == "CE":
        opt_type = "CE"
        hedge_type = "PE"
        strike_1 = day_data.get("strike_1", atm_strike + inst_config["spread_1"])  # ATM + 1000
        strike_2 = day_data.get("strike_2", atm_strike + inst_config["spread_2"])  # ATM + 2500
        hedge_strike = day_data.get("hedge_strike", int(round((atm_strike - 1000) / 500) * 500))
        adj_step = -spread_adjustment  # Move downward towards ATM / strike_1
    else:
        opt_type = "PE"
        hedge_type = "CE"
        strike_1 = day_data.get("strike_1", atm_strike - inst_config["spread_1"])  # ATM - 1000
        strike_2 = day_data.get("strike_2", atm_strike - inst_config["spread_2"])  # ATM - 2500
        hedge_strike = day_data.get("hedge_strike", int(round((atm_strike + 1000) / 500) * 500))
        adj_step = +spread_adjustment  # Move upward towards ATM / strike_1

    current_spread_2 = strike_2
    spread_adjustments = []
    exit_triggered = False
    exit_time = None
    exit_reason = None

    # Hedging tracking
    hedge_enabled = True
    hedge_multiplier = inst_config.get("hedge_multiplier", 2)
    hedge_bought = False
    hedge_entry_price = None
    hedge_buy_time = None
    hedge_key = f"{hedge_strike}_{hedge_type}"

    # Get entry prices at 9:30
    entry_timestamp = None
    for timestamp in spot_data.keys():
        if "09:30" in timestamp:
            entry_timestamp = timestamp
            break

    if not entry_timestamp:
        return {"status": "error", "date": date_str, "message": "No 9:30 timestamp found", "instrument": instrument_name}

    # Contract keys
    atm_key = f"{atm_strike}_{opt_type}"
    strike_1_key = f"{strike_1}_{opt_type}"
    strike_2_key = f"{strike_2}_{opt_type}"

    entry_prices = {}
    for key, leg_name in [(atm_key, "ATM"), (strike_1_key, "SPREAD_1"), (strike_2_key, "SPREAD_2")]:
        price = _get_option_price(option_data, key, entry_timestamp)
        if price is not None:
            entry_prices[leg_name] = price
        else:
            return {"status": "error", "date": date_str, "message": f"Missing entry data for {leg_name} ({key})", "instrument": instrument_name}

    # Backward compatibility aliases
    entry_prices[f"ATM_{opt_type}"] = entry_prices["ATM"]
    entry_prices[f"ATM_1000_{opt_type}"] = entry_prices["SPREAD_1"]
    entry_prices[f"ATM_1500_{opt_type}"] = entry_prices["SPREAD_2"]

    # Calculate hourly P&L with dynamic spread adjustment and hedging
    hourly_pnl = {}
    hourly_leg_pnl = {t: {} for t in MONITORING_TIMES}

    for time in MONITORING_TIMES:
        # Find closest timestamp (exact match first, then fallback to nearest)
        target_timestamp = None
        for timestamp in spot_data.keys():
            if time in timestamp:
                target_timestamp = timestamp
                break

        if not target_timestamp:
            # Fallback: find closest spot timestamp to the target time
            target_timestamp = _find_closest_option_timestamp(spot_data, f"{date_str}T{time}")
        
        if not target_timestamp:
            hourly_pnl[time] = None
            continue

        # Get current prices for active legs (with fallback to closest timestamp)
        current_prices = {}
        for key, leg_name in [(atm_key, "ATM"), (strike_1_key, "SPREAD_1"), (strike_2_key, "SPREAD_2")]:
            price = _get_option_price(option_data, key, target_timestamp)
            current_prices[leg_name] = price

        if None in current_prices.values():
            hourly_pnl[time] = None
            continue

        # Calculate P&L for each leg
        atm_pnl = (current_prices["ATM"] - entry_prices["ATM"]) * total_quantity
        spread_1_pnl = (entry_prices["SPREAD_1"] - current_prices["SPREAD_1"]) * total_quantity
        spread_2_pnl = (entry_prices["SPREAD_2"] - current_prices["SPREAD_2"]) * total_quantity
        total_pnl = atm_pnl + spread_1_pnl + spread_2_pnl

        # Calculate hedge P&L if hedge was bought (for stop-loss check)
        hedge_pnl_at_current = 0
        if hedge_bought and hedge_key:
            hedge_current_price = _get_option_price(option_data, hedge_key, target_timestamp)
            if hedge_current_price is not None:
                hedge_pnl_at_current = (hedge_current_price - hedge_entry_price) * total_quantity * hedge_multiplier
                total_pnl_with_hedge = total_pnl + hedge_pnl_at_current
            else:
                total_pnl_with_hedge = total_pnl
        else:
            total_pnl_with_hedge = total_pnl

        # Hedging: After 11:00, if position is negative, buy hedge contract
        if hedge_enabled and not hedge_bought and time == "11:00" and total_pnl < 0:
            hedge_price = _get_option_price(option_data, hedge_key, target_timestamp)
            if hedge_price is not None:
                hedge_entry_price = hedge_price
                hedge_bought = True
                hedge_buy_time = time
                spread_adjustments.append({
                    "time": time,
                    "type": f"{hedge_type}_HEDGE",
                    "hedge_type": hedge_type,
                    "hedge_strike": hedge_strike,
                    "hedge_entry_price": hedge_entry_price,
                    "hedge_multiplier": hedge_multiplier,
                    "reason": f"Position negative at 11:00 ({total_pnl:.2f}), bought {hedge_multiplier}x {hedge_strike} {hedge_type}",
                    "pnl_at_hedge": total_pnl,
                    "pe_strike": hedge_strike,
                    "pe_entry_price": hedge_entry_price,
                    "pe_multiplier": hedge_multiplier,
                })
                hourly_pnl[time] = total_pnl
                hourly_leg_pnl[time] = {
                    "ATM": atm_pnl,
                    "SPREAD_1": spread_1_pnl,
                    "SPREAD_2": spread_2_pnl,
                    "Total": total_pnl,
                    "Hedge_Just_Bought": True,
                    "PE_Just_Bought": (hedge_type == "PE")
                }
                continue  # Skip spread adjustment for 11:00
            else:
                spread_adjustments.append({
                    "time": time,
                    "type": f"{hedge_type}_HEDGE_FAILED",
                    "hedge_type": hedge_type,
                    "hedge_strike": hedge_strike,
                    "reason": f"Hedge data not available for {hedge_key} at 11:00",
                    "pnl_at_attempt": total_pnl,
                    "pe_strike": hedge_strike
                })

        # HARD STOP-LOSS CHECK - Exit immediately if loss exceeds threshold
        # Use total P&L including hedge for accurate stop-loss trigger
        if not exit_triggered and total_pnl_with_hedge < -stop_loss_threshold:
            exit_triggered = True
            exit_time = time
            exit_reason = f"Stop-loss triggered: Loss {total_pnl_with_hedge:.2f} < -{stop_loss_threshold}"
            hourly_pnl[time] = total_pnl_with_hedge
            hourly_leg_pnl[time] = {
                "ATM": atm_pnl,
                "SPREAD_1": spread_1_pnl,
                "SPREAD_2": spread_2_pnl,
                "Total": total_pnl_with_hedge,
                "Hedge_PNL": hedge_pnl_at_current if hedge_bought else 0,
                "Status": "STOP_LOSS_EXIT"
            }
            spread_adjustments.append({
                "time": time,
                "type": "STOP_LOSS",
                "reason": exit_reason,
                "pnl_at_exit": total_pnl_with_hedge
            })
            break  # Stop processing further times

        # For hours 12, 13, 14, calculate P&L for main 3 legs only if hedge was bought
        if hedge_bought and time in ["12:00", "13:00", "14:00"]:
            main_only_pnl = atm_pnl + spread_1_pnl + spread_2_pnl
            hourly_pnl[time] = main_only_pnl
            hourly_leg_pnl[time] = {
                "ATM": atm_pnl,
                "SPREAD_1": spread_1_pnl,
                "SPREAD_2": spread_2_pnl,
                "Total": main_only_pnl,
                "Hedge_Excluded": True,
                "PE_Excluded": (hedge_type == "PE")
            }
            continue

        # Dynamic spread adjustment check
        if not exit_triggered and total_pnl < -loss_threshold and not (hedge_bought and hedge_buy_time == time):
            new_spread_2 = current_spread_2 + adj_step
            # Exit check: CE reaches <= strike_1, PE reaches >= strike_1
            is_exit_met = (new_spread_2 <= strike_1) if chosen_side == "CE" else (new_spread_2 >= strike_1)

            if is_exit_met:
                exit_triggered = True
                exit_time = time
                op_symbol = "<=" if chosen_side == "CE" else ">="
                exit_reason = f"Spread 2 ({new_spread_2}) {op_symbol} Spread 1 ({strike_1})"

                # Include hedge P&L in exit P&L if hedge was bought
                exit_pnl = total_pnl_with_hedge if hedge_bought else total_pnl

                hourly_pnl[time] = exit_pnl
                hourly_leg_pnl[time] = {
                    "ATM": atm_pnl,
                    "SPREAD_1": spread_1_pnl,
                    "SPREAD_2": spread_2_pnl,
                    "Total": exit_pnl,
                    "Hedge_PNL": hedge_pnl_at_current if hedge_bought else 0,
                    "Status": "EXITED"
                }
                spread_adjustments.append({
                    "time": time,
                    "old_spread_2": current_spread_2,
                    "new_spread_2": new_spread_2,
                    "reason": "Exit condition triggered",
                    "pnl_at_adjustment": exit_pnl
                })
                break
            else:
                old_spread_2 = current_spread_2
                current_spread_2 = new_spread_2
                spread_adjustments.append({
                    "time": time,
                    "old_spread_2": old_spread_2,
                    "new_spread_2": current_spread_2,
                    "reason": f"Loss threshold exceeded ({total_pnl:.2f} < -{loss_threshold})",
                    "pnl_at_adjustment": total_pnl
                })

                # Update the spread 2 leg key
                new_strike_2_key = f"{current_spread_2}_{opt_type}"
                new_entry_price = _get_option_price(option_data, new_strike_2_key, target_timestamp)
                if new_entry_price is not None:
                    entry_prices["SPREAD_2"] = new_entry_price
                    strike_2_key = new_strike_2_key

        # Use total P&L including hedge for recording if hedge was bought
        record_pnl = total_pnl_with_hedge if hedge_bought else total_pnl

        hourly_pnl[time] = record_pnl
        hourly_leg_pnl[time] = {
            "ATM": atm_pnl,
            "SPREAD_1": spread_1_pnl,
            "SPREAD_2": spread_2_pnl,
            "Total": record_pnl,
            "Hedge_PNL": hedge_pnl_at_current if hedge_bought else 0
        }

    # Final exit P&L
    if exit_triggered:
        final_pnl = hourly_pnl.get(exit_time)
        final_time = exit_time
    else:
        final_pnl = hourly_pnl.get("15:10")
        final_time = "15:10"

    # Calculate hedge P&L if hedge was bought
    hedge_pnl = 0
    if hedge_bought and hedge_key:
        # Always exit hedge at the same time as main trade
        final_timestamp = None
        for timestamp in spot_data.keys():
            if final_time in timestamp:
                final_timestamp = timestamp
                break

        if not final_timestamp:
            final_timestamp = _find_closest_option_timestamp(spot_data, f"{date_str}T{final_time}")

        if not final_timestamp:
            final_timestamp = f"{date_str}T{final_time}"

        hedge_exit_price = _get_option_price(option_data, hedge_key, final_timestamp)
        if hedge_exit_price is not None:
            hedge_pnl = (hedge_exit_price - hedge_entry_price) * total_quantity * hedge_multiplier

        # Note: hourly_pnl at exit_time (both 15:10 normal exit and stop-loss / hard exit)
        # already includes total_pnl_with_hedge. Do NOT add hedge_pnl again to final_pnl.
        if final_pnl is None:
            final_pnl = hedge_pnl

    # Risk metrics
    valid_pnls = [pnl for pnl in hourly_pnl.values() if pnl is not None and isinstance(pnl, (int, float))]
    if valid_pnls:
        max_hourly_profit = max(valid_pnls)
        max_hourly_loss = min(valid_pnls)
        lowest_mtm = min(valid_pnls)
        highest_mtm = max(valid_pnls)
        max_intraday_drawdown = min(valid_pnls)
        max_profit_time = [t for t, pnl in hourly_pnl.items() if pnl == max_hourly_profit][0] if max_hourly_profit in hourly_pnl.values() else "N/A"
        max_loss_time = [t for t, pnl in hourly_pnl.items() if pnl == max_hourly_loss][0] if max_hourly_loss in hourly_pnl.values() else "N/A"
    else:
        max_hourly_profit = None
        max_hourly_loss = None
        lowest_mtm = None
        highest_mtm = None
        max_intraday_drawdown = None
        max_profit_time = "N/A"
        max_loss_time = "N/A"

    return {
        "status": "success",
        "date": date_str,
        "instrument": instrument_name,
        "side": chosen_side,
        "spot_915_open": spot_915_open,
        "spot_930": spot_930,
        "atm_strike": atm_strike,
        "strike_1": strike_1,
        "strike_2": strike_2,
        "entry_prices": entry_prices,
        "hourly_pnl": hourly_pnl,
        "hourly_leg_pnl": hourly_leg_pnl,
        "final_pnl": final_pnl,
        "max_hourly_profit": max_hourly_profit,
        "max_hourly_loss": max_hourly_loss,
        "lowest_mtm": lowest_mtm,
        "highest_mtm": highest_mtm,
        "max_intraday_drawdown": max_intraday_drawdown,
        "max_profit_time": max_profit_time,
        "max_loss_time": max_loss_time,
        "lots": lots,
        "lot_size": lot_size,
        "total_quantity": total_quantity,
        "spread_adjustments": spread_adjustments,
        "exit_triggered": exit_triggered,
        "exit_time": exit_time,
        "exit_reason": exit_reason,
        "hedge_bought": hedge_bought,
        "hedge_type": hedge_type,
        "hedge_strike": hedge_strike,
        "hedge_entry_price": hedge_entry_price,
        "hedge_pnl": hedge_pnl,
        "pe_bought": hedge_bought if hedge_type == "PE" else False,
        "pe_strike": hedge_strike if hedge_type == "PE" else None,
        "pe_entry_price": hedge_entry_price if hedge_type == "PE" else None,
        "pe_pnl": hedge_pnl if hedge_type == "PE" else 0,
    }


# ============================================================
# ANALYSIS AND REPORTING
# ============================================================

def calculate_30day_summary(daily_results: List[Dict]) -> Dict[str, Any]:
    """Calculates 30-day summary statistics."""
    valid_results = [r for r in daily_results if r["status"] == "success"]
    
    if not valid_results:
        return {}
    
    final_pnls = [r["final_pnl"] for r in valid_results if r["final_pnl"] is not None]
    
    if not final_pnls:
        return {}
    
    total_pnl = sum(final_pnls)
    avg_daily_pnl = total_pnl / len(final_pnls)
    median_daily_pnl = sorted(final_pnls)[len(final_pnls) // 2]
    
    profitable_days = sum(1 for pnl in final_pnls if pnl > 0)
    losing_days = sum(1 for pnl in final_pnls if pnl < 0)
    win_rate = (profitable_days / len(final_pnls)) * 100 if final_pnls else 0
    
    max_single_day_profit = max(final_pnls)
    max_single_day_loss = min(final_pnls)
    
    # Calculate max drawdown
    cumulative_pnl = 0
    peak = 0
    max_drawdown = 0
    for pnl in final_pnls:
        cumulative_pnl += pnl
        if cumulative_pnl > peak:
            peak = cumulative_pnl
        drawdown = peak - cumulative_pnl
        if drawdown > max_drawdown:
            max_drawdown = drawdown
    
    gross_profit = sum(pnl for pnl in final_pnls if pnl > 0)
    gross_loss = abs(sum(pnl for pnl in final_pnls if pnl < 0))
    profit_factor = gross_profit / gross_loss if gross_loss > 0 else 0
    
    avg_winning_day = gross_profit / profitable_days if profitable_days > 0 else 0
    avg_losing_day = gross_loss / losing_days if losing_days > 0 else 0
    
    # Calculate consecutive wins/losses
    consecutive_wins = 0
    consecutive_losses = 0
    current_streak = 0
    current_type = None
    
    for pnl in final_pnls:
        if pnl > 0:
            if current_type == "win":
                current_streak += 1
            else:
                current_streak = 1
                current_type = "win"
            consecutive_wins = max(consecutive_wins, current_streak)
        elif pnl < 0:
            if current_type == "loss":
                current_streak += 1
            else:
                current_streak = 1
                current_type = "loss"
            consecutive_losses = max(consecutive_losses, current_streak)
        else:
            current_type = None
            current_streak = 0
    
    return {
        "total_pnl": total_pnl,
        "avg_daily_pnl": avg_daily_pnl,
        "median_daily_pnl": median_daily_pnl,
        "profitable_days": profitable_days,
        "losing_days": losing_days,
        "win_rate": win_rate,
        "max_single_day_profit": max_single_day_profit,
        "max_single_day_loss": max_single_day_loss,
        "max_drawdown": max_drawdown,
        "profit_factor": profit_factor,
        "avg_winning_day": avg_winning_day,
        "avg_losing_day": avg_losing_day,
        "consecutive_wins": consecutive_wins,
        "consecutive_losses": consecutive_losses,
        "total_days": len(final_pnls)
    }


def calculate_hourly_performance(all_daily_results: List[Dict]) -> Dict[str, Dict[str, Any]]:
    """Calculates hourly performance statistics across all days."""
    hourly_stats = {time: {} for time in MONITORING_TIMES}
    
    for time in MONITORING_TIMES:
        pnls = []
        for day_result in all_daily_results:
            if day_result["status"] == "success" and time in day_result["hourly_pnl"]:
                pnl = day_result["hourly_pnl"][time]
                if pnl is not None:
                    pnls.append(pnl)
        
        if pnls:
            hourly_stats[time] = {
                "average_pnl": sum(pnls) / len(pnls),
                "median_pnl": sorted(pnls)[len(pnls) // 2],
                "profitable_pct": (sum(1 for pnl in pnls if pnl > 0) / len(pnls)) * 100,
                "losing_pct": (sum(1 for pnl in pnls if pnl < 0) / len(pnls)) * 100,
                "best_pnl": max(pnls),
                "worst_pnl": min(pnls),
                "count": len(pnls)
            }
        else:
            hourly_stats[time] = {
                "average_pnl": None,
                "median_pnl": None,
                "profitable_pct": None,
                "losing_pct": None,
                "best_pnl": None,
                "worst_pnl": None,
                "count": 0
            }
    
    return hourly_stats


def print_daily_table(daily_results: List[Dict], instrument_name: str):
    """Prints the daily results table."""
    print(f"\n{'='*185}")
    print(f"                    {instrument_name} DAILY RESULTS TABLE")
    print(f"{'='*185}")
    print(f"{'Date':<12} | {'Side':<8} | {'ATM':<8} | {'Entry Prices':<46} | {'10:00':<10} | {'11:00':<10} | {'12:00':<10} | {'13:00':<10} | {'14:00':<10} | {'15:00':<10} | {'15:10':<10}")
    print(f"{'-'*185}")
    
    for result in daily_results:
        if result.get("status") != "success" or not isinstance(result, dict):
            date_str = result.get("date", "N/A") if isinstance(result, dict) else "N/A"
            print(f"{date_str:<12} | {'N/A':<8} | {'N/A':<8} | DATA UNAVAILABLE")
            continue
        
        side = result.get("side", "CE")
        atm_p = result['entry_prices'].get('ATM', result['entry_prices'].get(f'ATM_{side}', 0.0))
        s1_p = result['entry_prices'].get('SPREAD_1', result['entry_prices'].get(f'ATM_1000_{side}', 0.0))
        s2_p = result['entry_prices'].get('SPREAD_2', result['entry_prices'].get(f'ATM_1500_{side}', 0.0))
        entry_str = f"ATM({side}):{atm_p:.2f}, S1:{s1_p:.2f}, S2:{s2_p:.2f}"
        
        hourly_strs = []
        for time in MONITORING_TIMES:
            pnl = result["hourly_pnl"].get(time)
            if pnl is not None:
                hourly_strs.append(f"{pnl:+.2f}")
            else:
                hourly_strs.append("N/A")
        
        print(f"{result['date']:<12} | {side:<8} | {result['atm_strike']:<8} | {entry_str:<46} | " + " | ".join(f"{s:<10}" for s in hourly_strs))
    
    print(f"{'='*185}\n")


def print_leg_pnl_table(daily_results: List[Dict], instrument_name: str):
    """Prints the leg-wise P&L table."""
    print(f"\n{'='*110}")
    print(f"                    {instrument_name} LEG-WISE P&L TABLE")
    print(f"{'='*110}")
    print(f"{'Date':<12} | {'Side':<6} | {'ATM Long P&L':<14} | {'Short S1 P&L':<14} | {'Short S2 P&L':<14} | {'Hedge P&L':<12} | {'Total P&L':<12}")
    print(f"{'-'*110}")
    
    for result in daily_results:
        if result.get("status") != "success":
            continue
        
        side = result.get("side", "CE")
        # Use exit time or 15:10
        exit_t = result.get("exit_time") or "15:10"
        final_leg_pnl = result.get("hourly_leg_pnl", {}).get(exit_t, {})
        if not final_leg_pnl:
            final_leg_pnl = result.get("hourly_leg_pnl", {}).get("15:10", {})
        
        atm_pnl = final_leg_pnl.get("ATM", final_leg_pnl.get("ATM_CE"))
        pnl_1 = final_leg_pnl.get("SPREAD_1", final_leg_pnl.get("ATM_1000_CE"))
        pnl_2 = final_leg_pnl.get("SPREAD_2", final_leg_pnl.get("ATM_1500_CE"))
        hedge_pnl = result.get("hedge_pnl", result.get("pe_pnl", 0))
        total = result.get("final_pnl")
        
        atm_str = f"{atm_pnl:+14.2f}" if atm_pnl is not None else f"{'N/A':<14}"
        pnl_1_str = f"{pnl_1:+14.2f}" if pnl_1 is not None else f"{'N/A':<14}"
        pnl_2_str = f"{pnl_2:+14.2f}" if pnl_2 is not None else f"{'N/A':<14}"
        hedge_str = f"{hedge_pnl:+12.2f}" if hedge_pnl is not None else f"{'N/A':<12}"
        total_str = f"{total:+12.2f}" if total is not None else f"{'N/A':<12}"
        
        print(f"{result['date']:<12} | {side:<6} | {atm_str} | {pnl_1_str} | {pnl_2_str} | {hedge_str} | {total_str}")
    
    print(f"{'='*110}\n")


def print_hourly_risk_analysis(daily_results: List[Dict], instrument_name: str):
    """Prints hourly risk analysis for each day."""
    print(f"\n{'='*140}")
    print(f"                    {instrument_name} HOURLY RISK ANALYSIS")
    print(f"{'='*140}")
    print(f"{'Date':<12} | {'Max Hourly Profit':<18} | {'Max Hourly Loss':<16} | {'Lowest MTM':<14} | {'Highest MTM':<15} | {'Final P&L':<12} | {'Max Drawdown':<14} | {'Max Profit Time':<16} | {'Max Loss Time':<15}")
    print(f"{'-'*140}")
    
    for result in daily_results:
        if result["status"] != "success":
            continue
        
        print(f"{result['date']:<12} | ", end="")
        print(f"{result['max_hourly_profit']:+18.2f} | " if result['max_hourly_profit'] is not None else f"{'N/A':<18} | ", end="")
        print(f"{result['max_hourly_loss']:+16.2f} | " if result['max_hourly_loss'] is not None else f"{'N/A':<16} | ", end="")
        print(f"{result['lowest_mtm']:+14.2f} | " if result['lowest_mtm'] is not None else f"{'N/A':<14} | ", end="")
        print(f"{result['highest_mtm']:+15.2f} | " if result['highest_mtm'] is not None else f"{'N/A':<15} | ", end="")
        print(f"{result['final_pnl']:+12.2f} | " if result['final_pnl'] is not None else f"{'N/A':<12} | ", end="")
        print(f"{result['max_intraday_drawdown']:+14.2f} | " if result['max_intraday_drawdown'] is not None else f"{'N/A':<14} | ", end="")
        print(f"{result['max_profit_time']:<16} | {result['max_loss_time']:<15}")
    
    print(f"{'='*140}\n")


def print_30day_summary(summary: Dict[str, Any], instrument_name: str):
    """Prints the 30-day summary statistics."""
    print(f"\n{'='*80}")
    print(f"                    {instrument_name} 30-DAY SUMMARY")
    print(f"{'='*80}")
    print(f"Total P&L              : Rs. {summary['total_pnl']:+,.2f}")
    print(f"Average Daily P&L      : Rs. {summary['avg_daily_pnl']:+,.2f}")
    print(f"Median Daily P&L       : Rs. {summary['median_daily_pnl']:+,.2f}")
    print(f"Profitable Days        : {summary['profitable_days']} ({summary['win_rate']:.1f}%)")
    print(f"Losing Days            : {summary['losing_days']}")
    print(f"Maximum Single-Day Profit : Rs. {summary['max_single_day_profit']:+,.2f}")
    print(f"Maximum Single-Day Loss  : Rs. {summary['max_single_day_loss']:+,.2f}")
    print(f"Maximum Drawdown       : Rs. {summary['max_drawdown']:+,.2f}")
    print(f"Profit Factor          : {summary['profit_factor']:.2f}")
    print(f"Average Winning Day    : Rs. {summary['avg_winning_day']:+,.2f}")
    print(f"Average Losing Day     : Rs. {summary['avg_losing_day']:+,.2f}")
    print(f"Consecutive Winning Days: {summary['consecutive_wins']}")
    print(f"Consecutive Losing Days : {summary['consecutive_losses']}")
    print(f"Total Days Analyzed    : {summary['total_days']}")
    print(f"{'='*80}\n")


def print_hourly_performance_summary(hourly_stats: Dict[str, Dict[str, Any]], instrument_name: str):
    """Prints hourly performance summary table."""
    print(f"\n{'='*120}")
    print(f"                    {instrument_name} HOURLY PERFORMANCE SUMMARY")
    print(f"{'='*120}")
    print(f"{'Time':<8} | {'Average P&L':<14} | {'Median P&L':<12} | {'Profitable %':<14} | {'Losing %':<10} | {'Best P&L':<12} | {'Worst P&L':<12}")
    print(f"{'-'*120}")
    
    for time in MONITORING_TIMES:
        stats = hourly_stats[time]
        if stats["count"] > 0:
            print(f"{time:<8} | {stats['average_pnl']:+14.2f} | {stats['median_pnl']:+12.2f} | {stats['profitable_pct']:>13.1f}% | {stats['losing_pct']:>9.1f}% | {stats['best_pnl']:+12.2f} | {stats['worst_pnl']:+12.2f}")
        else:
            print(f"{time:<8} | {'N/A':<14} | {'N/A':<12} | {'N/A':<14} | {'N/A':<10} | {'N/A':<12} | {'N/A':<12}")
    
    print(f"{'='*120}\n")


def print_spread_adjustments(daily_results: List[Dict], instrument_name: str):
    """Prints spread adjustment and hedging details for days with adjustments."""
    print(f"\n{'='*100}")
    print(f"                    {instrument_name} SPREAD ADJUSTMENT & HEDGING DETAILS")
    print(f"{'='*100}")
    
    adjustment_days = [r for r in daily_results if r.get("status") == "success" and r.get("spread_adjustments")]
    
    if not adjustment_days:
        print("No spread adjustments or hedging occurred during the backtest period.")
        print(f"{'='*100}\n")
        return
    
    for result in adjustment_days:
        side = result.get("side", "CE")
        print(f"\nDate: {result['date']} | Side: {side} | ATM Strike: {result['atm_strike']}")
        print(f"{'-'*80}")
        for adj in result['spread_adjustments']:
            adj_type = adj.get('type', '')
            if 'HEDGE' in adj_type and 'FAILED' not in adj_type:
                h_type = adj.get('hedge_type', 'PE' if 'PE' in adj_type else 'CE')
                h_strike = adj.get('hedge_strike', adj.get('pe_strike'))
                h_entry = adj.get('hedge_entry_price', adj.get('pe_entry_price', 0.0))
                h_mult = adj.get('hedge_multiplier', 1)
                print(f"  🛡️ {h_type} HEDGE AT {adj['time']}")
                print(f"  {h_type} Strike: {h_strike}")
                print(f"  {h_type} Entry Price: {h_entry:.2f}")
                print(f"  {h_type} Multiplier: {h_mult}x")
                print(f"  Reason: {adj['reason']}")
                pnl_value = adj.get('pnl_at_hedge', 0)
                print(f"  P&L at hedge: Rs.{pnl_value:+,.2f}")
            elif 'FAILED' in adj_type:
                h_type = adj.get('hedge_type', 'PE' if 'PE' in adj_type else 'CE')
                h_strike = adj.get('hedge_strike', adj.get('pe_strike'))
                print(f"  ❌ {h_type} HEDGE FAILED AT {adj['time']}")
                print(f"  {h_type} Strike: {h_strike}")
                print(f"  Reason: {adj['reason']}")
                pnl_value = adj.get('pnl_at_attempt', 0)
                print(f"  P&L at attempt: Rs.{pnl_value:+,.2f}")
            elif 'STOP_LOSS' in adj_type:
                print(f"  🚨 STOP-LOSS EXIT AT {adj['time']}")
                print(f"  Reason: {adj['reason']}")
                pnl_value = adj.get('pnl_at_exit', 0)
                print(f"  P&L at exit: Rs.{pnl_value:+,.2f}")
            else:
                print(f"  🔄 SPREAD ADJUSTMENT AT {adj['time']}")
                print(f"  Spread 2: {adj['old_spread_2']} → {adj['new_spread_2']}")
                print(f"  Reason: {adj['reason']}")
                pnl_value = adj.get('pnl_at_adjustment', adj.get('pnl_at_exit', 0))
                print(f"  P&L at adjustment: Rs.{pnl_value:+,.2f}")
            print()
        
        # Add Hedge P&L if hedge was bought
        if result.get('hedge_bought') or result.get('pe_bought'):
            h_type = result.get('hedge_type', 'PE' if result.get('pe_bought') else 'CE')
            h_pnl = result.get('hedge_pnl', result.get('pe_pnl', 0))
            print(f"  📊 {h_type} Final P&L: Rs.{h_pnl:+,.2f}")
            print()
    
    print(f"{'='*100}\n")


def save_results_to_csv(daily_results: List[Dict], instrument_name: str, custom_date: Optional[datetime] = None):
    """Saves detailed results to CSV."""
    if custom_date:
        date_suffix = custom_date.strftime("%Y%m%d")
    else:
        date_suffix = datetime.now().strftime("%Y%m%d")
    
    csv_file = DATA_ROOT / f"backtest_3leg_{instrument_name}_{date_suffix}.csv"
    
    if not daily_results:
        return
    
    # Determine fieldnames from successful results
    fieldnames = [
        "date", "instrument", "side", "spot_915_open", "spot_930", "atm_strike", "strike_1", "strike_2",
        "entry_atm", "entry_spread_1", "entry_spread_2",
        "pnl_10_00", "pnl_11_00", "pnl_12_00", "pnl_13_00", "pnl_14_00", "pnl_15_00", "pnl_15_10",
        "final_pnl", "max_hourly_profit", "max_hourly_loss", "lowest_mtm", "highest_mtm",
        "max_intraday_drawdown", "max_profit_time", "max_loss_time", "lots", "lot_size", "total_quantity",
        "spread_adjustments", "exit_triggered", "exit_time", "exit_reason",
        "hedge_bought", "hedge_type", "hedge_strike", "hedge_entry_price", "hedge_pnl"
    ]
    
    with open(csv_file, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        
        for result in daily_results:
            if result["status"] != "success":
                row = {k: "" for k in fieldnames}
                row["date"] = result.get("date", "")
                row["instrument"] = instrument_name
                writer.writerow(row)
                continue
            
            entry_p = result.get("entry_prices", {})
            side = result.get("side", "CE")
            row = {
                "date": result["date"],
                "instrument": instrument_name,
                "side": side,
                "spot_915_open": result.get("spot_915_open", ""),
                "spot_930": result.get("spot_930", ""),
                "atm_strike": result["atm_strike"],
                "strike_1": result.get("strike_1", ""),
                "strike_2": result.get("strike_2", ""),
                "entry_atm": entry_p.get("ATM", entry_p.get(f"ATM_{side}", "")),
                "entry_spread_1": entry_p.get("SPREAD_1", entry_p.get(f"ATM_1000_{side}", "")),
                "entry_spread_2": entry_p.get("SPREAD_2", entry_p.get(f"ATM_1500_{side}", "")),
                "final_pnl": result["final_pnl"],
                "max_hourly_profit": result["max_hourly_profit"],
                "max_hourly_loss": result["max_hourly_loss"],
                "lowest_mtm": result["lowest_mtm"],
                "highest_mtm": result["highest_mtm"],
                "max_intraday_drawdown": result["max_intraday_drawdown"],
                "max_profit_time": result["max_profit_time"],
                "max_loss_time": result["max_loss_time"],
                "lots": result["lots"],
                "lot_size": result["lot_size"],
                "total_quantity": result["total_quantity"],
                "spread_adjustments": len(result.get("spread_adjustments", [])),
                "exit_triggered": result.get("exit_triggered", False),
                "exit_time": result.get("exit_time", ""),
                "exit_reason": result.get("exit_reason", ""),
                "hedge_bought": result.get("hedge_bought", result.get("pe_bought", False)),
                "hedge_type": result.get("hedge_type", "PE" if result.get("pe_bought") else ""),
                "hedge_strike": result.get("hedge_strike", result.get("pe_strike", "")),
                "hedge_entry_price": result.get("hedge_entry_price", result.get("pe_entry_price", "")),
                "hedge_pnl": result.get("hedge_pnl", result.get("pe_pnl", 0)),
            }
            
            for time in MONITORING_TIMES:
                row[f"pnl_{time.replace(':', '_')}"] = result["hourly_pnl"].get(time)
            
            writer.writerow(row)
    
    print(f"[+] Detailed results saved to: {csv_file}")


# ============================================================
# MAIN BACKTEST FUNCTION
# ============================================================

def run_backtest(instrument_name: str, custom_date: Optional[datetime] = None, days_back: int = 30, lots: int = 5, side: str = "AUTO"):
    """Runs the complete backtest for a single instrument."""
    inst_config = INSTRUMENTS_CONFIG[instrument_name]
    
    print(f"\n{'='*80}")
    print(f"  3-LEG OPTIONS STRATEGY BACKTEST: {instrument_name} ({side.upper()} MODE)")
    print(f"{'='*80}")
    print(f"  CE Mode: BUY ATM CE, SELL ATM+1000 CE, SELL ATM+2500 CE | PE Hedge: ATM-1000")
    print(f"  PE Mode: BUY ATM PE, SELL ATM-1000 PE, SELL ATM-2500 PE | CE Hedge: ATM+1000")
    print(f"  Decision (AUTO): 9:30 Spot >= 9:15 Open -> CE, else PE")
    print(f"  Entry: 9:30 AM | Exit: 3:10 PM")
    print(f"  Dynamic Exit: If loss > {inst_config['loss_threshold']}, move spread 2 closer by {inst_config['spread_adjustment']} pts")
    print(f"  Exit Condition: If spread 2 touches/crosses spread 1, exit position")
    print(f"  HARD STOP-LOSS: If loss > {inst_config.get('stop_loss_threshold', 10000)}, exit immediately")
    print(f"  Hedging: After 11:00, if position negative, buy hedge contract")
    print(f"  Main-Only P&L: Hours 12, 13, 14 calculate main legs only if hedge bought")
    print(f"  Position Size: {lots} lot(s) (lot size fetched dynamically)")
    print(f"{'='*80}\n")
    
    # Initialize API
    print("[1/5] Authenticating with Angel One SmartAPI...")
    smart_api = get_smartapi_session()
    print("      ✓ Connected successfully.")
    
    # Get trading days
    print("[2/5] Determining trading dates...")
    trading_days = get_trading_days(smart_api, custom_date, days_back)
    print(f"      ✓ Selected {len(trading_days)} trading days:")
    print(f"        {trading_days[0]}  --->  {trading_days[-1]}")
    
    # Setup cache directories
    shared_cache_dir = ROOT / "backtest" / "cache_3leg"
    shared_cache_dir.mkdir(parents=True, exist_ok=True)
    
    instrument_cache_dir = shared_cache_dir / instrument_name.lower()
    instrument_cache_dir.mkdir(parents=True, exist_ok=True)
    
    # Load instrument master (with caching)
    print("[3/5] Loading instrument master...")
    
    # Pre-load and cache the instrument master once to show filtering info
    if trading_days:
        _, _, _ = load_instrument_map(instrument_name, None, trading_days[0], shared_cache_dir, verbose=True)
    
    # Now it's cached, so subsequent calls will be silent and fast
    master_instruments = None  # Will use cached version
    
    # Run daily backtests
    print(f"\n[4/5] Executing daily simulations...\n")
    
    daily_results = []
    for idx, day_str in enumerate(trading_days, 1):
        print(f"[{idx:02d}/{len(trading_days)}] Processing {day_str}...", end=" ", flush=True)
        
        try:
            # Load option map for this date (with caching, silent for subsequent calls)
            # Use shared cache for instrument master, instrument-specific cache for data
            shared_cache_dir = ROOT / "backtest" / "cache_3leg"
            option_map, expiry, lot_size = load_instrument_map(instrument_name, master_instruments, day_str, shared_cache_dir, verbose=False)
            
            # Fetch option data
            day_data = fetch_option_data(smart_api, day_str, option_map, instrument_name, instrument_cache_dir, lot_size, side=side)
            
            if not day_data:
                print("❌ No data available")
                daily_results.append({"status": "error", "date": day_str, "message": "No data available", "instrument": instrument_name})
                continue
            
            # Run strategy
            result = run_daily_strategy(day_str, day_data, instrument_name, lots, side=side)
            if result and isinstance(result, dict):
                daily_results.append(result)
            else:
                print("❌ Strategy execution returned invalid result")
                daily_results.append({"status": "error", "date": day_str, "message": "Invalid strategy result", "instrument": instrument_name})
            
            if result["status"] == "success":
                if result['final_pnl'] is not None:
                    pnl_str = f"+Rs.{result['final_pnl']:,.2f}" if result['final_pnl'] >= 0 else f"-Rs.{abs(result['final_pnl']):,.2f}"
                    status = "🟢" if result['final_pnl'] > 0 else ("🔴" if result['final_pnl'] < 0 else "⚪")
                    
                    # Show side and adjustments
                    trade_side = result.get('side', 'CE')
                    adjustments = []
                    if result.get('spread_adjustments'):
                        hedges = sum(1 for adj in result['spread_adjustments'] if 'HEDGE' in adj.get('type', '') and 'FAILED' not in adj.get('type', ''))
                        hedges_failed = sum(1 for adj in result['spread_adjustments'] if 'FAILED' in adj.get('type', ''))
                        spread_adj = len(result['spread_adjustments']) - hedges - hedges_failed
                        h_type = result.get('hedge_type', 'Hedge')
                        if spread_adj > 0:
                            adjustments.append(f"🔄 {spread_adj} spread adj(s)")
                        if hedges > 0:
                            adjustments.append(f"🛡️ {hedges} {h_type} hedge(s)")
                        if hedges_failed > 0:
                            adjustments.append(f"❌ {hedges_failed} {h_type} hedge failed")
                    
                    side_tag = f"[{trade_side}]"
                    if result.get('exit_triggered'):
                        print(f"{status} {side_tag} Final P&L: {pnl_str} | 🚫 Exit at {result['exit_time']}: {result['exit_reason']}")
                    elif adjustments:
                        print(f"{status} {side_tag} Final P&L: {pnl_str} | {' | '.join(adjustments)}")
                    else:
                        print(f"{status} {side_tag} Final P&L: {pnl_str}")
                else:
                    print("⚪ Final P&L: N/A")
            else:
                print(f"❌ {result['message']}")
        
        except Exception as e:
            print(f"❌ Error: {e}")
            daily_results.append({"status": "error", "date": day_str, "message": str(e), "instrument": instrument_name})
    
    # Calculate summaries
    print(f"\n[5/5] Generating reports...\n")
    
    summary = calculate_30day_summary(daily_results)
    hourly_stats = calculate_hourly_performance(daily_results)
    
    # Print all reports
    print_daily_table(daily_results, instrument_name)
    print_leg_pnl_table(daily_results, instrument_name)
    print_hourly_risk_analysis(daily_results, instrument_name)
    
    if summary:
        print_30day_summary(summary, instrument_name)
    
    print_hourly_performance_summary(hourly_stats, instrument_name)
    print_spread_adjustments(daily_results, instrument_name)
    
    # Save results
    save_results_to_csv(daily_results, instrument_name, custom_date)
    
    return daily_results, summary, hourly_stats


def main():
    parser = argparse.ArgumentParser(description="3-Leg Options Strategy Backtest with Dynamic Spread Adjustment & Hedging")
    parser.add_argument("--days", "-d", type=int, default=30, 
                        help="Number of trading days to backtest (default: 30)")
    parser.add_argument("--custom-date", type=str, 
                        help="Custom end date in YYYY-MM-DD format (default: today)")
    parser.add_argument("--lots", "-l", type=int, default=5,
                        help="Number of lots to trade (default: 5)")
    parser.add_argument("--side", "-s", choices=["AUTO", "CE", "PE"], default="AUTO",
                        help="Strategy side: AUTO (decide based on 9:30 Spot vs 9:15 Open), CE (Bullish), or PE (Bearish) (default: AUTO)")
    
    args = parser.parse_args()
    
    # Parse custom date if provided
    custom_date = None
    if args.custom_date:
        try:
            custom_date = datetime.strptime(args.custom_date, "%Y-%m-%d")
        except ValueError:
            print(f"Invalid date format: {args.custom_date}. Use YYYY-MM-DD.")
            return
    
    # Only run BANKNIFTY
    instrument = "BANKNIFTY"
    daily_results, summary, hourly_stats = run_backtest(instrument, custom_date, args.days, args.lots, side=args.side)


if __name__ == "__main__":
    main()