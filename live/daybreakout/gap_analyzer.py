"""
Gap & Opening Rules Analyzer for Day Breakout Strategy.

Identifies the overnight gap (previous close → today's open), classifies
its size and direction, and applies the rule table from the strategy spec:

Gap Size      | Range
-----------   | ------
SMALL         | < 0.30%
MEDIUM        | 0.30 – 0.60%
LARGE         | 0.60 – 1.00%
VERY_LARGE    | > 1.00%

Each size + direction combination maps to a caution level and optional
extra-confirmation requirement, exactly as documented in README.md.
"""

import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional

from live.daybreakout.config import GAP_CONFIG
from live.daybreakout.range_calculator import _safe_get_candle_data


# ---------------------------------------------------------------------------
# Pre-market wait helper
# ---------------------------------------------------------------------------

MARKET_OPEN_TIME = (9, 15)   # HH, MM  — first candle available after this time


def wait_until_market_open(status_interval: int = 60) -> None:
    """
    If the current time is before market open (9:15 AM), sleep and print a
    status message every *status_interval* seconds until 9:15 AM is reached.
    This prevents the API from returning
    'From datetime can\'t be greater than current datetime'.
    """
    open_h, open_m = MARKET_OPEN_TIME
    now = datetime.now()
    target = now.replace(hour=open_h, minute=open_m, second=0, microsecond=0)

    if now >= target:
        return   # already past market open — nothing to do

    total_wait = (target - now).total_seconds()
    print(
        f"\n[*] Pre-market mode: current time is {now.strftime('%H:%M:%S')}."
        f" Waiting {total_wait:.0f}s until {open_h:02d}:{open_m:02d} market open."
    )

    while True:
        now = datetime.now()
        remaining = (target - now).total_seconds()
        if remaining <= 0:
            print(f"[+] Market open reached ({open_h:02d}:{open_m:02d}). Proceeding with API calls.")
            return
        print(
            f"[{now.strftime('%H:%M:%S')}] Waiting for 09:15 market open — "
            f"{int(remaining // 60)}m {int(remaining % 60)}s remaining..."
        )
        sleep_for = min(status_interval, remaining)
        time.sleep(sleep_for)


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class GapInfo:
    """Holds all computed gap metadata for the current trading session."""

    # Raw values
    prev_close: float = 0.0
    today_open: float = 0.0
    gap_points: float = 0.0         # today_open - prev_close
    gap_pct: float = 0.0            # signed fraction; positive = Gap-Up

    # Classification
    direction: str = "FLAT"         # "UP" | "DOWN" | "FLAT"
    size: str = "SMALL"             # "SMALL" | "MEDIUM" | "LARGE" | "VERY_LARGE"
    label: str = ""                 # e.g. "Large Gap-Up (0.73%)"

    # Behavioural flags (consumed by strategy.py)
    caution_level: int = 1          # 1 (normal) → 4 (most selective)
    extra_confirmation_required: bool = False   # True for LARGE & VERY_LARGE
    no_chase_same_direction: bool = False       # True for LARGE & VERY_LARGE
    be_very_selective: bool = False             # True only for VERY_LARGE

    # Error state
    fetch_error: str = ""           # Non-empty if gap could not be computed


# ---------------------------------------------------------------------------
# Classification logic
# ---------------------------------------------------------------------------

def classify_gap(gap_pct: float) -> GapInfo:
    """
    Given a signed gap fraction (positive = up, negative = down),
    returns a fully-populated GapInfo with direction, size, and flags.

    Parameters
    ----------
    gap_pct : float
        Signed fraction (e.g. 0.0073 means +0.73%, -0.009 means -0.90%).
    """
    cfg = GAP_CONFIG
    flat_thresh   = cfg["flat_threshold"]
    small_thresh  = cfg["small_threshold"]
    medium_thresh = cfg["medium_threshold"]
    large_thresh  = cfg["large_threshold"]

    info = GapInfo(gap_pct=round(gap_pct, 6))
    abs_pct = abs(gap_pct)

    # --- Direction ---
    if abs_pct <= flat_thresh:
        info.direction = "FLAT"
    elif gap_pct > 0:
        info.direction = "UP"
    else:
        info.direction = "DOWN"

    # --- Size + behavioural flags ---
    if abs_pct < small_thresh:
        info.size = "SMALL"
        info.caution_level = 1
        info.extra_confirmation_required = False
        info.no_chase_same_direction = False
        info.be_very_selective = False

    elif abs_pct < medium_thresh:
        info.size = "MEDIUM"
        info.caution_level = 2
        info.extra_confirmation_required = False
        info.no_chase_same_direction = False
        info.be_very_selective = False

    elif abs_pct < large_thresh:
        info.size = "LARGE"
        info.caution_level = 3
        info.extra_confirmation_required = True
        info.no_chase_same_direction = True
        info.be_very_selective = False

    else:
        info.size = "VERY_LARGE"
        info.caution_level = 4
        info.extra_confirmation_required = True
        info.no_chase_same_direction = True
        info.be_very_selective = True

    # --- Human-readable label ---
    direction_str = {"UP": "Gap-Up", "DOWN": "Gap-Down", "FLAT": "Flat Open"}[info.direction]
    info.label = f"{info.size.replace('_', ' ').title()} {direction_str} ({abs_pct * 100:.2f}%)"

    return info


# ---------------------------------------------------------------------------
# API-based gap fetch helpers
# ---------------------------------------------------------------------------

def _get_prev_close(smart_api, spot_exchange: str, spot_token: str) -> Optional[float]:
    """
    Fetches the previous trading session's closing price using ONE_DAY candles.
    Returns the close of the last completed day before today, or None on failure.
    """
    try:
        today = datetime.now()
        from_date = (today - timedelta(days=10)).strftime("%Y-%m-%d 09:15")
        to_date = today.strftime("%Y-%m-%d 15:30")

        res = _safe_get_candle_data(smart_api, {
            "exchange": spot_exchange,
            "symboltoken": str(spot_token),
            "interval": "ONE_DAY",
            "fromdate": from_date,
            "todate": to_date,
        }, max_retries=3, retry_delay=2.0)

        if res and res.get("status") and res.get("data"):
            candles = res["data"]
            today_str = today.strftime("%Y-%m-%d")
            # Exclude any candle whose timestamp starts with today's date
            prev_candles = [c for c in candles if c[0][:10] < today_str]
            if prev_candles:
                # Candle format: [timestamp, open, high, low, close, volume]
                return float(prev_candles[-1][4])   # close of last prev-day candle
    except Exception as e:
        print(f"[!] Gap Analyzer: Could not fetch previous close: {e}")
    return None


def _get_today_open(smart_api, spot_exchange: str, spot_token: str) -> Optional[float]:
    """
    Fetches the first 1-minute candle open of today's session (9:15 AM).
    Returns the open price or None on failure.

    If called before 9:15 AM, waits (with status prints every 15 s) until
    market open so the API does not reject the request with AB1012.
    """
    # Block here until the 9:15 candle actually exists
    wait_until_market_open(status_interval=60)

    try:
        today = datetime.now()
        today_str = today.strftime("%Y-%m-%d")
        from_date = f"{today_str} 09:15"
        
        # Ensure to_date does not exceed current time to avoid future-timestamp API rejection
        target_to = today.replace(hour=9, minute=17, second=0, microsecond=0)
        max_to = min(target_to, today)
        to_date = max_to.strftime("%Y-%m-%d %H:%M")
        
        if to_date < from_date:
            to_date = from_date

        res = _safe_get_candle_data(smart_api, {
            "exchange": spot_exchange,
            "symboltoken": str(spot_token),
            "interval": "ONE_MINUTE",
            "fromdate": from_date,
            "todate": to_date,
        }, max_retries=3, retry_delay=2.0)

        if res and res.get("status") and res.get("data"):
            candles = res["data"]
            if candles:
                return float(candles[0][1])   # open of 9:15 candle
    except Exception as e:
        print(f"[!] Gap Analyzer: Could not fetch today's open: {e}")
    return None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def get_opening_gap(
    smart_api,
    spot_exchange: str,
    spot_token: str,
    prev_close_override: Optional[float] = None,
    today_open_override: Optional[float] = None,
) -> GapInfo:
    """
    Computes and classifies the overnight gap for the current trading session.

    Parameters
    ----------
    smart_api            : Authenticated SmartConnect instance.
    spot_exchange        : Exchange string e.g. "NSE" or "BSE".
    spot_token           : Instrument token for the spot index.
    prev_close_override  : Optional manual previous-close value (skips API call).
    today_open_override  : Optional manual today-open value (skips API call).

    Returns
    -------
    GapInfo dataclass with all classification and behavioural fields set.
    On any API failure, GapInfo.fetch_error is non-empty and caution_level=1.
    """
    info = GapInfo()

    # ---- Previous close ----
    prev_close = prev_close_override
    if prev_close is None:
        prev_close = _get_prev_close(smart_api, spot_exchange, spot_token)

    if not prev_close or prev_close <= 0:
        info.fetch_error = "Could not determine previous day close."
        info.label = "Gap unknown (API error)"
        return info

    info.prev_close = round(prev_close, 2)

    # ---- Today's open ----
    today_open = today_open_override
    if today_open is None:
        today_open = _get_today_open(smart_api, spot_exchange, spot_token)

    if not today_open or today_open <= 0:
        info.fetch_error = "Could not determine today's opening price."
        info.label = "Gap unknown (API error)"
        return info

    info.today_open = round(today_open, 2)

    # ---- Compute gap ----
    info.gap_points = round(today_open - prev_close, 2)
    raw_pct = (today_open - prev_close) / prev_close   # signed fraction

    # Merge full classification into our info object
    classified = classify_gap(raw_pct)
    info.gap_pct                     = classified.gap_pct
    info.direction                   = classified.direction
    info.size                        = classified.size
    info.label                       = classified.label
    info.caution_level               = classified.caution_level
    info.extra_confirmation_required = classified.extra_confirmation_required
    info.no_chase_same_direction     = classified.no_chase_same_direction
    info.be_very_selective           = classified.be_very_selective

    return info


# ---------------------------------------------------------------------------
# Console output helper
# ---------------------------------------------------------------------------

_CAUTION_ICONS = {1: "🟢", 2: "🟡", 3: "🔥", 4: "🔴"}


def print_gap_summary(info: GapInfo) -> None:
    """Prints a formatted gap analysis summary to the console."""
    icon = _CAUTION_ICONS.get(info.caution_level, "⬜")

    print("\n" + "=" * 65)
    print(f"  {icon}  GAP & OPENING ANALYSIS")
    print("=" * 65)

    if info.fetch_error:
        print(f"  [!] {info.fetch_error}")
        print("      Proceeding without gap filter — standard rules apply.")
        print("=" * 65 + "\n")
        return

    sign = "+" if info.gap_points >= 0 else ""
    print(f"  Previous Close     : {info.prev_close:.2f}")
    print(f"  Today's Open       : {info.today_open:.2f}")
    print(f"  Gap                : {sign}{info.gap_points:.2f} pts  ({sign}{info.gap_pct * 100:.2f}%)")
    print(f"  Classification     : {info.label}")
    print(f"  Caution Level      : {info.caution_level}/4")
    print()

    if info.size == "SMALL":
        print("  ✅ Small gap — standard breakout rules apply.")
        print("     ▸ Bull Call Spread on High breakout + 5 min confirmation")
        print("     ▸ Bear Put Spread on Low breakdown + 5 min confirmation")
        print("     ▸ NO TRADE if market moves sideways")

    elif info.size == "MEDIUM":
        print("  ✅ Medium gap — standard rules with normal confirmation.")
        print("     ▸ Bull Call Spread after breakout confirmation")
        print("     ▸ Bear Put Spread after breakdown confirmation")
        print("     ▸ NO TRADE if market moves sideways")

    elif info.size == "LARGE":
        print("  ⚠️  Large gap — DO NOT chase the opening move!")
        if info.direction == "UP":
            print("     ▸ Gap-Up: wait for consolidation then High breakout → Bull Call Spread")
            print("     ▸ If recovering DOWN: confirmed reversal → Bear Put Spread")
        else:
            print("     ▸ Gap-Down: wait for confirmation then Low breakdown → Bear Put Spread")
            print("     ▸ If recovering UP: confirmed recovery → Bull Call Spread")
        print("     ▸ NO TRADE if market moves sideways")
        print("     ▸ Extra consolidation wait is active (no-chase same direction)")

    elif info.size == "VERY_LARGE":
        print("  🚨 Very Large gap — BE VERY SELECTIVE!")
        print("     ▸ Wait for the market to clearly establish direction.")
        print("     ▸ Do NOT chase the opening move in any direction.")
        print("     ▸ Skipping the trade entirely is a valid decision.")

    print("=" * 65 + "\n")


# ---------------------------------------------------------------------------
# Standalone visual test (python -m daybreakout.gap_analyzer)
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    test_cases = [
        (0.0010, "Tiny gap-up     "),
        (0.0025, "Small gap-up    "),
        (-0.0018, "Small gap-down  "),
        (0.0045, "Medium gap-up   "),
        (-0.0055, "Medium gap-down "),
        (0.0073, "Large gap-up    "),
        (-0.0090, "Large gap-down  "),
        (0.0120, "Very Large gap-up  "),
        (-0.0150, "Very Large gap-down"),
    ]
    print(f"\n{'Description':30s}  {'Label':47s}  CL  no_chase  selective")
    print("-" * 100)
    for pct, desc in test_cases:
        g = classify_gap(pct)
        print(f"{desc:30s}  {g.label:47s}  {g.caution_level}   {str(g.no_chase_same_direction):5s}    {g.be_very_selective}")
