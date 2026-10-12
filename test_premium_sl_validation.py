"""
LIVE TEST: Broker-side fixed monetary loss stop loss order placement.
Logs in to SmartAPI, resolves a real option contract, and places a
STOPLOSS_LIMIT SELL order sized to cap loss at a fixed Rs. amount.

WARNING: This places REAL orders on your Angel One account.
"""

import sys
import time
from pathlib import Path

# ── Project root on path ────────────────────────────────────────────────────
ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

VENV_SP = ROOT / ".venv" / "Lib" / "site-packages"
if VENV_SP.exists() and str(VENV_SP) not in sys.path:
    sys.path.insert(0, str(VENV_SP))

# ── Imports from live strategy ───────────────────────────────────────────────
from live.daybreakout.executor import (
    login_smart_api,
    get_ltp,
    smart_api_lock,
    is_order_successful,
)
from live.daybreakout.logger import logger
from live.daybreakout.contract_finder import find_strategy_contracts
from live.daybreakout.config import INSTRUMENTS, RISK_CONFIG

# ── Test Configuration ───────────────────────────────────────────────────────
INSTRUMENT     = "SENSEX"          # or "NIFTY"
DIRECTION      = "BULLISH"         # "BULLISH" -> CE option, "BEARISH" -> PE option
STRUCTURE      = "naked"           # naked single-leg buy
LOTS           = 10                # Number of lots
FIXED_LOSS_RS  = 9000.0            # Fixed monetary SL loss amount (Rs.)
# ─────────────────────────────────────────────────────────────────────────────


def is_market_open() -> bool:
    """True if current IST time is within 9:15–15:30."""
    from datetime import datetime, time as dtime
    return dtime(9, 15) <= datetime.now().time() <= dtime(15, 30)


def place_sl_order_raw(smart_api, trading_symbol, symbol_token, quantity,
                       trigger_price, exchange):
    """
    Places a STOPLOSS SELL order via SmartAPI.
    Angel One only accepts NORMAL, STOPLOSS, ROBO as variety — AMO is not
    a valid variety. Using STOPLOSS works both during and after market hours;
    the broker handles the timing internally.
    """
    order_params = {
        "variety":         "STOPLOSS",
        "tradingsymbol":   trading_symbol,
        "symboltoken":     str(symbol_token),
        "transactiontype": "SELL",
        "exchange":        exchange,
        "ordertype":       "STOPLOSS_LIMIT",   # MARKET is invalid for STOPLOSS variety
        "producttype":     "INTRADAY",
        "duration":        "DAY",
        "price":           trigger_price,      # Limit execution price
        "triggerprice":    trigger_price,      # Trigger activation price
        "quantity":        quantity,
    }

    print(f"\n[*] Placing STOPLOSS order...")
    print(f"    symbol={trading_symbol} | trigger=Rs.{trigger_price:.2f} | qty={quantity}")

    try:
        with smart_api_lock:
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
            print(f"[+] Order accepted! Order ID: {order_id}")
            logger.info(f"STOPLOSS order placed: {trading_symbol} trigger={trigger_price} qty={quantity} id={order_id}")
            return order_id
        else:
            error_msg = response.get("message", str(response)) if isinstance(response, dict) else str(response)
            print(f"[!] Order FAILED: {error_msg}")
            logger.error(f"STOPLOSS order FAILED: {trading_symbol}: {error_msg}")
            return None
    except Exception as e:
        print(f"[!] Exception placing order: {e}")
        logger.error(f"Exception placing STOPLOSS order for {trading_symbol}: {e}")
        return None


def main():
    inst        = INSTRUMENTS[INSTRUMENT]
    market_open = is_market_open()

    print("\n" + "=" * 65)
    print("  REAL ORDER TEST - Fixed Loss Stop Loss Placement")
    print("=" * 65)
    print(f"  Instrument    : {INSTRUMENT}")
    print(f"  Direction     : {DIRECTION}")
    print(f"  Structure     : {STRUCTURE.upper()}")
    print(f"  Lots          : {LOTS}")
    print(f"  Fixed SL Loss : Rs. {FIXED_LOSS_RS:,.0f}")
    print(f"  Market Open   : {'YES' if market_open else 'NO (after hours)'}")
    print("=" * 65)

    # 1. Login
    print("\n[*] Logging in to Angel One SmartAPI...")
    smart_api = login_smart_api()
    time.sleep(2)

    # 2. Fetch spot price (best-effort — may be stale/zero after market close)
    spot_ltp = None
    try:
        spot_ltp = get_ltp(smart_api, inst["spot_token"], inst["spot_symbol"], inst["spot_exchange"])
        if spot_ltp and spot_ltp > 0:
            print(f"[+] Current {inst['display_name']} Spot: {spot_ltp:.2f}")
        else:
            spot_ltp = None
    except Exception as e:
        print(f"[!] Spot LTP fetch failed: {e}")
    if not spot_ltp:
        spot_ltp = float(input(f"    Enter {INSTRUMENT} spot price manually: ").strip())

    # 3. Resolve the nearest-expiry option contract
    print("\n[*] Resolving option contract...")
    plan = find_strategy_contracts(
        symbol_base=inst["name"],
        spot_price=spot_ltp,
        direction=DIRECTION,
        strike_step=inst["strike_step"],
        spread_width=inst["spread_width"],
        option_exchange=inst["option_exchange"],
        lots=LOTS,
        structure=STRUCTURE,
    )

    buy_leg  = plan["buy_leg"]
    quantity = plan["quantity"]

    print(f"[+] Contract  : {buy_leg['symbol']}")
    print(f"    Token     : {buy_leg['token']}")
    print(f"    Strike    : {buy_leg['strike']}")
    print(f"    Quantity  : {quantity} ({LOTS} lot(s))")

    # 4. Fetch option LTP — prompt manually if unavailable (after hours)
    opt_ltp = None
    try:
        opt_ltp = get_ltp(smart_api, buy_leg["token"], buy_leg["symbol"], inst["option_exchange"])
        if opt_ltp and opt_ltp > 0:
            print(f"\n[+] Option LTP (entry price)  : Rs. {opt_ltp:.2f}")
        else:
            opt_ltp = None
    except Exception as e:
        print(f"[!] Option LTP fetch failed: {e}")
    if not opt_ltp:
        print(f"[!] LTP unavailable (market closed?). Enter entry price manually.")
        opt_ltp = float(input(f"    Enter entry premium for {buy_leg['symbol']}: Rs. ").strip())

    # 5. Fixed-loss SL: trigger = entry_price - (fixed_loss / quantity)
    loss_per_unit = FIXED_LOSS_RS / quantity
    trigger_price = round(opt_ltp - loss_per_unit, 1)
    trigger_price = max(trigger_price, 0.1)   # floor at tick size

    print(f"\n[+] Entry Premium             : Rs. {opt_ltp:.2f}")
    print(f"[+] Fixed Loss Target         : Rs. {FIXED_LOSS_RS:,.0f}")
    print(f"[+] Loss per unit             : Rs. {loss_per_unit:.2f}")
    print(f"[+] SL Trigger Price          : Rs. {trigger_price:.2f}")
    print(f"[+] Actual loss at trigger    : Rs. {(opt_ltp - trigger_price) * quantity:,.2f}")
    print(f"[+] Order Variety             : STOPLOSS")

    # 6. Confirm — accepts y or yes (case-insensitive)
    print("\n" + "=" * 65)
    print("  WARNING: ABOUT TO PLACE A REAL ORDER ON YOUR ANGEL ONE ACCOUNT")
    print("=" * 65)
    try:
        confirm = input("  Confirm? [y/yes to proceed]: ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        confirm = ""

    if confirm not in ("y", "yes"):
        print("[!] Aborted by user.")
        return

    # 7. Place the order (STOPLOSS variety works both during and after market hours)
    sl_order_id = place_sl_order_raw(
        smart_api=smart_api,
        trading_symbol=buy_leg["symbol"],
        symbol_token=buy_leg["token"],
        quantity=quantity,
        trigger_price=trigger_price,
        exchange=inst["option_exchange"],
    )

    if sl_order_id:
        print(f"\n[OK] STOPLOSS order placed successfully!")
        print(f"    Order ID      : {sl_order_id}")
        print(f"    Symbol        : {buy_leg['symbol']}")
        print(f"    Trigger Price : Rs. {trigger_price:.2f}")
        print(f"    Quantity      : {quantity}")
    else:
        print("\n[FAIL] Order placement failed. Check output above for details.")


if __name__ == "__main__":
    main()
