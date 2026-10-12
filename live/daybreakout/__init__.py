"""
Day Breakout Strategy Module
Previous day (12:30 PM - 3:15 PM) range breakout with 15-minute candle-close confirmation,
Bull Call Spread / Bear Put Spread execution, and broken_call risk management.
Includes Gap & Opening Rules analysis for overnight gap classification.
"""

from live.daybreakout.config import INSTRUMENTS, RISK_CONFIG, GAP_CONFIG
from live.daybreakout.range_calculator import get_previous_day_range
from live.daybreakout.contract_finder import find_spread_contracts, find_strategy_contracts
from live.daybreakout.executor import execute_daybreakout_spread, recover_existing_positions, apply_x_factor
from live.daybreakout.strategy import run_daybreakout_strategy
from live.daybreakout.gap_analyzer import (
    GapInfo,
    classify_gap,
    get_opening_gap,
    print_gap_summary,
)

__all__ = [
    "INSTRUMENTS",
    "RISK_CONFIG",
    "GAP_CONFIG",
    "get_previous_day_range",
    "find_spread_contracts",
    "find_strategy_contracts",
    "execute_daybreakout_spread",
    "recover_existing_positions",
    "apply_x_factor",
    "run_daybreakout_strategy",
    "GapInfo",
    "classify_gap",
    "get_opening_gap",
    "print_gap_summary",
]


