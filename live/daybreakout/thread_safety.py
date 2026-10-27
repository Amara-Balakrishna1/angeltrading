"""
Cross-thread coordination for live Day Breakout.

- smart_api_lock: only one SmartAPI HTTP call at a time (shared session is not thread-safe).
- flatten_lock: only one square-off (SL, target, or EOD) at a time.
- FlattenCoordinator: skip a second EOD flatten for the same symbol/day.
"""

import threading
from datetime import datetime
from typing import Optional


smart_api_lock = threading.RLock()


class FlattenCoordinator:
    def __init__(self):
        self.lock = threading.RLock()
        self._completed = set()

    def _key(self, symbol_base: str, mode: Optional[str] = None) -> str:
        mode_str = f":{mode.upper().strip()}" if mode else ""
        return f"{datetime.now().strftime('%Y-%m-%d')}:{str(symbol_base).upper().strip()}{mode_str}"

    def is_completed(self, symbol_base: str, mode: Optional[str] = None) -> bool:
        with self.lock:
            if mode:
                k_mode = self._key(symbol_base, mode)
                return k_mode in self._completed
            k_sym = self._key(symbol_base)
            return k_sym in self._completed

    def mark_completed(self, symbol_base: str, mode: Optional[str] = None) -> None:
        with self.lock:
            self._completed.add(self._key(symbol_base, mode))

    def reset(self, symbol_base: str, mode: Optional[str] = None) -> None:
        """Reset completed state (e.g. when opening a new trade) so square-off can run cleanly."""
        with self.lock:
            if mode:
                self._completed.discard(self._key(symbol_base, mode))
            self._completed.discard(self._key(symbol_base))


flatten = FlattenCoordinator()


def infer_symbol_base(trading_symbol: Optional[str], fallback: str = "NIFTY") -> str:
    text = (trading_symbol or "").upper()
    if text.startswith("SENSEX"):
        return "SENSEX"
    if text.startswith("NIFTY"):
        return "NIFTY"
    return str(fallback).upper().strip() if fallback else "NIFTY"
