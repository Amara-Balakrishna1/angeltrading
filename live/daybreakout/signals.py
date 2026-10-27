"""
15-minute candle-close signal rules shared by live Day Breakout and the backtest.

A signal fires on the *close* of a completed 15-minute candle, not on a tick
hold. This matches backtest/backtest_daybreakout.py.
"""

from dataclasses import dataclass
from typing import Optional

from live.daybreakout.config import BREAKOUT_CONFIG, GAP_CONFIG
from live.daybreakout.gap_analyzer import GapInfo


def extra_15m_candles_wait() -> int:
    """LARGE-gap no-chase wait, in 15-minute candle units (minimum 1)."""
    extra_sec = GAP_CONFIG["large_gap_extra_wait_seconds"]
    return max(1, extra_sec // 900)


def apply_reverse(direction: str, reverse: bool) -> str:
    if not reverse:
        return direction
    return "BEARISH" if direction == "BULLISH" else "BULLISH"


def update_gap_territory(
    c_close: float,
    range_high: float,
    range_low: float,
    gap_info: GapInfo,
    gap_territory_touched: bool,
) -> bool:
    """Arm recovery after a LARGE / VERY_LARGE gap once price closes in gap territory."""
    if gap_info.caution_level < 3:
        return gap_territory_touched
    if gap_info.direction == "DOWN" and c_close < range_low:
        return True
    if gap_info.direction == "UP" and c_close > range_high:
        return True
    return gap_territory_touched


@dataclass
class CandleSignal:
    """Pre-reverse breakout/recovery decision for one completed 15-minute close."""

    direction: Optional[str] = None       # BULLISH / BEARISH before reverse
    trade_type: str = "BREAKOUT"          # BREAKOUT / RECOVERY
    spot_reason: str = ""
    gap_territory_touched: bool = False
    skip: bool = True
    skip_reason: str = ""


def evaluate_15m_close(
    c_time: str,
    c_close: float,
    range_high: float,
    range_low: float,
    gap_info: GapInfo,
    gap_territory_touched: bool,
    candle_index: int,
    extra_candles_wait: Optional[int] = None,
    last_entry_hhmm: Optional[str] = None,
    first_entry_hhmm: Optional[str] = None,
    no_chase_cutoff_hhmm: Optional[str] = None,
) -> CandleSignal:
    """
    Evaluate one completed 15-minute candle the same way the backtest does.

    Parameters
    ----------
    c_time : str
        Candle start as HH:MM (e.g. "09:15").
    candle_index : int
        0-based index of this candle in today's 15-minute series.
    no_chase_cutoff_hhmm : str, optional
        Once the candle time is at/after this HH:MM, the no-chase opening-candle
        skip is suppressed. Lets a late session start confirm a with-gap breakout
        immediately instead of waiting out the opening no-chase window.
    """
    if extra_candles_wait is None:
        extra_candles_wait = extra_15m_candles_wait()
    if last_entry_hhmm is None:
        last_entry_hhmm = BREAKOUT_CONFIG["last_entry_hhmm"]
    if first_entry_hhmm is None:
        first_entry_hhmm = BREAKOUT_CONFIG.get("first_entry_hhmm", "09:30")
    if no_chase_cutoff_hhmm is None:
        no_chase_cutoff_hhmm = GAP_CONFIG.get("no_chase_cutoff_hhmm")
    past_no_chase_cutoff = (
        no_chase_cutoff_hhmm is not None and c_time >= no_chase_cutoff_hhmm
    )

    touched = update_gap_territory(c_close, range_high, range_low, gap_info, gap_territory_touched)

    if first_entry_hhmm and c_time < first_entry_hhmm:
        return CandleSignal(
            gap_territory_touched=touched,
            skip=True,
            skip_reason=f"No entries before {first_entry_hhmm} (candle {c_time})",
        )

    if c_time > last_entry_hhmm:
        return CandleSignal(
            gap_territory_touched=touched,
            skip=True,
            skip_reason=f"No entries after {last_entry_hhmm} (candle {c_time})",
        )

    trade_type = "BREAKOUT"
    direction = None
    spot_reason = ""

    if c_close > range_high:
        direction = "BULLISH"
        spot_reason = f"Spot {c_close:.1f} > High {range_high:.1f}"
    elif c_close < range_low:
        direction = "BEARISH"
        spot_reason = f"Spot {c_close:.1f} < Low {range_low:.1f}"
    elif gap_info.caution_level >= 3 and touched:
        if gap_info.direction == "DOWN" and c_close > range_low:
            direction = "BULLISH"
            trade_type = "RECOVERY"
            spot_reason = f"Reentered range past Low {range_low:.1f}"
            touched = False
        elif gap_info.direction == "UP" and c_close < range_high:
            direction = "BEARISH"
            trade_type = "RECOVERY"
            spot_reason = f"Reentered range past High {range_high:.1f}"
            touched = False

    if not direction:
        return CandleSignal(gap_territory_touched=touched, skip=True, skip_reason="No 15m close signal")

    same_gap_direction = (
        (direction == "BULLISH" and gap_info.direction == "UP")
        or (direction == "BEARISH" and gap_info.direction == "DOWN")
    )
    no_chase_skip = (
        trade_type == "BREAKOUT"
        and gap_info.no_chase_same_direction
        and same_gap_direction
        and candle_index < extra_candles_wait
        and not past_no_chase_cutoff
    )
    if no_chase_skip:
        return CandleSignal(
            direction=direction,
            trade_type=trade_type,
            spot_reason=spot_reason,
            gap_territory_touched=touched,
            skip=True,
            skip_reason="No-chase: skipped opening 15m candle in gap direction",
        )

    return CandleSignal(
        direction=direction,
        trade_type=trade_type,
        spot_reason=spot_reason,
        gap_territory_touched=touched,
        skip=False,
    )
