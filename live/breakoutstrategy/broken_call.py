import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Auto-detect and include .venv site-packages if running with global Python
VENV_SITE_PACKAGES = ROOT / ".venv" / "Lib" / "site-packages"
if VENV_SITE_PACKAGES.exists() and str(VENV_SITE_PACKAGES) not in sys.path:
    sys.path.insert(0, str(VENV_SITE_PACKAGES))

import re
import csv
import time
from datetime import datetime
import requests
import pyotp
from SmartApi.smartConnect import SmartConnect
import config

# Cache for contract token lookup
symbol_cache = {}

# Cache for dynamic expiry lookup
_expiry_cache = {}

# Default number of lots to trade
DEFAULT_LOTS = 5


def login_smart_api():
    """Authenticates and returns a SmartConnect instance using credentials from config."""
    api_key = config.api_key.strip()
    username = config.username
    pwd = config.pwd
    token = config.token
    disable_ssl = getattr(config, "disable_ssl", True)

    smart_api = SmartConnect(api_key, disable_ssl=disable_ssl)
    totp = pyotp.TOTP(token).now()
    session = smart_api.generateSession(username, pwd, totp)
    if session.get("status"):
        refresh_token = session["data"]["refreshToken"]
        smart_api.generateToken(refresh_token)
        print("[+] Logged in to SmartAPI.")
        return smart_api
    else:
        raise RuntimeError(f"SmartAPI login failed: {session}")


def get_current_expiry(smart_api=None, symbol_base="BANKNIFTY", exchange="NFO"):
    """
    Dynamically finds the current (nearest non-expired) expiry string for the given index.
    """
    cache_key = f"{symbol_base}_{exchange}"
    if cache_key in _expiry_cache:
        return _expiry_cache[cache_key]

    try:
        url = "https://margincalculator.angelone.in/OpenAPI_File/files/OpenAPIScripMaster.json"
        disable_ssl = getattr(config, "disable_ssl", True)
        response = requests.get(url, timeout=30, verify=not disable_ssl)
        if response.status_code == 200:
            data = response.json()
            today = datetime.now().date()
            valid_expiries = []
            for item in data:
                if (
                    item.get("name") == symbol_base
                    and item.get("exch_seg") == exchange
                    and item.get("instrumenttype") == "OPTIDX"
                ):
                    exp_str = item.get("expiry")
                    if exp_str:
                        try:
                            exp_date = datetime.strptime(exp_str, "%d%b%Y").date()
                            if exp_date >= today:
                                valid_expiries.append(exp_date)
                        except ValueError:
                            pass
            if valid_expiries:
                nearest = min(valid_expiries)
                nearest_str = nearest.strftime("%d%b%y").upper()
                _expiry_cache[cache_key] = nearest_str
                return nearest_str
    except Exception as e:
        print(f"[!] Warning: Could not dynamically fetch expiry from scrip master: {e}")

    # Fallback: Current date formatted
    fallback_str = datetime.now().strftime("%d%b%y").upper()
    return fallback_str


def get_open_positions(smart_api, symbol_filter=None):
    """
    Fetches active open positions (where netqty != 0) from SmartAPI.

    Parameters:
        smart_api (SmartConnect): Authenticated SmartConnect instance.
        symbol_filter (str, optional): Substring to filter positions (e.g. "BANKNIFTY").

    Returns:
        list: List of active position dictionaries.
    """
    try:
        response = smart_api.position()
        if not response or not response.get("status"):
            return []

        positions_data = response.get("data")
        if not positions_data or not isinstance(positions_data, list):
            return []

        open_positions = []
        for pos in positions_data:
            try:
                net_qty = int(pos.get("netqty", 0))
            except (ValueError, TypeError):
                net_qty = 0

            if net_qty != 0:
                if symbol_filter:
                    tradingsymbol = pos.get("tradingsymbol", "")
                    symbolname = pos.get("symbolname", "")
                    if symbol_filter.upper() in tradingsymbol.upper() or symbol_filter.upper() in symbolname.upper():
                        open_positions.append(pos)
                else:
                    open_positions.append(pos)

        return open_positions
    except Exception as e:
        print(f"[!] Error checking open positions: {e}")
        return []


def get_symbol_token(smart_api, expiry, strike, option_type="CE", exchange="NFO", symbol_base="BANKNIFTY", cache=None):
    """Searches for and returns the symbol token and trading symbol for a given strike and option type."""
    if cache is None:
        cache = symbol_cache

    if not expiry:
        expiry = get_current_expiry(smart_api=smart_api, symbol_base=symbol_base, exchange=exchange)

    key = f"{symbol_base}{expiry}{strike}{option_type}"
    if key in cache:
        return cache[key]

    search_str = f"{symbol_base}{expiry}{strike}{option_type}"
    result = smart_api.searchScrip(exchange, search_str)
    if result.get("status") and result.get("data"):
        match = None
        for scrip in result["data"]:
            tsymbol = scrip["tradingsymbol"]
            if option_type in tsymbol and str(strike) in tsymbol:
                match = scrip
                break
        if match:
            token = str(match["symboltoken"])
            tsymbol = match["tradingsymbol"]
            cache[key] = (token, tsymbol)
            return token, tsymbol

    # Fallback search by base symbol and strike
    search_fallback = f"{symbol_base}{strike}"
    result_fb = smart_api.searchScrip(exchange, search_fallback)
    if result_fb.get("status") and result_fb.get("data"):
        for scrip in result_fb["data"]:
            tsymbol = scrip["tradingsymbol"]
            if option_type in tsymbol and str(strike) in tsymbol:
                token = str(scrip["symboltoken"])
                tsymbol = scrip["tradingsymbol"]
                cache[key] = (token, tsymbol)
                return token, tsymbol

    print(f"[!] No matching symbol found for {search_str}")
    return None, None


def get_ltp(smart_api, symbol_token, trading_symbol, exchange="NFO", retries=2, last_price=0.0):
    """Fetches the last traded price (LTP) for a given contract with retry on timeout."""
    if not symbol_token:
        return last_price
    for attempt in range(retries):
        try:
            data = smart_api.ltpData(exchange, trading_symbol, str(symbol_token))
            if data and data.get("status") and data.get("data"):
                return float(data["data"]["ltp"])
        except Exception as e:
            if attempt == retries - 1:
                print(f"[!] Error fetching LTP for {trading_symbol}: {e}")
            time.sleep(0.5)
    return last_price


def is_order_successful(response):
    """
    Validates if placeOrder response from SmartAPI was successful and generated an order ID.
    """
    if response is None:
        return False
    if isinstance(response, str) and len(response.strip()) > 0:
        return True
    if isinstance(response, dict):
        if response.get("status") is True:
            return True
        data = response.get("data")
        if data and (isinstance(data, dict) and data.get("orderid") or isinstance(data, str)):
            return True
    return False


def place_order(smart_api, trading_symbol, symbol_token, transaction_type, quantity, exchange="NFO", order_type="MARKET", product_type="INTRADAY"):
    """Places a market order via SmartAPI."""
    if not symbol_token or not trading_symbol:
        print(f"[!] Cannot place order: missing symbol or token ({trading_symbol}, {symbol_token})")
        return None

    order_params = {
        "variety": "NORMAL",
        "tradingsymbol": trading_symbol,
        "symboltoken": str(symbol_token),
        "transactiontype": transaction_type,
        "exchange": exchange,
        "ordertype": order_type,
        "producttype": product_type,
        "duration": "DAY",
        "quantity": quantity
    }
    try:
        if hasattr(smart_api, "placeOrderFullResponse"):
            response = smart_api.placeOrderFullResponse(order_params)
        else:
            response = smart_api.placeOrder(order_params)

        if is_order_successful(response):
            if isinstance(response, str):
                order_id = response
            elif isinstance(response, dict):
                order_id = (
                    response.get("data", {}).get("orderid", "SUCCESS")
                    if isinstance(response.get("data"), dict)
                    else str(response.get("data", "SUCCESS"))
                )
            else:
                order_id = "SUCCESS"
            print(f"[+] {transaction_type} order placed successfully for {trading_symbol} (Qty: {quantity}, Order ID: {order_id})")
        else:
            if isinstance(response, dict):
                error_msg = response.get("message", "Unknown error")
                error_code = response.get("errorcode", response.get("errorCode", ""))
            else:
                error_msg = str(response) if response else "No response"
                error_code = ""
            print(f"[!] {transaction_type} order FAILED for {trading_symbol}: {error_msg} (Error Code: {error_code})")
        return response
    except Exception as e:
        print(f"[!] Error placing {transaction_type} order for {trading_symbol}: {e}")
        return None


def _normalize_leg(leg, default_type="BUY", default_option_type="CE", default_lot_size=None, default_lots=DEFAULT_LOTS):
    """
    Normalizes leg input (int/float, string like '58000ce', or dict) into a standard dictionary.
    Lot size must be provided from API data, no defaults allowed.
    """
    if default_lot_size is None:
        raise RuntimeError("Lot size must be provided from API data, no defaults allowed")
    
    default_qty = default_lots * default_lot_size

    if isinstance(leg, (int, float)):
        return {
            "type": default_type.upper(),
            "strike": int(leg),
            "option_type": default_option_type.upper(),
            "quantity": default_qty,
            "lots": default_lots,
            "lot_size": default_lot_size,
            "token": None,
            "symbol": None,
            "entry_price": 0.0
        }
    elif isinstance(leg, str):
        # Support string format like "58000ce", "58000 CE", "58000"
        match = re.search(r"(\d+)\s*([A-Za-z]+)?", leg.strip())
        if match:
            strike = int(match.group(1))
            opt_type = match.group(2).upper() if match.group(2) else default_option_type.upper()
            return {
                "type": default_type.upper(),
                "strike": strike,
                "option_type": opt_type,
                "quantity": default_qty,
                "lots": default_lots,
                "lot_size": default_lot_size,
                "token": None,
                "symbol": None,
                "entry_price": 0.0
            }
        else:
            raise ValueError(f"Could not parse strike/option type from string: {leg}")
    elif isinstance(leg, dict):
        lots = int(leg.get("lots") or default_lots)
        unit_lot_size = leg.get("lot_size")
        if not unit_lot_size:
            raise RuntimeError("Lot size must be provided in leg data, no defaults allowed")
        unit_lot_size = int(unit_lot_size)
        qty = int(leg.get("quantity") or (lots * unit_lot_size))
        return {
            "type": leg.get("type", default_type).upper(),
            "strike": int(leg.get("strike", 0)),
            "option_type": leg.get("option_type", default_option_type).upper(),
            "quantity": qty,
            "lots": lots,
            "lot_size": unit_lot_size,
            "token": str(leg.get("token") or leg.get("symboltoken")) if (leg.get("token") or leg.get("symboltoken")) else None,
            "symbol": leg.get("symbol") or leg.get("tradingsymbol"),
            "entry_price": float(leg.get("entry_price", 0.0))
        }
    else:
        raise ValueError(f"Invalid leg format: {leg}. Expected strike (int/float/str) or dict.")


def square_off(smart_api, buy_leg, sell_leg, exchange="NFO"):
    """
    Squares off active positions. Exits SELL leg first (by BUYing back), then BUY leg (by SELLing).
    """
    print("\n[!] Squaring off all positions...")

    # 1. Exit SELL leg first
    if sell_leg.get("symbol") and sell_leg.get("token"):
        place_order(
            smart_api=smart_api,
            trading_symbol=sell_leg["symbol"],
            symbol_token=sell_leg["token"],
            transaction_type="BUY",
            quantity=sell_leg["quantity"],
            exchange=exchange
        )

    # 2. Exit BUY leg next
    if buy_leg.get("symbol") and buy_leg.get("token"):
        place_order(
            smart_api=smart_api,
            trading_symbol=buy_leg["symbol"],
            symbol_token=buy_leg["token"],
            transaction_type="SELL",
            quantity=buy_leg["quantity"],
            exchange=exchange
        )


DATA_DIR = ROOT / "data"
DATA_DIR.mkdir(parents=True, exist_ok=True)
TRADES_FILE = DATA_DIR / "trades.csv"
TRIGGERED_TRADES_FILE = DATA_DIR / "triggered_trades.csv"


def get_date_trades_file(date_obj=None, symbol_base="BANKNIFTY"):
    """Returns path to daily trades file: data/<DD-MM-YYYY>/trades_BN.csv or trades_SENSEX.csv"""
    if date_obj is None:
        date_obj = datetime.now()
    if isinstance(date_obj, str):
        for fmt in ("%Y-%m-%d", "%d-%m-%Y"):
            try:
                date_obj = datetime.strptime(date_obj, fmt)
                break
            except ValueError:
                continue
        else:
            date_obj = datetime.now()
    date_folder = DATA_DIR / date_obj.strftime("%d-%m-%Y")
    date_folder.mkdir(parents=True, exist_ok=True)
    suffix = "SENSEX" if "SENSEX" in str(symbol_base).upper() else "BN"
    return date_folder / f"trades_{suffix}.csv"


def get_date_triggered_trades_file(date_obj=None, symbol_base="BANKNIFTY"):
    """Returns path to daily triggered trades file: data/<DD-MM-YYYY>/triggered_trades_BN.csv or triggered_trades_SENSEX.csv"""
    if date_obj is None:
        date_obj = datetime.now()
    if isinstance(date_obj, str):
        for fmt in ("%Y-%m-%d", "%d-%m-%Y"):
            try:
                date_obj = datetime.strptime(date_obj, fmt)
                break
            except ValueError:
                continue
        else:
            date_obj = datetime.now()
    date_folder = DATA_DIR / date_obj.strftime("%d-%m-%Y")
    date_folder.mkdir(parents=True, exist_ok=True)
    suffix = "SENSEX" if "SENSEX" in str(symbol_base).upper() else "BN"
    return date_folder / f"triggered_trades_{suffix}.csv"


def record_triggered_trade(symbol_base="BANKNIFTY", structure="SPREAD", lots=1, quantity=0, trade_reason="", buy_symbol="", sell_symbol="", mode="LIVE"):
    """Logs triggered trade details immediately upon trade execution."""
    try:
        now = datetime.now()
        date_str = now.strftime("%Y-%m-%d")
        time_str = now.strftime("%H:%M:%S")

        daily_file = get_date_triggered_trades_file(now, symbol_base=symbol_base)
        headers = ["date", "time", "symbol", "structure", "lots", "quantity", "trade_reason", "buy_symbol", "sell_symbol", "mode"]
        row = [date_str, time_str, symbol_base, str(structure).upper(), str(lots), str(quantity), trade_reason, buy_symbol, sell_symbol, mode]

        for target_file in (daily_file, TRIGGERED_TRADES_FILE):
            file_exists = target_file.exists()
            with open(target_file, "a", newline="", encoding="utf-8") as f:
                import csv
                writer = csv.writer(f)
                if not file_exists:
                    writer.writerow(headers)
                writer.writerow(row)
    except Exception as e:
        print(f"[!] Failed to record triggered trade: {e}")


def record_trade(buy_item, sell_item, total_pnl, max_pnl, status, symbol_base="BANKNIFTY", expiry="", start_time=None, end_time=None):
    """
    Appends closed trade details to data/<date>/trades_<SYMBOL>.csv and data/trades.csv.
    """
    try:
        now = datetime.now()
        date_str = now.strftime("%Y-%m-%d")
        time_str = now.strftime("%H:%M:%S")

        trade_start = start_time or buy_item.get("start_time") or buy_item.get("entry_time") or time_str
        trade_end = end_time or buy_item.get("end_time") or buy_item.get("exit_time") or time_str

        daily_file = get_date_trades_file(now, symbol_base=symbol_base)

        buy_strike = buy_item.get("strike", "")
        buy_opt = buy_item.get("option_type", "")
        buy_str = f"{buy_strike} {buy_opt}".strip()
        buy_entry = buy_item.get("entry_price", 0.0)
        buy_exit = buy_item.get("exit_price", 0.0)

        sell_strike = sell_item.get("strike", "")
        sell_opt = sell_item.get("option_type", "")
        sell_str = f"{sell_strike} {sell_opt}".strip()
        sell_entry = sell_item.get("entry_price", 0.0)
        sell_exit = sell_item.get("exit_price", 0.0)

        qty = buy_item.get("quantity", 0)
        lots = buy_item.get("lots", 0)

        headers = [
            "date", "start_time", "end_time", "symbol", "expiry", "lots", "quantity",
            "buy_leg", "buy_entry", "buy_exit",
            "sell_leg", "sell_entry", "sell_exit",
            "exit_reason", "pnl"
        ]

        row = [
            date_str, trade_start, trade_end, symbol_base, str(expiry), str(lots), str(qty),
            buy_str, f"{buy_entry:.2f}", f"{buy_exit:.2f}",
            sell_str, f"{sell_entry:.2f}", f"{sell_exit:.2f}",
            status, f"{total_pnl:.2f}"
        ]

        for target_file in (daily_file, TRADES_FILE):
            file_exists = target_file.exists()
            with open(target_file, "a", newline="", encoding="utf-8") as f:
                import csv
                writer = csv.writer(f)
                if not file_exists:
                    writer.writerow(headers)
                writer.writerow(row)
        print(f"[+] Recorded closed trade to {daily_file} (Start: {trade_start}, End: {trade_end}, P&L: Rs.{total_pnl:.2f})")
    except Exception as e:
        print(f"[!] Failed to record trade: {e}")


# Daily Risk Limits
DAILY_MAX_LOSS = -10000.0   # Stop trading if cumulative daily loss reaches Rs. 10,000
MAX_DAILY_TRADES = 4        # Maximum 4 executed trades per day


def get_daily_trade_stats(for_date=None, symbol_base="BANKNIFTY"):
    """
    Reads daily trades file and returns (daily_triggered_trade_count, daily_realized_pnl).
    """
    if for_date is None:
        now_dt = datetime.now()
        for_date_str = now_dt.strftime("%Y-%m-%d")
    elif isinstance(for_date, str):
        now_dt = datetime.now()
        for_date_str = for_date
    else:
        now_dt = for_date
        for_date_str = for_date.strftime("%Y-%m-%d")

    # 1. Count triggered trades
    triggered_daily_file = get_date_triggered_trades_file(now_dt, symbol_base=symbol_base)
    triggered_files = [triggered_daily_file] if triggered_daily_file.exists() else [TRIGGERED_TRADES_FILE]

    triggered_count = 0
    for t_file in triggered_files:
        if t_file.exists():
            try:
                with open(t_file, newline="", encoding="utf-8") as f:
                    reader = csv.reader(f)
                    rows = [r for r in reader if any(c.strip() for c in r)]
                if rows:
                    header = [c.lower().strip() for c in rows[0]] if (rows and rows[0][0].lower() == "date") else None
                    start_idx = 1 if header else 0
                    date_idx = header.index("date") if (header and "date" in header) else 0
                    sym_idx = header.index("symbol") if (header and "symbol" in header) else 2
                    for row in rows[start_idx:]:
                        if len(row) > date_idx and row[date_idx].strip() == for_date_str:
                            if sym_idx < len(row) and symbol_base:
                                if row[sym_idx].strip().upper() != str(symbol_base).upper():
                                    continue
                            triggered_count += 1
            except Exception:
                pass

    # 2. Count completed trades & daily realized PnL
    daily_file = get_date_trades_file(now_dt, symbol_base=symbol_base)
    target_file = daily_file if daily_file.exists() else TRADES_FILE

    completed_count = 0
    total_pnl = 0.0
    if target_file.exists():
        try:
            with open(target_file, newline="", encoding="utf-8") as f:
                reader = csv.reader(f)
                rows = [r for r in reader if any(c.strip() for c in r)]
            if rows:
                header = [c.lower().strip() for c in rows[0]] if (rows and rows[0][0].lower() == "date") else None
                start_idx = 1 if header else 0
                date_idx = header.index("date") if (header and "date" in header) else 0
                sym_idx = header.index("symbol") if (header and "symbol" in header) else (3 if (header and "start_time" in header) else 2)
                pnl_idx = header.index("pnl") if (header and "pnl" in header) else -1

                for row in rows[start_idx:]:
                    if len(row) > date_idx and row[date_idx].strip() == for_date_str:
                        if sym_idx < len(row) and symbol_base:
                            if row[sym_idx].strip().upper() != str(symbol_base).upper():
                                continue
                        completed_count += 1
                        try:
                            total_pnl += float(row[pnl_idx].strip())
                        except (ValueError, IndexError):
                            pass
        except Exception as e:
            print(f"[!] Error reading daily trade stats: {e}")

    final_count = max(triggered_count, completed_count)
    return final_count, total_pnl


def execute_spread(
    buy_leg,
    sell_leg,
    smart_api=None,
    expiry=None,
    symbol_base="BANKNIFTY",
    exchange="NFO",
    lots=DEFAULT_LOTS,
    lot_size=None,
    max_loss=-5000,
    max_profit=50000,
    profit_threshold=3000,
    min_profit_step=2000,
    check_interval=1,
    option_type="CE",
    check_existing=True
):
    """
    Executes and monitors a 2-leg spread strategy (1 BUY leg and 1 SELL leg) with dynamic current expiry and lot sizing.

    IMPORTANT: Live P&L monitoring is ONLY started if BOTH the BUY and SELL orders are placed successfully.
    """
    # 1. Initialize API session if not passed
    if smart_api is None:
        smart_api = login_smart_api()

    # 2. Check Daily Risk & Trades Limits
    daily_trades, daily_pnl = get_daily_trade_stats()
    print(f"[*] Today's Trading Summary: {daily_trades}/{MAX_DAILY_TRADES} trades completed | Net Realized P&L: Rs.{daily_pnl:.2f}")
    if daily_trades >= MAX_DAILY_TRADES:
        msg = f"Daily maximum trades limit reached ({daily_trades}/{MAX_DAILY_TRADES}). Refusing new trade."
        print(f"\n[🛑 RISK LIMIT] {msg}")
        return {"status": "DAILY_MAX_TRADES_EXCEEDED", "message": msg, "daily_trades": daily_trades, "daily_pnl": daily_pnl}

    if daily_pnl <= DAILY_MAX_LOSS:
        msg = f"Daily maximum loss limit reached (Daily P&L: Rs.{daily_pnl:.2f} <= Rs.{DAILY_MAX_LOSS:.2f}). Refusing new trade."
        print(f"\n[🛑 RISK LIMIT] {msg}")
        return {"status": "DAILY_MAX_LOSS_EXCEEDED", "message": msg, "daily_trades": daily_trades, "daily_pnl": daily_pnl}

    # 3. Determine dynamic current expiry if not passed
    if not expiry:
        expiry = get_current_expiry(smart_api=smart_api, symbol_base=symbol_base, exchange=exchange)
        print(f"[+] Dynamically selected current expiry: {expiry}")

    # Determine default unit lot size dynamically if not provided
    if lot_size is None:
        # Try to get lot size from API data for the instrument
        try:
            from SmartApi.smartConnect import SmartConnect
            import config
            api_key = config.api_key.strip()
            smart_api = SmartConnect(api_key, disable_ssl=True)
            
            # Get instrument data for the symbol
            instruments = smart_api.searchInstrument(exchange, symbol_base)
            if instruments and instruments.get("data"):
                # Try to find lot size from option instruments
                for inst in instruments["data"]:
                    if inst.get("instrumenttype") == "OPTIDX" and inst.get("lotsize"):
                        lot_size = inst["lotsize"]
                        break
            
            if not lot_size:
                raise RuntimeError(f"Lot size must be provided from API data for {symbol_base}. Could not find lot size in search results.")
        except Exception as e:
            raise RuntimeError(f"Lot size must be provided from API data for {symbol_base}. Search failed: {e}")
    unit_lot_size = int(lot_size)

    # 4. Check for existing open positions
    if check_existing:
        open_positions = get_open_positions(smart_api, symbol_filter=symbol_base)
        if open_positions:
            print("\n[!] There is already an ongoing trade. It is not recommended to take trade.")
            print(f"Found {len(open_positions)} active position(s):")
            for pos in open_positions:
                tsymbol = pos.get("tradingsymbol", "Unknown")
                net_qty = pos.get("netqty", 0)
                pnl = pos.get("pnl", "0.00")
                print(f"   * {tsymbol} | Net Qty: {net_qty} | P&L: Rs.{pnl}")
            return {
                "status": "POSITION_EXISTS",
                "message": "There is already an ongoing trade. It is not recommended to take trade.",
                "open_positions": open_positions
            }
        print(f"\n[+] No existing open positions found for {symbol_base}. Proceeding to execute trade...")

    # 4. Normalize legs (applying dynamic 5 lots * unit lot size)
    buy_item = _normalize_leg(buy_leg, default_type="BUY", default_option_type=option_type, default_lot_size=unit_lot_size, default_lots=lots)
    sell_item = _normalize_leg(sell_leg, default_type="SELL", default_option_type=option_type, default_lot_size=unit_lot_size, default_lots=lots)

    # 5. Resolve symbols & tokens if not already provided
    if not buy_item["token"] or not buy_item["symbol"]:
        token, symbol = get_symbol_token(
            smart_api=smart_api,
            expiry=expiry,
            strike=buy_item["strike"],
            option_type=buy_item["option_type"],
            exchange=exchange,
            symbol_base=symbol_base
        )
        buy_item["token"] = token
        buy_item["symbol"] = symbol

    if not sell_item["token"] or not sell_item["symbol"]:
        token, symbol = get_symbol_token(
            smart_api=smart_api,
            expiry=expiry,
            strike=sell_item["strike"],
            option_type=sell_item["option_type"],
            exchange=exchange,
            symbol_base=symbol_base
        )
        sell_item["token"] = token
        sell_item["symbol"] = symbol

    if not buy_item["token"] or not sell_item["token"]:
        error_msg = f"Failed to resolve tokens: BUY leg ({buy_item['strike']}{buy_item['option_type']}) -> {buy_item['token']}, SELL leg ({sell_item['strike']}{sell_item['option_type']}) -> {sell_item['token']}"
        print(f"[!] {error_msg}")
        return {"status": "ERROR", "message": error_msg}

    # 6. Place Entry Orders (BUY leg first for margin benefit, then SELL leg)
    print(f"\n[+] Entering spread positions ({lots} lots = {buy_item['quantity']} qty, Expiry: {expiry})...")
    
    # Place BUY Leg
    buy_response = place_order(
        smart_api=smart_api,
        trading_symbol=buy_item["symbol"],
        symbol_token=buy_item["token"],
        transaction_type="BUY",
        quantity=buy_item["quantity"],
        exchange=exchange
    )

    if not is_order_successful(buy_response):
        print(f"[!] BUY leg order was NOT placed successfully. Aborting spread execution -- monitoring will NOT start.")
        return {
            "status": "BUY_ORDER_FAILED",
            "message": "BUY order failed to execute",
            "response": buy_response
        }

    buy_item["entry_price"] = get_ltp(smart_api, buy_item["token"], buy_item["symbol"], exchange)
    buy_item["order_id"] = buy_response.get("data", {}).get("orderid") if isinstance(buy_response.get("data"), dict) else None
    time.sleep(1)

    # Place SELL Leg
    sell_response = place_order(
        smart_api=smart_api,
        trading_symbol=sell_item["symbol"],
        symbol_token=sell_item["token"],
        transaction_type="SELL",
        quantity=sell_item["quantity"],
        exchange=exchange
    )

    if not is_order_successful(sell_response):
        print(f"[!] SELL leg order FAILED after BUY leg succeeded!")
        print(f"[!] Squaring off BUY leg immediately to prevent unhedged naked position...")
        # Square off BUY leg
        place_order(
            smart_api=smart_api,
            trading_symbol=buy_item["symbol"],
            symbol_token=buy_item["token"],
            transaction_type="SELL",
            quantity=buy_item["quantity"],
            exchange=exchange
        )
        print(f"[!] Aborting spread execution -- monitoring will NOT start.")
        return {
            "status": "SELL_ORDER_FAILED",
            "message": "SELL order failed (BUY leg was squared off for protection)",
            "response": sell_response
        }

    sell_item["entry_price"] = get_ltp(smart_api, sell_item["token"], sell_item["symbol"], exchange)
    sell_item["order_id"] = sell_response.get("data", {}).get("orderid") if isinstance(sell_response.get("data"), dict) else None

    print("\n[+] Both BUY and SELL orders placed successfully!")
    trade_start_time = datetime.now().strftime("%H:%M:%S")

    # Record triggered trade for daily trades counter
    record_triggered_trade(
        symbol_base=symbol_base,
        structure="SPREAD",
        lots=lots,
        quantity=buy_item.get("quantity", 0),
        trade_reason=f"Broken Call Spread ({option_type})",
        buy_symbol=buy_item.get("symbol", ""),
        sell_symbol=sell_item.get("symbol", ""),
        mode="LIVE"
    )
    buy_item["start_time"] = trade_start_time
    sell_item["start_time"] = trade_start_time
    buy_item["entry_time"] = trade_start_time
    sell_item["entry_time"] = trade_start_time

    print("Entry Prices:")
    print(f"   BUY  : {buy_item['symbol']} @ Rs.{buy_item['entry_price']:.2f} ({lots} lots | Qty: {buy_item['quantity']} | Order ID: {buy_item['order_id']})")
    print(f"   SELL : {sell_item['symbol']} @ Rs.{sell_item['entry_price']:.2f} ({lots} lots | Qty: {sell_item['quantity']} | Order ID: {sell_item['order_id']})")

    # 7. Monitoring & Trailing Exit Loop (ONLY RUNS AFTER BOTH ORDERS SUCCEED)
    max_pnl = 0.0
    min_profit = profit_threshold

    print(f"\n[*] Starting live spread monitoring ({lots} lots | {expiry})... Auto-exit if P/L <= Rs.{max_loss}, below trailing min profit after crossing Rs.{profit_threshold}, or at Rs.{max_profit}")

    while True:
        buy_live = get_ltp(smart_api, buy_item["token"], buy_item["symbol"], exchange, last_price=buy_item["entry_price"])
        sell_live = get_ltp(smart_api, sell_item["token"], sell_item["symbol"], exchange, last_price=sell_item["entry_price"])

        buy_pnl = (buy_live - buy_item["entry_price"]) * buy_item["quantity"]
        sell_pnl = (sell_item["entry_price"] - sell_live) * sell_item["quantity"]
        total_pnl = buy_pnl + sell_pnl

        now = datetime.now().strftime("%H:%M:%S")
        print(f"[{now}] Spread P/L: Rs.{total_pnl:.2f} (BUY {buy_item['strike']}: Rs.{buy_pnl:.2f} | SELL {sell_item['strike']}: Rs.{sell_pnl:.2f})")

        # Exit Condition 1: Loss limit reached
        if total_pnl <= max_loss:
            print(f"[!] Exit triggered -- Loss limit reached (Rs.{total_pnl:.2f} <= Rs.{max_loss})")
            square_off(smart_api, buy_item, sell_item, exchange)
            trade_end_time = datetime.now().strftime("%H:%M:%S")
            buy_item["exit_price"] = buy_live
            sell_item["exit_price"] = sell_live
            buy_item["end_time"] = trade_end_time
            sell_item["end_time"] = trade_end_time
            record_trade(buy_item, sell_item, total_pnl, max_pnl, "EXIT_MAX_LOSS", symbol_base, expiry, start_time=trade_start_time, end_time=trade_end_time)
            return {"status": "EXIT_MAX_LOSS", "pnl": total_pnl, "max_pnl": max_pnl, "buy_leg": buy_item, "sell_leg": sell_item, "start_time": trade_start_time, "end_time": trade_end_time}

        # Trailing profit update
        if total_pnl > max_pnl:
            max_pnl = total_pnl
            if max_pnl >= profit_threshold:
                new_min_profit = ((max_pnl - profit_threshold) // min_profit_step) * min_profit_step + profit_threshold
                if new_min_profit > min_profit:
                    min_profit = new_min_profit
                    print(f"[*] Profit crossed Rs.{max_pnl:.0f}. Minimum profit locked to Rs.{min_profit}.")

        # Exit Condition 2: Profit dropped below locked trailing threshold
        if max_pnl >= profit_threshold and total_pnl < min_profit:
            print(f"[+] Exiting: Profit fell below trailing threshold Rs.{min_profit} after crossing Rs.{profit_threshold} (Current: Rs.{total_pnl:.2f})")
            square_off(smart_api, buy_item, sell_item, exchange)
            trade_end_time = datetime.now().strftime("%H:%M:%S")
            buy_item["exit_price"] = buy_live
            sell_item["exit_price"] = sell_live
            buy_item["end_time"] = trade_end_time
            sell_item["end_time"] = trade_end_time
            record_trade(buy_item, sell_item, total_pnl, max_pnl, "EXIT_TRAILING_PROFIT", symbol_base, expiry, start_time=trade_start_time, end_time=trade_end_time)
            return {"status": "EXIT_TRAILING_PROFIT", "pnl": total_pnl, "max_pnl": max_pnl, "buy_leg": buy_item, "sell_leg": sell_item, "start_time": trade_start_time, "end_time": trade_end_time}

        # Exit Condition 3: Max profit target reached
        if total_pnl >= max_profit:
            print(f"[+] Exiting: Profit target Rs.{max_profit} reached (Current: Rs.{total_pnl:.2f})")
            square_off(smart_api, buy_item, sell_item, exchange)
            trade_end_time = datetime.now().strftime("%H:%M:%S")
            buy_item["exit_price"] = buy_live
            sell_item["exit_price"] = sell_live
            buy_item["end_time"] = trade_end_time
            sell_item["end_time"] = trade_end_time
            record_trade(buy_item, sell_item, total_pnl, max_pnl, "EXIT_MAX_PROFIT", symbol_base, expiry, start_time=trade_start_time, end_time=trade_end_time)
            return {"status": "EXIT_MAX_PROFIT", "pnl": total_pnl, "max_pnl": max_pnl, "buy_leg": buy_item, "sell_leg": sell_item, "start_time": trade_start_time, "end_time": trade_end_time}

        time.sleep(check_interval)


# Aliases for convenience
broken_call = execute_spread
execute_broken_call = execute_spread


# ====== STANDALONE EXECUTION EXAMPLE ======
if __name__ == "__main__":
    # Example usage: 5 lots of BUY leg and SELL leg with dynamic current expiry
    sample_buy_leg = "58000ce"
    sample_sell_leg = "59000ce"

    execute_spread(
        buy_leg=sample_buy_leg,
        sell_leg=sample_sell_leg,
        lots=5,
        max_loss=-5000,
        max_profit=50000,
        profit_threshold=3000,
        min_profit_step=2000,
        check_interval=1,
        check_existing=True
    )
