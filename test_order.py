"""
Simple script to place a NIFTY CE order for testing.
User can cancel the order manually.
"""

import sys
import pyotp
from SmartApi.smartConnect import SmartConnect
import config
from pathlib import Path

# Add parent directory to path for imports
ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from live.daybreakout.contract_finder import load_scrip_master, find_strategy_contracts

def place_test_order():
    """Place a simple NIFTY CE order for testing."""
    
    print("=" * 60)
    print("TEST ORDER PLACEMENT - NIFTY CE")
    print("=" * 60)
    
    try:
        # Login to SmartAPI
        print("\n[1] Logging in to SmartAPI...")
        api_key = config.api_key.strip()
        smart_api = SmartConnect(api_key, disable_ssl=config.disable_ssl)
        totp = pyotp.TOTP(config.token).now()
        session = smart_api.generateSession(config.username, config.pwd, totp)
        
        if not session.get("status"):
            print(f"[FAILED] LOGIN FAILED: {session}")
            return False
        
        print(f"[SUCCESS] Login successful! Account: {config.username}")
        
        # Generate refresh token
        refresh_token = session.get('data', {}).get('refreshToken')
        if refresh_token:
            smart_api.generateToken(refresh_token)
            print(f"[SUCCESS] Refresh token generated")
        
        # Get current NIFTY LTP to determine strike
        print("\n[2] Getting current NIFTY LTP...")
        ltp_data = smart_api.ltpData('NSE', 'Nifty 50', '99926000')
        if ltp_data and ltp_data.get('status'):
            current_ltp = ltp_data.get('data', {}).get('ltp')
            print(f"[SUCCESS] NIFTY LTP: {current_ltp}")
        else:
            print(f"[FAILED] Could not get LTP")
            return False
        
        # Calculate ATM strike (round to nearest 50)
        atm_strike = round(current_ltp / 50) * 50
        print(f"[INFO] ATM Strike: {atm_strike}")
        
        # Use the built-in contract finder from live daybreakout
        print("\n[3] Using built-in contract finder...")
        try:
            # Simulate NIFTY instrument config
            inst_config = {
                "name": "NIFTY",
                "spot_symbol": "Nifty 50",
                "spot_token": "99926000",
                "spot_exchange": "NSE",
                "option_exchange": "NFO",
                "strike_step": 50,
                "spread_width": 300,
                "default_lot_size": 25,
            }
            
            # Find the option contract using the built-in function
            print(f"[INFO] Finding NIFTY ATM CE contract (Strike: {atm_strike})...")
            
            # For naked option, we need to use structure="naked"
            spread_plan = find_strategy_contracts(
                symbol_base="NIFTY",
                spot_price=current_ltp,
                direction="BULLISH",
                strike_step=50,
                spread_width=300,
                option_exchange="NFO",
                lots=1,
                structure="naked"
            )
            
            if spread_plan:
                buy_leg = spread_plan.get("buy_leg", {})
                trading_symbol = buy_leg.get("symbol", "")
                symbol_token = buy_leg.get("token", "")
                strike = buy_leg.get("strike", "")
                
                print(f"[SUCCESS] Found contract using built-in finder:")
                print(f"   Trading Symbol: {trading_symbol}")
                print(f"   Symbol Token: {symbol_token}")
                print(f"   Strike: {strike}")
                print(f"   Quantity: {spread_plan.get('quantity', 25)}")
            else:
                print("[ERROR] Could not find contract using built-in finder")
                return False
                
        except Exception as e:
            print(f"[ERROR] Contract finder failed: {e}")
            import traceback
            traceback.print_exc()
            return False
        
        # Order parameters
        quantity = spread_plan.get("quantity", 25)
        print("\n[4] Order Details:")
        print(f"   Symbol: {trading_symbol}")
        print(f"   Token: {symbol_token}")
        print(f"   Type: BUY CE (Naked)")
        print(f"   Quantity: {quantity}")
        print(f"   Order Type: MARKET")
        print(f"   Product: INTRADAY")
        
        confirm = input("\n[CONFIRM] Place this order? (yes/no): ").strip().lower()
        if confirm != 'yes':
            print("[CANCELLED] Order not placed")
            return True
        
        # Place the order
        print("\n[5] Placing order...")
        order_params = {
            "variety": "NORMAL",
            "tradingsymbol": trading_symbol,
            "symboltoken": str(symbol_token),  # Ensure token is string
            "transactiontype": "BUY",
            "exchange": "NFO",
            "ordertype": "MARKET",
            "producttype": "INTRADAY",
            "duration": "DAY",
            "quantity": str(quantity)  # Ensure quantity is string
        }
        
        try:
            order_response = smart_api.placeOrder(order_params)
            if order_response:
                print(f"[SUCCESS] Order placed successfully!")
                print(f"   Order ID: {order_response}")
                print(f"\n[IMPORTANT] You can cancel this order manually from your trading app")
                return True
            else:
                print(f"[FAILED] Order failed: {order_response}")
                return False
        except Exception as e:
            print(f"[ERROR] Order placement error: {e}")
            print(f"[INFO] Error details: {str(e)}")
            import traceback
            traceback.print_exc()
            return False
        
    except Exception as e:
        print(f"\n[ERROR] {e}")
        import traceback
        traceback.print_exc()
        return False

if __name__ == "__main__":
    success = place_test_order()
    sys.exit(0 if success else 1)
