"""
Backtest Engine for Day Breakout Strategy using 15-minute candles.
- Marks previous day (12:30 PM - 3:15 PM) range (High & Low).
- Evaluates 15-minute candles on subsequent day for breakouts:
    * High Broken -> Buy Bull Call Spread (300 pt NIFTY / 1,000 pt SENSEX)
    * Low Broken  -> Buy Bear Put Spread  (300 pt NIFTY / 1,000 pt SENSEX)
- Gap & Opening Rules applied per day:
    * Gap classified: SMALL / MEDIUM / LARGE / VERY_LARGE (UP / DOWN / FLAT)
    * LARGE gaps: no-chase rule adds extra confirmation wait in gap direction
    * VERY_LARGE gaps: flagged in output; extra wait also applied
    * Gap data recorded in CSV: gap_pct, gap_direction, gap_size, caution_level
- Recovery Detection (LARGE / VERY_LARGE gaps only):
    * Tracks when price enters gap territory (below range_low for Gap-Down,
      above range_high for Gap-Up).
    * When a 15-min candle closes BACK inside the range, a recovery trade fires:
        Gap-Down recovery -> Bull Call Spread (price rebounding)
        Gap-Up  recovery -> Bear Put Spread  (price fading)
    * trade_type column: 'BREAKOUT' or 'RECOVERY'
    * After a recovery trade, gap_territory_touched resets.
- Exact broken_call risk management:
    * max_loss = -5000
    * max_profit = 50000
    * profit_threshold = 3000
    * min_profit_step = 2000
    * Trailing locked profit
    * Daily max loss = -10000
    * Max daily trades = 4
- Profit tracking on 15-minute candles (matches live trading behavior)
- Outputs a comprehensive performance summary table on run.
"""

import sys
import io
import math
import time
import argparse
from pathlib import Path
from datetime import datetime, timedelta
from typing import Dict, Any, List, Optional, Tuple
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

import csv
import json
import pyotp
from SmartApi.smartConnect import SmartConnect
import config
from live.daybreakout.config import INSTRUMENTS, RISK_CONFIG, GAP_CONFIG, DATA_DIR, VIX_CONFIG, BREAKOUT_CONFIG
from live.daybreakout.gap_analyzer import classify_gap, GapInfo
from live.daybreakout.signals import apply_reverse, evaluate_15m_close, extra_15m_candles_wait
from live.daybreakout.contract_finder import load_scrip_master, find_option_contract


def candle_end_datetime(timestamp: str) -> datetime:
    """Return the close time of a 15-minute candle from its start timestamp."""
    normalized = str(timestamp).replace("T", " ").split("+")[0].split("Z")[0].strip()
    if len(normalized) == 16:
        normalized += ":00"
    return datetime.strptime(normalized[:19], "%Y-%m-%d %H:%M:%S") + timedelta(minutes=15)


def candle_end_hhmm(timestamp: str) -> str:
    """Return the 15-minute candle close time as HH:MM for trade reporting."""
    return candle_end_datetime(timestamp).strftime("%H:%M")


# ============================================================
# OPTION PRICING HELPERS (Black-Scholes Fallback)
# ============================================================

def _norm_cdf(x: float) -> float:
    return (1.0 + math.erf(x / math.sqrt(2.0))) / 2.0


def _bs_call(S: float, K: float, T: float, r: float = 0.07, sigma: float = 0.14) -> float:
    if T <= 1e-5:
        return max(0.0, S - K)
    d1 = (math.log(S / K) + (r + 0.5 * sigma ** 2) * T) / (sigma * math.sqrt(T))
    d2 = d1 - sigma * math.sqrt(T)
    return S * _norm_cdf(d1) - K * math.exp(-r * T) * _norm_cdf(d2)


def _bs_put(S: float, K: float, T: float, r: float = 0.07, sigma: float = 0.14) -> float:
    if T <= 1e-5:
        return max(0.0, K - S)
    d1 = (math.log(S / K) + (r + 0.5 * sigma ** 2) * T) / (sigma * math.sqrt(T))
    d2 = d1 - sigma * math.sqrt(T)
    return K * math.exp(-r * T) * _norm_cdf(-d2) - S * _norm_cdf(-d1)


def get_option_premium(
    spot: float,
    buy_strike: float,
    sell_strike: Optional[float],
    direction: str,
    days_to_expiry: float = 4.0,
    sigma: float = 0.14,
    structure: str = "spread",
    time_to_expiry_years: Optional[float] = None
) -> float:
    """Calculates model option premium for 2-leg spread or 1-leg naked option.

    time_to_expiry_years: Exact time to expiry in years (preferred). When
        supplied it is used directly so intraday time decay is modelled.
    days_to_expiry: Fallback calendar days to expiry (converted to years).
    """
    if time_to_expiry_years is not None:
        # One-minute floor keeps Black-Scholes well-defined on expiry day.
        T = max(time_to_expiry_years, 1.0 / (365.0 * 24.0 * 60.0))
    else:
        T = max(0.001, days_to_expiry / 365.0)
    is_bullish = direction.upper() in ("BULLISH", "BUY_CALL_SPREAD", "CE")
    is_naked = structure.lower() == "naked" or sell_strike is None

    if is_naked:
        if is_bullish:
            return max(0.05, _bs_call(spot, buy_strike, T, sigma=sigma))
        else:
            return max(0.05, _bs_put(spot, buy_strike, T, sigma=sigma))
    else:
        if is_bullish:
            c_buy = _bs_call(spot, buy_strike, T, sigma=sigma)
            c_sell = _bs_call(spot, sell_strike, T, sigma=sigma)
            return max(0.05, c_buy - c_sell)
        else:
            p_buy = _bs_put(spot, buy_strike, T, sigma=sigma)
            p_sell = _bs_put(spot, sell_strike, T, sigma=sigma)
            return max(0.05, p_buy - p_sell)


def get_spread_premium(spot: float, buy_strike: float, sell_strike: float, direction: str, days_to_expiry: float = 4.0, sigma: float = 0.14) -> float:
    """Legacy wrapper around get_option_premium for spread structure."""
    return get_option_premium(spot, buy_strike, sell_strike, direction, days_to_expiry, sigma, structure="spread")


def calculate_days_to_expiry(current_date: str, instrument: str = "NIFTY") -> float:
    """
    Calculate days to expiry based on current date and instrument.
    For Indian markets:
    - NIFTY/BANKNIFTY: Weekly options expire on Tuesday
    - SENSEX: Weekly options expire on Thursday
    - Monthly options expire on last Thursday of month
    - This uses weekly expiry for intraday trading (most common approach)
    """
    from datetime import datetime, timedelta

    try:
        curr_date = datetime.strptime(current_date, "%Y-%m-%d")
    except:
        # Fallback if date format is different
        return 4.0

    # Determine expiry day based on instrument
    # Monday=0, Tuesday=1, Wednesday=2, Thursday=3, Friday=4, Saturday=5, Sunday=6
    instr_upper = instrument.upper()
    if instr_upper in ("NIFTY", "BANKNIFTY"):
        expiry_weekday = 1  # Tuesday
    elif instr_upper == "SENSEX":
        expiry_weekday = 3  # Thursday
    else:
        expiry_weekday = 3  # Default to Thursday

    current_weekday = curr_date.weekday()

    if current_weekday < expiry_weekday:
        days_to_expiry = expiry_weekday - current_weekday
    elif current_weekday == expiry_weekday:
        days_to_expiry = 0  # Same day expiry
    else:
        days_to_expiry = 7 - current_weekday + expiry_weekday  # Next expiry day

    # For intraday trading, we typically trade weekly options
    # Return days (will be converted to fraction of year in get_option_premium)
    return max(0.01, days_to_expiry)


def expiry_weekday_for(instrument: str) -> int:
    """Weekly expiry weekday (Mon=0 ... Sun=6) for the instrument."""
    instr_upper = instrument.upper()
    if instr_upper in ("NIFTY", "BANKNIFTY"):
        return 1  # Tuesday
    if instr_upper == "SENSEX":
        return 3  # Thursday
    return 3  # Default to Thursday


def nearest_expiry_datetime(ref_dt: datetime, instrument: str) -> datetime:
    """Returns the next weekly expiry at 15:30 on/after ref_dt."""
    expiry_weekday = expiry_weekday_for(instrument)
    days_ahead = (expiry_weekday - ref_dt.weekday()) % 7
    expiry_date = (ref_dt + timedelta(days=days_ahead)).date()
    expiry_dt = datetime.combine(expiry_date, datetime.min.time()).replace(
        hour=15, minute=30
    )
    if expiry_dt <= ref_dt:
        expiry_dt += timedelta(days=7)
    return expiry_dt


def time_to_expiry_years(
    curr_date: str,
    hhmm: str,
    instrument: str,
    expiry_date: Optional[Any] = None,
) -> float:
    """Exact time to expiry (in years) from a date/time until 15:30 on expiry day.

    expiry_date: Optional real expiry date (from the scrip master). When given
        it correctly handles holiday-shifted expiries; otherwise the weekly
        weekday heuristic is used.
    """
    ref_dt = datetime.strptime(f"{curr_date} {hhmm}", "%Y-%m-%d %H:%M")
    if expiry_date is not None:
        expiry_dt = datetime.combine(expiry_date, datetime.min.time()).replace(
            hour=15, minute=30
        )
        if expiry_dt <= ref_dt:
            expiry_dt += timedelta(days=7)
    else:
        expiry_dt = nearest_expiry_datetime(ref_dt, instrument)
    secs = max(60.0, (expiry_dt - ref_dt).total_seconds())
    return secs / (365.0 * 24.0 * 3600.0)


def term_structure_sigma(
    days_to_expiry: float,
    base_sigma: float = 0.14,
    instrument: Optional[str] = None,
) -> float:
    """Implied volatility term structure for weekly index options.

    Calibrated from real option premiums for each index. NIFTY ATM IV is
    roughly 12-14% with 4-5 days left but rises steeply to ~32-36% in the
    final day (0DTE gamma). SENSEX ATM IV is lower and flatter (~15-17% at
    6-8 days, ~20-23% at 2-3 days, ~27% at 1 day). Used only when real
    option candles are unavailable.
    """
    days = max(0.0, float(days_to_expiry))
    if (instrument or "").upper() == "SENSEX":
        long_iv = max(0.10, base_sigma)
        near_iv = max(long_iv, 0.28)
        decay = 0.30
    else:
        long_iv = max(0.10, base_sigma)
        near_iv = max(long_iv, 0.36)
        decay = 0.90
    return long_iv + (near_iv - long_iv) * math.exp(-decay * max(0.0, days - 1.0))


def str2bool(v):
    if isinstance(v, bool):
        return v
    if str(v).strip().lower() in ("yes", "true", "t", "y", "1"):
        return True
    elif str(v).strip().lower() in ("no", "false", "f", "n", "0"):
        return False
    else:
        raise argparse.ArgumentTypeError("Boolean value expected (true/false).")


def get_nearest_expiry_instrument_for_date(curr_date: str) -> Tuple[str, float]:
    """
    Returns (instrument_name, days_to_expiry) for the nearest weekly expiry between NIFTY and SENSEX.
    """
    days_nifty = calculate_days_to_expiry(curr_date, "NIFTY")
    days_sensex = calculate_days_to_expiry(curr_date, "SENSEX")
    if days_sensex < days_nifty:
        return "SENSEX", days_sensex
    else:
        return "NIFTY", days_nifty


# ============================================================
# GAP COMPUTATION FROM CANDLE DATA (no extra API call)
# ============================================================

def compute_gap_from_candles(
    prev_candles: List[List[Any]],
    curr_candles: List[List[Any]],
) -> GapInfo:
    """
    Computes the overnight gap purely from already-fetched 15-minute candles.

    Previous day close  = close of the last 15-min candle on prev_date.
    Today's open        = open of the first 15-min candle on curr_date (9:15).

    Returns a classified GapInfo (no API calls needed).
    """
    if not prev_candles or not curr_candles:
        return GapInfo(fetch_error="Insufficient candle data for gap computation.")

    # Previous day close — last candle of the previous session
    prev_close = float(prev_candles[-1][4])

    # Today's open — first candle of the current session
    today_open = float(curr_candles[0][1])

    if prev_close <= 0:
        return GapInfo(fetch_error="Invalid previous close (<=0).")

    gap_pct = (today_open - prev_close) / prev_close   # signed fraction
    info = classify_gap(gap_pct)
    info.prev_close = round(prev_close, 2)
    info.today_open = round(today_open, 2)
    info.gap_points = round(today_open - prev_close, 2)
    return info


# ============================================================
# API LOGIN & CANDLE FETCHING
# ============================================================

def login_smart_api() -> SmartConnect:
    """Authenticates with Angel One SmartAPI."""
    api_key = config.api_key.strip()
    smart_api = SmartConnect(api_key, disable_ssl=True)
    totp = pyotp.TOTP(config.token).now()
    session = smart_api.generateSession(config.username, config.pwd, totp)
    if not session.get("status"):
        raise RuntimeError(f"SmartAPI login failed: {session}")
    return smart_api


def fetch_15m_candles(smart_api: SmartConnect, exchange: str, token: str, days_back: int = 15, end_date: Optional[datetime] = None) -> List[List[Any]]:
    """Fetches 15-minute historical candles for the spot index with retry on rate-limit."""
    custom_date = end_date if end_date else datetime.now()
    from_date = (custom_date - timedelta(days=days_back + 5)).strftime("%Y-%m-%d 09:15")
    to_date = custom_date.strftime("%Y-%m-%d 15:30")

    print(f"[*] Fetching 15-minute candles from {from_date} to {to_date}...")

    params = {
        "exchange": exchange,


        "symboltoken": str(token),
        "interval": "FIFTEEN_MINUTE",
        "fromdate": from_date,
        "todate": to_date,
    }

    max_attempts = 5
    base_wait = 3  # seconds
    for attempt in range(1, max_attempts + 1):
        try:
            res = smart_api.getCandleData(params)
        except Exception as exc:
            err_msg = str(exc)
            if "access rate" in err_msg.lower() or "JSONDecodeError" in type(exc).__name__:
                wait = base_wait * (2 ** (attempt - 1))
                print(f"[!] Rate-limited by API (attempt {attempt}/{max_attempts}). Retrying in {wait}s...")
                time.sleep(wait)
                continue
            raise

        if res and res.get("status") and res.get("data"):
            candles = res["data"]
            print(f"[+] Loaded {len(candles)} 15-minute candles.")
            return candles

        # Check for rate-limit message inside a valid response body
        msg = (res or {}).get("message", "") if res else ""
        if "access rate" in msg.lower():
            wait = base_wait * (2 ** (attempt - 1))
            print(f"[!] Rate-limited by API (attempt {attempt}/{max_attempts}): {msg}. Retrying in {wait}s...")
            time.sleep(wait)
            continue

        raise RuntimeError(f"Failed to fetch 15m candles: {res}")

    raise RuntimeError(f"API rate-limit persisted after {max_attempts} attempts. Please wait and retry.")


def fetch_5m_candles(smart_api: SmartConnect, exchange: str, token: str, days_back: int = 15, end_date: Optional[datetime] = None) -> List[List[Any]]:
    """Fetches 15-minute candles for profit tracking (using same interval as signal generation)."""
    # Note: Function name kept for backward compatibility, but now fetches 15-minute candles
    # to match live trading behavior where P&L is checked every 15 minutes
    custom_date = end_date if end_date else datetime.now()
    from_date = (custom_date - timedelta(days=days_back + 5)).strftime("%Y-%m-%d 09:15")
    to_date = custom_date.strftime("%Y-%m-%d 15:30")

    print(f"[*] Fetching 15-minute candles for profit tracking from {from_date} to {to_date}...")

    params = {
        "exchange": exchange,
        "symboltoken": str(token),
        "interval": "FIFTEEN_MINUTE",
        "fromdate": from_date,
        "todate": to_date,
    }

    max_attempts = 5
    base_wait = 3  # seconds
    for attempt in range(1, max_attempts + 1):
        try:
            res = smart_api.getCandleData(params)
        except Exception as exc:
            err_msg = str(exc)
            if "access rate" in err_msg.lower() or "JSONDecodeError" in type(exc).__name__:
                wait = base_wait * (2 ** (attempt - 1))
                print(f"[!] Rate-limited by API (attempt {attempt}/{max_attempts}). Retrying in {wait}s...")
                time.sleep(wait)
                continue
            raise

        if res and res.get("status") and res.get("data"):
            candles = res["data"]
            print(f"[+] Loaded {len(candles)} 15-minute candles for profit tracking.")
            return candles

        # Check for rate-limit message inside a valid response body
        msg = (res or {}).get("message", "") if res else ""
        if "access rate" in msg.lower():
            wait = base_wait * (2 ** (attempt - 1))
            print(f"[!] Rate-limited by API (attempt {attempt}/{max_attempts}): {msg}. Retrying in {wait}s...")
            time.sleep(wait)
            continue

        raise RuntimeError(f"Failed to fetch 15m candles: {res}")

    raise RuntimeError(f"API rate-limit persisted after {max_attempts} attempts. Please wait and retry.")


# ============================================================
# REAL OPTION PRICE LOOKUP (historical market premiums)
# ============================================================

OPTION_CACHE_DIR = ROOT / "backtest" / "cache_daybreakout_options"
_option_price_cache: Dict[Tuple[str, str], Dict[str, float]] = {}
_contract_token_cache: Dict[Tuple[str, str, float, str, str], Optional[str]] = {}
_expiry_date_cache: Dict[Tuple[str, str, str], Optional[Any]] = {}


def nearest_expiry_for_date(symbol_base: str, exchange: str, date_str: str) -> Optional[Any]:
    """Nearest weekly expiry on/after date_str from the scrip master.

    Returns None when the master does not cover that period (e.g. old backtests)
    or when the nearest expiry is too far away to be the weekly contract.
    """
    key = (symbol_base.upper(), exchange.upper(), date_str)
    if key in _expiry_date_cache:
        return _expiry_date_cache[key]

    try:
        ref_date = datetime.strptime(date_str, "%Y-%m-%d").date()
    except ValueError:
        _expiry_date_cache[key] = None
        return None

    expiries = set()
    for item in load_scrip_master():
        if (
            item.get("name") == symbol_base
            and item.get("exch_seg") == exchange
            and item.get("instrumenttype") == "OPTIDX"
        ):
            exp_str = item.get("expiry")
            if not exp_str:
                continue
            try:
                exp_date = datetime.strptime(exp_str, "%d%b%Y").date()
            except ValueError:
                continue
            if exp_date >= ref_date:
                expiries.add(exp_date)

    expiry = min(expiries) if expiries else None
    # Guard against using a months-away expiry when historical data isn't covered.
    if expiry is not None and (expiry - ref_date).days > 14:
        expiry = None
    _expiry_date_cache[key] = expiry
    return expiry


def resolve_option_token(symbol_base: str, exchange: str, strike: float, option_type: str, date_str: str) -> Optional[str]:
    """Resolves the broker token for an option contract on a given historical date."""
    expiry = nearest_expiry_for_date(symbol_base, exchange, date_str)
    if expiry is None:
        return None

    key = (symbol_base.upper(), exchange.upper(), float(strike), option_type.upper(), expiry.isoformat())
    if key in _contract_token_cache:
        return _contract_token_cache[key]

    contract = find_option_contract(
        symbol_base=symbol_base,
        exchange=exchange,
        strike=strike,
        option_type=option_type,
        expiry_date=expiry,
    )
    token = contract.get("token") if contract else None
    _contract_token_cache[key] = token
    return token


def fetch_option_day_prices(smart_api: SmartConnect, exchange: str, token: str, date_str: str) -> Dict[str, float]:
    """Fetches 15-minute closing prices for one option on one date.

    Returns a mapping of candle start HH:MM -> close. Cached in memory and on disk.
    """
    key = (date_str, str(token))
    if key in _option_price_cache:
        return _option_price_cache[key]

    OPTION_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache_file = OPTION_CACHE_DIR / f"{date_str}_{token}_{exchange}.json"
    if cache_file.exists():
        try:
            with open(cache_file, "r", encoding="utf-8") as f:
                prices = {str(k): float(v) for k, v in json.load(f).items()}
            _option_price_cache[key] = prices
            return prices
        except Exception:
            pass

    params = {
        "exchange": exchange,
        "symboltoken": str(token),
        "interval": "FIFTEEN_MINUTE",
        "fromdate": f"{date_str} 09:15",
        "todate": f"{date_str} 15:30",
    }

    max_attempts = 4
    base_wait = 2  # seconds
    for attempt in range(1, max_attempts + 1):
        try:
            res = smart_api.getCandleData(params)
        except Exception as exc:
            if "access rate" in str(exc).lower() or "JSONDecodeError" in type(exc).__name__:
                time.sleep(base_wait * attempt)
                continue
            raise

        if res and res.get("status") and res.get("data"):
            prices: Dict[str, float] = {}
            for c in res["data"]:
                prices[str(c[0])[11:16]] = float(c[4])
            _option_price_cache[key] = prices
            try:
                with open(cache_file, "w", encoding="utf-8") as f:
                    json.dump(prices, f)
            except Exception:
                pass
            time.sleep(0.35)  # gentle pacing to avoid API rate limits
            return prices

        msg = str((res or {}).get("message", ""))
        if "access rate" in msg.lower():
            time.sleep(base_wait * attempt)
            continue

        # Contract/date has no traded candles (e.g. illiquid deep OTM strike).
        _option_price_cache[key] = {}
        return {}

    return {}


def price_at(prices: Optional[Dict[str, float]], hhmm: str) -> Optional[float]:
    """Latest known option price at or before hhmm."""
    if not prices:
        return None
    if hhmm in prices:
        return prices[hhmm]
    prior = [k for k in prices if k <= hhmm]
    if not prior:
        return None
    return prices[max(prior)]


def market_premium_at(
    buy_prices: Optional[Dict[str, float]],
    sell_prices: Optional[Dict[str, float]],
    hhmm: str,
    is_naked: bool,
) -> Optional[float]:
    """Net market premium at hhmm, or None when market prices are unavailable."""
    buy_px = price_at(buy_prices, hhmm)
    if buy_px is None:
        return None
    if is_naked:
        return buy_px
    sell_px = price_at(sell_prices, hhmm)
    if sell_px is None:
        return None
    return buy_px - sell_px


# ============================================================
# BACKTEST ENGINE
# ============================================================
def run_15m_backtest(
    instrument_name: str = "NIFTY",
    days_back: int = 15,
    lots: int = 5,
    max_loss: float = RISK_CONFIG["max_loss"],
    fixed_loss_amount: float = RISK_CONFIG.get("fixed_loss_amount", 15000.0),
    enable_fixed_loss_sl: bool = RISK_CONFIG.get("enable_fixed_loss_sl", True),
    max_profit: float = RISK_CONFIG["max_profit"],
    profit_threshold: float = RISK_CONFIG["profit_threshold"],
    min_profit_step: float = RISK_CONFIG["min_profit_step"],
    daily_max_loss: float = RISK_CONFIG["daily_max_loss"],
    daily_max_profit: float = RISK_CONFIG.get("daily_max_profit", 25000.0),
    sl_cooldown_minutes: int = RISK_CONFIG.get("sl_cooldown_minutes", 45),
    exit_cooldown_minutes: Dict[str, int] = RISK_CONFIG.get("exit_cooldown_minutes", {}),
    first_entry_hhmm: str = BREAKOUT_CONFIG.get("first_entry_hhmm", "09:30"),
    max_daily_trades: int = RISK_CONFIG["max_daily_trades"],
    reverse: bool = True,
    structure: str = "spread",
    vix_bearish_thresh: float = VIX_CONFIG["bearish_vix_threshold"],
    vix_bullish_thresh: float = VIX_CONFIG["bullish_vix_threshold"],
    vix_max_stall_minutes: int = VIX_CONFIG.get("vix_max_stall_minutes", 45),
    vix_candle_check_count: int = VIX_CONFIG.get("vix_candle_check_count", 3),
    end_date: Optional[datetime] = None,
    combination: bool = False,
    x_factor: float = RISK_CONFIG.get("x_factor", 1.0),
    tag: str = "",
) -> Dict[str, Any]:
    """Runs the 15-minute candle Day Breakout backtest with Gap & Opening Rules and VIX volatility filter."""
    if combination:
        inst_key = "COMBINATION"
        display_title = "COMBINATION (Nearest Expiry: NIFTY & SENSEX)"
        inst = INSTRUMENTS["NIFTY"]
        unit_lot_size = "Dynamic"
        lot_source = "Per-instrument"
        spread_width = "300 / 1000"
        total_qty = f"{lots} lots"
        sigma = 0.14
    else:
        inst_key = instrument_name.upper()
        if inst_key not in INSTRUMENTS:
            raise ValueError(f"Unknown instrument: {instrument_name}")
        inst = INSTRUMENTS[inst_key]
        display_title = inst["display_name"]
        spot_symbol   = inst["spot_symbol"]
        spot_token    = inst["spot_token"]
        spot_exchange = inst["spot_exchange"]
        strike_step   = inst["strike_step"]
        spread_width  = inst["spread_width"]
        try:
            from live.daybreakout.contract_finder import get_instrument_lot_size
            unit_lot_size = get_instrument_lot_size(inst_key, inst.get("option_exchange"))
            lot_source = "OpenAPIScripMaster.json"
        except Exception as e:
            unit_lot_size = inst.get("default_lot_size", 65)
            lot_source = f"fallback ({e})"
        total_qty     = lots * unit_lot_size
        sigma         = 0.15 if inst_key == "SENSEX" else 0.14

    # Apply x-factor multiplier
    if x_factor != 1.0:
        lots = int(lots * x_factor)
        if combination:
            total_qty = f"{lots} lots"
        else:
            total_qty = lots * unit_lot_size
        max_loss = max_loss * x_factor
        fixed_loss_amount = fixed_loss_amount * x_factor
        max_profit = max_profit * x_factor
        profit_threshold = profit_threshold * x_factor
        min_profit_step = min_profit_step * x_factor
        daily_max_loss = daily_max_loss * x_factor
        daily_max_profit = daily_max_profit * x_factor
        print(f"[*] X-Factor Applied: {x_factor}x")
        print(f"    Scaled Lots: {lots} (Base: {int(lots/x_factor)})")
        print(f"    Scaled Total Qty: {total_qty}")
        print(f"    Scaled Max Loss: Rs.{max_loss:.0f}")
        print(f"    Scaled Fixed Loss SL: Rs.{fixed_loss_amount:.0f}")
        print(f"    Scaled Max Profit: Rs.{max_profit:.0f}")
        print(f"    Scaled Profit Threshold: Rs.{profit_threshold:.0f}")
        print(f"    Scaled Daily Max Loss: Rs.{daily_max_loss:.0f}")
        print(f"    Scaled Daily Max Profit: Rs.{daily_max_profit:.0f}")

    struct_clean  = structure.lower().strip()
    if struct_clean not in ("spread", "naked"):
        struct_clean = "spread"

    # Extra confirmation wait for LARGE/VERY_LARGE gaps (in 15-min candle units)
    extra_candles_wait = extra_15m_candles_wait()
    large_gap_extra_wait_sec = GAP_CONFIG["large_gap_extra_wait_seconds"]

    smart_api  = login_smart_api()

    if combination:
        print("[*] COMBINATION MODE: Loading historical candles for NIFTY and SENSEX...")
        inst_nifty = INSTRUMENTS["NIFTY"]
        inst_sensex = INSTRUMENTS["SENSEX"]
        raw_15m_nifty = fetch_15m_candles(smart_api, inst_nifty["spot_exchange"], inst_nifty["spot_token"], days_back=days_back, end_date=end_date)
        raw_15m_profit_nifty = fetch_5m_candles(smart_api, inst_nifty["spot_exchange"], inst_nifty["spot_token"], days_back=days_back, end_date=end_date)
        time.sleep(1)
        raw_15m_sensex = fetch_15m_candles(smart_api, inst_sensex["spot_exchange"], inst_sensex["spot_token"], days_back=days_back, end_date=end_date)
        raw_15m_profit_sensex = fetch_5m_candles(smart_api, inst_sensex["spot_exchange"], inst_sensex["spot_token"], days_back=days_back, end_date=end_date)

        days_15m_by_inst: Dict[str, Dict[str, List[List[Any]]]] = {"NIFTY": {}, "SENSEX": {}}
        days_profit_by_inst: Dict[str, Dict[str, List[List[Any]]]] = {"NIFTY": {}, "SENSEX": {}}
        for c in raw_15m_nifty:
            days_15m_by_inst["NIFTY"].setdefault(c[0][:10], []).append(c)
        for c in raw_15m_profit_nifty:
            days_profit_by_inst["NIFTY"].setdefault(c[0][:10], []).append(c)
        for c in raw_15m_sensex:
            days_15m_by_inst["SENSEX"].setdefault(c[0][:10], []).append(c)
        for c in raw_15m_profit_sensex:
            days_profit_by_inst["SENSEX"].setdefault(c[0][:10], []).append(c)

        sorted_dates = sorted(set(days_15m_by_inst["NIFTY"].keys()) | set(days_15m_by_inst["SENSEX"].keys()))
    else:
        raw_15m_candles = fetch_15m_candles(smart_api, spot_exchange, spot_token, days_back=days_back, end_date=end_date)
        raw_15m_profit_candles = fetch_5m_candles(smart_api, spot_exchange, spot_token, days_back=days_back, end_date=end_date)
        days_15m_candles: Dict[str, List[List[Any]]] = {}
        for c in raw_15m_candles:
            date_str = c[0][:10]
            days_15m_candles.setdefault(date_str, []).append(c)

        days_15m_profit_candles: Dict[str, List[List[Any]]] = {}
        for c in raw_15m_profit_candles:
            date_str = c[0][:10]
            days_15m_profit_candles.setdefault(date_str, []).append(c)

        sorted_dates = sorted(days_15m_candles.keys())

    try:
        raw_vix_candles = fetch_15m_candles(smart_api, VIX_CONFIG["exchange"], VIX_CONFIG["token"], days_back=days_back, end_date=end_date)
    except Exception as exc:
        print(f"[!] Warning: Could not fetch India VIX candles: {exc}. VIX filter will be disabled.")
        raw_vix_candles = []

    # Group India VIX 15-minute candles by trading date for volatility filter
    days_vix_candles: Dict[str, List[List[Any]]] = {}
    for c in raw_vix_candles:
        date_str = c[0][:10]
        days_vix_candles.setdefault(date_str, []).append(c)
    sorted_vix_dates = sorted(days_vix_candles.keys())

    if len(sorted_dates) < 2:
        print("[!] Not enough trading days to run backtest (minimum 2 days required).")
        return {"trades": [], "summary": {}}

    print("\n" + "=" * 75)
    print(f"       DAY BREAKOUT 15-MIN BACKTEST: {display_title}")
    print("=" * 75)
    print(f"  Total Trading Days       : {len(sorted_dates)} days ({sorted_dates[0]} to {sorted_dates[-1]})")
    print(f"  Signal Interval          : 15 Minutes (FIFTEEN_MINUTE)")
    print(f"  Profit Tracking Interval : 15 Minutes (FIFTEEN_MINUTE)")
    print(f"  Range Window             : Previous Day 12:30 PM - 3:15 PM")
    print(f"  Entry Window             : {first_entry_hhmm} to {BREAKOUT_CONFIG.get('last_entry_hhmm', '14:45')}")
    print(f"  Option Structure         : {struct_clean.upper()} {'(' + str(spread_width) + ' pt spread)' if struct_clean == 'spread' else '(Single Leg Option Buy)'}")
    print(f"  Position Sizing          : {lots} lots ({total_qty} | Lot Size: {unit_lot_size} [{lot_source}])")
    print(f"  Risk Parameters          : Max Loss Rs.{max_loss:.0f} | Fixed Loss SL {'ON' if enable_fixed_loss_sl else 'OFF'} Rs.{fixed_loss_amount:.0f} | Trailing Min Profit Rs.{profit_threshold:.0f}")
    print(f"  Daily Limits             : Max Loss Rs.{daily_max_loss:.0f} | Max Profit Rs.{daily_max_profit:.0f} | SL Cooldown {sl_cooldown_minutes}m")
    print(f"  Gap Rules                : ACTIVE (LARGE gap extra wait = {large_gap_extra_wait_sec}s = {extra_candles_wait} candle(s))")
    print(f"  VIX Volatility Filter    : ACTIVE (Neg Inst & VIX > +{vix_bearish_thresh:.1f}% -> No Reverse | Pos Inst & VIX <= {vix_bullish_thresh:.1f}% -> No Reverse)")
    print("=" * 75 + "\n")

    all_trades: List[Dict[str, Any]] = []

    # Per-gap-size counters for summary
    gap_size_stats: Dict[str, Dict[str, Any]] = {
        "SMALL":      {"days": 0, "trades": 0, "pnl": 0.0, "recoveries": 0},
        "MEDIUM":     {"days": 0, "trades": 0, "pnl": 0.0, "recoveries": 0},
        "LARGE":      {"days": 0, "trades": 0, "pnl": 0.0, "recoveries": 0},
        "VERY_LARGE": {"days": 0, "trades": 0, "pnl": 0.0, "recoveries": 0},
    }
    total_recovery_trades = 0
    total_vix_filter_trades = 0
    total_vix_delay_trades = 0           # Trades that entered after a VIX delay
    total_vix_delay_override_trades = 0  # Trades where direction was overridden at max stall
    total_vix_delay_skipped = 0          # Signals skipped because delay pushed past 14:45

    # VIX delay config (from VIX_CONFIG)
    vix_increase_min  = VIX_CONFIG.get("vix_increase_min",  2.0)   # +2% lower bound for delay
    vix_delay_minutes = VIX_CONFIG.get("vix_delay_minutes", 30)    # 30-min delay window
    vix_decrease_max  = VIX_CONFIG.get("vix_decrease_max", -2.0)   # -2% upper bound for delay

    # Iterate starting from the second day
    for day_idx in range(1, len(sorted_dates)):
        prev_date = sorted_dates[day_idx - 1]
        curr_date = sorted_dates[day_idx]

        if combination:
            target_inst, days_to_expiry = get_nearest_expiry_instrument_for_date(curr_date)
            # Ensure target_inst has candles for prev_date and curr_date
            if (target_inst not in days_15m_by_inst or 
                prev_date not in days_15m_by_inst[target_inst] or 
                curr_date not in days_15m_by_inst[target_inst]):
                alt_inst = "NIFTY" if target_inst == "SENSEX" else "SENSEX"
                if (alt_inst in days_15m_by_inst and 
                    prev_date in days_15m_by_inst[alt_inst] and 
                    curr_date in days_15m_by_inst[alt_inst]):
                    target_inst = alt_inst
                    days_to_expiry = calculate_days_to_expiry(curr_date, target_inst)
                else:
                    continue

            active_inst_name = target_inst
            inst_info = INSTRUMENTS[active_inst_name]
            strike_step = inst_info["strike_step"]
            spread_width = inst_info["spread_width"]
            sigma = 0.15 if active_inst_name == "SENSEX" else 0.14
            try:
                from live.daybreakout.contract_finder import get_instrument_lot_size
                unit_lot_size = get_instrument_lot_size(
                    active_inst_name, inst_info.get("option_exchange")
                )
            except Exception:
                unit_lot_size = inst_info.get(
                    "default_lot_size", 25 if active_inst_name == "SENSEX" else 65
                )
            total_qty = lots * unit_lot_size

            prev_15m_candles = days_15m_by_inst[active_inst_name][prev_date]
            curr_15m_candles = days_15m_by_inst[active_inst_name][curr_date]
            curr_15m_profit_candles = days_profit_by_inst[active_inst_name].get(curr_date, [])
        else:
            active_inst_name = inst_key
            prev_15m_candles = days_15m_candles[prev_date]
            curr_15m_candles = days_15m_candles[curr_date]
            curr_15m_profit_candles = days_15m_profit_candles.get(curr_date, [])
            days_to_expiry = calculate_days_to_expiry(curr_date, active_inst_name)

        # Step 1: Mark Previous Day Range (12:30 -> 15:15)
        range_candles = [c for c in prev_15m_candles if "12:30" <= c[0][11:16] <= "15:15"]
        if not range_candles:
            continue

        range_high = max(float(c[2]) for c in range_candles)
        range_low  = min(float(c[3]) for c in range_candles)

        # Step 1b: Compute Gap & Opening Classification from candle data
        gap_info = compute_gap_from_candles(prev_15m_candles, curr_15m_candles)
        gap_size_stats[gap_info.size]["days"] += 1

        # Step 1c: Determine previous day VIX close for intraday VIX % calculation
        vix_prior_dates = [d for d in sorted_vix_dates if d < curr_date]
        vix_prev_close = float(days_vix_candles[vix_prior_dates[-1]][-1][4]) if vix_prior_dates else None
        curr_vix_candles = days_vix_candles.get(curr_date, [])

        daily_trades_count = 0
        daily_pnl = 0.0
        daily_loss = 0.0  # Track losses separately for daily max loss check (losses only)
        daily_profit = 0.0  # Track net P&L (profit + loss) for daily max profit check
        last_sl_exit_dt: Optional[datetime] = None
        last_exit_dt: Optional[datetime] = None
        last_exit_reason: Optional[str] = None

        # Recovery detection state (reset each day)
        gap_territory_touched = False   # Has price been in gap territory today?

        # VIX delay state for backtest (reset each day).
        # ONE active delay at a time; keyed by start candle index so lookup never changes.
        # Dict keys: orig_dir, trade_type, spot_reason, start_idx, vix_pct_at_park
        active_delay: Optional[Dict[str, Any]] = None

        last_entry_hhmm = BREAKOUT_CONFIG.get("last_entry_hhmm", "14:45")

        # Step 2: Evaluate 15-min candles on current day for signal generation
        c_idx = 0
        while c_idx < len(curr_15m_candles):
            if daily_trades_count >= max_daily_trades or daily_loss <= daily_max_loss or daily_profit >= daily_max_profit:
                break

            candle  = curr_15m_candles[c_idx]
            # Signal rules use the candle's start label; trade logs use when
            # that candle actually closes and the close-based signal is known.
            c_time  = candle[0][11:16]
            c_end_time = candle_end_hhmm(candle[0])
            c_open  = float(candle[1])
            c_high  = float(candle[2])
            c_low   = float(candle[3])
            c_close = float(candle[4])

            # ====================================================================
            # PATH A: active VIX delay – re-check every candle until it resolves
            # ====================================================================
            if active_delay is not None:
                elapsed_candles = c_idx - active_delay["start_idx"]
                elapsed_minutes = elapsed_candles * 15
                max_stall_candles = vix_max_stall_minutes // 15

                # Re-evaluate VIX at this (later) candle
                vix_match_d = [vc for vc in curr_vix_candles if vc[0][11:16] <= c_time]
                vix_close_d = float(vix_match_d[-1][4]) if vix_match_d else None
                vix_pct_d   = None
                if vix_close_d is not None and vix_prev_close and vix_prev_close > 0:
                    vix_pct_d = ((vix_close_d - vix_prev_close) / vix_prev_close) * 100.0

                parked_dir = active_delay["orig_dir"]
                in_delay_band_now = False
                if vix_pct_d is not None:
                    in_delay_band_now = (
                        (parked_dir == "BEARISH" and vix_increase_min < vix_pct_d <= vix_bearish_thresh) or
                        (parked_dir == "BULLISH" and vix_bullish_thresh < vix_pct_d <= vix_decrease_max)
                    )

                # (a) Still within delay window AND VIX still in band → keep waiting
                if elapsed_minutes < vix_delay_minutes and in_delay_band_now:
                    print(f"  [VIX Delay Wait] {curr_date} {c_time}  "
                          f"{elapsed_minutes}m/{vix_delay_minutes}m  VIX={vix_pct_d:+.2f}%")
                    c_idx += 1
                    continue

                # Delay elapsed (or VIX normalised): resolve
                orig_dir    = parked_dir
                trade_type  = active_delay["trade_type"]
                spot_reason = active_delay["spot_reason"]
                vix_pct     = vix_pct_d

                vix_delay_active      = True
                vix_direction_override = False

                # (b) Filter 3: at max-stall AND VIX still elevated → candle-trend decision
                if elapsed_candles >= max_stall_candles and in_delay_band_now:
                    check_start = max(0, c_idx - vix_candle_check_count)
                    recent_cdls = curr_15m_candles[check_start:c_idx + 1]
                    if orig_dir == "BULLISH":
                        low_breakdown = any(
                            recent_cdls[k + 1][3] < recent_cdls[k][3]
                            for k in range(len(recent_cdls) - 1)
                        )
                        if low_breakdown:
                            orig_dir = "BEARISH"
                            vix_direction_override = True
                            spot_reason += " [VIX MaxStall:LowBreakdown→BEARISH]"
                    else:  # BEARISH
                        high_breakdown = any(
                            recent_cdls[k + 1][2] > recent_cdls[k][2]
                            for k in range(len(recent_cdls) - 1)
                        )
                        if high_breakdown:
                            orig_dir = "BULLISH"
                            vix_direction_override = True
                            spot_reason += " [VIX MaxStall:HighBreakdown→BULLISH]"
                    vix_delay_reason = (
                        f"VIX MaxStall ({elapsed_minutes}m) – "
                        f"{'Direction overridden' if vix_direction_override else 'Direction kept'} → {orig_dir}"
                    )
                    if vix_direction_override:
                        total_vix_delay_override_trades += 1
                else:
                    # VIX normalised or delay window passed without hitting max stall
                    vix_delay_reason = (
                        f"VIX {'normalised' if not in_delay_band_now else 'delay elapsed'} "
                        f"after {elapsed_minutes}m – proceeding with {orig_dir}"
                    )

                total_vix_delay_trades += 1
                active_delay = None  # consume the parked signal
                print(f"  [VIX Delay Resolved] {curr_date} {c_time}: {vix_delay_reason}")

                # Re-derive same_gap_direction with (possibly overridden) orig_dir
                same_gap_direction = (
                    (orig_dir == "BULLISH" and gap_info.direction == "UP") or
                    (orig_dir == "BEARISH" and gap_info.direction == "DOWN")
                )

                # Re-apply Filter 1 at the resume candle with fresh VIX
                should_reverse     = reverse
                vix_filter_applied = False
                vix_filter_reason  = ""
                if vix_pct is not None:
                    if orig_dir == "BEARISH" and vix_pct > vix_bearish_thresh:
                        if should_reverse:
                            should_reverse = False
                            vix_filter_applied = True
                            vix_filter_reason = f"VIX Spike (+{vix_pct:.2f}%>+{vix_bearish_thresh:.1f}%): No Reverse (Kept BEARISH)"
                    elif orig_dir == "BULLISH" and vix_pct <= vix_bullish_thresh:
                        if should_reverse:
                            should_reverse = False
                            vix_filter_applied = True
                            vix_filter_reason = f"VIX Drop ({vix_pct:.2f}%<={vix_bullish_thresh:.1f}%): No Reverse (Kept BULLISH)"
                if vix_filter_applied:
                    total_vix_filter_trades += 1

                breakout_dir = apply_reverse(orig_dir, should_reverse)
                # Fall through to trade-entry code below

            else:
                # ================================================================
                # PATH B: no active delay – evaluate fresh 15-min candle signal
                # ================================================================
                signal = evaluate_15m_close(
                    c_time=c_time,
                    c_close=c_close,
                    range_high=range_high,
                    range_low=range_low,
                    gap_info=gap_info,
                    gap_territory_touched=gap_territory_touched,
                    candle_index=c_idx,
                    extra_candles_wait=extra_candles_wait,
                    first_entry_hhmm=first_entry_hhmm,
                )
                gap_territory_touched = signal.gap_territory_touched

                if signal.skip or not signal.direction:
                    c_idx += 1
                    continue

                # Exit-type specific cooldown check
                if last_exit_dt is not None and last_exit_reason is not None:
                    # Keep decision timing on the original candle-start clock;
                    # c_end_time is only the user-facing trade-log timestamp.
                    candle_dt   = datetime.strptime(f"{curr_date} {c_time}", "%Y-%m-%d %H:%M")
                    elapsed_min = (candle_dt - last_exit_dt).total_seconds() / 60.0
                    cooldown_minutes = exit_cooldown_minutes.get(last_exit_reason, 0)
                    if cooldown_minutes >= 9999:  # Special value for "no re-entry"
                        c_idx += 1
                        continue
                    if cooldown_minutes > 0 and elapsed_min < cooldown_minutes:
                        c_idx += 1
                        continue

                # Profit round hour cooldown check
                # If trade exited in profit at a round hour (e.g., 10:00, 11:00), wait 60 minutes
                profit_round_hour_cooldown = RISK_CONFIG.get("profit_round_hour_cooldown_minutes", 60)
                if last_exit_dt is not None and last_exit_reason in ("EXIT_TRAILING_PROFIT", "EXIT_MAX_PROFIT"):
                    if last_exit_dt.minute == 0:  # Round hour
                        candle_dt = datetime.strptime(f"{curr_date} {c_time}", "%Y-%m-%d %H:%M")
                        elapsed_min = (candle_dt - last_exit_dt).total_seconds() / 60.0
                        if elapsed_min < profit_round_hour_cooldown:
                            c_idx += 1
                            continue

                orig_dir    = signal.direction
                trade_type  = signal.trade_type
                spot_reason = signal.spot_reason

                same_gap_direction = (
                    (orig_dir == "BULLISH" and gap_info.direction == "UP") or
                    (orig_dir == "BEARISH" and gap_info.direction == "DOWN")
                )

                # VIX at this candle
                vix_match = [vc for vc in curr_vix_candles if vc[0][11:16] <= c_time]
                vix_close = float(vix_match[-1][4]) if vix_match else None
                vix_pct   = None
                if vix_close is not None and vix_prev_close and vix_prev_close > 0:
                    vix_pct = ((vix_close - vix_prev_close) / vix_prev_close) * 100.0

                # ── Filter 1: Reversal Suppression ────────────────────────────────
                should_reverse     = reverse
                vix_filter_applied = False
                vix_filter_reason  = ""
                vix_delay_active   = False
                vix_delay_reason   = ""
                vix_direction_override = False

                if vix_pct is not None:
                    if orig_dir == "BEARISH" and vix_pct > vix_bearish_thresh:
                        if should_reverse:
                            should_reverse = False
                            vix_filter_applied = True
                            vix_filter_reason = f"VIX Spike (+{vix_pct:.2f}% > +{vix_bearish_thresh:.1f}%): No Reverse (Kept BEARISH)"
                    elif orig_dir == "BULLISH" and vix_pct <= vix_bullish_thresh:
                        if should_reverse:
                            should_reverse = False
                            vix_filter_applied = True
                            vix_filter_reason = f"VIX Drop ({vix_pct:.2f}% <= {vix_bullish_thresh:.1f}%): No Reverse (Kept BULLISH)"
                if vix_filter_applied:
                    total_vix_filter_trades += 1

                # ── Filter 2: Non-Blocking Entry Delay ────────────────────────────
                # BEARISH + VIX (+2%…+8%] OR BULLISH + VIX [-6%…-2%) → park & wait
                if vix_pct is not None and not vix_filter_applied:
                    in_delay_band = (
                        (orig_dir == "BEARISH" and vix_increase_min < vix_pct <= vix_bearish_thresh) or
                        (orig_dir == "BULLISH" and vix_bullish_thresh < vix_pct <= vix_decrease_max)
                    )
                    if in_delay_band:
                        delay_candles = vix_delay_minutes // 15
                        resume_idx    = c_idx + delay_candles
                        resume_time   = (
                            curr_15m_candles[resume_idx][0][11:16]
                            if resume_idx < len(curr_15m_candles) else "99:99"
                        )
                        if resume_time > last_entry_hhmm:
                            # Delay would exceed last-entry window → skip delay, trade now
                            print(f"  [VIX Delay Skip] {curr_date} {c_time} {orig_dir}: "
                                  f"VIX {vix_pct:+.2f}% in band but delay would push past "
                                  f"{last_entry_hhmm} – entering immediately")
                            total_vix_delay_skipped += 1
                            # fall through to normal trade entry
                        else:
                            # Park the signal – PATH A will handle it from next candle
                            active_delay = {
                                "orig_dir":        orig_dir,
                                "trade_type":      trade_type,
                                "spot_reason":     spot_reason,
                                "start_idx":       c_idx,
                                "vix_pct_at_park": vix_pct,
                            }
                            print(f"  [VIX Delay Park] {curr_date} {c_time} {orig_dir}: "
                                  f"VIX {'Spike' if orig_dir == 'BEARISH' else 'Drop'} {vix_pct:+.2f}% "
                                  f"in delay band – parked, resume ~{vix_delay_minutes}m "
                                  f"(candle idx {resume_idx} ≈ {resume_time})")
                            c_idx += 1
                            continue  # ← PATH A picks it up next iteration

                # Trade Entry (with optional reversal unless suppressed by VIX condition)
                breakout_dir = apply_reverse(orig_dir, should_reverse)

            # ── Common trade entry point for both PATH A and PATH B ──────────────
            opt_type = "CE" if breakout_dir == "BULLISH" else "PE"

            atm_strike = int(round(c_close / strike_step) * strike_step)

            # Strike selection mirrors live find_strategy_contracts():
            #   spread -> Buy ATM, Sell ATM +/- spread_width
            #   naked  -> Buy ATM
            if struct_clean == "naked":
                buy_strike = atm_strike
                sell_strike = None
            else:
                buy_strike = atm_strike
                sell_strike = (
                    atm_strike + spread_width
                    if breakout_dir == "BULLISH"
                    else atm_strike - spread_width
                )

            # Expiry-day flag is kept only for reporting.
            curr_date_obj = datetime.strptime(curr_date, "%Y-%m-%d")
            is_expiry = (curr_date_obj.weekday() == expiry_weekday_for(active_inst_name))

            # Resolve real option contracts and load historical market prices.
            buy_prices: Optional[Dict[str, float]] = None
            sell_prices: Optional[Dict[str, float]] = None
            pricing_source = "MODEL"
            option_exchange = INSTRUMENTS[active_inst_name]["option_exchange"]
            active_expiry_date = nearest_expiry_for_date(
                active_inst_name, option_exchange, curr_date
            )
            try:
                buy_token = resolve_option_token(
                    active_inst_name, option_exchange, buy_strike, opt_type, curr_date
                )
                if buy_token:
                    buy_prices = fetch_option_day_prices(
                        smart_api, option_exchange, buy_token, curr_date
                    )
                if struct_clean != "naked" and sell_strike is not None:
                    sell_token = resolve_option_token(
                        active_inst_name, option_exchange, sell_strike, opt_type, curr_date
                    )
                    if sell_token:
                        sell_prices = fetch_option_day_prices(
                            smart_api, option_exchange, sell_token, curr_date
                        )
            except Exception as exc:
                print(f"[!] Market premium lookup failed for {curr_date} "
                      f"{buy_strike}/{sell_strike} {opt_type}: {exc}")

            # Build human-readable Trade Reason
            if should_reverse:
                trade_reason = f"Reverse {breakout_dir} ({'Breakout' if trade_type == 'BREAKOUT' else 'Recovery'}): {spot_reason}"
            else:
                trade_reason = f"{breakout_dir} {'Breakout' if trade_type == 'BREAKOUT' else 'Recovery'}: {spot_reason}"

            if vix_filter_applied:
                trade_reason += f" [{vix_filter_reason}]"
            if vix_delay_active:
                trade_reason += f" [VIX Delay: {vix_delay_reason}]"

            daily_trades_count += 1
            entry_time    = c_end_time
            entry_spot    = c_close
            
            # Calculate actual days to expiry based on current date and instrument
            days_to_expiry = calculate_days_to_expiry(curr_date, active_inst_name)
            entry_ttx = time_to_expiry_years(
                curr_date, entry_time, active_inst_name, expiry_date=active_expiry_date
            )
            market_entry = market_premium_at(
                buy_prices, sell_prices, c_time, struct_clean == "naked"
            )
            if market_entry is not None and market_entry > 0:
                entry_premium = market_entry
                use_market_prices = True
                pricing_source = "MARKET"
            else:
                entry_sigma = term_structure_sigma(
                    entry_ttx * 365.0, sigma, instrument=active_inst_name
                )
                entry_premium = get_option_premium(
                    entry_spot, buy_strike, sell_strike, breakout_dir,
                    days_to_expiry=days_to_expiry, sigma=entry_sigma,
                    structure=struct_clean, time_to_expiry_years=entry_ttx,
                )
                use_market_prices = False
                pricing_source = "MODEL"

            # Intraday Trade Monitor on 15-minute candles for profit tracking (matches live trading)
            max_trade_pnl = 0.0
            min_trade_pnl = 0.0
            locked_profit = profit_threshold
            exit_reason   = "EXIT_EOD"
            exit_time     = "15:10"
            exit_candle_start_time = None
            exit_spot     = entry_spot
            exit_pnl      = 0.0
            exit_premium  = 0.0  # Will be calculated at exit

            # Preserve the original candle selection: start monitoring after
            # the entry candle, while recording the entry at its close time.
            entry_candle_start_datetime = datetime.strptime(
                f"{curr_date} {c_time}", "%Y-%m-%d %H:%M"
            )
            
            forward_idx = 0
            while forward_idx < len(curr_15m_profit_candles):
                f_candle = curr_15m_profit_candles[forward_idx]
                # Handle both ISO format (2026-09-01T09:15:00) and space format (2026-09-01 09:15:00)
                f_datetime = datetime.strptime(
                    f"{curr_date} {f_candle[0][11:16]}", "%Y-%m-%d %H:%M"
                )
                if f_datetime > entry_candle_start_datetime:
                    break
                forward_idx += 1

            # Monitor on 15-minute candles for profit tracking
            while forward_idx < len(curr_15m_profit_candles):
                f_candle = curr_15m_profit_candles[forward_idx]
                f_start_time = f_candle[0][11:16]
                f_time   = candle_end_hhmm(f_candle[0])
                f_close  = float(f_candle[4])

                candle_ttx = time_to_expiry_years(
                    curr_date, f_time, active_inst_name, expiry_date=active_expiry_date
                )
                candle_sigma = term_structure_sigma(
                    candle_ttx * 365.0, sigma, instrument=active_inst_name
                )
                if use_market_prices:
                    market_curr = market_premium_at(
                        buy_prices, sell_prices, f_start_time, struct_clean == "naked"
                    )
                    if market_curr is not None and market_curr > 0:
                        curr_premium = market_curr
                    else:
                        curr_premium = get_option_premium(
                            f_close, buy_strike, sell_strike, breakout_dir,
                            days_to_expiry=days_to_expiry, sigma=candle_sigma,
                            structure=struct_clean, time_to_expiry_years=candle_ttx,
                        )
                else:
                    curr_premium = get_option_premium(
                        f_close, buy_strike, sell_strike, breakout_dir,
                        days_to_expiry=days_to_expiry, sigma=candle_sigma,
                        structure=struct_clean, time_to_expiry_years=candle_ttx,
                    )
                current_pnl  = (curr_premium - entry_premium) * total_qty
                min_trade_pnl = min(min_trade_pnl, current_pnl)

                # Simulate the broker-side fixed monetary stop. If a sampled
                # candle gaps past the trigger, use the configured stop price.
                if enable_fixed_loss_sl and current_pnl <= -abs(fixed_loss_amount):
                    exit_reason = "EXIT_FIXED_LOSS_SL"
                    exit_time   = f_time
                    exit_candle_start_time = f_start_time
                    exit_spot   = f_close
                    exit_premium = max(0.0, entry_premium - abs(fixed_loss_amount) / total_qty)
                    exit_pnl    = (exit_premium - entry_premium) * total_qty
                    break

                # Check if daily max loss would be breached if we exit now
                potential_daily_pnl = daily_pnl + current_pnl
                if potential_daily_pnl <= daily_max_loss:
                    exit_reason = "EXIT_DAILY_MAX_LOSS"
                    exit_time   = f_time
                    exit_candle_start_time = f_start_time
                    exit_spot   = f_close
                    exit_pnl    = current_pnl
                    exit_premium = curr_premium
                    break

                # Condition 1: Stop Loss
                if current_pnl <= max_loss:
                    exit_reason = "EXIT_MAX_LOSS"
                    exit_time   = f_time
                    exit_candle_start_time = f_start_time
                    exit_spot   = f_close
                    exit_pnl    = current_pnl  # Use actual P&L at trigger time for realism
                    exit_premium = curr_premium
                    break

                # Trailing profit update
                if current_pnl > max_trade_pnl:
                    max_trade_pnl = current_pnl
                    if max_trade_pnl >= profit_threshold:
                        new_locked = ((max_trade_pnl - profit_threshold) // min_profit_step) * min_profit_step + profit_threshold
                        if new_locked > locked_profit:
                            locked_profit = new_locked

                # Condition 2: Trailing profit hit
                if max_trade_pnl >= profit_threshold and current_pnl < locked_profit:
                    exit_reason = "EXIT_TRAILING_PROFIT"
                    exit_time   = f_time
                    exit_candle_start_time = f_start_time
                    exit_spot   = f_close
                    exit_pnl    = current_pnl  # Use actual P&L at trigger time for realism
                    exit_premium = curr_premium
                    break

                # Condition 3: Max profit target
                if current_pnl >= max_profit:
                    exit_reason = "EXIT_MAX_PROFIT"
                    exit_time   = f_time
                    exit_candle_start_time = f_start_time
                    exit_spot   = f_close
                    exit_pnl    = current_pnl  # Use actual P&L at trigger time for realism
                    exit_premium = curr_premium
                    break

                # End of day check
                if f_start_time >= "15:00" or forward_idx == len(curr_15m_profit_candles) - 1:
                    exit_reason = "EXIT_EOD"
                    exit_time   = f_time
                    exit_candle_start_time = f_start_time
                    exit_spot   = f_close
                    exit_pnl    = current_pnl
                    exit_premium = curr_premium
                    break

                forward_idx += 1

            # Calculate exit premium if not already set during exit conditions
            if exit_premium == 0:
                exit_key = exit_candle_start_time or exit_time
                market_exit = None
                if use_market_prices:
                    market_exit = market_premium_at(
                        buy_prices, sell_prices, exit_key, struct_clean == "naked"
                    )
                if market_exit is not None and market_exit > 0:
                    exit_premium = market_exit
                else:
                    exit_ttx = time_to_expiry_years(
                        curr_date, exit_time, active_inst_name, expiry_date=active_expiry_date
                    )
                    exit_sigma = term_structure_sigma(
                        exit_ttx * 365.0, sigma, instrument=active_inst_name
                    )
                    exit_premium = get_option_premium(
                        exit_spot, buy_strike, sell_strike, breakout_dir,
                        days_to_expiry=days_to_expiry, sigma=exit_sigma,
                        structure=struct_clean, time_to_expiry_years=exit_ttx,
                    )

            # Cooldowns retain the original candle-start clock so changing
            # displayed timestamps cannot alter which trades the backtest takes.
            cooldown_exit_time = exit_candle_start_time or exit_time
            last_exit_dt = datetime.strptime(f"{curr_date} {cooldown_exit_time}", "%Y-%m-%d %H:%M")
            last_exit_reason = exit_reason

            # Keep legacy SL cooldown for backward compatibility
            if exit_reason in ("EXIT_MAX_LOSS", "EXIT_DAILY_MAX_LOSS", "EXIT_FIXED_LOSS_SL"):
                last_sl_exit_dt = last_exit_dt

            daily_pnl += exit_pnl
            # Track losses separately for daily max loss check (profits ignored)
            if exit_pnl < 0:
                daily_loss += exit_pnl
            # Track net P&L (profit + loss) for daily max profit check
            daily_profit = daily_pnl

            # Accumulate gap-size stats
            gap_size_stats[gap_info.size]["trades"] += 1
            gap_size_stats[gap_info.size]["pnl"]    += exit_pnl
            if trade_type == "RECOVERY":
                gap_size_stats[gap_info.size]["recoveries"] += 1
                total_recovery_trades += 1

            trade_record = {
                "date":                curr_date,
                "instrument":          active_inst_name,
                "expiry":              "YES" if is_expiry else "NO",
                "prev_date":           prev_date,
                "range_high":          range_high,
                "range_low":           range_low,
                "gap_pct":             round(gap_info.gap_pct * 100, 3),
                "gap_direction":       gap_info.direction,
                "gap_size":            gap_info.size,
                "caution_level":       gap_info.caution_level,
                "vix_pct":             round(vix_pct, 2) if vix_pct is not None else "N/A",
                "vix_filter":          vix_filter_applied,
                "vix_delay":           vix_delay_active,
                "vix_dir_override":    vix_direction_override,
                "no_chase":            same_gap_direction and gap_info.no_chase_same_direction,
                "structure":           struct_clean.upper(),
                "trade_type":          trade_type,
                "mode":                "REVERSE" if reverse else "NO-REVERSE",
                "direction":           breakout_dir,
                "opt_type":            opt_type,
                "entry_time":          entry_time,
                "entry_spot":          entry_spot,
                "pricing_source":      pricing_source,
                "buy_strike":          buy_strike,
                "sell_strike":         sell_strike if sell_strike else "N/A",
                "entry_premium":       round(entry_premium, 2),
                "exit_time":           exit_time,
                "exit_spot":           exit_spot,
                "exit_premium":        round(exit_premium, 2),
                "trade_reason":        trade_reason,
                "exit_reason":         exit_reason,
                "pnl":                 round(exit_pnl, 2),
                "min_unrealized_pnl":  round(min_trade_pnl, 2),
                "lots":                lots,
                "quantity":            total_qty,
            }
            all_trades.append(trade_record)

            # Daily max profit reached -> no more trades of that day
            if daily_pnl >= daily_max_profit:
                break

            # Advance 15-minute candle index past the trade exit
            if exit_candle_start_time is None:
                # Fallback 15:10 EOD exit when no profit-check candle exists.
                exit_candle_start_time = exit_time
            exit_15m_idx = c_idx + 1
            while exit_15m_idx < len(curr_15m_candles):
                exit_candle_time = curr_15m_candles[exit_15m_idx][0][11:16]
                if exit_candle_time >= exit_candle_start_time:
                    break
                exit_15m_idx += 1
            c_idx = exit_15m_idx

    # ============================================================
    # PRINT RESULTS TABLE
    # ============================================================
    print("\n" + "=" * 215)
    print(
        f"{'#':<3} | {'Date':<10} | {'Instr':<7} | {'Time':<5} | {'Gap':<18} | {'VIX%':<8} | {'Type':<9} | {'Struct':<7} | {'Dir':<7} | "
        f"{'Range [Low - High]':<21} | {'Spot':<8} | {'Strike(s)':<16} | "
        f"{'Exit':<5} | {'Exit Reason':<19} | {'Trade Reason':<45} | {'P&L (Rs.)':>10}"
    )
    print("=" * 215)

    cum_pnl = 0.0
    wins    = 0
    losses  = 0

    for i, t in enumerate(all_trades, 1):
        cum_pnl += t["pnl"]
        if t["pnl"] > 0:
            wins += 1
        elif t["pnl"] < 0:
            losses += 1

        range_str   = f"[{t['range_low']:.1f} - {t['range_high']:.1f}]"
        if t["structure"] == "NAKED":
            strikes_str = f"{t['buy_strike']} {t['opt_type']}"
        else:
            strikes_str = f"{t['buy_strike']}/{t['sell_strike']} {t['opt_type']}"

        pnl_str     = f"{t['pnl']:+,.2f}"
        gap_str     = f"{t['gap_direction'][:1]}{t['gap_pct']:+.2f}% {t['gap_size'][:3]} CL{t['caution_level']}"
        type_str    = f"{'🔄' if t['trade_type'] == 'RECOVERY' else '⚡'} {t['trade_type'][:8]}"

        vix_val = t.get("vix_pct")
        if isinstance(vix_val, (int, float)):
            vix_str = f"{vix_val:+5.2f}%{'*' if t.get('vix_filter') else ''}"
        else:
            vix_str = str(vix_val)

        instr_str = t.get("instrument", inst_key)

        print(
            f"{i:<3} | {t['date']:<10} | {instr_str:<7} | {t['entry_time']:<5} | {gap_str:<18} | {vix_str:<8} | {type_str:<9} | {t['structure']:<7} | {t['direction']:<7} | "
            f"{range_str:<21} | {t['entry_spot']:<8.1f} | {strikes_str:<16} | "
            f"{t['exit_time']:<5} | {t['exit_reason']:<19} | {t['trade_reason']:<45} | {pnl_str:>10}"
        )
        print(f"       Entry Premium: {t['entry_premium']:.2f} | Exit Premium: {t['exit_premium']:.2f}")

    print("=" * 215)

    # ============================================================
    # SAVE DETAILED TRADE LOG
    # ============================================================
    x_factor_suffix = f"_x{x_factor}" if x_factor != 1.0 else ""
    tag_suffix = f"_{tag}" if tag else ""
    csv_file = DATA_DIR / f"backtest_daybreakout_15m_{inst_key}_{struct_clean}{x_factor_suffix}{tag_suffix}.csv"
    if all_trades:
        with open(csv_file, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=list(all_trades[0].keys()))
            writer.writeheader()
            writer.writerows(all_trades)
        print(f"\n[+] Detailed trade log saved to: {csv_file}")

    # ============================================================
    # OVERALL PERFORMANCE SUMMARY
    # ============================================================
    total_trades = len(all_trades)
    win_rate     = (wins / total_trades * 100) if total_trades > 0 else 0.0
    gross_profit = sum(t["pnl"] for t in all_trades if t["pnl"] > 0)
    gross_loss   = sum(abs(t["pnl"]) for t in all_trades if t["pnl"] < 0)
    profit_factor = (gross_profit / gross_loss) if gross_loss > 0 else (99.0 if gross_profit > 0 else 0.0)
    avg_pnl      = (cum_pnl / total_trades) if total_trades > 0 else 0.0

    print("\n" + "=" * 55)
    print("        \U0001f4ca 15-MINUTE BACKTEST PERFORMANCE SUMMARY")
    print("=" * 55)
    print(f"  Instrument                  : {display_title}")
    print(f"  Option Structure            : {struct_clean.upper()}")
    print(f"  X-Factor                    : {x_factor}x")
    print(f"  Total Days Evaluated        : {len(sorted_dates) - 1}")
    print(f"  Total Trades Executed       : {total_trades}")
    print(f"    ↳ Regular Breakouts          : {total_trades - total_recovery_trades}")
    print(f"    ↳ Recovery Trades            : {total_recovery_trades}")
    print(f"    ↳ VIX Filter (No Reverse)   : {total_vix_filter_trades}")
    print(f"    ↳ VIX Delayed Entries        : {total_vix_delay_trades}")
    print(f"    ↳ VIX Direction Overrides    : {total_vix_delay_override_trades}")
    print(f"    ↳ VIX Delay Skipped Signals  : {total_vix_delay_skipped}")
    print(f"  Winning Trades              : {wins} ({win_rate:.1f}%)")
    print(f"  Losing Trades               : {losses}")
    print(f"  Gross Profit                : Rs. {gross_profit:+,.2f}")
    print(f"  Gross Loss                  : Rs. -{gross_loss:,.2f}")
    print(f"  ---------------------------------------------------")
    print(f"  Net Realized P&L            : Rs. {cum_pnl:+,.2f}")
    print(f"  Profit Factor               : {profit_factor:.2f}")
    print(f"  Average Profit / Trade      : Rs. {avg_pnl:+,.2f}")
    print("=" * 55)

    # ---- Gap Breakdown by Size (with recovery trades) ----
    print()
    print("  \U0001f4c8 PERFORMANCE BY GAP SIZE")
    print(f"  {'Gap Size':<12} {'Days':>5} {'Trades':>7} {'Recov':>6} {'Net P&L':>12} {'Avg/Trade':>12}")
    print(f"  {'-'*12} {'-'*5} {'-'*7} {'-'*6} {'-'*12} {'-'*12}")
    gap_icons = {"SMALL": "\U0001f7e2", "MEDIUM": "\U0001f7e1", "LARGE": "\U0001f525", "VERY_LARGE": "\U0001f534"}
    for size in ("SMALL", "MEDIUM", "LARGE", "VERY_LARGE"):
        s = gap_size_stats[size]
        avg = (s["pnl"] / s["trades"]) if s["trades"] > 0 else 0.0
        icon = gap_icons[size]
        print(f"  {icon} {size:<10} {s['days']:>5} {s['trades']:>7} {s['recoveries']:>6} {s['pnl']:>+12,.0f} {avg:>+12,.0f}")
    print("=" * 55 + "\n")

    return {
        "trades":                      all_trades,
        "total_trades":                total_trades,
        "recovery_trades":             total_recovery_trades,
        "vix_filter_trades":           total_vix_filter_trades,
        "vix_delay_trades":            total_vix_delay_trades,
        "vix_delay_override_trades":   total_vix_delay_override_trades,
        "vix_delay_skipped":           total_vix_delay_skipped,
        "wins":                        wins,
        "losses":                      losses,
        "win_rate":                    win_rate,
        "net_pnl":                     cum_pnl,
        "profit_factor":               profit_factor,
        "avg_pnl":                     avg_pnl,
        "gap_size_stats":              gap_size_stats,
    }


def main():
    parser = argparse.ArgumentParser(description="15-Minute Candle Backtest for Day Breakout Strategy")
    parser.add_argument("--instrument", "-i", choices=["NIFTY", "SENSEX"], default="NIFTY", help="Instrument to backtest (default: NIFTY)")
    parser.add_argument("--combination", type=str2bool, nargs="?", const=True, default=False, help="Combination mode (true/false): test nearest expiry instrument per day (default: false)")
    parser.add_argument("--structure", "-s", choices=["spread", "naked"], default="spread", help="Option structure to test (spread or naked) (default: spread)")
    parser.add_argument("--days", "-d", type=int, default=185, help="Number of past days to backtest (default: 185)")
    parser.add_argument("--lots", "-l", type=int, default=15, help="Lots to trade (default: 15)")
    parser.add_argument("--x-factor", "-x", type=float, default=RISK_CONFIG.get("x_factor", 1.0), help="Multiplier for position sizing and risk parameters (default: from RISK_CONFIG, e.g., 1.5 for 50%% more, 2.0 for double)")
    parser.add_argument("--end-date", type=str, default=None, help="End date for backtest in YYYY-MM-DD format (default: today)")
    parser.add_argument("--vix-bearish-thresh", type=float, default=VIX_CONFIG["bearish_vix_threshold"], help="VIX spike threshold (pct) above which bearish breakdown does not reverse (default: {})".format(VIX_CONFIG["bearish_vix_threshold"]))
    parser.add_argument("--vix-bullish-thresh", type=float, default=VIX_CONFIG["bullish_vix_threshold"], help="VIX crush threshold (pct) below which bullish breakout does not reverse (default: {})".format(VIX_CONFIG["bullish_vix_threshold"]))
    parser.add_argument("--first-entry", default=BREAKOUT_CONFIG.get("first_entry_hhmm", "09:30"), help="Earliest entry candle start time (default: 09:30)")
    parser.add_argument("--daily-max-profit", type=float, default=RISK_CONFIG.get("daily_max_profit", 25000.0), help="Daily profit target to halt trading for the day (default: 25000)")
    parser.add_argument("--sl-cooldown", type=int, default=RISK_CONFIG.get("sl_cooldown_minutes", 45), help="Cooldown minutes after a stop-loss before next entry (default: 45)")
    # Reverse flag – default ON. Use --no-reverse to disable.
    reverse_group = parser.add_mutually_exclusive_group()
    reverse_group.add_argument("--reverse", dest="reverse", action="store_true", default=True, help="Reverse trade direction (default: ON)")
    reverse_group.add_argument("--no-reverse", dest="reverse", action="store_false", help="Do NOT reverse trade direction")
    parser.add_argument("--tag", type=str, default="", help="Optional tag for output CSV (e.g. reverse, no-reverse)")
    args = parser.parse_args()

    # Parse end date if provided
    end_date = None
    if args.end_date:
        try:
            end_date = datetime.strptime(args.end_date, "%Y-%m-%d")
        except ValueError:
            print(f"[!] Invalid end date format: {args.end_date}. Use YYYY-MM-DD format.")
            return

    run_15m_backtest(
        instrument_name=args.instrument,
        days_back=args.days,
        lots=args.lots,
        daily_max_profit=args.daily_max_profit,
        sl_cooldown_minutes=args.sl_cooldown,
        first_entry_hhmm=args.first_entry,
        reverse=args.reverse,
        structure=args.structure,
        vix_bearish_thresh=args.vix_bearish_thresh,
        vix_bullish_thresh=args.vix_bullish_thresh,
        end_date=end_date,
        combination=args.combination,
        x_factor=args.x_factor,
        tag=args.tag,
    )



if __name__ == "__main__":
    main()

