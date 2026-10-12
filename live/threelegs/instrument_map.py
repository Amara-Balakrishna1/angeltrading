"""
Instrument Map for 3-Leg Options Strategy.
Downloads & caches OpenAPIScripMaster, finds expiry/strikes.
"""

import sys
from pathlib import Path

# Add parent directory to path for imports
ROOT = Path(__file__).resolve().parent.parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import json
import requests
from datetime import datetime
from typing import Dict, Any, Optional, Tuple, Union

from live.threelegs.config import INSTRUMENT_CONFIG

# Cache directory
ROOT_DIR = Path(__file__).resolve().parent.parent.parent
CACHE_DIR = ROOT_DIR / "backtest" / "cache_3leg"
CACHE_DIR.mkdir(parents=True, exist_ok=True)

_scrip_master_cache: Optional[list] = None
_instrument_cache_file = CACHE_DIR / "instrument_master.json"


def download_instrument_map(force_reload: bool = True) -> list:
    """Downloads and caches the Angel One OpenAPIScripMaster."""
    global _scrip_master_cache

    if _scrip_master_cache is not None and not force_reload:
        return _scrip_master_cache

    # Always force reload to get fresh data
    url = "https://margincalculator.angelone.in/OpenAPI_File/files/OpenAPIScripMaster.json"
    print("[*] Downloading fresh OpenAPIScripMaster for contract lookup...")
    resp = requests.get(url, timeout=35, verify=False)
    resp.raise_for_status()
    _scrip_master_cache = resp.json()
    print(f"[+] Loaded {len(_scrip_master_cache)} instruments into cache.")
    
    # Debug: Check if lot sizes are present
    lot_size_count = sum(1 for item in _scrip_master_cache if item.get("lotsize"))
    print(f"[+] {lot_size_count} instruments have lot size data")
    
    return _scrip_master_cache


def find_expiry_and_strikes(
    spot: float,
    side: str,
    date: Optional[datetime] = None,
    instrument_name: str = "BANKNIFTY"
) -> Dict[str, Any]:
    """
    Finds the nearest expiry and strikes for the 3-leg strategy.

    Args:
        spot: Current spot price
        side: "CE" or "PE"
        date: Reference date for expiry (default: today)
        instrument_name: Instrument name (default: BANKNIFTY)

    Returns:
        Dict with atm, strike_1, strike_2, hedge_strike, option_map, lot_size
    """
    inst_config = INSTRUMENT_CONFIG[instrument_name]
    scrip_master = download_instrument_map()

    target_date = date.date() if date else datetime.now().date()

    # Pre-filter to relevant instruments
    matching = [
        item for item in scrip_master
        if item.get("name") == instrument_name
        and item.get("exch_seg") == inst_config["option_exchange"]
        and item.get("instrumenttype") == "OPTIDX"
        and item.get("expiry")
    ]

    # Find nearest expiry that has lot size data
    valid_expiries = []
    all_expiries = []
    expiry_lot_sizes = {}  # expiry -> list of lot sizes
    
    for item in matching:
        try:
            expiry_str = item.get("expiry")
            if expiry_str:
                exp_date = datetime.strptime(expiry_str, "%d%b%Y").date()
                all_expiries.append(exp_date)
                
                # Only consider future expiries (not expired)
                if exp_date >= target_date:
                    valid_expiries.append(exp_date)
                    
                    # Track lot sizes for this expiry
                    item_lot_size = item.get("lotsize")
                    if item_lot_size and isinstance(item_lot_size, (int, float)):
                        if expiry_str not in expiry_lot_sizes:
                            expiry_lot_sizes[expiry_str] = []
                        expiry_lot_sizes[expiry_str].append(int(item_lot_size))
        except (ValueError, TypeError):
            pass

    print(f"[*] Found {len(all_expiries)} total expiries, {len(valid_expiries)} future expiries")
    print(f"[*] Expiries with lot size data: {len(expiry_lot_sizes)}")
    
    # Select expiry that is future AND has lot size data
    if expiry_lot_sizes:
        # Find the minimum future expiry that has lot size data
        selected_expiry_str = min(expiry_lot_sizes.keys(), 
                                  key=lambda x: datetime.strptime(x, "%d%b%Y").date())
        selected_expiry_date = datetime.strptime(selected_expiry_str, "%d%b%Y").date()
        print(f"[+] Selected future expiry with lot size: {selected_expiry_str}")
    elif valid_expiries:
        # Fallback: use minimum future expiry even without lot size data
        selected_expiry_date = min(valid_expiries)
        selected_expiry_str = selected_expiry_date.strftime("%d%b%Y").upper()
        print(f"[!] Warning: Selected expiry without lot size data: {selected_expiry_str}")
    else:
        # If no future expiries found, use the most recent expiry as fallback
        if all_expiries:
            selected_expiry_date = max(all_expiries)  # Most recent expiry
            selected_expiry_str = selected_expiry_date.strftime("%d%b%Y").upper()
            print(f"[!] Warning: No future expiries found, using most recent: {selected_expiry_str}")
        else:
            selected_expiry_date = target_date
            selected_expiry_str = target_date.strftime("%d%b%y").upper()
            print(f"[!] Warning: No expiries found, using current date: {selected_expiry_str}")

    # Build option map for selected expiry
    option_map = {}
    lot_sizes_seen = set()

    for item in matching:
        if item.get("expiry") == selected_expiry_str:
            strike = float(item["strike"]) / 100.0
            opt_type = item["symbol"][-2:]
            item_lot_size = item.get("lotsize")
            
            # Store contract even if lot size is missing (will use common lot size)
            option_map[(strike, opt_type)] = {
                "token": str(item["token"]),
                "symbol": item["symbol"],
                "strike": strike,
                "option_type": opt_type,
                "expiry": selected_expiry_str,
                "lot_size": item_lot_size  # Can be None if not in data
            }
            
            # Track lot sizes that are present
            if item_lot_size and isinstance(item_lot_size, (int, float)):
                lot_sizes_seen.add(int(item_lot_size))

    # Validate we found at least some lot sizes
    if not lot_sizes_seen:
        print(f"[!] No lot sizes found in instrument data for {instrument_name} expiry {selected_expiry_str}")
        print(f"[*] Sample instruments checked: {len(matching)}")
        if matching:
            sample_item = matching[0]
            print(f"[!] Sample instrument keys: {list(sample_item.keys())}")
            print(f"[!] Sample instrument data: {sample_item}")
        raise RuntimeError(f"No lot sizes found in instrument data for {instrument_name} expiry {selected_expiry_str}. Check if OpenAPIScripMaster.json has lotsize field.")

    # Use the most common lot size (there should typically be only one)
    lot_size = max(set(lot_sizes_seen), key=lot_sizes_seen.count)

    # Apply the common lot size to contracts that were missing it
    for key, contract in option_map.items():
        if contract["lot_size"] is None:
            contract["lot_size"] = lot_size

    # Log lot size information for debugging
    print(f"[*] Lot sizes found in instrument data: {sorted(lot_sizes_seen)}")
    print(f"[*] Using lot size: {lot_size}")

    # Calculate strikes
    atm_strike = int(round(spot / 500) * 500)

    if side.upper() == "CE":
        strike_1 = atm_strike + inst_config["spread_1"]  # ATM + 1000
        strike_2 = atm_strike + inst_config["spread_2"]  # ATM + 2500
        hedge_strike = int(round((atm_strike - 1000) / 500) * 500)
        hedge_type = "PE"
    else:
        strike_1 = atm_strike - inst_config["spread_1"]  # ATM - 1000
        strike_2 = atm_strike - inst_config["spread_2"]  # ATM - 2500
        hedge_strike = int(round((atm_strike + 1000) / 500) * 500)
        hedge_type = "CE"

    return {
        "atm": atm_strike,
        "strike_1": strike_1,
        "strike_2": strike_2,
        "hedge_strike": hedge_strike,
        "hedge_type": hedge_type,
        "option_map": option_map,
        "lot_size": lot_size,
        "expiry": selected_expiry_str,
        "exchange": inst_config["option_exchange"],
    }
