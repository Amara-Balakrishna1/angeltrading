"""
Trade Executor for 3-Leg Options Strategy.
Implements order placement, LTP fetch, and square-off with paper trading support.
"""

import sys
import io
from pathlib import Path

# Add parent directory to path for imports
ROOT = Path(__file__).resolve().parent.parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import time
from datetime import datetime
from typing import Dict, Any, Optional
import pyotp

# Ensure UTF-8 output on Windows console
if sys.platform == "win32":
    try:
        if hasattr(sys.stdout, "reconfigure"):
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        else:
            sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    except Exception:
        pass

from SmartApi.smartConnect import SmartConnect
import config
from live.threelegs.config import INSTRUMENT_CONFIG, RISK_CONFIG
from live.threelegs.logger import logger


def login_smart_api() -> SmartConnect:
    """Authenticates and returns a SmartConnect instance."""
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
        logger.info("Successfully logged in to SmartAPI")
        return smart_api
    else:
        logger.error(f"SmartAPI login failed: {session}")
        raise RuntimeError(f"SmartAPI login failed: {session}")


def get_ltp(
    smart_api: SmartConnect,
    symbol_token: str,
    trading_symbol: str,
    exchange: str = "NFO",
    retries: int = 2,
    last_price: float = 0.0
) -> float:
    """Fetches LTP for a contract with retries."""
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
                logger.error(f"Error fetching LTP for {trading_symbol}: {e}")
            time.sleep(0.3)
    return last_price


def is_order_successful(response: Any) -> bool:
    """Validates order response (handles string order ID or full dict response)."""
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


def place_order(
    smart_api: SmartConnect,
    trading_symbol: str,
    symbol_token: str,
    transaction_type: str,
    quantity: int,
    exchange: str = "NFO",
    order_type: str = "MARKET",
    product_type: str = "INTRADAY",
    paper_trade: bool = False
) -> Optional[Any]:
    """
    Places a market order via SmartAPI.
    If paper_trade=True, prints the order details without executing.
    """
    if not symbol_token or not trading_symbol:
        print(f"[!] Cannot place order: missing symbol or token ({trading_symbol}, {symbol_token})")
        return None

    if paper_trade:
        print(f"[PAPER TRADE] {transaction_type} order for {trading_symbol} (Qty: {quantity})")
        logger.info(f"[PAPER TRADE] {transaction_type} order for {trading_symbol} (Qty: {quantity})")
        return "PAPER_ORDER_ID"

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

    # Log order details for debugging
    logger.info(f"Placing order: {transaction_type} {trading_symbol} Qty:{quantity} Token:{symbol_token}")

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
            logger.info(f"{transaction_type} order placed successfully for {trading_symbol} (Qty: {quantity}, Order ID: {order_id})")
        else:
            if isinstance(response, dict):
                error_msg = response.get("message", "Unknown error")
                error_code = response.get("errorcode", response.get("errorCode", ""))
            else:
                error_msg = str(response) if response else "No response / order rejected"
                error_code = ""
            print(f"[!] {transaction_type} order FAILED for {trading_symbol}: {error_msg} (Error Code: {error_code})")
            logger.error(f"{transaction_type} order FAILED for {trading_symbol}: {error_msg} (Error Code: {error_code})")
        return response
    except Exception as e:
        print(f"[!] Error placing {transaction_type} order for {trading_symbol}: {e}")
        logger.error(f"Error placing {transaction_type} order for {trading_symbol}: {e}")
        return None


def get_open_positions(smart_api: SmartConnect, symbol_filter: Optional[str] = None) -> Optional[list]:
    """Fetches active open positions. Returns list of open positions, or None on API failure."""
    try:
        response = smart_api.position()
        if not response or not isinstance(response, dict) or not response.get("status"):
            return None
        positions_data = response.get("data")
        if positions_data is None or not isinstance(positions_data, list):
            return None

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
        logger.error(f"Error checking open positions: {e}")
        return None


def square_off_all(
    smart_api: SmartConnect,
    positions: list,
    paper_trade: bool = False,
    exchange: str = "NFO"
):
    """
    Squares off all provided positions.
    For paper_trade mode, prints what would be squared off.
    """
    print(f"\n[!] Squaring off {len(positions)} position(s)...")
    logger.info(f"Squaring off {len(positions)} position(s)")

    for pos in positions:
        symbol = pos.get("tradingsymbol", "")
        try:
            netqty = int(pos.get("netqty", 0))
            if netqty == 0:
                continue

            token = pos.get("symboltoken", "")

            if paper_trade:
                transaction_type = "SELL" if netqty > 0 else "BUY"
                print(f"[PAPER TRADE] Square off {transaction_type} {abs(netqty)} of {symbol}")
                logger.info(f"[PAPER TRADE] Square off {transaction_type} {abs(netqty)} of {symbol}")
                continue

            transaction_type = "SELL" if netqty > 0 else "BUY"

            resp = place_order(
                smart_api=smart_api,
                trading_symbol=symbol,
                symbol_token=token,
                transaction_type=transaction_type,
                quantity=abs(netqty),
                exchange=exchange,
                paper_trade=False
            )

            if not is_order_successful(resp):
                logger.error(f"Failed to square off {symbol}")
        except Exception as e:
            logger.error(f"Error squaring off {symbol}: {e}")


def get_order_status(smart_api: SmartConnect, order_id: str) -> Optional[Dict[str, Any]]:
    """Fetches the status of an order by order ID."""
    try:
        response = smart_api.orderBook()
        if response and response.get("status") and response.get("data"):
            for order in response["data"]:
                if order.get("orderid") == order_id:
                    return order
        return None
    except Exception as e:
        logger.error(f"Error fetching order status for {order_id}: {e}")
        return None
