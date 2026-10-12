"""
Contract Finder for Day Breakout Strategy.
Resolves dynamic nearest expiry and option contracts (tokens, symbols, lot sizes)
using Angel One OpenAPIScripMaster to avoid searchScrip rate limits.
"""

import sys
from pathlib import Path

# Add parent directory to path for imports
ROOT = Path(__file__).resolve().parent.parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from datetime import datetime
from typing import Dict, Any, Optional, Tuple, List
import requests
import config

_scrip_master_cache: Optional[List[Dict[str, Any]]] = None
_nearest_expiry_cache: Dict[str, str] = {}


def _get_common_lot_size(scrip_master: List[Dict[str, Any]], symbol_base: str, exchange: str, expiry: str) -> Optional[int]:
    """
    Gets the most common lot size for a given symbol/exchange/expiry from scrip master.
    Used when individual contracts don't have lot size data.
    """
    lot_sizes = []
    for item in scrip_master:
        if (item.get("name") == symbol_base and 
            item.get("exch_seg") == exchange and 
            item.get("instrumenttype") == "OPTIDX" and 
            item.get("expiry") == expiry):
            lot_size = item.get("lotsize")
            if lot_size and isinstance(lot_size, (int, float)):
                lot_sizes.append(int(lot_size))
    
    if not lot_sizes:
        return None
    
    # Return the most common lot size
    return max(set(lot_sizes), key=lot_sizes.count)


def load_scrip_master(force_reload: bool = False) -> List[Dict[str, Any]]:
    """Loads and caches the Angel One OpenAPIScripMaster, prioritizing local live/OpenAPIScripMaster.json."""
    global _scrip_master_cache
    if _scrip_master_cache is not None and not force_reload:
        return _scrip_master_cache

    local_file = ROOT / "live" / "OpenAPIScripMaster.json"
    if local_file.exists() and not force_reload:
        import json
        try:
            with open(local_file, "r", encoding="utf-8") as f:
                _scrip_master_cache = json.load(f)
            return _scrip_master_cache
        except Exception as e:
            print(f"[!] Warning reading {local_file}: {e}")

    url = "https://margincalculator.angelbroking.com/OpenAPI_File/files/OpenAPIScripMaster.json"
    disable_ssl = getattr(config, "disable_ssl", True)
    print("[*] Downloading OpenAPIScripMaster for contract lookup...")
    resp = requests.get(url, timeout=35, verify=not disable_ssl)
    resp.raise_for_status()
    _scrip_master_cache = resp.json()
    try:
        import json
        with open(local_file, "w", encoding="utf-8") as f:
            json.dump(_scrip_master_cache, f)
    except Exception:
        pass
    print(f"[+] Loaded {len(_scrip_master_cache)} instruments into cache.")
    return _scrip_master_cache


def get_instrument_lot_size(symbol_base: str, exchange: Optional[str] = None) -> int:
    """
    Extracts the dynamic lot size directly from OpenAPIScripMaster JSON.
    Never relies on hardcoded defaults.
    """
    scrip_master = load_scrip_master(force_reload=False)
    lot_sizes = []
    for item in scrip_master:
        if item.get("name") == symbol_base and item.get("instrumenttype") == "OPTIDX":
            if exchange and item.get("exch_seg") != exchange:
                continue
            ls = item.get("lotsize")
            if ls:
                try:
                    val = int(ls)
                    if val > 0:
                        lot_sizes.append(val)
                except (ValueError, TypeError):
                    pass
    if not lot_sizes:
        raise RuntimeError(f"Could not find lot size for {symbol_base} in OpenAPIScripMaster.json")
    from collections import Counter
    return Counter(lot_sizes).most_common(1)[0][0]


def get_nearest_expiry(
    symbol_base: str,
    exchange: str,
    for_date: Optional[datetime] = None,
    scrip_master: Optional[List[Dict[str, Any]]] = None
) -> Tuple[str, datetime]:
    """
    Finds the nearest non-expired weekly expiry date for the given index.
    Returns (expiry_str_like_15SEP26, expiry_date_obj).
    """
    cache_key = f"{symbol_base}_{exchange}"
    if cache_key in _nearest_expiry_cache and for_date is None:
        return _nearest_expiry_cache[cache_key]

    if scrip_master is None:
        scrip_master = load_scrip_master()

    ref_date = for_date.date() if for_date else datetime.now().date()
    valid_expiries = set()
    expiry_lot_sizes = {}  # expiry -> list of lot sizes

    for item in scrip_master:
        if (
            item.get("name") == symbol_base
            and item.get("exch_seg") == exchange
            and item.get("instrumenttype") == "OPTIDX"
        ):
            exp_str = item.get("expiry")
            if exp_str:
                try:
                    exp_date = datetime.strptime(exp_str, "%d%b%Y").date()
                    if exp_date >= ref_date:
                        valid_expiries.add(exp_date)
                        
                        # Track lot sizes for this expiry
                        item_lot_size = item.get("lotsize")
                        if item_lot_size and isinstance(item_lot_size, (int, float)):
                            if exp_str not in expiry_lot_sizes:
                                expiry_lot_sizes[exp_str] = []
                            expiry_lot_sizes[exp_str].append(int(item_lot_size))
                except ValueError:
                    pass

    # Select expiry that is future AND has lot size data
    if expiry_lot_sizes:
        # Find the minimum future expiry that has lot size data
        nearest_str = min(expiry_lot_sizes.keys(), 
                         key=lambda x: datetime.strptime(x, "%d%b%Y").date())
        nearest_date = datetime.strptime(nearest_str, "%d%b%Y").date()
        _nearest_expiry_cache[cache_key] = (nearest_str, nearest_date)
        print(f"[+] Selected expiry with lot size: {nearest_str}")
        return nearest_str, nearest_date
    elif valid_expiries:
        # Fallback: use minimum future expiry even without lot size data
        nearest_date = min(valid_expiries)
        nearest_str = nearest_date.strftime("%d%b%y").upper()
        _nearest_expiry_cache[cache_key] = (nearest_str, nearest_date)
        print(f"[!] Warning: Selected expiry without lot size data: {nearest_str}")
        return nearest_str, nearest_date


def find_option_contract(
    symbol_base: str,
    exchange: str,
    strike: float,
    option_type: str,
    expiry_date: Optional[datetime] = None,
    scrip_master: Optional[List[Dict[str, Any]]] = None
) -> Optional[Dict[str, Any]]:
    """
    Finds exact option contract matching base symbol, exchange, strike, option_type and expiry.
    """
    if scrip_master is None:
        scrip_master = load_scrip_master()

    if expiry_date is None:
        _, exp_obj = get_nearest_expiry(symbol_base, exchange, scrip_master=scrip_master)
    else:
        exp_obj = expiry_date.date() if isinstance(expiry_date, datetime) else expiry_date

    for item in scrip_master:
        if (
            item.get("name") == symbol_base
            and item.get("exch_seg") == exchange
            and item.get("instrumenttype") == "OPTIDX"
        ):
            exp_str = item.get("expiry")
            if not exp_str:
                continue
            try:
                item_exp = datetime.strptime(exp_str, "%d%b%Y").date()
                if item_exp != exp_obj:
                    continue

                item_strike = float(item.get("strike", 0)) / 100.0
                item_symbol = item.get("symbol", "")
                item_opt_type = item_symbol[-2:].upper()

                if abs(item_strike - strike) < 0.1 and item_opt_type == option_type.upper():
                    try:
                        lot_size = int(item.get("lotsize", 0))
                    except (ValueError, TypeError):
                        lot_size = 0

                    return {
                        "token": str(item["token"]),
                        "symbol": str(item["symbol"]),
                        "tradingsymbol": str(item.get("symbol")),
                        "strike": int(strike),
                        "option_type": option_type.upper(),
                        "expiry": item_exp.strftime("%d%b%y").upper(),
                        "lot_size": lot_size,
                        "exchange": exchange,
                        "symbol_base": symbol_base,
                    }
            except Exception:
                continue

    return None


def find_strategy_contracts(
    symbol_base: str,
    spot_price: float,
    direction: str,  # "BULLISH" or "BEARISH"
    strike_step: int,
    spread_width: int,
    option_exchange: str,
    lots: int = 5,
    structure: str = "spread"  # "spread" or "naked"
) -> Dict[str, Any]:
    """
    Constructs contract details for 2-leg spread or 1-leg naked option.

    Structure:
      - spread: 
          BULLISH -> Bull Call Spread (Buy ATM CE, Sell [ATM + spread_width] CE)
          BEARISH -> Bear Put Spread  (Buy ATM PE, Sell [ATM - spread_width] PE)
      - naked:
          BULLISH -> Naked Call Buy (Buy ATM CE)
          BEARISH -> Naked Put Buy  (Buy ATM PE)
    """
    struct_clean = structure.lower().strip()
    if struct_clean not in ("spread", "naked"):
        struct_clean = "spread"

    atm_strike = int(round(spot_price / strike_step) * strike_step)
    scrip_master = load_scrip_master()
    expiry_str, expiry_date = get_nearest_expiry(symbol_base, option_exchange, scrip_master=scrip_master)

    is_bullish = direction.upper() in ("BULLISH", "BUY_CALL_SPREAD", "CE")
    opt_type = "CE" if is_bullish else "PE"

    if struct_clean == "naked":
        strategy_name = f"{symbol_base} Naked Call {opt_type}" if is_bullish else f"{symbol_base} Naked Put {opt_type}"
        buy_strike = atm_strike
        sell_strike = None
    else:
        if is_bullish:
            strategy_name = f"{symbol_base} Bull Call Spread ({spread_width} pts)"
            buy_strike = atm_strike
            sell_strike = atm_strike + spread_width
        else:
            strategy_name = f"{symbol_base} Bear Put Spread ({spread_width} pts)"
            buy_strike = atm_strike
            sell_strike = atm_strike - spread_width

    buy_contract = find_option_contract(
        symbol_base=symbol_base,
        exchange=option_exchange,
        strike=buy_strike,
        option_type=opt_type,
        expiry_date=expiry_date,
        scrip_master=scrip_master
    )
    if not buy_contract:
        raise RuntimeError(f"Could not find BUY leg contract for {symbol_base} {buy_strike} {opt_type} ({expiry_str})")

    actual_lot_size = buy_contract.get("lot_size")
    if not actual_lot_size:
        # If individual contract doesn't have lot size, get common lot size from scrip master
        actual_lot_size = _get_common_lot_size(scrip_master, symbol_base, option_exchange, expiry_str)
        if not actual_lot_size:
            raise RuntimeError(f"Lot size not found in contract data for {symbol_base} {buy_strike} {opt_type}")
    
    actual_lot_size = int(actual_lot_size)
    quantity = lots * actual_lot_size

    buy_leg = {
        "type": "BUY",
        "strike": buy_strike,
        "option_type": opt_type,
        "token": buy_contract["token"],
        "symbol": buy_contract["symbol"],
        "tradingsymbol": buy_contract["symbol"],
        "lots": lots,
        "lot_size": actual_lot_size,
        "quantity": quantity,
        "expiry": expiry_str,
        "entry_price": 0.0,
    }

    sell_leg = None
    if struct_clean == "spread":
        sell_contract = find_option_contract(
            symbol_base=symbol_base,
            exchange=option_exchange,
            strike=sell_strike,
            option_type=opt_type,
            expiry_date=expiry_date,
            scrip_master=scrip_master
        )
        if not sell_contract:
            raise RuntimeError(f"Could not find SELL leg contract for {symbol_base} {sell_strike} {opt_type} ({expiry_str})")

        sell_leg = {
            "type": "SELL",
            "strike": sell_strike,
            "option_type": opt_type,
            "token": sell_contract["token"],
            "symbol": sell_contract["symbol"],
            "tradingsymbol": sell_contract["symbol"],
            "lots": lots,
            "lot_size": actual_lot_size,
            "quantity": quantity,
            "expiry": expiry_str,
            "entry_price": 0.0,
        }

    return {
        "strategy_name": strategy_name,
        "structure": struct_clean.upper(),
        "direction": direction.upper(),
        "symbol_base": symbol_base,
        "expiry": expiry_str,
        "atm_strike": atm_strike,
        "spread_width": spread_width if struct_clean == "spread" else 0,
        "lots": lots,
        "lot_size": actual_lot_size,
        "quantity": quantity,
        "buy_leg": buy_leg,
        "sell_leg": sell_leg,
        "exchange": option_exchange,
    }


def find_spread_contracts(
    symbol_base: str,
    spot_price: float,
    direction: str,
    strike_step: int,
    spread_width: int,
    option_exchange: str,
    lots: int = 5
) -> Dict[str, Any]:
    """Legacy wrapper around find_strategy_contracts for spread structure."""
    return find_strategy_contracts(
        symbol_base=symbol_base,
        spot_price=spot_price,
        direction=direction,
        strike_step=strike_step,
        spread_width=spread_width,
        option_exchange=option_exchange,
        lots=lots,
        structure="spread",
    )

