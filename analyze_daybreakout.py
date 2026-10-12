"""
Analysis script to simulate daybreakout/main.py for SENSEX naked options using local data.
"""

import sys
from pathlib import Path
from datetime import datetime, timedelta
import pyotp
import csv

# Ensure ROOT and .venv are on sys.path
ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

VENV_SITE_PACKAGES = ROOT / ".venv" / "Lib" / "site-packages"
if VENV_SITE_PACKAGES.exists() and str(VENV_SITE_PACKAGES) not in sys.path:
    sys.path.insert(0, str(VENV_SITE_PACKAGES))

from SmartApi.smartConnect import SmartConnect
import config
from live.daybreakout.config import INSTRUMENTS, RANGE_CONFIG, GAP_CONFIG, DATA_DIR
from live.daybreakout.range_calculator import get_previous_day_range, fetch_15m_candles, is_15m_candle_complete, get_previous_trading_day
from live.daybreakout.gap_analyzer import get_opening_gap, GapInfo, classify_gap
from live.daybreakout.signals import evaluate_15m_close, apply_reverse, extra_15m_candles_wait, update_gap_territory
from live.daybreakout.contract_finder import find_strategy_contracts

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
        return smart_api
    else:
        raise RuntimeError(f"SmartAPI login failed: {session}")

def safe_get_candle_data(smart_api, params, max_retries=3, retry_delay=2.0):
    """Fetches candle data with retry logic."""
    import time
    for attempt in range(max_retries):
        try:
            res = smart_api.getCandleData(params)
            if res and res.get("status"):
                return res
            time.sleep(retry_delay)
        except Exception as e:
            if attempt < max_retries - 1:
                time.sleep(retry_delay * (attempt + 1))
            else:
                raise e
    return None

def get_prev_close(smart_api, spot_exchange, spot_token, target_date):
    """Get previous day close for a specific date."""
    from_date = (target_date - timedelta(days=10)).strftime("%Y-%m-%d 09:15")
    to_date = target_date.strftime("%Y-%m-%d 15:30")

    res = safe_get_candle_data(smart_api, {
        "exchange": spot_exchange,
        "symboltoken": str(spot_token),
        "interval": "ONE_DAY",
        "fromdate": from_date,
        "todate": to_date,
    })

    if res and res.get("status") and res.get("data"):
        candles = res["data"]
        target_str = target_date.strftime("%Y-%m-%d")
        prev_candles = [c for c in candles if c[0][:10] < target_str]
        if prev_candles:
            return float(prev_candles[-1][4])  # close of last prev-day candle
    return None

def get_today_open(smart_api, spot_exchange, spot_token, target_date):
    """Get opening price for a specific date."""
    target_str = target_date.strftime("%Y-%m-%d")
    from_date = f"{target_str} 09:15"
    to_date = f"{target_str} 09:17"

    res = safe_get_candle_data(smart_api, {
        "exchange": spot_exchange,
        "symboltoken": str(spot_token),
        "interval": "ONE_MINUTE",
        "fromdate": from_date,
        "todate": to_date,
    })

    if res and res.get("status") and res.get("data"):
        candles = res["data"]
        if candles:
            return float(candles[0][1])  # open of 9:15 candle
    return None

def get_option_ltp(smart_api, option_token, option_symbol, option_exchange, target_date, target_time):
    """Get option LTP at a specific time using historical candles."""
    import time
    target_str = target_date.strftime("%Y-%m-%d")
    from_date = f"{target_str} {target_time}"
    to_date = f"{target_str} {target_time}"

    print(f"    Fetching option data for {option_symbol} at {target_time}...")
    time.sleep(1.0)  # Rate limit delay

    res = safe_get_candle_data(smart_api, {
        "exchange": option_exchange,
        "symboltoken": str(option_token),
        "interval": "ONE_MINUTE",
        "fromdate": from_date,
        "todate": to_date,
    })

    if res and res.get("status") and res.get("data"):
        candles = res["data"]
        if candles:
            close_price = float(candles[0][4])
            print(f"    Got option price: Rs.{close_price:.2f}")
            return close_price
        else:
            print(f"    No candles found for {target_time}")
    else:
        print(f"    API call failed for {target_time}")

    return None

def load_local_sensex_data(target_date):
    """Load local SENSEX CSV data for the target date."""
    date_folder = target_date.strftime("%d-%m-%Y")
    csv_path = DATA_DIR / date_folder / f"SENSEX-{date_folder}.csv"

    if not csv_path.exists():
        print(f"    [!] Local data file not found: {csv_path}")
        return None

    print(f"    Loading local data from: {csv_path}")
    data = []
    try:
        with open(csv_path, 'r', encoding='utf-8') as f:
            reader = csv.DictReader(f)
            for row in reader:
                data.append(row)
        print(f"    Loaded {len(data)} rows of local data")
        return data
    except Exception as e:
        print(f"    [!] Error loading local data: {e}")
        return None

def load_backtest_data():
    """Load backtest data to get actual trade results."""
    backtest_path = DATA_DIR / "backtest_daybreakout_15m_SENSEX_naked.csv"

    if not backtest_path.exists():
        print(f"    [!] Backtest data file not found: {backtest_path}")
        return None

    print(f"    Loading backtest data from: {backtest_path}")
    data = []
    try:
        with open(backtest_path, 'r', encoding='utf-8') as f:
            reader = csv.DictReader(f)
            for row in reader:
                data.append(row)
        print(f"    Loaded {len(data)} backtest records")
        return data
    except Exception as e:
        print(f"    [!] Error loading backtest data: {e}")
        return None

def get_trade_from_backtest(backtest_data, target_date, entry_time):
    """Get trade details from backtest data for specific date and time."""
    if not backtest_data:
        return None

    target_date_str = target_date.strftime("%Y-%m-%d")

    for row in backtest_data:
        if row['date'] == target_date_str and row['entry_time'] == entry_time:
            return row

    return None

def get_available_strikes_from_local_data(local_data, option_type):
    """Get available strikes from local data for a given option type."""
    if not local_data:
        return []

    first_row = local_data[0]
    strikes = []

    if option_type == 'CE':
        for i in range(1, 6):
            strike_col = f'ce_strike_{i}'
            if strike_col in first_row and first_row[strike_col]:
                try:
                    strike = int(first_row[strike_col])
                    strikes.append(strike)
                except (ValueError, KeyError):
                    continue
    elif option_type == 'PE':
        for i in range(1, 6):
            strike_col = f'pe_strike_{i}'
            if strike_col in first_row and first_row[strike_col]:
                try:
                    strike = int(first_row[strike_col])
                    strikes.append(strike)
                except (ValueError, KeyError):
                    continue

    return strikes

def find_closest_strike(target_strike, available_strikes):
    """Find the closest available strike to target."""
    if not available_strikes:
        return None

    return min(available_strikes, key=lambda x: abs(x - target_strike))

def get_option_price_from_local_data(local_data, target_time, option_type, target_strike):
    """Get option price from local data at specific time."""
    if not local_data:
        return None

    # First check if exact strike is available
    available_strikes = get_available_strikes_from_local_data(local_data, option_type)

    if target_strike not in available_strikes:
        # Find closest strike
        closest_strike = find_closest_strike(target_strike, available_strikes)
        if closest_strike:
            print(f"    Target strike {target_strike} not found, using closest: {closest_strike}")
            target_strike = closest_strike
        else:
            return None

    # Find the closest time match
    for row in local_data:
        row_time = row['time'][:5]  # Get HH:MM from HH:MM:SS
        if row_time == target_time:
            # Map column names to find the right option price
            if option_type == 'CE':
                # Look for the matching strike in CE columns
                for i in range(1, 6):
                    strike_col = f'ce_strike_{i}'
                    price_col = f'ce_{i}'
                    if strike_col in row and price_col in row:
                        try:
                            strike = int(row[strike_col])
                            if strike == target_strike:
                                return float(row[price_col])
                        except (ValueError, KeyError):
                            continue
            elif option_type == 'PE':
                # Look for the matching strike in PE columns
                for i in range(1, 6):
                    strike_col = f'pe_strike_{i}'
                    price_col = f'pe_{i}'
                    if strike_col in row and price_col in row:
                        try:
                            strike = int(row[strike_col])
                            if strike == target_strike:
                                return float(row[price_col])
                        except (ValueError, KeyError):
                            continue

    return None

def get_option_intraday_candles(smart_api, option_token, option_exchange, target_date):
    """Get all intraday candles for the option to find prices at specific times."""
    import time
    target_str = target_date.strftime("%Y-%m-%d")
    from_date = f"{target_str} 09:15"
    to_date = f"{target_str} 15:30"

    # Try different intervals in order of preference
    intervals = ["ONE_MINUTE", "FIVE_MINUTE", "FIFTEEN_MINUTE", "ONE_DAY"]

    for interval in intervals:
        print(f"    Fetching option candles for token {option_token} with {interval} interval...")
        print(f"    Date range: {from_date} to {to_date}")
        time.sleep(1.0)  # Rate limit delay

        params = {
            "exchange": option_exchange,
            "symboltoken": str(option_token),
            "interval": interval,
            "fromdate": from_date,
            "todate": to_date,
        }

        print(f"    API params: {params}")
        res = safe_get_candle_data(smart_api, params)

        if res:
            print(f"    API response status: {res.get('status')}")
            if res.get("data"):
                candles = res["data"]
                print(f"    Number of candles received: {len(candles)}")
                if len(candles) > 0:
                    return candles, interval
                else:
                    print(f"    No data in response for {interval}")
            else:
                print(f"    No data in response for {interval}")
        else:
            print(f"    API call returned None for {interval}")

        # Try wider date range if specific date fails
        print(f"    Trying wider date range for {interval}...")
        wider_from = f"{target_str} 09:00"
        wider_to = f"{target_str} 16:00"

        params_wide = {
            "exchange": option_exchange,
            "symboltoken": str(option_token),
            "interval": interval,
            "fromdate": wider_from,
            "todate": wider_to,
        }

        time.sleep(0.5)
        res_wide = safe_get_candle_data(smart_api, params_wide)

        if res_wide and res_wide.get("data") and len(res_wide["data"]) > 0:
            print(f"    Success with wider range: {len(res_wide['data'])} candles")
            return res_wide["data"], interval
        else:
            print(f"    Wider range also failed for {interval}")

    return [], None

def analyze_daybreakout(target_date):
    """Main analysis function for a given target date."""
    # Find actual previous trading day (handle weekends/holidays)
    smart_api = login_smart_api()
    inst = INSTRUMENTS["SENSEX"]

    print(f"[*] Finding previous trading day before {target_date.strftime('%Y-%m-%d')}...")
    prev_date_str = get_previous_trading_day(
        smart_api,
        inst["spot_exchange"],
        inst["spot_token"],
        target_date
    )
    prev_date = datetime.strptime(prev_date_str, "%Y-%m-%d")
    print(f"[+] Previous trading day: {prev_date_str}")

    print("=" * 65)
    print(f"ANALYZING DAYBREAKOUT STRATEGY FOR {target_date.strftime('%Y-%m-%d')}")
    print(f"INSTRUMENT: SENSEX NAKED OPTIONS")
    print("=" * 65)

    # Get SENSEX config
    inst = INSTRUMENTS["SENSEX"]
    spot_exchange = inst["spot_exchange"]
    spot_token = inst["spot_token"]

    # Step 1: Get previous day range (2026-07-23)
    print(f"\n[*] Step 1: Fetching previous day range for {prev_date.strftime('%Y-%m-%d')}...")
    range_data = get_previous_day_range(
        smart_api=smart_api,
        spot_exchange=spot_exchange,
        spot_token=spot_token,
        trading_day=prev_date.strftime("%Y-%m-%d")
    )
    range_high = range_data["high"]
    range_low = range_data["low"]

    # Step 2: Get gap analysis
    print(f"\n[*] Step 2: Fetching gap analysis for {target_date.strftime('%Y-%m-%d')}...")
    prev_close = get_prev_close(smart_api, spot_exchange, spot_token, target_date)
    today_open = get_today_open(smart_api, spot_exchange, spot_token, target_date)

    if prev_close and today_open:
        gap_points = today_open - prev_close
        gap_pct = gap_points / prev_close
        gap_info = classify_gap(gap_pct)
        gap_info.prev_close = prev_close
        gap_info.today_open = today_open
        gap_info.gap_points = gap_points

        print(f"    Previous Close: {prev_close:.2f}")
        print(f"    Today's Open: {today_open:.2f}")
        print(f"    Gap: {gap_points:+.2f} pts ({gap_pct*100:+.2f}%)")
        print(f"    Classification: {gap_info.label}")
        print(f"    Caution Level: {gap_info.caution_level}/4")
    else:
        print("    [!] Could not fetch gap data")
        gap_info = GapInfo()

    # Step 3: Fetch 15-minute candles for 2026-07-24
    print(f"\n[*] Step 3: Fetching 15-minute candles for {target_date.strftime('%Y-%m-%d')}...")
    from_str = f"{target_date.strftime('%Y-%m-%d')} 09:15"
    to_str = f"{target_date.strftime('%Y-%m-%d')} 15:30"

    candles = fetch_15m_candles(smart_api, spot_exchange, spot_token, from_str, to_str)
    print(f"    Fetched {len(candles)} 15-minute candles")

    # Step 4: Simulate strategy signals
    print(f"\n[*] Step 4: Simulating strategy signals (REVERSE mode, NAKED structure)...")
    print(f"    Range: [{range_low:.2f}, {range_high:.2f}]")
    print(f"    Last entry: 14:45")
    print(f"    Extra wait for large gap: {extra_15m_candles_wait()} candles")

    gap_territory_touched = False
    signals = []

    for i, candle in enumerate(candles):
        c_time = str(candle[0])[11:16]
        c_close = float(candle[4])

        signal = evaluate_15m_close(
            c_time=c_time,
            c_close=c_close,
            range_high=range_high,
            range_low=range_low,
            gap_info=gap_info,
            gap_territory_touched=gap_territory_touched,
            candle_index=i,
            extra_candles_wait=extra_15m_candles_wait(),
            last_entry_hhmm="14:45"
        )

        gap_territory_touched = signal.gap_territory_touched

        if not signal.skip:
            # Apply reverse (default is True)
            final_direction = apply_reverse(signal.direction, reverse=True)
            signals.append({
                "time": c_time,
                "close": c_close,
                "original_direction": signal.direction,
                "final_direction": final_direction,
                "trade_type": signal.trade_type,
                "reason": signal.spot_reason
            })
            print(f"    [{c_time}] SIGNAL: {signal.direction} {signal.trade_type} @ {c_close:.2f} -> REVERSE to {final_direction}")
            print(f"             Reason: {signal.spot_reason}")
        elif signal.skip_reason != "No 15m close signal":
            print(f"    [{c_time}] SKIP: {signal.skip_reason} @ {c_close:.2f}")

    # Step 5: Option Contract Analysis and P&L for all signals
    print(f"\n[*] Step 5: Analyzing All Trades ({len(signals)} signals)")

    # Load backtest data once for all trades
    backtest_data = load_backtest_data()
    backtest_trades_for_date = []
    if backtest_data:
        target_date_str = target_date.strftime("%Y-%m-%d")
        for row in backtest_data:
            if row['date'] == target_date_str:
                backtest_trades_for_date.append(row)
        print(f"    Found {len(backtest_trades_for_date)} backtest trades for {target_date_str}")

    # Analyze each signal
    for i, sig in enumerate(signals):
        print(f"\n    {'='*60}")
        print(f"    TRADE #{i+1} of {len(signals)}")
        print(f"    {'='*60}")

        spot_price = sig['close']
        direction = sig['final_direction']
        entry_time = sig['time']

        print(f"    Signal: {direction} {sig['trade_type']} @ {spot_price:.2f} ({entry_time})")
        print(f"    Reason: {sig['reason']}")

        # Check if this trade exists in backtest data
        backtest_trade = None
        for bt in backtest_trades_for_date:
            if bt['entry_time'] == entry_time:
                backtest_trade = bt
                break

        if backtest_trade:
            print(f"\n    [✓] Found in backtest data:")
            print(f"    Entry: {backtest_trade['entry_spot']} @ {backtest_trade['entry_time']}")
            print(f"    Exit: {backtest_trade['exit_spot']} @ {backtest_trade['exit_time']}")
            print(f"    Strike: {backtest_trade['buy_strike']} {backtest_trade['opt_type']}")
            print(f"    Entry Premium: Rs.{backtest_trade['entry_premium']}")
            print(f"    Exit Reason: {backtest_trade['exit_reason']}")
            print(f"    P&L: Rs.{backtest_trade['pnl']}")

            if float(backtest_trade['pnl']) > 0:
                print(f"    RESULT: PROFIT of Rs.{backtest_trade['pnl']}")
            else:
                print(f"    RESULT: LOSS of Rs.{abs(float(backtest_trade['pnl']))}")
        else:
            print(f"    [!] Not found in backtest data")
            print(f"    Skipping detailed analysis for this trade")

    # Summary of all trades
    if backtest_trades_for_date:
        print(f"\n[*] SUMMARY: All Trades for {target_date_str}")
        total_pnl = 0.0
        for i, bt in enumerate(backtest_trades_for_date):
            pnl = float(bt['pnl'])
            total_pnl += pnl
            result = "PROFIT" if pnl > 0 else "LOSS"
            print(f"    Trade {i+1}: {bt['entry_time']} {bt['direction']} {bt['opt_type']} - {result} Rs.{pnl:.2f}")

        print(f"\n    Total P&L for {target_date_str}: Rs.{total_pnl:.2f}")
        if total_pnl > 0:
            print(f"    NET RESULT: PROFIT of Rs.{total_pnl:.2f}")
        else:
            print(f"    NET RESULT: LOSS of Rs.{abs(total_pnl):.2f}")

    return  # Skip the single trade analysis below

    # Final Summary
    print(f"\n[*] FINAL SUMMARY")
    print(f"    Previous Day Range ({prev_date.strftime('%Y-%m-%d')}):")
    print(f"      High: {range_high:.2f}")
    print(f"      Low: {range_low:.2f}")

    if gap_info.label:
        print(f"    Gap Analysis ({target_date.strftime('%Y-%m-%d')}):")
        print(f"      Previous Close: {prev_close:.2f}")
        print(f"      Today's Open: {today_open:.2f}")
        print(f"      Gap: {gap_points:+.2f} pts ({gap_pct*100:+.2f}%)")
        print(f"      Classification: {gap_info.label}")
        print(f"      Caution Level: {gap_info.caution_level}/4")

    print(f"    Total Signals Generated: {len(signals)}")
    if signals:
        print(f"    First Signal: {signals[0]['time']} - {signals[0]['final_direction']} {signals[0]['trade_type']}")
    else:
        print(f"    RESULT: NO TRADE - No valid signals generated")

    print("\n" + "=" * 65)

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Analyze Day Breakout Strategy for a specific date")
    parser.add_argument("--date", type=str, help="Target date in YYYY-MM-DD format (default: 2026-08-10)")
    args = parser.parse_args()

    if args.date:
        try:
            target_date = datetime.strptime(args.date, "%Y-%m-%d")
        except ValueError:
            print("Invalid date format. Use YYYY-MM-DD")
            exit(1)
    else:
        target_date = datetime(2026, 8, 10)

    analyze_daybreakout(target_date)
