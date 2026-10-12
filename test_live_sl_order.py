"""
Test script to attempt placing a broker-side stop loss order with actual API.
This will show the exact order parameters even if rejected (market closed).
"""

import sys
from pathlib import Path

# Add project root to path
project_root = Path(__file__).resolve().parent
sys.path.insert(0, str(project_root))

try:
    from SmartApi.smartConnect import SmartConnect
    import config
    import pyotp
except ImportError as e:
    print(f"[!] Import error: {e}")
    print("[!] This script requires SmartAPI and config module")
    sys.exit(1)


def login_smart_api():
    """Authenticate with SmartAPI."""
    api_key = config.api_key.strip()
    username = config.username
    pwd = config.pwd
    token = config.token

    try:
        smart_api = SmartConnect(api_key)
        totp = pyotp.TOTP(token).now()
        session = smart_api.generateSession(username, pwd, totp)

        if session.get("status"):
            refresh_token = session["data"]["refreshToken"]
            smart_api.generateToken(refresh_token)
            print("[+] Logged in to SmartAPI successfully")
            return smart_api
        else:
            print(f"[!] Login failed: {session.get('message')}")
            return None
    except Exception as e:
        print(f"[!] Login error: {e}")
        return None


def place_test_stop_loss_order(smart_api, trading_symbol, symbol_token, quantity, trigger_price):
    """
    Attempt to place a stop loss order to see the API call parameters.
    """
    order_params = {
        "variety": "STOPLOSS",
        "tradingsymbol": trading_symbol,
        "symboltoken": str(symbol_token),
        "transactiontype": "SELL",
        "exchange": "BFO",
        "ordertype": "MARKET",
        "producttype": "INTRADAY",
        "duration": "DAY",
        "price": trigger_price,
        "triggerprice": trigger_price,
        "quantity": quantity
    }

    print("\n" + "=" * 70)
    print("  ORDER PARAMETERS BEING SENT TO BROKER")
    print("=" * 70)
    for key, value in order_params.items():
        print(f"  {key:20s} : {value}")
    print("=" * 70)

    try:
        print("\n[*] Attempting to place stop loss order...")
        if hasattr(smart_api, "placeOrderFullResponse"):
            response = smart_api.placeOrderFullResponse(order_params)
        else:
            response = smart_api.placeOrder(order_params)

        print(f"\n[+] Broker Response:")
        print(f"    Type: {type(response)}")
        print(f"    Value: {response}")

        if response:
            if isinstance(response, dict):
                status = response.get("status")
                message = response.get("message", "")
                order_id = response.get("data", {}).get("orderid", "") if isinstance(response.get("data"), dict) else ""
                print(f"\n  Status: {status}")
                print(f"  Message: {message}")
                print(f"  Order ID: {order_id}")
            elif isinstance(response, str):
                print(f"\n  Order ID: {response}")
        else:
            print("\n  No response from broker")

        return response

    except Exception as e:
        print(f"\n[!] Error placing order: {e}")
        return None


def main():
    print("\n" + "=" * 70)
    print("  LIVE STOP LOSS ORDER PLACEMENT TEST")
    print("=" * 70)
    print("\n[!] This will attempt to place a REAL order with the broker")
    print("[!] Market is currently closed - order will likely be rejected")
    print("[!] Keep Angel Broking app open to cancel if order goes through")
    print("\n")

    # Login
    smart_api = login_smart_api()
    if not smart_api:
        print("\n[!] Failed to login. Exiting.")
        return

    # Test parameters (SENSEX 72000 CE example)
    # You can modify these to match a real contract
    trading_symbol = "SENSEX72000CE"  # Example - replace with actual contract
    symbol_token = "99999999"  # Example - replace with actual token from scrip master
    quantity = 25  # 1 lot for SENSEX
    entry_price = 150.0  # Example entry price
    fixed_loss = 9000.0  # Fixed loss target

    # Calculate trigger price
    trigger_price = entry_price - (fixed_loss / quantity)

    print(f"\nTest Configuration:")
    print(f"  Trading Symbol: {trading_symbol}")
    print(f"  Symbol Token: {symbol_token}")
    print(f"  Quantity: {quantity}")
    print(f"  Entry Price: Rs. {entry_price:.2f}")
    print(f"  Fixed Loss Target: Rs. {fixed_loss:.0f}")
    print(f"  Calculated Trigger: Rs. {trigger_price:.2f}")

    # Attempt to place order
    response = place_test_stop_loss_order(
        smart_api,
        trading_symbol,
        symbol_token,
        quantity,
        trigger_price
    )

    print("\n" + "=" * 70)
    print("  TEST COMPLETE")
    print("=" * 70)
    print("\n[!] If order was placed successfully, cancel it immediately in Angel app")
    print("[!] If order was rejected (expected during off-hours), no action needed")
    print("\n")


if __name__ == "__main__":
    main()
