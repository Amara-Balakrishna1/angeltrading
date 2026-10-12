"""
Range Calculator for Day Breakout Strategy.
Fetches 12:30 PM to 3:15 PM historical candles for previous day to mark High and Low.
"""

import time
from datetime import datetime, timedelta
from typing import Dict, Any, Optional
from live.daybreakout.config import RANGE_CONFIG, BREAKOUT_CONFIG
from live.daybreakout.thread_safety import smart_api_lock


def _safe_get_candle_data(smart_api, params: Dict[str, Any], max_retries: int = 5, retry_delay: float = 2.5) -> Optional[Dict[str, Any]]:
    """Fetches candle data with retry logic for API rate limits."""
    last_res = None
    for attempt in range(max_retries):
        try:
            time.sleep(1.0)  # Always ensure at least 1s between candle calls to respect Angel One rate limit
            with smart_api_lock:
                res = smart_api.getCandleData(params)
            if res and res.get("status"):
                return res
            # Print the actual failure so the user can see the real error
            last_res = res
            err_msg = (res.get("message") or res.get("errorcode") or str(res)) if res else "No response / None"
            print(f"[!] getCandleData attempt {attempt+1}/{max_retries} failed: {err_msg}")
            print(f"    Params: exchange={params.get('exchange')} token={params.get('symboltoken')} "
                  f"interval={params.get('interval')} from={params.get('fromdate')} to={params.get('todate')}")
            if attempt < max_retries - 1:
                time.sleep(retry_delay * (attempt + 1))
        except Exception as e:
            last_res = None
            err_str = str(e)
            is_rate_limit = (
                "exceeding access rate" in err_str
                or "Too many requests" in err_str
                or "Access denied" in err_str
            )
            backoff = retry_delay * (attempt + 1) * (2 if is_rate_limit else 1)
            print(f"[!] getCandleData attempt {attempt+1}/{max_retries} "
                  f"{'[rate-limit]' if is_rate_limit else '[error]'}: {err_str[:120]}")
            if attempt < max_retries - 1:
                print(f"    Retrying in {backoff:.0f}s...")
                time.sleep(backoff)
            else:
                print(f"[!] All {max_retries} attempts failed for getCandleData.")
                raise e
    return last_res


def get_previous_trading_day(smart_api, spot_exchange: str, spot_token: str, ref_date: Optional[datetime] = None) -> str:
    """
    Finds the last completed trading day prior to ref_date (defaults to today).
    Tries ONE_DAY candles from SmartAPI (single attempt only — falls back immediately
    to weekday calculation so startup is not delayed by rate-limit retries).
    """
    if ref_date is None:
        ref_date = datetime.now()

    from_date = (ref_date - timedelta(days=15)).strftime("%Y-%m-%d 09:15")
    to_date = ref_date.strftime("%Y-%m-%d 15:30")

    try:
        res = _safe_get_candle_data(smart_api, {
            "exchange": spot_exchange,
            "symboltoken": str(spot_token),
            "interval": "ONE_DAY",
            "fromdate": from_date,
            "todate": to_date,
        }, max_retries=3, retry_delay=2.0)
        if res and res.get("status") and res.get("data"):
            trading_dates = [c[0][:10] for c in res["data"]]
            today_str = ref_date.strftime("%Y-%m-%d")
            prev_dates = [d for d in trading_dates if d < today_str]
            if prev_dates:
                return prev_dates[-1]
            elif trading_dates:
                return trading_dates[-1]
    except Exception as e:
        print(f"[!] get_previous_trading_day API call failed (using weekday fallback): {str(e)[:80]}")

    # Reliable weekday fallback
    d = ref_date - timedelta(days=1)
    while d.weekday() >= 5:  # Saturday=5, Sunday=6
        d -= timedelta(days=1)
    prev_day = d.strftime("%Y-%m-%d")
    print(f"[*] Using weekday fallback for previous trading day: {prev_day}")
    return prev_day


def get_previous_day_range(
    smart_api,
    spot_exchange: str,
    spot_token: str,
    trading_day: Optional[str] = None,
    ref_date: Optional[datetime] = None
) -> Dict[str, Any]:
    """
    Fetches 15-minute candles for the previous trading day, then marks High/Low
    on bars whose start time is between 12:30 and 15:15 (same window as the backtest).

    Returns:
        dict: {
            "date": "YYYY-MM-DD",
            "high": float,
            "low": float,
            "high_time": "HH:MM",
            "low_time": "HH:MM",
            "candles_count": int,
            "candles": list
        }
    """
    range_start = RANGE_CONFIG["start_time"]
    range_end = RANGE_CONFIG["end_time"]

    # If trading_day is given explicitly by the user, we try that exact date.
    # Otherwise, we start from the previous business day and step backward if the day was a holiday (0 candles).
    current_dt = ref_date if ref_date is not None else datetime.now()
    explicit_date = bool(trading_day)
    # If trading_day is today, treat it as "auto-detect previous day"
    if trading_day == current_dt.strftime("%Y-%m-%d"):
        trading_day = None
        explicit_date = False
    candidate_day = trading_day

    candles = []
    session_candles = []
    last_res = None

    for attempt_day in range(7):
        if not candidate_day:
            current_dt -= timedelta(days=1)
            while current_dt.weekday() >= 5:  # Skip Saturday/Sunday
                current_dt -= timedelta(days=1)
            candidate_day = current_dt.strftime("%Y-%m-%d")

        start_str = f"{candidate_day} 09:15"
        end_str = f"{candidate_day} 15:30"
        print(f"[*] Fetching 15-minute candles for {candidate_day} range window ({range_start} -> {range_end})...")

        res = _safe_get_candle_data(smart_api, {
            "exchange": spot_exchange,
            "symboltoken": str(spot_token),
            "interval": RANGE_CONFIG["interval"],
            "fromdate": start_str,
            "todate": end_str,
        }, max_retries=4, retry_delay=2.5)

        last_res = res
        if res and res.get("status") and res.get("data"):
            session_candles = res["data"]
            candles = [c for c in session_candles if range_start <= str(c[0])[11:16] <= range_end]
            if candles:
                trading_day = candidate_day
                break

        if explicit_date:
            # User gave a specific date, don't auto-switch days
            break

        print(f"[!] No candles found for {candidate_day} (market holiday / closed). Trying previous trading day...")
        candidate_day = None  # Will step back on next iteration

    if not candles:
        err_detail = last_res if last_res is not None else "No response from server"
        raise RuntimeError(
            f"Failed to fetch range candles for {candidate_day or trading_day} ({start_str} to {end_str}): {err_detail}"
        )

    # Candle format: [timestamp, open, high, low, close, volume]
    high_val = -1e9
    low_val = 1e9
    high_time = ""
    low_time = ""

    for c in candles:
        c_time = c[0]
        c_high = float(c[2])
        c_low = float(c[3])

        if c_high > high_val:
            high_val = c_high
            high_time = c_time[11:16]

        if c_low < low_val:
            low_val = c_low
            low_time = c_time[11:16]

    result = {
        "date": trading_day,
        "high": round(high_val, 2),
        "low": round(low_val, 2),
        "high_time": high_time,
        "low_time": low_time,
        "candles_count": len(candles),
    }

    print(f"[+] Previous Day Range Marked ({trading_day}):")
    print(f"    [^] High: {result['high']} (at {result['high_time']})")
    print(f"    [v] Low : {result['low']} (at {result['low_time']})")
    print(f"    [*] Total 15-min candles in range window: {result['candles_count']}")

    return result


def parse_candle_datetime(ts: str) -> datetime:
    """Parse SmartAPI candle timestamp (ISO or 'YYYY-MM-DD HH:MM[:SS]')."""
    raw = str(ts).replace("T", " ").split("+")[0].split("Z")[0].strip()
    if len(raw) == 16:  # YYYY-MM-DD HH:MM
        raw += ":00"
    return datetime.strptime(raw[:19], "%Y-%m-%d %H:%M:%S")


def is_15m_candle_complete(candle, now: Optional[datetime] = None) -> bool:
    """True when the 15-minute bar that *starts* at candle[0] has finished."""
    if not candle or not isinstance(candle, (list, tuple)) or len(candle) == 0:
        return False
    if now is None:
        now = datetime.now()
    try:
        start = parse_candle_datetime(candle[0])
        return now >= start + timedelta(minutes=15)
    except Exception:
        return False


def fetch_15m_candles(
    smart_api,
    spot_exchange: str,
    spot_token: str,
    from_str: str,
    to_str: str,
) -> list:
    """Fetches FIFTEEN_MINUTE candles for the given window."""
    res = _safe_get_candle_data(smart_api, {
        "exchange": spot_exchange,
        "symboltoken": str(spot_token),
        "interval": "FIFTEEN_MINUTE",
        "fromdate": from_str,
        "todate": to_str,
    })
    if not res or not res.get("status") or not res.get("data"):
        return []
    return res["data"]


def seconds_until_next_15m_close(now: Optional[datetime] = None, buffer_seconds: Optional[int] = None) -> float:
    """Seconds to sleep until the next 15-minute boundary plus a small API buffer."""
    if now is None:
        now = datetime.now()
    if buffer_seconds is None:
        buffer_seconds = BREAKOUT_CONFIG["candle_close_buffer_seconds"]

    minute_offset = now.minute % 15
    second_offset = now.second

    # If at boundary (minute_offset == 0) and within buffer window (second_offset < buffer_seconds),
    # wait only remaining buffer seconds for the JUST-CLOSED candle.
    if minute_offset == 0 and second_offset < buffer_seconds:
        return max(1.0, float(buffer_seconds - second_offset))

    minutes_to_next = 15 - minute_offset if minute_offset > 0 else 15
    next_close = now.replace(second=0, microsecond=0) + timedelta(minutes=minutes_to_next)
    target = next_close + timedelta(seconds=buffer_seconds)
    return max(1.0, (target - now).total_seconds())

