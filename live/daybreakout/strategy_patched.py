"""
Patched Strategy Runner for Dual-Mode Tracking.
Provides mode-specific tracking for REVERSE and NO-REVERSE instances.

Design: executor._resolve_mode() already reads the thread-local mode set by
set_mode() below, so all stats, cooldowns, and trade gating route through the
mode-aware paths in executor.py natively. No module-level monkey-patching is
needed — and would be a race condition in dual-thread execution because the
first thread to finish would call restore_executor_functions(), wiping out the
patched globals mid-flight for the still-running second thread.
"""

import sys
from pathlib import Path
from typing import Dict, Any, Optional

# Add parent directory to path for imports
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from live.daybreakout.strategy import run_daybreakout_strategy as _original_run_daybreakout_strategy

try:
    from live.daybreakout.logger import logger
except ImportError:
    logger = None

import threading

# Thread-local mode tracking so parallel REVERSE and NO-REVERSE threads never collide
_thread_local = threading.local()


def set_mode(mode: str):
    """Set the current tracking mode (REVERSE or NO-REVERSE) for the current thread."""
    _thread_local.mode = mode.upper()
    if logger:
        logger.info(f"Strategy tracking mode set to: {_thread_local.mode} in thread {threading.current_thread().name}")


def get_mode() -> Optional[str]:
    """Get the current tracking mode for the current thread."""
    return getattr(_thread_local, "mode", None)


def run_daybreakout_strategy_patched(
    instrument_name: str = "NIFTY",
    range_high: Optional[float] = None,
    range_low: Optional[float] = None,
    trading_day: Optional[str] = None,
    lots: int = 10,
    confirmation_seconds: int = 300,
    poll_interval: float = 1.0,
    smart_api=None,
    paper_trade: bool = False,
    auto_execute: bool = True,
    gap_info=None,
    skip_gap_analysis: bool = False,
    reverse: bool = True,
    structure: str = "spread",
    confirm_mode: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Run daybreakout strategy with mode-specific tracking.

    executor._resolve_mode() reads get_mode() from this module's thread-local
    storage, so all mode-scoped gating, stats, cooldowns, and square-offs are
    handled natively by the executor without any monkey-patching.
    """
    current_mode = get_mode()
    if logger:
        logger.info(f"Running daybreakout strategy with mode: {current_mode} in thread {threading.current_thread().name}")

    return _original_run_daybreakout_strategy(
        instrument_name=instrument_name,
        range_high=range_high,
        range_low=range_low,
        trading_day=trading_day,
        lots=lots,
        confirmation_seconds=confirmation_seconds,
        poll_interval=poll_interval,
        smart_api=smart_api,
        paper_trade=paper_trade,
        auto_execute=auto_execute,
        gap_info=gap_info,
        skip_gap_analysis=skip_gap_analysis,
        reverse=reverse,
        structure=structure,
        confirm_mode=confirm_mode,
        mode=current_mode,
    )
