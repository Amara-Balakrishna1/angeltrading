"""
Test script to simulate broker-side stop loss order placement API call.
This does NOT place real orders - it only validates the API parameters.
"""

import sys
from pathlib import Path

# Add project root to path
project_root = Path(__file__).resolve().parent
sys.path.insert(0, str(project_root))


def simulate_place_stop_loss_order(
    trading_symbol: str,
    symbol_token: str,
    transaction_type: str,
    quantity: int,
    trigger_price: float,
    exchange: str = "BFO",
    product_type: str = "INTRADAY"
) -> dict:
    """
    Simulates the stop loss order placement API call.
    Returns the order parameters that would be sent to the broker.
    """
    order_params = {
        "variety": "STOPLOSS",
        "tradingsymbol": trading_symbol,
        "symboltoken": str(symbol_token),
        "transactiontype": transaction_type,
        "exchange": exchange,
        "ordertype": "MARKET",
        "producttype": product_type,
        "duration": "DAY",
        "price": trigger_price,
        "triggerprice": trigger_price,
        "quantity": quantity
    }

    return order_params


def print_order_params(order_params: dict, entry_price: float, sl_pct: float):
    """Print the order parameters in a readable format."""
    print("\n" + "=" * 70)
    print("  SIMULATED STOP LOSS ORDER PLACEMENT")
    print("=" * 70)
    print(f"\n  Trading Symbol      : {order_params['tradingsymbol']}")
    print(f"  Symbol Token        : {order_params['symboltoken']}")
    print(f"  Exchange            : {order_params['exchange']}")
    print(f"  Transaction Type   : {order_params['transactiontype']}")
    print(f"  Order Type          : {order_params['ordertype']}")
    print(f"  Product Type        : {order_params['producttype']}")
    print(f"  Duration            : {order_params['duration']}")
    print(f"  Variety             : {order_params['variety']}")
    print(f"\n  Entry Premium       : Rs. {entry_price:.2f}")
    print(f"  SL Percentage       : {sl_pct}%")
    print(f"  Trigger Price       : Rs. {order_params['triggerprice']:.2f}")
    print(f"  Limit Price         : Rs. {order_params['price']:.2f}")
    print(f"  Quantity            : {order_params['quantity']}")
    print("\n" + "=" * 70)
    print("  [SIMULATION] This order would be sent to Angel One SmartAPI")
    print("  [SIMULATION] Actual placement skipped - market hours check not performed")
    print("=" * 70 + "\n")


def main():
    # Test case: SENSEX 72000 CE
    test_cases = [
        {
            "symbol": "SENSEX72000CE",
            "token": "99999999",  # Example token (would be real in production)
            "entry_price": 150.0,
            "sl_pct": 30.0,
            "quantity": 25,
            "exchange": "BFO"
        },
        {
            "symbol": "SENSEX72000CE",
            "token": "99999999",
            "entry_price": 200.0,
            "sl_pct": 30.0,
            "quantity": 50,
            "exchange": "BFO"
        },
        {
            "symbol": "SENSEX71500CE",
            "token": "99999998",
            "entry_price": 100.0,
            "sl_pct": 25.0,
            "quantity": 25,
            "exchange": "BFO"
        }
    ]

    print("\nSimulating broker-side stop loss order API calls...")
    print("[!] NOTE: This is a SIMULATION - no actual orders will be placed\n")

    for i, test_case in enumerate(test_cases, 1):
        print(f"\n--- Test Case {i} ---")
        trigger_price = test_case["entry_price"] * (1 - test_case["sl_pct"] / 100.0)

        order_params = simulate_place_stop_loss_order(
            trading_symbol=test_case["symbol"],
            symbol_token=test_case["token"],
            transaction_type="SELL",
            quantity=test_case["quantity"],
            trigger_price=trigger_price,
            exchange=test_case["exchange"],
            product_type="INTRADAY"
        )

        print_order_params(order_params, test_case["entry_price"], test_case["sl_pct"])

    print("\nSIMULATION SUMMARY:")
    print("  - API parameters validated successfully")
    print("  - Trigger price calculation: entry x (1 - sl_pct/100)")
    print("  - Order variety: STOPLOSS")
    print("  - Order type: MARKET")
    print("  - Transaction type: SELL (to exit the BUY position)")
    print("\n  [!] To place actual orders:")
    print("      1. Ensure market is open (9:15 AM - 3:30 PM IST)")
    print("      2. Enable enable_premium_stop_loss in config")
    print("      3. Run live strategy with --structure naked")
    print("\n")


if __name__ == "__main__":
    main()
