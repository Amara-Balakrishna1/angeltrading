"""
Trade Executor and Live Risk Management Monitor for Day Breakout Strategy.
Implements broken_call risk management:
- max_loss = -5000
- max_profit = 50000
- profit_threshold = 3000
- min_profit_step = 2000
- Trailing locked profit logic
- BUY leg first entry / SELL leg first exit
- Trade logging to daily and global CSV
"""

import sys
import io
import csv
import time
import threading
import concurrent.futures
from decimal import Decimal, ROUND_CEILING
from datetime import datetime, timedelta
from typing import Dict, Any, List, Optional, Tuple
from collections import defaultdict
import re
import pyotp
import requests

# Ensure UTF-8 output on Windows console
if sys.platform == "win32":
    try:
        if hasattr(sys.stdout, "reconfigure"):
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        else:
            sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    except Exception:
        pass

from SmartApi.smartConnect import SmartConnect
import config
from live.daybreakout.config import RISK_CONFIG, BREAKOUT_CONFIG, DATA_DIR, TRADES_FILE, TRIGGERED_TRADES_FILE, ORDERS_FILE
from live.daybreakout.logger import logger
from live.daybreakout.order_updates import OrderUpdateStream
from live.daybreakout.thread_safety import flatten, infer_symbol_base, smart_api_lock

_order_update_stream: Optional[OrderUpdateStream] = None
_order_update_stream_lock = threading.Lock()


def start_order_update_stream(smart_api: SmartConnect) -> bool:
    """Start the single shared broker order-update socket used by dual strategies."""
    global _order_update_stream
    with _order_update_stream_lock:
        if _order_update_stream is not None:
            return True
        try:
            stream = OrderUpdateStream(
                auth_token=smart_api.access_token,
                api_key=config.api_key.strip(),
                client_code=config.username,
                feed_token=smart_api.getfeedToken(),
            )
            stream.start()
            _order_update_stream = stream
            logger.info("Started shared Angel order-update WebSocket")
            return True
        except Exception as exc:
            logger.warning(f"Unable to start Angel order-update WebSocket; REST polling remains active: {exc}")
            return False


def stop_order_update_stream() -> None:
    global _order_update_stream
    with _order_update_stream_lock:
        stream, _order_update_stream = _order_update_stream, None
    if stream is not None:
        stream.close()

# Thread-safe monitoring state management
def _resolve_mode(mode: Optional[str] = None) -> Optional[str]:
    """Helper to resolve current strategy mode (REVERSE or NO-REVERSE) from argument or thread-local storage."""
    if mode is not None:
        return mode.upper().strip()
    try:
        from live.daybreakout.strategy_patched import get_mode
        m = get_mode()
        return m.upper().strip() if m else None
    except Exception:
        return None


def _trade_eod_time(trade: Dict[str, Any], today: datetime) -> str:
    """Resolve regular/expiry close time using the trade's actual contract expiry."""
    expiry_values = [str(trade.get("expiry", ""))]
    for leg_key in ("buy_leg", "sell_leg"):
        leg = trade.get(leg_key) or {}
        symbol = str(leg.get("symbol", "")).upper()
        match = re.search(r"(\d{2}[A-Z]{3}\d{2,4})", symbol)
        if match:
            expiry_values.append(match.group(1))
    expiry_date = None
    for expiry_value in expiry_values:
        for date_format in ("%d%b%y", "%d%b%Y"):
            try:
                expiry_date = datetime.strptime(expiry_value.upper(), date_format).date()
                break
            except ValueError:
                continue
        if expiry_date:
            break
    if expiry_date == today.date():
        return BREAKOUT_CONFIG.get("expiry_exit_hhmm", "14:45")
    return trade.get("eod_exit_hhmm") or BREAKOUT_CONFIG.get("eod_exit_hhmm", "15:10")


class MonitoringStateManager:
    """Thread-safe manager for active trade positions and consolidated monitoring."""
    def __init__(self):
        self._lock = threading.RLock()  # Use RLock for reentrant locking
        self._active_trades: Dict[str, Dict[str, Any]] = {}  # trade_id -> trade_state dict
        self._last_stop_loss_time: Dict[str, datetime] = {}  # symbol_base -> datetime of last SL
        self._last_exit_time: Dict[str, Tuple[datetime, str]] = {}  # symbol_base -> (exit_time, exit_reason)
        self._consecutive_losses: Dict[str, int] = {}  # symbol_base -> consecutive loss count
        self._trade_entry_time: Dict[str, datetime] = {}  # trade_id -> entry time
        self._monitor_thread: Optional[threading.Thread] = None
        self._stop_requested: bool = False
        self._stop_event = threading.Event()
        self._smart_api: Optional[SmartConnect] = None
        self._last_reconcile_ts: float = 0.0

    def add_trade(self, trade_id: str, trade_data: Dict[str, Any]) -> bool:
        """Register a new trade for monitoring."""
        with self._lock:
            if trade_id in self._active_trades:
                logger.warning(f"Trade {trade_id} already registered in monitoring state.")
                return False
            self._active_trades[trade_id] = trade_data
            self._trade_entry_time[trade_id] = datetime.now()
            logger.info(f"Registered trade {trade_id} in monitoring state. Total active trades: {len(self._active_trades)}")
            return True

    def remove_trade(self, trade_id: str) -> Optional[Dict[str, Any]]:
        """Remove a trade after square-off."""
        with self._lock:
            trade = self._active_trades.pop(trade_id, None)
            if trade:
                self._trade_entry_time.pop(trade_id, None)
                logger.info(f"Removed trade {trade_id} from monitoring state. Remaining active trades: {len(self._active_trades)}")
            return trade

    def _make_key(self, symbol_base: Optional[str] = None, mode: Optional[str] = None) -> str:
        sym = str(symbol_base).upper().strip() if symbol_base else "ALL"
        m = str(mode).upper().strip() if mode else ""
        return f"{sym}:{m}" if m else sym

    def record_stop_loss(self, symbol_base: str, exit_time: Optional[datetime] = None, mode: Optional[str] = None):
        """Record when a trade exited due to stop loss."""
        with self._lock:
            sl_time = exit_time or datetime.now()
            sym_clean = str(symbol_base).upper().strip()
            if mode:
                key = self._make_key(sym_clean, mode)
                self._last_stop_loss_time[key] = sl_time
                self._consecutive_losses[key] = self._consecutive_losses.get(key, 0) + 1
                logger.info(f"Recorded stop-loss for {sym_clean} ({mode}) at {sl_time.strftime('%H:%M:%S')}. Consecutive losses: {self._consecutive_losses[key]}")
            else:
                self._last_stop_loss_time[sym_clean] = sl_time
                self._last_stop_loss_time["ALL"] = sl_time
                self._consecutive_losses[sym_clean] = self._consecutive_losses.get(sym_clean, 0) + 1
                self._consecutive_losses["ALL"] = self._consecutive_losses.get("ALL", 0) + 1
                logger.info(f"Recorded stop-loss for {sym_clean} at {sl_time.strftime('%H:%M:%S')}. Consecutive losses: {self._consecutive_losses[sym_clean]}")

    def record_profit_exit(self, symbol_base: str, exit_time: Optional[datetime] = None, mode: Optional[str] = None):
        """Record when a trade exited with profit (resets consecutive loss counter)."""
        with self._lock:
            sym_clean = str(symbol_base).upper().strip()
            if mode:
                key = self._make_key(sym_clean, mode)
                self._consecutive_losses[key] = 0
                logger.info(f"Recorded profit exit for {sym_clean} ({mode}). Consecutive losses reset to 0.")
            else:
                self._consecutive_losses[sym_clean] = 0
                self._consecutive_losses["ALL"] = 0
                logger.info(f"Recorded profit exit for {sym_clean}. Consecutive losses reset to 0.")

    def record_exit(self, symbol_base: str, exit_reason: str, exit_time: Optional[datetime] = None, mode: Optional[str] = None):
        """Record when a trade exited with specific reason for cooldown tracking."""
        with self._lock:
            exit_dt = exit_time or datetime.now()
            sym_clean = str(symbol_base).upper().strip()
            if mode:
                key = self._make_key(sym_clean, mode)
                self._last_exit_time[key] = (exit_dt, exit_reason)
                logger.info(f"Recorded exit for {sym_clean} ({mode}) - Reason: {exit_reason} at {exit_dt.strftime('%H:%M:%S')}")
            else:
                self._last_exit_time[sym_clean] = (exit_dt, exit_reason)
                self._last_exit_time["ALL"] = (exit_dt, exit_reason)
                logger.info(f"Recorded exit for {sym_clean} - Reason: {exit_reason} at {exit_dt.strftime('%H:%M:%S')}")

    def get_consecutive_losses(self, symbol_base: Optional[str] = None, mode: Optional[str] = None) -> int:
        """Get the consecutive loss count for a symbol (and optional mode)."""
        with self._lock:
            if mode:
                key = self._make_key(symbol_base, mode)
                return self._consecutive_losses.get(key, 0)
            key = str(symbol_base).upper().strip() if symbol_base else "ALL"
            return self._consecutive_losses.get(key, 0)

    def get_trade_holding_time(self, trade_id: str) -> Optional[float]:
        """Get the holding time in minutes for a trade."""
        with self._lock:
            if trade_id not in self._trade_entry_time:
                return None
            entry_time = self._trade_entry_time[trade_id]
            elapsed = (datetime.now() - entry_time).total_seconds() / 60.0
            return elapsed

    def get_last_stop_loss_time(self, symbol_base: Optional[str] = None, mode: Optional[str] = None) -> Optional[datetime]:
        """Returns the in-memory datetime of the last stop loss exit."""
        with self._lock:
            key = self._make_key(symbol_base, mode)
            return self._last_stop_loss_time.get(key)

    def get_last_exit_time(self, symbol_base: Optional[str] = None, mode: Optional[str] = None) -> Tuple[Optional[datetime], Optional[str]]:
        """Returns the in-memory datetime and reason of the last exit."""
        with self._lock:
            key = self._make_key(symbol_base, mode)
            last_exit = self._last_exit_time.get(key)
            if last_exit:
                return last_exit[0], last_exit[1]
            return None, None

    def is_reentry_allowed(self, symbol_base: Optional[str] = None, mode: Optional[str] = None) -> Tuple[bool, str]:
        """
        Check if re-entry is allowed based on exit-type specific cooldowns.
        Returns (is_allowed, message) tuple.
        """
        with self._lock:
            key = self._make_key(symbol_base, mode)
            last_exit = self._last_exit_time.get(key)

            if not last_exit:
                return True, "No previous exit recorded"

            exit_time, exit_reason = last_exit
            cooldown_config = RISK_CONFIG.get("exit_cooldown_minutes", {})
            cooldown_minutes = cooldown_config.get(exit_reason, 0)

            if cooldown_minutes >= 9999:  # Special value for "no re-entry"
                return False, f"Re-entry blocked after {exit_reason} (no re-entry allowed)"

            if cooldown_minutes <= 0:
                return True, f"No cooldown required after {exit_reason}"

            elapsed_min = (datetime.now() - exit_time).total_seconds() / 60.0
            if elapsed_min < cooldown_minutes:
                remaining_min = cooldown_minutes - elapsed_min
                return False, f"Cooldown active after {exit_reason} ({remaining_min:.0f}m remaining of {cooldown_minutes}m)"

            return True, f"Cooldown expired after {exit_reason}"

    def get_trade(self, trade_id: str) -> Optional[Dict[str, Any]]:
        """Get copy of a specific trade's state."""
        with self._lock:
            trade = self._active_trades.get(trade_id)
            return trade.copy() if trade else None

    def get_all_trades(self) -> Dict[str, Dict[str, Any]]:
        """Get copy of all active trades."""
        with self._lock:
            return {k: v.copy() for k, v in self._active_trades.items()}

    def update_trade_field(self, trade_id: str, key: str, value: Any) -> bool:
        """
        Persist a single field update back to the live trade dict.
        Used by the monitor worker to write back max_pnl / locked_profit
        so the values survive across polling cycles.
        Returns False if trade_id is no longer registered.
        """
        with self._lock:
            if trade_id not in self._active_trades:
                return False
            self._active_trades[trade_id][key] = value
            return True

    def has_active_trade(self, symbol_base: Optional[str] = None, mode: Optional[str] = None) -> bool:
        """Checks if an active trade is open, optionally filtered by symbol_base and/or mode."""
        with self._lock:
            if not self._active_trades:
                return False
            trades = list(self._active_trades.values())
            if symbol_base is not None:
                sym_clean = str(symbol_base).upper().strip()
                trades = [t for t in trades if str(t.get("symbol_base", "")).upper().strip() == sym_clean]
            if mode is not None:
                mode_clean = str(mode).upper().strip()
                # Only records explicitly tagged with this mode count. Treating an
                # untagged record as a match let a single legacy trade block both
                # the REVERSE and the NO-REVERSE strategy threads at once.
                trades = [
                    t for t in trades
                    if str(t.get("mode") or "").upper().strip() == mode_clean
                ]
            return len(trades) > 0

    def note_flat_confirmation(self, trade_id: str) -> int:
        """Increment and return the consecutive 'broker reports this trade flat' counter."""
        with self._lock:
            trade = self._active_trades.get(trade_id)
            if trade is None:
                return 0
            count = int(trade.get("flat_confirmations", 0) or 0) + 1
            trade["flat_confirmations"] = count
            return count

    def clear_flat_confirmations(self, trade_id: str) -> None:
        """Reset the flat-confirmation counter when the broker still shows the legs."""
        with self._lock:
            trade = self._active_trades.get(trade_id)
            if trade is not None and int(trade.get("flat_confirmations", 0) or 0):
                trade["flat_confirmations"] = 0

    def set_smart_api(self, smart_api: SmartConnect) -> None:
        """Remember the client so gating checks can reconcile against the broker."""
        with self._lock:
            self._smart_api = smart_api

    def get_smart_api(self) -> Optional[SmartConnect]:
        with self._lock:
            return getattr(self, "_smart_api", None)

    def is_monitoring_active(self) -> bool:
        """Backward-compatible check: True if monitoring is active and has active trades."""
        with self._lock:
            return not self._stop_requested and len(self._active_trades) > 0 and (
                self._monitor_thread is not None and self._monitor_thread.is_alive()
            )

    def is_stop_requested(self) -> bool:
        """Check if monitor shutdown has been requested."""
        with self._lock:
            return self._stop_requested

    def ensure_monitor_running(self, smart_api: SmartConnect, check_interval: float = RISK_CONFIG["check_interval"]) -> threading.Thread:
        """Starts the single consolidated monitor thread if not already running."""
        with self._lock:
            self._stop_requested = False
            self._stop_event.clear()
            if self._monitor_thread is None or not self._monitor_thread.is_alive():
                self._monitor_thread = threading.Thread(
                    target=_consolidated_monitor_worker,
                    args=(smart_api, check_interval),
                    daemon=True,
                    name="Consolidated_PnL_Monitor"
                )
                self._monitor_thread.start()
                logger.info(f"Consolidated P&L monitor thread started (check_interval={check_interval}s).")
            return self._monitor_thread

    def wait_for_stop(self, timeout: float) -> bool:
        """Sleep up to timeout seconds, or return immediately when stop is requested."""
        return self._stop_event.wait(timeout=max(0.05, float(timeout)))

    def stop_monitoring(self):
        """Signals the consolidated monitoring thread to stop."""
        with self._lock:
            if self._active_trades:
                logger.info(
                    "Keeping consolidated P&L monitor running because "
                    f"{len(self._active_trades)} trade(s) remain active."
                )
                return False
            self._stop_requested = True
            self._stop_event.set()
            logger.info("Signaled consolidated P&L monitor to stop.")
            return True

    def is_current_monitor_thread(self) -> bool:
        with self._lock:
            t = self._monitor_thread
        return t is not None and threading.current_thread() is t


# Global instance
_monitoring_state = MonitoringStateManager()


def stop_position_monitor():
    """Stops the active P&L monitoring thread. Can be called from other modules."""
    return _monitoring_state.stop_monitoring()


def get_active_trade_count() -> int:
    """Return the number of positions currently tracked by the P&L monitor."""
    return len(_monitoring_state.get_all_trades())


def has_active_trade(symbol_base: Optional[str] = None, mode: Optional[str] = None) -> bool:
    """Returns True if there is currently an active trade open in monitoring state."""
    mode = _resolve_mode(mode)
    # Memory alone is not authoritative: a record the P&L worker failed to drop
    # (failed square-off, exhausted EOD retries, dead monitor thread) would gate
    # this strategy thread off forever while the broker holds nothing.
    reconcile_ghost_trades(symbol_base=symbol_base, mode=mode)
    return _monitoring_state.has_active_trade(symbol_base=symbol_base, mode=mode)


# ── Broker-backed reconciliation of the in-memory trade registry ─────────────
# A stale record is only dropped after this many consecutive checks that report
# the legs flat, so a single lagging position snapshot can never orphan a live
# trade (worse outcome: we stop monitoring a real exposure).
GHOST_CONFIRMATION_THRESHOLD = 2

# How long a freshly registered trade is given before an empty snapshot counts.
GHOST_MIN_HOLDING_MINUTES = 3.0

# Minimum seconds between /position calls from the gating checks.
GHOST_RECONCILE_MIN_INTERVAL = 45.0


def reconcile_ghost_trades(
    symbol_base: Optional[str] = None,
    mode: Optional[str] = None,
    force: bool = False,
) -> List[str]:
    """
    Drop monitoring records the broker no longer holds.

    The consolidated P&L worker normally removes a trade once it squares it off,
    but every failure path in that worker deliberately keeps the record for a
    retry. If the worker is dead, or a leg's LTP is unavailable so its checks
    never run, the record outlives the position and the strategy loops stay
    gated on "[ACTIVE POSITION]" with no open trade.

    Deliberately conservative:
      * only drops trades whose *every* tracked leg is absent at the broker,
        so a half-filled spread stays registered for the worker to close;
      * requires several consecutive flat snapshots;
      * never drops a trade past its EOD close time - the worker owns that
        close and dropping it would hide a real position;
      * restarts the monitor thread if it died while trades remained.

    Returns the trade_ids that were purged.
    """
    state = _monitoring_state
    with state._lock:
        candidates = [
            (tid, dict(trade))
            for tid, trade in state._active_trades.items()
            if (symbol_base is None
                or str(trade.get("symbol_base", "")).upper().strip() == str(symbol_base).upper().strip())
            and (mode is None
                 or str(trade.get("mode") or "").upper().strip() == str(mode).upper().strip())
        ]
        last_ts = state._last_reconcile_ts

    if not candidates:
        return []

    # Revive the cleaner before doing anything else; it normally reconciles
    # these records itself within one check interval.
    if not (state._monitor_thread is not None and state._monitor_thread.is_alive()):
        smart_api = state.get_smart_api()
        if smart_api is not None:
            logger.warning(
                f"P&L monitor thread is not running with {len(candidates)} trade(s) registered; restarting it."
            )
            try:
                state.ensure_monitor_running(smart_api, check_interval=RISK_CONFIG["check_interval"])
            except Exception as restart_err:
                logger.error(f"Could not restart the P&L monitor thread: {restart_err}")

    if not force and (time.time() - last_ts) < GHOST_RECONCILE_MIN_INTERVAL:
        return []
    with state._lock:
        state._last_reconcile_ts = time.time()

    smart_api = state.get_smart_api()
    if smart_api is None:
        return []

    now_dt = datetime.now()
    now_str = now_dt.strftime("%H:%M:%S")
    open_pos = get_open_positions(smart_api)
    if open_pos is None:
        # API failure is not evidence of a flat position.
        return []
    held_symbols = {pos.get("tradingsymbol") for pos in open_pos}

    purged: List[str] = []
    for trade_id, trade in candidates:
        legs = [leg for leg in (trade.get("buy_leg"), trade.get("sell_leg")) if leg]
        if not legs:
            continue

        # Past the scheduled close the worker is retrying an exit; dropping the
        # record here would stop us ever closing the position.
        if now_str >= _trade_eod_time(trade, now_dt):
            continue

        holding_minutes = state.get_trade_holding_time(trade_id)
        if holding_minutes is None:
            # Registration stamp lost - fall back to the trade's own start time.
            try:
                started = datetime.strptime(
                    str(trade.get("trade_start_time", "")).split(".")[0], "%H:%M:%S"
                )
                start_dt = now_dt.replace(hour=started.hour, minute=started.minute, second=started.second)
                holding_minutes = (now_dt - start_dt).total_seconds() / 60.0
            except (ValueError, TypeError):
                holding_minutes = GHOST_MIN_HOLDING_MINUTES
        if holding_minutes < GHOST_MIN_HOLDING_MINUTES:
            continue

        if any(leg.get("symbol") in held_symbols for leg in legs):
            state.clear_flat_confirmations(trade_id)
            continue

        # A leg we track but the broker does not show means the worker should
        # already have reconciled it. Only drop a fully flat trade.
        count = state.note_flat_confirmation(trade_id)
        if count < GHOST_CONFIRMATION_THRESHOLD:
            logger.info(
                f"[Reconcile] {trade_id} flat at broker ({count}/{GHOST_CONFIRMATION_THRESHOLD} confirmations); "
                "waiting for the monitor worker."
            )
            continue

        state.remove_trade(trade_id)
        purged.append(trade_id)
        logger.warning(
            f"[Reconcile] Dropped stale trade record {trade_id} "
            f"({trade.get('symbol_base')} {trade.get('mode')}) - broker reports no position after "
            f"{count} consecutive checks. Strategy gating released."
        )
        print(
            f"\n[+] [RECONCILE] Removed stale in-memory trade {trade_id} "
            f"({trade.get('symbol_base')} / {trade.get('mode')}); broker holds no such position."
        )

    return purged


def get_consecutive_losses(symbol_base: Optional[str] = None, mode: Optional[str] = None) -> int:
    """Get the consecutive loss count for a symbol."""
    mode = _resolve_mode(mode)
    return _monitoring_state.get_consecutive_losses(symbol_base=symbol_base, mode=mode)


def check_position_timeout(symbol_base: Optional[str] = None, max_minutes: int = 120, mode: Optional[str] = None) -> Tuple[bool, Optional[str]]:
    """
    Check if any active position has exceeded the maximum holding time.
    Returns (has_timeout, trade_id) tuple.
    """
    mode = _resolve_mode(mode)
    with _monitoring_state._lock:
        active_trades = _monitoring_state.get_all_trades()
        for trade_id, trade_data in active_trades.items():
            if symbol_base and trade_data.get("symbol_base", "").upper() != str(symbol_base).upper():
                continue
            if mode and trade_data.get("mode") and trade_data.get("mode", "").upper() != str(mode).upper():
                continue
            
            holding_time = _monitoring_state.get_trade_holding_time(trade_id)
            if holding_time and holding_time > max_minutes:
                return True, trade_id
        return False, None


def is_reentry_allowed(symbol_base: Optional[str] = None, mode: Optional[str] = None) -> Tuple[bool, str]:
    """
    Check if re-entry is allowed based on exit-type specific cooldowns.
    Returns (is_allowed, message) tuple.
    """
    mode = _resolve_mode(mode)
    return _monitoring_state.is_reentry_allowed(symbol_base=symbol_base, mode=mode)


def get_last_stop_loss_time(symbol_base: Optional[str] = None, for_date: Optional[str] = None, mode: Optional[str] = None) -> Optional[datetime]:
    """
    Returns the datetime of the most recent stop loss exit today (or for_date),
    checking both active in-memory state and the logged trades CSV files.
    """
    mode = _resolve_mode(mode)
    mem_time = _monitoring_state.get_last_stop_loss_time(symbol_base, mode=mode)

    now_dt = datetime.now()
    for_date_str = for_date if isinstance(for_date, str) else now_dt.strftime("%Y-%m-%d")

    target_files = []
    if mode:
        try:
            from live.daybreakout.tracking_dual import get_date_trades_file as get_dual_trades_file
            dual_f = get_dual_trades_file(now_dt, symbol_base=symbol_base or "NIFTY", mode=mode)
            if dual_f.exists():
                target_files.append(dual_f)
        except Exception:
            pass

    if not target_files:
        daily_file = get_date_trades_file(now_dt, symbol_base=symbol_base or "NIFTY")
        target_files = [daily_file] if daily_file.exists() else [TRADES_FILE]

    csv_latest_dt = None
    for t_file in target_files:
        if not t_file.exists():
            continue
        try:
            with open(t_file, newline="", encoding="utf-8") as f:
                reader = csv.reader(f)
                rows = [r for r in reader if any(c.strip() for c in r)]
            if not rows:
                continue
            header = [c.lower().strip() for c in rows[0]] if (rows and rows[0][0].lower() == "date") else None
            start_idx = 1 if header else 0
            date_idx = header.index("date") if (header and "date" in header) else 0
            end_time_idx = header.index("end_time") if (header and "end_time" in header) else 2
            sym_idx = header.index("symbol") if (header and "symbol" in header) else (3 if (header and "start_time" in header) else 2)
            exit_reason_idx = header.index("exit_reason") if (header and "exit_reason" in header) else -2

            for row in rows[start_idx:]:
                if len(row) > date_idx and row[date_idx].strip() == for_date_str:
                    if sym_idx < len(row) and symbol_base:
                        if row[sym_idx].strip().upper() != str(symbol_base).upper():
                            continue
                    exit_reason = row[exit_reason_idx].strip().upper() if exit_reason_idx < len(row) else ""
                    if "MAX_LOSS" in exit_reason or "STOP_LOSS" in exit_reason:
                        if len(row) > end_time_idx:
                            time_part = row[end_time_idx].strip()[:8]
                            try:
                                dt_parsed = datetime.strptime(f"{for_date_str} {time_part}", "%Y-%m-%d %H:%M:%S")
                            except Exception:
                                try:
                                    dt_parsed = datetime.strptime(f"{for_date_str} {time_part[:5]}", "%Y-%m-%d %H:%M")
                                except Exception:
                                    continue
                            if csv_latest_dt is None or dt_parsed > csv_latest_dt:
                                csv_latest_dt = dt_parsed
        except Exception as e:
            logger.error(f"Error checking last stop loss in trade stats: {e}")

    candidates = [t for t in (mem_time, csv_latest_dt) if t is not None]
    return max(candidates) if candidates else None


def login_smart_api(max_retries: int = 5, retry_delay: float = 2.5) -> SmartConnect:
    """Authenticates and returns a SmartConnect instance with retry logic."""
    api_key = config.api_key.strip()
    username = config.username
    pwd = config.pwd
    token = config.token
    disable_ssl = getattr(config, "disable_ssl", True)

    for attempt in range(max_retries):
        try:
            smart_api = SmartConnect(api_key, disable_ssl=disable_ssl)
            totp = pyotp.TOTP(token).now()
            session = smart_api.generateSession(username, pwd, totp)
            
            if session.get("status"):
                refresh_token = session["data"]["refreshToken"]
                smart_api.generateToken(refresh_token)
                print("[+] Logged in to SmartAPI.")
                logger.info("Successfully logged in to SmartAPI")
                return smart_api
            else:
                err_msg = session.get("message") or session.get("errorcode") or str(session)
                print(f"[!] SmartAPI login attempt {attempt+1}/{max_retries} failed: {err_msg}")
                logger.error(f"SmartAPI login attempt {attempt+1}/{max_retries} failed: {err_msg}")
                
                if attempt < max_retries - 1:
                    backoff = retry_delay * (attempt + 1)
                    print(f"    Retrying in {backoff:.0f}s...")
                    time.sleep(backoff)
                else:
                    print(f"[!] All {max_retries} login attempts failed.")
                    raise RuntimeError(f"SmartAPI login failed after {max_retries} attempts: {session}")
                    
        except Exception as e:
            err_str = str(e)
            is_rate_limit = (
                "exceeding access rate" in err_str
                or "Too many requests" in err_str
                or "Access denied" in err_str
                or "timeout" in err_str.lower()
                or "connection" in err_str.lower()
            )
            backoff = retry_delay * (attempt + 1) * (2 if is_rate_limit else 1)
            print(f"[!] SmartAPI login attempt {attempt+1}/{max_retries} "
                  f"{'[rate-limit/network]' if is_rate_limit else '[error]'}: {err_str[:120]}")
            
            if attempt < max_retries - 1:
                print(f"    Retrying in {backoff:.0f}s...")
                time.sleep(backoff)
            else:
                print(f"[!] All {max_retries} login attempts failed.")
                raise RuntimeError(f"SmartAPI login failed after {max_retries} attempts: {err_str}")


def apply_x_factor(x_factor: float = 1.0) -> Dict[str, Any]:
    """
    Applies x-factor multiplier to position sizing and risk parameters.
    Returns a dictionary with scaled values.
    
    Parameters scaled by x_factor:
    - max_loss, max_profit, profit_threshold, min_profit_step
    - daily_max_loss, daily_max_profit, fixed_loss_amount
    - Lots/quantity
    
    Parameters NOT scaled:
    - sl_cooldown_minutes (time-based)
    - max_daily_trades (count-based)
    - check_interval (time-based)
    """
    if x_factor <= 0:
        x_factor = 1.0
    
    scaled_config = {
        "x_factor": x_factor,
        "max_loss": RISK_CONFIG["max_loss"] * x_factor,
        "max_profit": RISK_CONFIG["max_profit"] * x_factor,
        "profit_threshold": RISK_CONFIG["profit_threshold"] * x_factor,
        "min_profit_step": RISK_CONFIG["min_profit_step"] * x_factor,
        "daily_max_loss": RISK_CONFIG["daily_max_loss"] * x_factor,
        "daily_max_profit": RISK_CONFIG["daily_max_profit"] * x_factor,
        "fixed_loss_amount": RISK_CONFIG.get("fixed_loss_amount", 9000.0) * x_factor,
        "sl_cooldown_minutes": RISK_CONFIG["sl_cooldown_minutes"],  # Not scaled
        "max_daily_trades": RISK_CONFIG["max_daily_trades"],  # Not scaled
        "check_interval": RISK_CONFIG["check_interval"],  # Not scaled
    }
    
    return scaled_config


def get_ltp(smart_api: SmartConnect, symbol_token: str, trading_symbol: str, exchange: str = "NFO", retries: int = 2, last_price: float = 0.0, timeout: float = 5.0) -> float:
    """Fetches LTP for a contract with retries and timeout (cross-platform, thread-safe)."""
    if not symbol_token:
        return last_price

    for attempt in range(retries):
        acquired = smart_api_lock.acquire(timeout=timeout)
        if not acquired:
            if attempt == retries - 1:
                logger.warning(f"Timeout waiting for smart_api_lock to fetch LTP for {trading_symbol} after {timeout}s")
            time.sleep(0.3)
            continue
        try:
            data = smart_api.ltpData(exchange, trading_symbol, str(symbol_token))
            if data and data.get("status") and data.get("data"):
                return float(data["data"]["ltp"])
        except Exception as e:
            if attempt == retries - 1:
                print(f"[!] Error fetching LTP for {trading_symbol}: {e}")
                logger.error(f"Error fetching LTP for {trading_symbol}: {e}")
            time.sleep(0.3)
        finally:
            smart_api_lock.release()

    return last_price


def is_order_successful(response: Any) -> bool:
    """Validates order response (handles string order ID or full dict response)."""
    if response is None:
        return False
    if isinstance(response, str) and len(response.strip()) > 0:
        return True
    if isinstance(response, dict):
        if "status" in response:
            return response.get("status") is True
        data = response.get("data")
        if data and (isinstance(data, dict) and data.get("orderid") or isinstance(data, str)):
            return True
    return False


def round_price_up_to_tick(price: float, tick_size: float = 0.05) -> float:
    """Round a protective sell trigger upward to a valid exchange price tick."""
    tick = Decimal(str(tick_size))
    value = Decimal(str(price))
    if tick <= 0:
        raise ValueError("tick_size must be positive")
    ticks = (value / tick).quantize(Decimal("1"), rounding=ROUND_CEILING)
    return float(ticks * tick)


def place_stop_loss_order(
    smart_api: SmartConnect,
    trading_symbol: str,
    symbol_token: str,
    transaction_type: str,
    quantity: int,
    trigger_price: float,
    exchange: str = "NFO",
    product_type: str = "INTRADAY"
) -> Optional[Any]:
    """
    Places a stop loss market order via SmartAPI.
    Trigger price is the price at which the order becomes active.
    """
    if not symbol_token or not trading_symbol:
        print(f"[!] Cannot place stop loss order: missing symbol or token ({trading_symbol}, {symbol_token})")
        return None

    trigger_price = round_price_up_to_tick(trigger_price)
    order_params = {
        "variety": "STOPLOSS",
        "tradingsymbol": trading_symbol,
        "symboltoken": str(symbol_token),
        "transactiontype": transaction_type,
        "exchange": exchange,
        "ordertype": "STOPLOSS_LIMIT",
        "producttype": product_type,
        "duration": "DAY",
        "price": trigger_price,
        "triggerprice": trigger_price,
        "quantity": quantity
    }
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
            print(f"[+] Stop Loss order accepted for {trading_symbol} (Trigger: {trigger_price:.2f}, Qty: {quantity}, Order ID: {order_id})")
            logger.info(f"Stop Loss order accepted for {trading_symbol} (Trigger: {trigger_price:.2f}, Qty: {quantity}, Order ID: {order_id}); broker order status still needs confirmation")
            return order_id
        else:
            if isinstance(response, dict):
                error_msg = response.get("message", "Unknown error")
                error_code = response.get("errorcode", response.get("errorCode", ""))
            else:
                error_msg = str(response) if response else "No response / order rejected"
                error_code = ""
            print(f"[!] Stop Loss order FAILED for {trading_symbol}: {error_msg} (Error Code: {error_code})")
            logger.error(f"Stop Loss order FAILED for {trading_symbol}: {error_msg} (Error Code: {error_code})")
            return None
    except Exception as e:
        print(f"[!] Error placing stop loss order for {trading_symbol}: {e}")
        logger.error(f"Error placing stop loss order for {trading_symbol}: {e}")
        return None


def cancel_order(smart_api: SmartConnect, order_id: str, variety: str = "STOPLOSS") -> bool:
    """
    Cancels an order (typically a stop loss order) via SmartAPI.
    Returns True if cancellation was successful.
    """
    if not order_id:
        return False

    try:
        order_params = {
            "variety": variety,
            "orderid": order_id,
        }
        with smart_api_lock:
            if hasattr(smart_api, "cancelOrder"):
                response = smart_api.cancelOrder(order_params)
            else:
                logger.warning(f"cancelOrder method not available for order {order_id}")
                return False

        if response and response.get("status"):
            print(f"[+] Order {order_id} cancelled successfully")
            logger.info(f"Order {order_id} cancelled successfully")
            return True
        else:
            error_msg = response.get("message", "Unknown error") if response else "No response"
            print(f"[!] Failed to cancel order {order_id}: {error_msg}")
            logger.warning(f"Failed to cancel order {order_id}: {error_msg}")
            return False
    except Exception as e:
        print(f"[!] Error cancelling order {order_id}: {e}")
        logger.error(f"Error cancelling order {order_id}: {e}")
        return False


def place_order(
    smart_api: SmartConnect,
    trading_symbol: str,
    symbol_token: str,
    transaction_type: str,
    quantity: int,
    exchange: str = "NFO",
    order_type: str = "MARKET",
    product_type: str = "INTRADAY"
) -> Optional[Any]:
    """Places a market order via SmartAPI."""
    if not symbol_token or not trading_symbol:
        print(f"[!] Cannot place order: missing symbol or token ({trading_symbol}, {symbol_token})")
        return None

    order_params = {
        "variety": "NORMAL",
        "tradingsymbol": trading_symbol,
        "symboltoken": str(symbol_token),
        "transactiontype": transaction_type,
        "exchange": exchange,
        "ordertype": order_type,
        "producttype": product_type,
        "duration": "DAY",
        "quantity": quantity
    }
    try:
        # placeOrderFullResponse returns full dict; placeOrder returns string order ID
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
            print(f"[+] {transaction_type} order placed successfully for {trading_symbol} (Qty: {quantity}, Order ID: {order_id})")
            logger.info(f"{transaction_type} order placed successfully for {trading_symbol} (Qty: {quantity}, Order ID: {order_id})")
        else:
            if isinstance(response, dict):
                error_msg = response.get("message", "Unknown error")
                error_code = response.get("errorcode", response.get("errorCode", ""))
            else:
                error_msg = str(response) if response else "No response / order rejected"
                error_code = ""
            print(f"[!] {transaction_type} order FAILED for {trading_symbol}: {error_msg} (Error Code: {error_code})")
            logger.error(f"{transaction_type} order FAILED for {trading_symbol}: {error_msg} (Error Code: {error_code})")
        return response
    except Exception as e:
        print(f"[!] Error placing {transaction_type} order for {trading_symbol}: {e}")
        logger.error(f"Error placing {transaction_type} order for {trading_symbol}: {e}")
        return None


# Broker order states that can still transition into a fill.
PENDING_ORDER_STATUSES = (
    "PENDING", "OPEN", "ORDER PLACED", "MODIFY PENDING",
    "PARTLY FILLED", "PENDING_ON_CONFIRM",
)
# States in which an order can no longer produce a fill.
TERMINAL_ORDER_STATUSES = (
    "COMPLETE", "FILLED", "EXECUTED", "REJECTED", "CANCELLED", "CANCELED",
    "TRADE CANCELLED", "ERROR",
)


def get_order_list(smart_api: SmartConnect) -> Optional[List[Dict[str, Any]]]:
    """Fetch the account order book list. Returns None when the API fails."""
    try:
        with smart_api_lock:
            response = smart_api.orderBook()
        if not response or not isinstance(response, dict) or not response.get("status"):
            return None
        data = response.get("data")
        return data if isinstance(data, list) else None
    except Exception as e:
        logger.warning(f"Could not fetch order book: {e}")
        return None


def _order_field(order: Dict[str, Any], *names: str) -> str:
    """Read the first non-empty field from an order-book row (key spellings vary)."""
    for name in names:
        value = order.get(name)
        if value not in (None, ""):
            return str(value)
    return ""


def _normalize_exchange(value: str) -> str:
    """
    Map broker exchange spellings onto the codes the strategy uses. The order
    book reports option orders as NSE_FNO/BSE_FNO while placements use NFO/BFO.
    """
    v = str(value).upper().strip()
    if not v:
        return ""
    if "FNO" in v or v == "BCD":
        return "BFO" if (v.startswith("BSE") or v == "BCD") else "NFO"
    if v == "MCXFO":
        return "MCX"
    return v


def find_pending_order(
    smart_api: SmartConnect,
    trading_symbol: str,
    transaction_type: str,
    exchange: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """
    Return a still-fillable order for this exact symbol and side, if one exists.
    Retry loops use this so a pending exit is never replaced by a second order
    for the same contract.
    """
    orders = get_order_list(smart_api)
    if not orders:
        return None

    wanted_exchange = _normalize_exchange(exchange) if exchange else ""
    candidate = None
    for order in orders:
        if _order_field(order, "tradingsymbol", "tsymbol").upper() != str(trading_symbol).upper():
            continue
        if _order_field(order, "transactiontype", "transactionType").upper() != str(transaction_type).upper():
            continue
        book_exchange = _normalize_exchange(_order_field(order, "exchange"))
        if wanted_exchange and book_exchange and book_exchange != wanted_exchange:
            continue
        if _order_field(order, "orderstatus", "status").upper() not in PENDING_ORDER_STATUSES:
            continue
        # A broker stop-loss order matches symbol and side but is not an exit:
        # adopting (then cancelling) it would strip the position's protection.
        variety = _order_field(order, "ordervariety", "variety").upper()
        if "STOPLOSS" in variety or "STOP_LOSS" in variety or "SL" in variety:
            continue
        if candidate is None or _order_field(order, "updatetime") >= _order_field(candidate, "updatetime"):
            candidate = order
    return candidate


def cancel_and_confirm_order(
    smart_api: SmartConnect,
    order_id: str,
    variety: str = "NORMAL",
    timeout: Optional[float] = None,
    poll_interval: float = 3.0,
) -> str:
    """
    Cancel an order and wait until the broker's order book reports a terminal
    state. A fire-and-forget cancellation leaves the order live, so the caller
    cannot tell whether the position is still being closed; this returns the
    confirmed status, 'STILL_PENDING' when it never settled, or 'UNKNOWN' when
    the book could not be read.
    """
    if not order_id:
        return "UNKNOWN"

    timeout = float(RISK_CONFIG.get("cancel_confirm_timeout", 30.0)) if timeout is None else float(timeout)
    cancel_order(smart_api, order_id, variety=variety)
    deadline = time.time() + max(0.0, timeout)
    saw_order = False
    book_readable = False

    while time.time() < deadline:
        orders = get_order_list(smart_api)
        if orders is None:
            time.sleep(poll_interval)
            continue
        book_readable = True
        info = next(
            (o for o in orders
             if _order_field(o, "orderid", "orderId") == str(order_id)),
            None,
        )
        if info is None:
            if saw_order:
                # The order left the book after being listed; it can no longer fill.
                return "CANCELLED"
            time.sleep(poll_interval)
            continue
        saw_order = True
        status = _order_field(info, "orderstatus", "status").upper()
        if status in TERMINAL_ORDER_STATUSES:
            logger.info(f"Order {order_id} confirmed in terminal state {status}")
            return status
        time.sleep(poll_interval)

    if saw_order:
        logger.warning(f"Order {order_id} cancellation was not confirmed within {timeout:.0f}s")
        return "STILL_PENDING"
    if book_readable:
        # The order never appeared in the book, so it is not live either; let the
        # caller reconcile the outcome against the position book.
        logger.warning(f"Order {order_id} was never listed in the order book during cancellation confirmation")
        return "NOT_LISTED"
    logger.warning(f"Order book unreadable while confirming cancellation of {order_id}")
    return "UNKNOWN"


def _closing_position_quantity(
    smart_api: SmartConnect,
    trading_symbol: str,
    transaction_type: str,
    symbol_base: str,
) -> Optional[int]:
    """
    Quantity of the opposite-signed position an exit order is working against.
    Used to recognise a fill as a *reduction* instead of requiring the position
    to flip sign.
    """
    positions = get_open_positions(smart_api, symbol_filter=symbol_base)
    if positions is None:
        return None
    closing_side = -1 if str(transaction_type).upper() == "BUY" else 1
    for pos in positions:
        if str(pos.get("tradingsymbol", "")).upper() != str(trading_symbol).upper():
            continue
        try:
            netqty = int(pos.get("netqty", 0) or 0)
        except (ValueError, TypeError):
            netqty = 0
        if closing_side * netqty > 0:
            return abs(netqty)
        return 0
    return 0


def _market_session_open(now_dt: Optional[datetime] = None) -> bool:
    """True while the exchange accepts orders that can actually fill."""
    now_dt = now_dt or datetime.now()
    open_h, open_m = map(int, str(BREAKOUT_CONFIG.get("market_open_hhmm", "09:15")).split(":"))
    close_h, close_m = map(int, str(BREAKOUT_CONFIG.get("market_close_hhmm", "15:30")).split(":"))
    minutes = now_dt.hour * 60 + now_dt.minute
    return (open_h * 60 + open_m) <= minutes <= (close_h * 60 + close_m)


def _confirm_order_fill(
    smart_api: SmartConnect,
    result: Dict[str, Any],
    *,
    trading_symbol: str,
    symbol_token: str,
    transaction_type: str,
    quantity: int,
    exchange: str,
    order_type_label: str,
    trade_id: Optional[str],
    symbol_base: str,
    enable_polling: bool,
    order_is_exit: bool,
    pre_position_qty: Optional[int],
) -> Dict[str, Any]:
    """
    Confirm whether an accepted order actually filled.

    For exits, success is measured as a *reduction* of the position being
    closed: a short bought back disappears instead of flipping sign, so the
    previous direction match never recognised a completed exit and every retry
    reported the fill as unconfirmed.
    """
    if enable_polling and result['status'] == 'SUBMITTED' and result.get('order_id'):
        logger.info(f"Order status polling enabled, starting polling for {result['order_id']}...")
        poll_interval = RISK_CONFIG.get("order_poll_interval", 15.0)
        poll_max_time = RISK_CONFIG.get("order_poll_max_time", 120.0)
        poll_result = poll_order_status(
            smart_api=smart_api,
            order_id=result['order_id'],
            symbol=trading_symbol,
            symbol_token=symbol_token,
            exchange=exchange,
            poll_interval=poll_interval,
            max_poll_time=poll_max_time,
            trade_id=trade_id,
            symbol_base=symbol_base,
        )
        result['poll_result'] = poll_result
        if poll_result['status'] == 'FILLED' and poll_result.get('filled_quantity', 0) >= quantity:
            result['filled_quantity'] = poll_result['filled_quantity']
            result['average_price'] = poll_result['average_price']
            if poll_result.get('average_price', 0) > 0:
                result['execution_price'] = poll_result['average_price']
            result['success'] = True
            result['status'] = 'FILLED'
        elif poll_result['status'] in ('REJECTED', 'CANCELLED', 'CANCELED', 'ERROR'):
            result['success'] = False
            result['status'] = poll_result['status']
            result['error'] = poll_result.get('error_message', 'Broker rejected or cancelled the order')
        elif poll_result['status'] == 'FILLED':
            # Partial fill reported by the order book; reconcile the remainder
            # against the position book below.
            result['filled_quantity'] = int(poll_result.get('filled_quantity', 0) or 0)
            result['average_price'] = float(poll_result.get('average_price', 0.0) or 0.0)
            result['success'] = False
            result['status'] = 'PARTIALLY_FILLED'
            result['error'] = f"Partial fill: {result['filled_quantity']} of {quantity} shares"
        else:
            # orderBook never confirmed a terminal state. Cancel and wait for the
            # broker to report that the order is dead, otherwise a live order
            # would keep working the position while the caller assumes failure.
            cancel_status = "NOT_ATTEMPTED"
            if poll_result['status'] in ('POLL_TIMEOUT', 'POLL_ERROR'):
                logger.warning(
                    f"Order {result['order_id']} remains {poll_result['status']}; "
                    "cancelling and waiting for broker confirmation"
                )
                cancel_status = cancel_and_confirm_order(
                    smart_api, result['order_id'], variety="NORMAL",
                    timeout=RISK_CONFIG.get("cancel_confirm_timeout", 30.0),
                )
                if cancel_status == 'STILL_PENDING':
                    result['success'] = False
                    result['status'] = 'ORDER_STILL_PENDING'
                    result['error'] = (
                        f"Order {result['order_id']} is still pending at the broker "
                        f"({transaction_type} {trading_symbol}); no duplicate order was placed"
                    )
                    logger.error(f"{result['error']} — leaving it live for the next reconciliation")
                    return result

            positions = get_open_positions(smart_api, symbol_filter=symbol_base)
            if positions is None:
                result['success'] = False
                result['status'] = 'UNCONFIRMED'
                result['error'] = (
                    f"Broker position book unreadable after order {result['order_id']} "
                    f"({cancel_status}); fill state unknown"
                )
                logger.error(f"{result['error']} for {trading_symbol}")
                return result

            if order_is_exit:
                remaining_qty = _closing_position_quantity(
                    smart_api, trading_symbol, transaction_type, symbol_base
                )
                if remaining_qty is None:
                    result['success'] = False
                    result['status'] = 'UNCONFIRMED'
                    result['error'] = "Broker position book unreadable while reconciling exit"
                    logger.error(f"{result['error']} for {trading_symbol}")
                    return result

                starting_qty = quantity if pre_position_qty is None else max(pre_position_qty, quantity)
                closed_qty = max(0, starting_qty - remaining_qty)
                result['filled_quantity'] = closed_qty
                if remaining_qty == 0:
                    result['success'] = True
                    result['status'] = 'FILLED_POSITION_CONFIRMED'
                    result['error'] = None
                elif closed_qty > 0:
                    result['success'] = False
                    result['status'] = 'PARTIALLY_FILLED'
                    result['error'] = (
                        f"Exit partially filled: {closed_qty} of {starting_qty} closed, "
                        f"{remaining_qty} still open"
                    )
                else:
                    result['success'] = False
                    result['status'] = 'NOT_FILLED'
                    result['error'] = (
                        f"Exit did not fill (order {cancel_status}); {remaining_qty} of "
                        f"{starting_qty} still open at broker"
                    )
                record_order_event(
                    order_type="STATUS_POLL", symbol=trading_symbol,
                    symbol_token=symbol_token, transaction_type=transaction_type,
                    quantity=result['filled_quantity'], status=result['status'],
                    order_id=result['order_id'], execution_price=result['execution_price'],
                    trade_id=trade_id, symbol_base=symbol_base,
                )
                logger.warning(
                    f"Exit order {result['order_id']} reconciled as {result['status']} "
                    f"({closed_qty}/{starting_qty} closed, {remaining_qty} open) after "
                    f"orderBook status was {poll_result['status']}"
                )
                return result

            direction = 1 if str(transaction_type).upper() == 'BUY' else -1
            position = next((pos for pos in positions
                             if pos.get('tradingsymbol') == trading_symbol
                             and direction * int(pos.get('netqty', 0) or 0) > 0), None)
            if position:
                actual_quantity = abs(int(position.get('netqty', 0) or 0))
                result['filled_quantity'] = actual_quantity
                result['average_price'] = float(position.get('averageprice', 0.0) or 0.0)
                if result['average_price'] > 0:
                    result['execution_price'] = result['average_price']
                result['success'] = actual_quantity >= quantity
                result['status'] = 'FILLED_POSITION_CONFIRMED' if result['success'] else 'PARTIALLY_FILLED'
                result['error'] = None if result['success'] else f"Partial broker position: {actual_quantity} of {quantity} shares"
                record_order_event(
                    order_type="STATUS_POLL", symbol=trading_symbol,
                    symbol_token=symbol_token, transaction_type=transaction_type,
                    quantity=result['filled_quantity'], status=result['status'],
                    order_id=result['order_id'], execution_price=result['execution_price'],
                    trade_id=trade_id, symbol_base=symbol_base,
                )
                logger.warning(f"Order {result['order_id']} broker position reconciled as {result['status']} ({actual_quantity}/{quantity}) after orderBook status was {poll_result['status']}")
            else:
                result['success'] = False
                result['status'] = 'UNCONFIRMED'
                result['error'] = f"Broker fill not confirmed (order status: {poll_result['status']}, cancel: {cancel_status}); P&L monitoring was not started"
                logger.error(f"{result['error']} for {trading_symbol} order {result['order_id']}")
    elif result['status'] == 'SUBMITTED':
        if result.get('adopted_order'):
            # Nothing was placed by this call. Keep the broker's live order and
            # report it as pending so the next pass adopts it rather than
            # stacking another exit order.
            result['success'] = False
            result['error'] = (
                f"Existing exit order {result['order_id']} for {transaction_type} "
                f"{trading_symbol} is still pending at the broker; no duplicate placed"
            )
            logger.warning(result['error'])
            return result
        if result.get('order_id'):
            logger.warning(
                f"Order {result['order_id']} has no fill polling enabled; requesting cancellation "
                "so a later fill cannot become an untracked position"
            )
            cancel_and_confirm_order(smart_api, result['order_id'], variety="NORMAL")
        result['success'] = False
        result['status'] = 'UNCONFIRMED'
        result['error'] = "Order was accepted but fill confirmation is disabled; P&L monitoring was not started"
        logger.error(f"{result['error']} for {trading_symbol}")

    return result


def place_order_with_validation(
    smart_api: SmartConnect,
    trading_symbol: str,
    symbol_token: str,
    transaction_type: str,
    quantity: int,
    exchange: str = "NFO",
    order_type: str = "MARKET",
    product_type: str = "INTRADAY",
    expected_price_range: Optional[Tuple[float, float]] = None,
    timeout: float = 30.0,
    order_type_label: str = "ENTRY_BUY",  # For logging: ENTRY_BUY, ENTRY_SELL, EXIT_BUY, EXIT_SELL
    trade_id: Optional[str] = None,
    symbol_base: str = "NIFTY",
    retry_count: int = 0,
    enable_retry: bool = False,  # Enable retry with backoff for rejected orders
    enable_polling: bool = False,  # Enable order status polling
) -> Dict[str, Any]:
    """
    Enhanced order placement with validation, error handling, and comprehensive order lifecycle tracking.
    Returns detailed result with status, order_id, execution_price, etc.
    Records all order status transitions to orders.csv.
    """
    result = {
        'success': False,
        'order_id': None,
        'execution_price': None,
        'error': None,
        'status': 'PENDING'
    }

    start_time = time.time()

    # Log PENDING status
    record_order_event(
        order_type=order_type_label,
        symbol=trading_symbol,
        symbol_token=symbol_token,
        transaction_type=transaction_type,
        quantity=quantity,
        status="PENDING",
        trade_id=trade_id,
        retry_count=retry_count,
        symbol_base=symbol_base
    )

    if not symbol_token or not trading_symbol:
        result['error'] = "Missing symbol or token"
        result['status'] = 'VALIDATION_ERROR'
        duration_ms = int((time.time() - start_time) * 1000)
        record_order_event(
            order_type=order_type_label,
            symbol=trading_symbol,
            symbol_token=symbol_token,
            transaction_type=transaction_type,
            quantity=quantity,
            status="VALIDATION_ERROR",
            error_message=result['error'],
            trade_id=trade_id,
            retry_count=retry_count,
            duration_ms=duration_ms,
            symbol_base=symbol_base
        )
        return result

    # Pre-order validation
    try:
        current_ltp = get_ltp(smart_api, symbol_token, trading_symbol, exchange)
        if expected_price_range:
            min_price, max_price = expected_price_range
            if not (min_price <= current_ltp <= max_price):
                result['error'] = f"Price {current_ltp} outside expected range {min_price}-{max_price}"
                result['status'] = 'VALIDATION_ERROR'
                duration_ms = int((time.time() - start_time) * 1000)
                record_order_event(
                    order_type=order_type_label,
                    symbol=trading_symbol,
                    symbol_token=symbol_token,
                    transaction_type=transaction_type,
                    quantity=quantity,
                    status="VALIDATION_ERROR",
                    error_message=result['error'],
                    trade_id=trade_id,
                    retry_count=retry_count,
                    duration_ms=duration_ms,
                    symbol_base=symbol_base
                )
                return result
    except Exception as e:
        logger.warning(f"Could not validate price for {trading_symbol}: {e}")

    # Exits are driven by retry loops, so both the size being closed and any
    # order already live at the broker must be known before placing.
    order_is_exit = str(order_type_label).upper().startswith("EXIT")
    pre_position_qty = (
        _closing_position_quantity(smart_api, trading_symbol, transaction_type, symbol_base)
        if order_is_exit else None
    )

    if order_is_exit:
        pending = find_pending_order(smart_api, trading_symbol, transaction_type, exchange)
        if pending:
            pending_id = _order_field(pending, "orderid", "orderId")
            result['order_id'] = pending_id
            result['status'] = 'SUBMITTED'
            result['adopted_order'] = True
            result['error'] = None
            logger.warning(
                f"Exit order {pending_id} ({transaction_type} {trading_symbol} "
                f"{_order_field(pending, 'quantity')}) is already pending at the broker; "
                "adopting it instead of placing a duplicate"
            )
            record_order_event(
                order_type=order_type_label,
                symbol=trading_symbol,
                symbol_token=symbol_token,
                transaction_type=transaction_type,
                quantity=quantity,
                status="ADOPTED_PENDING",
                order_id=pending_id,
                trade_id=trade_id,
                retry_count=retry_count,
                symbol_base=symbol_base,
            )
            return _confirm_order_fill(
                smart_api,
                result,
                trading_symbol=trading_symbol,
                symbol_token=symbol_token,
                transaction_type=transaction_type,
                quantity=quantity,
                exchange=exchange,
                order_type_label=order_type_label,
                trade_id=trade_id,
                symbol_base=symbol_base,
                enable_polling=enable_polling,
                order_is_exit=True,
                pre_position_qty=pre_position_qty,
            )

    # Place order with timeout
    try:
        # Log SUBMITTED status
        record_order_event(
            order_type=order_type_label,
            symbol=trading_symbol,
            symbol_token=symbol_token,
            transaction_type=transaction_type,
            quantity=quantity,
            status="SUBMITTED",
            trade_id=trade_id,
            retry_count=retry_count,
            symbol_base=symbol_base
        )

        order_params = {
            "variety": "NORMAL",
            "tradingsymbol": trading_symbol,
            "symboltoken": str(symbol_token),
            "transactiontype": transaction_type,
            "exchange": exchange,
            "ordertype": order_type,
            "producttype": product_type,
            "duration": "DAY",
            "quantity": quantity
        }

        with smart_api_lock:
            if hasattr(smart_api, "placeOrderFullResponse"):
                response = smart_api.placeOrderFullResponse(order_params)
            else:
                response = smart_api.placeOrder(order_params)

        if is_order_successful(response):
            result['success'] = False
            result['status'] = 'SUBMITTED'

            if isinstance(response, str):
                result['order_id'] = response
            elif isinstance(response, dict):
                result['order_id'] = response.get("data", {}).get("orderid", "SUCCESS")

            # Get execution price
            try:
                execution_price = get_ltp(smart_api, symbol_token, trading_symbol, exchange)
                result['execution_price'] = execution_price
            except Exception as e:
                logger.warning(f"Could not get execution price for {trading_symbol}: {e}")
                result['execution_price'] = current_ltp if 'current_ltp' in locals() else None

            duration_ms = int((time.time() - start_time) * 1000)
            record_order_event(
                order_type=order_type_label,
                symbol=trading_symbol,
                symbol_token=symbol_token,
                transaction_type=transaction_type,
                quantity=quantity,
                status="SUBMITTED",
                order_id=result['order_id'],
                execution_price=result['execution_price'],
                trade_id=trade_id,
                retry_count=retry_count,
                duration_ms=duration_ms,
                symbol_base=symbol_base
            )
        else:
            error_msg = response.get("message", "Unknown error") if isinstance(response, dict) else str(response)
            error_code = response.get("errorcode", response.get("errorCode", "")) if isinstance(response, dict) else ""
            result['error'] = error_msg
            result['status'] = 'REJECTED'

            duration_ms = int((time.time() - start_time) * 1000)
            record_order_event(
                order_type=order_type_label,
                symbol=trading_symbol,
                symbol_token=symbol_token,
                transaction_type=transaction_type,
                quantity=quantity,
                status="REJECTED",
                error_message=error_msg,
                error_code=error_code,
                trade_id=trade_id,
                retry_count=retry_count,
                duration_ms=duration_ms,
                symbol_base=symbol_base
            )

    except Exception as e:
        result['error'] = str(e)
        result['status'] = 'EXCEPTION'

        duration_ms = int((time.time() - start_time) * 1000)
        record_order_event(
            order_type=order_type_label,
            symbol=trading_symbol,
            symbol_token=symbol_token,
            transaction_type=transaction_type,
            quantity=quantity,
            status="EXCEPTION",
            error_message=str(e),
            trade_id=trade_id,
            retry_count=retry_count,
            duration_ms=duration_ms,
            symbol_base=symbol_base
        )

    # Retry logic for rejected orders (if enabled)
    if enable_retry and result['status'] == 'REJECTED':
        logger.info(f"Retry logic enabled for rejected order, attempting retry...")
        max_retry_attempts = RISK_CONFIG.get("order_retry_max_attempts", 3)
        retry_delay = RISK_CONFIG.get("order_retry_initial_delay", 180.0)
        retry_result = retry_order_with_backoff(
            smart_api=smart_api,
            trading_symbol=trading_symbol,
            symbol_token=symbol_token,
            transaction_type=transaction_type,
            quantity=quantity,
            exchange=exchange,
            order_type=order_type,
            product_type=product_type,
            max_retries=max_retry_attempts,
            initial_delay=retry_delay,
            order_type_label=order_type_label,
            trade_id=trade_id,
            symbol_base=symbol_base,
            enable_polling=enable_polling,
        )
        # Preserve the last attempt's actual state, including SUBMITTED,
        # UNCONFIRMED, or PARTIALLY_FILLED; hiding it as the first rejection
        # can lead the caller to ignore a live broker order.
        result = retry_result

    # An accepted placeOrder response only means the broker received the
    # request. Do not report a fill (or start P&L monitoring) until orderBook
    # confirms it, or the positions endpoint confirms the resulting position.
    return _confirm_order_fill(
        smart_api,
        result,
        trading_symbol=trading_symbol,
        symbol_token=symbol_token,
        transaction_type=transaction_type,
        quantity=quantity,
        exchange=exchange,
        order_type_label=order_type_label,
        trade_id=trade_id,
        symbol_base=symbol_base,
        enable_polling=enable_polling,
        order_is_exit=order_is_exit,
        pre_position_qty=pre_position_qty,
    )


def get_open_positions(smart_api: SmartConnect, symbol_filter: Optional[str] = None) -> Optional[List[Dict[str, Any]]]:
    """Fetches active open positions. Returns list of open positions, or None on API failure."""
    try:
        with smart_api_lock:
            response = smart_api.position()
        if not response or not isinstance(response, dict) or not response.get("status"):
            return None
        positions_data = response.get("data")
        if positions_data is None or not isinstance(positions_data, list):
            return None

        open_positions = []
        for pos in positions_data:
            try:
                net_qty = int(pos.get("netqty", 0))
            except (ValueError, TypeError):
                net_qty = 0

            if net_qty != 0:
                if symbol_filter:
                    tradingsymbol = pos.get("tradingsymbol", "")
                    symbolname = pos.get("symbolname", "")
                    if symbol_filter.upper() in tradingsymbol.upper() or symbol_filter.upper() in symbolname.upper():
                        open_positions.append(pos)
                else:
                    open_positions.append(pos)
        return open_positions
    except Exception as e:
        print(f"[!] Error checking open positions: {e}")
        logger.error(f"Error checking open positions: {e}")
        return None


def verify_position_state(smart_api, expected_positions, max_retries=3):
    """
    Verifies that actual positions match expected positions with retries.
    Returns (is_valid, actual_positions, mismatch_details)
    """
    for attempt in range(max_retries):
        try:
            actual_positions = get_open_positions(smart_api)
            if actual_positions is None:
                if attempt < max_retries - 1:
                    time.sleep(1)
                    continue
                return False, None, "API error fetching positions"
            
            # Build expected position map
            expected_map = {pos['symbol']: pos for pos in expected_positions}
            actual_map = {pos['tradingsymbol']: pos for pos in actual_positions}
            
            mismatches = []
            for symbol, expected_pos in expected_map.items():
                if symbol not in actual_map:
                    mismatches.append(f"Missing expected position: {symbol}")
                else:
                    actual_pos = actual_map[symbol]
                    expected_qty = expected_pos.get('quantity', 0)
                    actual_qty = int(actual_pos.get('netqty', 0))
                    if actual_qty != expected_qty:
                        mismatches.append(f"Quantity mismatch for {symbol}: expected {expected_qty}, got {actual_qty}")
            
            # Check for unexpected positions
            for symbol in actual_map:
                if symbol not in expected_map:
                    mismatches.append(f"Unexpected position found: {symbol}")
            
            return len(mismatches) == 0, actual_positions, mismatches
            
        except Exception as e:
            if attempt < max_retries - 1:
                time.sleep(1)
                continue
            return False, None, f"Error during verification: {str(e)}"
    
    return False, None, "Max retries exceeded"


class PositionTracker:
    """Thread-safe position tracking system."""
    def __init__(self):
        self._lock = threading.RLock()
        self._positions = {}  # symbol -> position_data
        self._last_update = None
        
    def update_position(self, symbol, quantity, price=None, metadata=None):
        """Update position tracking with thread safety."""
        with self._lock:
            self._positions[symbol] = {
                'quantity': quantity,
                'price': price,
                'metadata': metadata or {},
                'last_updated': datetime.now()
            }
            self._last_update = datetime.now()
    
    def get_position(self, symbol):
        """Get position data safely."""
        with self._lock:
            return self._positions.get(symbol)
    
    def get_all_positions(self):
        """Get all positions safely."""
        with self._lock:
            return self._positions.copy()
    
    def reconcile_with_broker(self, smart_api, symbol_filter=None):
        """Reconcile internal tracking with broker positions."""
        with self._lock:
            broker_positions = get_open_positions(smart_api, symbol_filter)
            if broker_positions is None:
                return False, "API error"
            
            # Update internal state based on broker data
            for pos in broker_positions:
                symbol = pos['tradingsymbol']
                self._positions[symbol] = {
                    'quantity': int(pos['netqty']),
                    'price': float(pos.get('buyavgprice', 0)),
                    'metadata': {'broker_data': pos},
                    'last_updated': datetime.now()
                }
            
            return True, "Reconciled successfully"


def get_date_trades_file(date_obj=None, symbol_base="NIFTY", mode: Optional[str] = None):
    """Returns path to daily trades file: data/<DD-MM-YYYY>/trades_<SYMBOL>.csv or mode-specific path"""
    if date_obj is None:
        date_obj = datetime.now()
    if isinstance(date_obj, str):
        for fmt in ("%Y-%m-%d", "%d-%m-%Y"):
            try:
                date_obj = datetime.strptime(date_obj, fmt)
                break
            except ValueError:
                continue
        else:
            date_obj = datetime.now()

    # If mode is specified, use the dual tracking format (YYYY-MM-DD folder with mode suffix)
    if mode and mode.upper() in ("REVERSE", "NO-REVERSE"):
        from live.daybreakout.tracking_dual import get_date_trades_file as get_dual_trades_file
        return get_dual_trades_file(date_obj, symbol_base=symbol_base, mode=mode.upper())

    # Otherwise use legacy format (DD-MM-YYYY folder)
    date_folder = DATA_DIR / date_obj.strftime("%d-%m-%Y")
    date_folder.mkdir(parents=True, exist_ok=True)
    suffix = "SENSEX" if "SENSEX" in str(symbol_base).upper() else "NIFTY"
    return date_folder / f"trades_{suffix}.csv"


def get_date_triggered_trades_file(date_obj=None, symbol_base="NIFTY", mode: Optional[str] = None):
    """Returns path to daily triggered trades file: data/<DD-MM-YYYY>/triggered_trades_<SYMBOL>.csv or mode-specific path"""
    if date_obj is None:
        date_obj = datetime.now()
    if isinstance(date_obj, str):
        for fmt in ("%Y-%m-%d", "%d-%m-%Y"):
            try:
                date_obj = datetime.strptime(date_obj, fmt)
                break
            except ValueError:
                continue
        else:
            date_obj = datetime.now()

    # If mode is specified, use the dual tracking format (YYYY-MM-DD folder with mode suffix)
    if mode and mode.upper() in ("REVERSE", "NO-REVERSE"):
        from live.daybreakout.tracking_dual import get_date_triggered_trades_file as get_dual_triggered_file
        return get_dual_triggered_file(date_obj, symbol_base=symbol_base, mode=mode.upper())

    # Otherwise use legacy format (DD-MM-YYYY folder)
    date_folder = DATA_DIR / date_obj.strftime("%d-%m-%Y")
    date_folder.mkdir(parents=True, exist_ok=True)
    suffix = "SENSEX" if "SENSEX" in str(symbol_base).upper() else "NIFTY"
    return date_folder / f"triggered_trades_{suffix}.csv"


def record_triggered_trade(
    symbol_base: str = "NIFTY",
    structure: str = "SPREAD",
    lots: int = 1,
    quantity: int = 0,
    trade_reason: str = "",
    buy_symbol: str = "",
    sell_symbol: str = "",
    mode: str = "LIVE",
):
    """Logs triggered trade details immediately upon trade execution."""
    try:
        now = datetime.now()
        date_str = now.strftime("%Y-%m-%d")
        time_str = now.strftime("%H:%M:%S")

        # Use mode-specific file if mode is REVERSE or NO-REVERSE
        if mode and mode.upper() in ("REVERSE", "NO-REVERSE"):
            daily_file = get_date_triggered_trades_file(now, symbol_base=symbol_base, mode=mode.upper())
        else:
            daily_file = get_date_triggered_trades_file(now, symbol_base=symbol_base)

        headers = ["date", "time", "symbol", "structure", "lots", "quantity", "trade_reason", "buy_symbol", "sell_symbol", "mode"]
        row = [date_str, time_str, symbol_base, str(structure).upper(), str(lots), str(quantity), trade_reason, buy_symbol, sell_symbol, mode]

        for target_file in (daily_file, TRIGGERED_TRADES_FILE):
            file_exists = target_file.exists()
            with open(target_file, "a", newline="", encoding="utf-8") as f:
                writer = csv.writer(f)
                if not file_exists:
                    writer.writerow(headers)
                writer.writerow(row)

        logger.info(f"Triggered trade logged to {daily_file} - Symbol: {symbol_base}, Reason: {trade_reason}, Mode: {mode}")
    except Exception as e:
        print(f"[!] Failed to record triggered trade: {e}")
        logger.error(f"Failed to record triggered trade: {e}")


def get_daily_trade_stats(for_date=None, symbol_base="NIFTY", mode: Optional[str] = None) -> Tuple[int, float]:
    """Returns (daily_triggered_trade_count, daily_realized_pnl_loss_only). Supports mode-scoped tracking."""
    resolved_mode = _resolve_mode(mode)
    if resolved_mode:
        try:
            from live.daybreakout.tracking_dual import get_daily_trade_stats_dual
            return get_daily_trade_stats_dual(symbol_base=symbol_base, for_date=for_date, mode=resolved_mode)
        except Exception as e:
            logger.error(f"Error reading daily trade stats dual for mode {resolved_mode}: {e}")

    now_dt = datetime.now()
    for_date_str = for_date if isinstance(for_date, str) else now_dt.strftime("%Y-%m-%d")

    # 1. Count triggered trades
    triggered_daily_file = get_date_triggered_trades_file(now_dt, symbol_base=symbol_base, mode=mode)
    triggered_files = [triggered_daily_file] if triggered_daily_file.exists() else [TRIGGERED_TRADES_FILE]

    triggered_count = 0
    for t_file in triggered_files:
        if t_file.exists():
            try:
                with open(t_file, newline="", encoding="utf-8") as f:
                    reader = csv.reader(f)
                    rows = [r for r in reader if any(c.strip() for c in r)]
                if rows:
                    header = [c.lower().strip() for c in rows[0]] if (rows and rows[0][0].lower() == "date") else None
                    start_idx = 1 if header else 0
                    date_idx = header.index("date") if (header and "date" in header) else 0
                    sym_idx = header.index("symbol") if (header and "symbol" in header) else 2
                    for row in rows[start_idx:]:
                        if len(row) > date_idx and row[date_idx].strip() == for_date_str:
                            if sym_idx < len(row) and symbol_base:
                                if row[sym_idx].strip().upper() != str(symbol_base).upper():
                                    continue
                            # Filter by mode if specified
                            if mode and header and "mode" in header:
                                mode_idx = header.index("mode")
                                if mode_idx < len(row) and row[mode_idx].strip().upper() != mode.strip().upper():
                                    continue
                            triggered_count += 1
            except Exception as e:
                logger.error(f"Error reading triggered trade stats: {e}")

    # 2. Count completed trades & daily realized PnL (LOSS ONLY for max loss check)
    daily_file = get_date_trades_file(now_dt, symbol_base=symbol_base, mode=mode)
    target_file = daily_file if daily_file.exists() else TRADES_FILE

    completed_count = 0
    total_pnl = 0.0
    if target_file.exists():
        try:
            with open(target_file, newline="", encoding="utf-8") as f:
                reader = csv.reader(f)
                rows = [r for r in reader if any(c.strip() for c in r)]
            if rows:
                header = [c.lower().strip() for c in rows[0]] if (rows and rows[0][0].lower() == "date") else None
                start_idx = 1 if header else 0
                date_idx = header.index("date") if (header and "date" in header) else 0
                sym_idx = header.index("symbol") if (header and "symbol" in header) else (3 if (header and "start_time" in header) else 2)
                pnl_idx = header.index("pnl") if (header and "pnl" in header) else -1

                for row in rows[start_idx:]:
                    if len(row) > date_idx and row[date_idx].strip() == for_date_str:
                        if sym_idx < len(row) and symbol_base:
                            if row[sym_idx].strip().upper() != str(symbol_base).upper():
                                continue
                        # Filter by mode if specified
                        if mode and header and "mode" in header:
                            mode_idx = header.index("mode")
                            if mode_idx < len(row) and row[mode_idx].strip().upper() != mode.strip().upper():
                                continue
                        completed_count += 1
                        try:
                            pnl = float(row[pnl_idx].strip())
                            # Only add negative P&L (losses) for daily max loss check
                            # Profits are ignored when calculating daily loss
                            if pnl < 0:
                                total_pnl += pnl
                        except (ValueError, IndexError):
                            pass
        except Exception as e:
            print(f"[!] Error reading daily trade stats: {e}")
            logger.error(f"Error reading daily trade stats: {e}")

    # Total triggered trades is max of triggered_count and completed_count (for backward compatibility)
    final_count = max(triggered_count, completed_count)
    return final_count, total_pnl


def get_daily_profit_stats(for_date=None, symbol_base="NIFTY", mode: Optional[str] = None) -> float:
    """Returns daily net P&L (profit + loss) for max profit check. Supports mode-scoped tracking."""
    resolved_mode = _resolve_mode(mode)
    if resolved_mode:
        try:
            from live.daybreakout.tracking_dual import get_daily_profit_stats_dual
            return get_daily_profit_stats_dual(symbol_base=symbol_base, for_date=for_date, mode=resolved_mode)
        except Exception as e:
            logger.error(f"Error reading daily profit stats dual for mode {resolved_mode}: {e}")

    now_dt = datetime.now()
    for_date_str = for_date if isinstance(for_date, str) else now_dt.strftime("%Y-%m-%d")

    daily_file = get_date_trades_file(now_dt, symbol_base=symbol_base, mode=mode)
    target_file = daily_file if daily_file.exists() else TRADES_FILE

    total_pnl = 0.0
    if target_file.exists():
        try:
            with open(target_file, newline="", encoding="utf-8") as f:
                reader = csv.reader(f)
                rows = [r for r in reader if any(c.strip() for c in r)]
            if rows:
                header = [c.lower().strip() for c in rows[0]] if (rows and rows[0][0].lower() == "date") else None
                start_idx = 1 if header else 0
                date_idx = header.index("date") if (header and "date" in header) else 0
                sym_idx = header.index("symbol") if (header and "symbol" in header) else (3 if (header and "start_time" in header) else 2)
                pnl_idx = header.index("pnl") if (header and "pnl" in header) else -1

                for row in rows[start_idx:]:
                    if len(row) > date_idx and row[date_idx].strip() == for_date_str:
                        if sym_idx < len(row) and symbol_base:
                            if row[sym_idx].strip().upper() != str(symbol_base).upper():
                                continue
                        # Filter by mode if specified
                        if mode and header and "mode" in header:
                            mode_idx = header.index("mode")
                            if mode_idx < len(row) and row[mode_idx].strip().upper() != mode.strip().upper():
                                continue
                        try:
                            pnl = float(row[pnl_idx].strip())
                            # Add both profits and losses for net P&L calculation
                            total_pnl += pnl
                        except (ValueError, IndexError):
                            pass
        except Exception as e:
            logger.error(f"Error reading daily profit stats: {e}")

    return total_pnl


def record_trade(
    buy_item: Dict[str, Any],
    sell_item: Optional[Dict[str, Any]],
    total_pnl: float,
    max_pnl: float,
    status: str,
    symbol_base: str = "NIFTY",
    expiry: str = "",
    start_time: Optional[str] = None,
    end_time: Optional[str] = None,
    trade_reason: str = "",
    structure: str = "SPREAD",
    mode: Optional[str] = None,
):
    """Logs closed trade details to daily CSV, global trades.csv, and mode-specific CSV if mode is given."""
    try:
        now = datetime.now()
        date_str = now.strftime("%Y-%m-%d")
        time_str = now.strftime("%H:%M:%S")

        trade_start = start_time or buy_item.get("start_time") or time_str
        trade_end = end_time or buy_item.get("end_time") or time_str

        # Use mode-specific file if mode is REVERSE or NO-REVERSE
        daily_file = get_date_trades_file(now, symbol_base=symbol_base, mode=mode)
        target_files = [daily_file, TRADES_FILE]

        buy_str = f"{buy_item.get('strike', '')} {buy_item.get('option_type', '')}".strip()
        buy_entry = buy_item.get("entry_price", 0.0)
        buy_exit = buy_item.get("exit_price", 0.0)

        if sell_item:
            sell_str = f"{sell_item.get('strike', '')} {sell_item.get('option_type', '')}".strip()
            sell_entry = sell_item.get("entry_price", 0.0)
            sell_exit = sell_item.get("exit_price", 0.0)
        else:
            sell_str = "N/A"
            sell_entry = 0.0
            sell_exit = 0.0

        qty = buy_item.get("quantity", 0)
        lots = buy_item.get("lots", 0)

        headers = [
            "date", "start_time", "end_time", "symbol", "expiry", "structure", "lots", "quantity",
            "buy_leg", "buy_entry", "buy_exit",
            "sell_leg", "sell_entry", "sell_exit",
            "trade_reason", "exit_reason", "pnl", "mode"
        ]

        row = [
            date_str, trade_start, trade_end, symbol_base, str(expiry), str(structure).upper(), str(lots), str(qty),
            buy_str, f"{buy_entry:.2f}", f"{buy_exit:.2f}",
            sell_str, f"{sell_entry:.2f}", f"{sell_exit:.2f}",
            trade_reason, status, f"{total_pnl:.2f}", mode or ""
        ]

        for target_file in target_files:
            file_exists = target_file.exists()
            with open(target_file, "a", newline="", encoding="utf-8") as f:
                writer = csv.writer(f)
                if not file_exists:
                    writer.writerow(headers)
                writer.writerow(row)

        print(f"[+] Trade logged to {daily_file} (Start: {trade_start}, End: {trade_end}, P&L: Rs.{total_pnl:.2f}, Exit Reason: {status})")
        logger.info(f"Trade logged - Symbol: {symbol_base}, Start: {trade_start}, End: {trade_end}, P&L: Rs.{total_pnl:.2f}, Exit Reason: {status}")
    except Exception as e:
        print(f"[!] Failed to record trade: {e}")
        logger.error(f"Failed to record trade: {e}")


def record_order_event(
    order_type: str,  # "ENTRY_BUY", "ENTRY_SELL", "EXIT_BUY", "EXIT_SELL"
    symbol: str,
    symbol_token: str,
    transaction_type: str,  # "BUY" or "SELL"
    quantity: int,
    status: str,  # "PENDING", "SUBMITTED", "FILLED", "REJECTED", "PARTIALLY_FILLED", "CANCELLED", "EXCEPTION"
    order_id: Optional[str] = None,
    execution_price: Optional[float] = None,
    error_message: Optional[str] = None,
    error_code: Optional[str] = None,
    trade_id: Optional[str] = None,
    retry_count: int = 0,
    duration_ms: Optional[int] = None,
    symbol_base: str = "NIFTY",
) -> None:
    """
    Logs detailed order lifecycle events to orders.csv for complete audit trail.
    Tracks every status transition: PENDING → SUBMITTED → FILLED/REJECTED
    """
    try:
        now = datetime.now()
        date_str = now.strftime("%Y-%m-%d")
        time_str = now.strftime("%H:%M:%S.%f")[:-3]  # Milliseconds

        daily_file = get_date_orders_file(now, symbol_base=symbol_base)

        headers = [
            "date", "time", "order_type", "symbol", "symbol_token", "transaction_type",
            "quantity", "status", "order_id", "execution_price", "error_message",
            "error_code", "trade_id", "retry_count", "duration_ms", "symbol_base"
        ]

        row = [
            date_str, time_str, order_type, symbol, symbol_token, transaction_type,
            str(quantity), status, order_id or "",
            f"{execution_price:.2f}" if execution_price else "",
            error_message or "", error_code or "",
            trade_id or "", str(retry_count),
            str(duration_ms) if duration_ms else "", symbol_base
        ]

        # Write to both daily file and global file
        for target_file in (daily_file, ORDERS_FILE):
            file_exists = target_file.exists()
            with open(target_file, "a", newline="", encoding="utf-8") as f:
                writer = csv.writer(f)
                if not file_exists:
                    writer.writerow(headers)
                writer.writerow(row)

        # Log to console and logger
        status_emoji = {
            "PENDING": "⏳",
            "SUBMITTED": "📤",
            "FILLED": "✅",
            "REJECTED": "❌",
            "PARTIALLY_FILLED": "⚠️",
            "CANCELLED": "🚫",
            "EXCEPTION": "💥",
            "VALIDATION_ERROR": "🔴"
        }.get(status, "📋")

        emoji = status_emoji or "📋"
        price_str = f"@ {execution_price:.2f}" if execution_price else ""

        if status in ("FILLED", "PARTIALLY_FILLED"):
            msg = f"[{emoji}] {order_type} {transaction_type} {symbol} {quantity} qty {price_str} - Order ID: {order_id}"
            logger.info(msg)
        elif status in ("REJECTED", "EXCEPTION", "VALIDATION_ERROR"):
            msg = f"[{emoji}] {order_type} {transaction_type} {symbol} FAILED - {error_message} (Code: {error_code})"
            logger.error(msg)
        else:
            msg = f"[{emoji}] {order_type} {transaction_type} {symbol} {quantity} qty - Status: {status}"
            logger.info(msg)

    except Exception as e:
        print(f"[!] Failed to record order event: {e}")
        logger.error(f"Failed to record order event: {e}")


def get_date_orders_file(date_obj=None, symbol_base="NIFTY"):
    """Returns path to daily orders file: data/<DD-MM-YYYY>/orders_<SYMBOL>.csv"""
    if date_obj is None:
        date_obj = datetime.now()
    if isinstance(date_obj, str):
        for fmt in ("%Y-%m-%d", "%d-%m-%Y"):
            try:
                date_obj = datetime.strptime(date_obj, fmt)
                break
            except ValueError:
                continue
        else:
            date_obj = datetime.now()
    date_folder = DATA_DIR / date_obj.strftime("%d-%m-%Y")
    date_folder.mkdir(parents=True, exist_ok=True)
    suffix = "SENSEX" if "SENSEX" in str(symbol_base).upper() else "NIFTY"
    return date_folder / f"orders_{suffix}.csv"


def poll_order_status(
    smart_api: SmartConnect,
    order_id: str,
    symbol: str,
    symbol_token: str,
    exchange: str = "NFO",
    poll_interval: float = 15.0,
    max_poll_time: float = 120.0,
    trade_id: Optional[str] = None,
    symbol_base: str = "NIFTY"
) -> Dict[str, Any]:
    """
    Polls order status every 15-20 seconds until filled/rejected or timeout.
    Returns final status with filled quantity and price.
    """
    start_time = time.time()
    poll_count = 0
    consecutive_errors = 0
    last_ws_version = 0
    
    while time.time() - start_time < max_poll_time:
        poll_count += 1
        # Consume broker-pushed order events first for low-latency fill/reject
        # detection. REST orderBook below remains the reconciliation fallback.
        if _order_update_stream is not None:
            ws_version, ws_event = _order_update_stream.snapshot(order_id)
            if ws_event is not None and ws_version > last_ws_version:
                last_ws_version = ws_version
                ws_status = str(
                    ws_event.get("orderstatus", ws_event.get("orderStatus", ws_event.get("status", "")))
                ).upper().replace(" ", "_")
                ws_filled_qty = int(
                    ws_event.get("filledshares", ws_event.get("filledquantity", ws_event.get("filledQuantity", 0))) or 0
                )
                ws_avg_price = float(
                    ws_event.get("averageprice", ws_event.get("averagePrice", ws_event.get("avgPrice", 0.0))) or 0.0
                )
                if ws_status in ("COMPLETE", "FILLED", "EXECUTED"):
                    record_order_event(
                        order_type="STATUS_WS", symbol=symbol, symbol_token=symbol_token,
                        transaction_type="N/A", quantity=ws_filled_qty, status="FILLED_WS",
                        order_id=order_id, execution_price=ws_avg_price, trade_id=trade_id,
                        retry_count=poll_count, duration_ms=int((time.time() - start_time) * 1000),
                        symbol_base=symbol_base,
                    )
                    return {
                        'status': 'FILLED', 'order_id': order_id,
                        'filled_quantity': ws_filled_qty, 'average_price': ws_avg_price,
                        'poll_count': poll_count, 'source': 'WEBSOCKET',
                    }
                if ws_status in ("REJECTED", "CANCELLED", "CANCELED", "ERROR"):
                    error_msg = str(ws_event.get("text", ws_event.get("message", ws_status)))
                    record_order_event(
                        order_type="STATUS_WS", symbol=symbol, symbol_token=symbol_token,
                        transaction_type="N/A", quantity=ws_filled_qty, status=ws_status,
                        order_id=order_id, error_message=error_msg,
                        error_code=str(ws_event.get("errorcode", ws_event.get("errorCode", ""))),
                        trade_id=trade_id, retry_count=poll_count,
                        duration_ms=int((time.time() - start_time) * 1000), symbol_base=symbol_base,
                    )
                    return {
                        'status': ws_status, 'order_id': order_id,
                        'filled_quantity': ws_filled_qty, 'average_price': ws_avg_price,
                        'error_message': error_msg, 'poll_count': poll_count,
                        'source': 'WEBSOCKET',
                    }
                if ws_status:
                    record_order_event(
                        order_type="STATUS_WS", symbol=symbol, symbol_token=symbol_token,
                        transaction_type="N/A", quantity=ws_filled_qty, status=ws_status,
                        order_id=order_id,
                        execution_price=ws_avg_price if ws_avg_price > 0 else None,
                        trade_id=trade_id, retry_count=poll_count,
                        duration_ms=int((time.time() - start_time) * 1000),
                        symbol_base=symbol_base,
                    )
        try:
            with smart_api_lock:
                # SmartAPI's orderBook endpoint takes no order ID argument and
                # returns the account's order list; select our order locally.
                response = smart_api.orderBook()
            
            if response and response.get("status"):
                order_data = response.get("data", {})
                if isinstance(order_data, list) and order_data:
                    order_info = next((item for item in order_data
                                       if str(item.get("orderid", item.get("orderId", ""))) == str(order_id)), None)
                    if order_info is None:
                        logger.warning(f"[Poll #{poll_count}] Order {order_id} not present in order book yet")
                        consecutive_errors = 0
                        time.sleep(poll_interval)
                        continue
                    status = str(order_info.get("orderstatus", order_info.get("status", ""))).upper()
                    consecutive_errors = 0
                    
                    if status in ("COMPLETE", "FILLED", "EXECUTED"):
                        filled_qty = int(order_info.get("filledquantity", order_info.get("filledshares", 0)) or 0)
                        avg_price = float(order_info.get("averageprice", 0.0) or 0.0)
                        
                        record_order_event(
                            order_type="STATUS_POLL",
                            symbol=symbol,
                            symbol_token=symbol_token,
                            transaction_type="N/A",
                            quantity=filled_qty,
                            status="FILLED_POLL",
                            order_id=order_id,
                            execution_price=avg_price,
                            trade_id=trade_id,
                            retry_count=poll_count,
                            duration_ms=int((time.time() - start_time) * 1000),
                            symbol_base=symbol_base
                        )
                        
                        return {
                            'status': 'FILLED',
                            'order_id': order_id,
                            'filled_quantity': filled_qty,
                            'average_price': avg_price,
                            'poll_count': poll_count
                        }
                    
                    elif status in ("REJECTED", "CANCELLED", "ERROR"):
                        error_msg = order_info.get("message", "Order rejected/cancelled")
                        error_code = order_info.get("errorcode", "")
                        
                        record_order_event(
                            order_type="STATUS_POLL",
                            symbol=symbol,
                            symbol_token=symbol_token,
                            transaction_type="N/A",
                            quantity=0,
                            status=status,
                            order_id=order_id,
                            error_message=error_msg,
                            error_code=error_code,
                            trade_id=trade_id,
                            retry_count=poll_count,
                            duration_ms=int((time.time() - start_time) * 1000),
                            symbol_base=symbol_base
                        )
                        
                        return {
                            'status': status,
                            'order_id': order_id,
                            'filled_quantity': 0,
                            'average_price': 0.0,
                            'error_message': error_msg,
                            'error_code': error_code,
                            'poll_count': poll_count
                        }
                    
                    elif status in ("PENDING", "OPEN", "TRIGGER PENDING", "ORDER PLACED"):
                        # Still pending, continue polling
                        logger.info(f"[Poll #{poll_count}] Order {order_id} still {status}, continuing...")
                    
        except Exception as e:
            logger.warning(f"[Poll #{poll_count}] Error checking order status: {e}")
            consecutive_errors += 1
            if consecutive_errors >= 2:
                logger.error(f"Order status polling stopped after {consecutive_errors} consecutive API errors; preserving the placement result and continuing trade setup")
                break
        
        # A WebSocket event wakes this wait early; otherwise reconcile by REST
        # on the normal polling cadence.
        if _order_update_stream is not None:
            _order_update_stream.wait_next(order_id, last_ws_version, poll_interval)
        else:
            time.sleep(poll_interval)
    
    # Timeout reached
    poll_status = "POLL_ERROR" if consecutive_errors >= 2 else "POLL_TIMEOUT"
    logger.warning(f"Order {order_id} status polling ended as {poll_status} after {time.time() - start_time:.1f}s ({poll_count} polls)")
    record_order_event(
        order_type="STATUS_POLL",
        symbol=symbol,
        symbol_token=symbol_token,
        transaction_type="N/A",
        quantity=0,
        status=poll_status,
        order_id=order_id,
        error_message=poll_status,
        trade_id=trade_id,
        retry_count=poll_count,
        duration_ms=int((time.time() - start_time) * 1000),
        symbol_base=symbol_base
    )
    
    return {
        'status': poll_status,
        'order_id': order_id,
        'filled_quantity': 0,
        'average_price': 0.0,
        'poll_count': poll_count
    }


def retry_order_with_backoff(
    smart_api: SmartConnect,
    trading_symbol: str,
    symbol_token: str,
    transaction_type: str,
    quantity: int,
    exchange: str = "NFO",
    order_type: str = "MARKET",
    product_type: str = "INTRADAY",
    max_retries: int = 3,
    initial_delay: float = 180.0,  # 3 minutes initial delay
    order_type_label: str = "ENTRY_BUY",
    trade_id: Optional[str] = None,
    symbol_base: str = "NIFTY",
    enable_polling: bool = False,
) -> Dict[str, Any]:
    """
    Retries rejected orders with exponential backoff (3 mins delay base).
    Retries up to 3 times with increasing delay: 3min, 6min, 9min.
    """
    for attempt in range(max_retries):
        delay = initial_delay * (attempt + 1)
        
        if attempt > 0:
            logger.info(f"[Retry {attempt+1}/{max_retries}] Waiting {delay:.0f}s before retrying order...")
            time.sleep(delay)
        
        # Place order
        result = place_order_with_validation(
            smart_api=smart_api,
            trading_symbol=trading_symbol,
            symbol_token=symbol_token,
            transaction_type=transaction_type,
            quantity=quantity,
            exchange=exchange,
            order_type=order_type,
            product_type=product_type,
            order_type_label=order_type_label,
            trade_id=trade_id,
            symbol_base=symbol_base,
            retry_count=attempt,
            enable_polling=enable_polling,
        )
        
        # If successful, return immediately
        if result['success'] or result['status'] == 'FILLED':
            logger.info(f"[Retry {attempt+1}/{max_retries}] Order succeeded on attempt {attempt+1}")
            return result
        
        # If validation error, don't retry (permanent error)
        if result['status'] in ('VALIDATION_ERROR', 'PRICE_VALIDATION_FAILED'):
            logger.warning(f"[Retry {attempt+1}/{max_retries}] Validation error, not retrying: {result.get('error')}")
            return result
        
        # Check if error is transient (rate limit, timeout, etc.)
        error_msg = result.get('error', '').lower()
        is_transient = any(keyword in error_msg for keyword in [
            'exceeding access rate',
            'too many requests',
            'timeout',
            'connection',
            'network',
            'temporary'
        ])
        
        if not is_transient:
            logger.warning(f"[Retry {attempt+1}/{max_retries}] Non-transient error, not retrying: {result.get('error')}")
            return result
        
        logger.warning(f"[Retry {attempt+1}/{max_retries}] Transient error, will retry in {delay:.0f}s: {result.get('error')}")
    
    # All retries exhausted
    logger.error(f"All {max_retries} retry attempts exhausted for {trading_symbol}")
    return result


def fetch_manual_exit_price(
    smart_api: SmartConnect,
    symbol: str,
    symbol_token: str,
    exchange: str = "NFO",
    exit_time: Optional[datetime] = None,
    trade_id: Optional[str] = None,
    symbol_base: str = "NIFTY"
) -> Optional[float]:
    """
    Fetches historical option price at the time of manual exit.
    Used when manual exit is detected to calculate actual P&L.
    """
    if exit_time is None:
        logger.warning("No exit time provided for manual exit price fetch")
        return None
    
    try:
        # Fetch 5-minute candle data around the exit time
        exit_time_str = exit_time.strftime("%Y-%m-%d %H:%M")
        
        # Get candle for the 5-minute period containing the exit time
        from live.daybreakout.range_calculator import _safe_get_candle_data
        
        params = {
            "exchange": exchange,
            "symboltoken": symbol_token,
            "interval": "FIVE_MINUTE",
            "fromdate": exit_time_str,
            "todate": exit_time_str
        }
        
        response = _safe_get_candle_data(smart_api, params, max_retries=5, retry_delay=2.5)
        
        if response and response.get("status") and response.get("data"):
            candles = response["data"]
            if candles and len(candles) > 0:
                # Use the close price of the candle
                close_price = float(candles[0].get("close", 0.0))
                logger.info(f"Fetched manual exit price for {symbol} at {exit_time_str}: {close_price:.2f}")
                
                record_order_event(
                    order_type="MANUAL_EXIT_PRICE",
                    symbol=symbol,
                    symbol_token=symbol_token,
                    transaction_type="N/A",
                    quantity=0,
                    status="PRICE_FETCHED",
                    execution_price=close_price,
                    trade_id=trade_id,
                    symbol_base=symbol_base
                )
                
                return close_price
        
        logger.warning(f"Could not fetch historical price for manual exit of {symbol} at {exit_time_str}")
        return None
        
    except Exception as e:
        logger.error(f"Error fetching manual exit price: {e}")
        return None


def execute_spread_atomically(
    smart_api: SmartConnect,
    buy_leg: Dict[str, Any],
    sell_leg: Optional[Dict[str, Any]],
    exchange: str,
    timeout: float = 60.0,
    trade_id: Optional[str] = None,
    symbol_base: str = "NIFTY",
    enable_retry: bool = False,
    enable_polling: bool = False
) -> Dict[str, Any]:
    """
    Executes spread as atomic operation - either both legs succeed or both fail.
    Includes rollback logic for partial fills.
    Tracks all order lifecycle events.
    """
    result = {
        'success': False,
        'buy_result': None,
        'sell_result': None,
        'rollback_performed': False,
        'final_positions': None
    }

    # Execute BUY leg first
    buy_result = place_order_with_validation(
        smart_api,
        buy_leg['symbol'],
        buy_leg['token'],
        "BUY",
        buy_leg['quantity'],
        exchange,
        order_type_label="ENTRY_BUY",
        trade_id=trade_id,
        symbol_base=symbol_base,
        enable_retry=enable_retry,
        enable_polling=enable_polling
    )
    result['buy_result'] = buy_result

    if not buy_result['success']:
        rollback_result = None
        if buy_result.get('filled_quantity', 0) > 0 or buy_result.get('status') in ('UNCONFIRMED', 'POLL_TIMEOUT', 'POLL_ERROR'):
            # The order may have filled even though orderBook confirmation
            # failed. Close only the actual broker quantity (square_off reads
            # live netqty) and retain residual state if it cannot be flattened.
            rollback_result = square_off(
                smart_api, buy_leg=buy_leg, exchange=exchange,
                trade_id=trade_id, mode=None,
            )
            result['rollback_performed'] = True
        result['final_positions'] = {
            'status': 'BUY_FAILED' if rollback_result is not False else 'RESIDUAL_POSITIONS',
            'rollback_success': rollback_result,
        }
        return result

    # Update buy leg with execution details
    buy_leg['entry_price'] = buy_result['execution_price']
    buy_leg['order_id'] = buy_result['order_id']

    # Small delay to ensure order processing
    time.sleep(0.5)

    # Execute SELL leg if present
    if sell_leg:
        sell_result = place_order_with_validation(
            smart_api,
            sell_leg['symbol'],
            sell_leg['token'],
            "SELL",
            sell_leg['quantity'],
            exchange,
            order_type_label="ENTRY_SELL",
            trade_id=trade_id,
            symbol_base=symbol_base,
            enable_retry=enable_retry,
            enable_polling=enable_polling
        )
        result['sell_result'] = sell_result

        if not sell_result['success']:
            logger.warning("SELL leg failed or was only partially filled; flattening all actual spread legs...")
            rollback_success = square_off(
                smart_api, buy_leg=buy_leg, sell_leg=sell_leg,
                exchange=exchange, trade_id=trade_id, mode=None,
            )
            result['rollback_performed'] = True
            result['final_positions'] = {
                'status': 'SELL_FAILED_ROLLED_BACK' if rollback_success else 'RESIDUAL_POSITIONS',
                'rollback_success': rollback_success,
            }
            if not rollback_success:
                logger.critical("CRITICAL: Spread rollback incomplete; residual positions require monitoring/reconciliation")
            return result
        
        # Update sell leg with execution details
        sell_leg['entry_price'] = sell_result['execution_price']
        sell_leg['order_id'] = sell_result['order_id']
    
    # Verify final position state
    try:
        expected_positions = [
            {'symbol': buy_leg['symbol'], 'quantity': buy_leg['quantity']}
        ]
        if sell_leg:
            expected_positions.append({'symbol': sell_leg['symbol'], 'quantity': -sell_leg['quantity']})
        
        is_valid, actual_positions, mismatches = verify_position_state(
            smart_api, expected_positions, max_retries=2
        )
        
        if not is_valid:
            logger.warning(f"Position verification failed: {mismatches}")
            rollback_success = square_off(
                smart_api, buy_leg=buy_leg, sell_leg=sell_leg,
                exchange=exchange, trade_id=trade_id, mode=None,
            )
            result['final_positions'] = {
                'status': 'VERIFICATION_FAILED_ROLLED_BACK' if rollback_success else 'RESIDUAL_POSITIONS',
                'rollback_success': rollback_success,
                'mismatches': mismatches,
                'actual_positions': actual_positions
            }
        else:
            result['success'] = True
            result['final_positions'] = {
                'status': 'SUCCESS',
                'actual_positions': actual_positions
            }
            
    except Exception as e:
        logger.error(f"Position verification error: {e}")
        result['final_positions'] = {'status': 'VERIFICATION_ERROR', 'error': str(e)}
    
    return result


def square_off(
    smart_api: SmartConnect,
    buy_leg: Optional[Dict[str, Any]] = None,
    sell_leg: Optional[Dict[str, Any]] = None,
    exchange: Optional[str] = None,
    trade_id: Optional[str] = None,
    mode: Optional[str] = None,
    buy_item: Optional[Dict[str, Any]] = None,
    sell_item: Optional[Dict[str, Any]] = None,
    **kwargs
) -> bool:
    """
    Squares off active option positions for a specific trade.
    Only closes this specific trade's strikes/tokens without bulk-flattening the symbol.
    Accepts both (buy_leg, sell_leg) and (buy_item, sell_item).
    For Spread: Exits SELL leg FIRST (by BUYing back), then exits BUY leg (by SELLing).
    For Naked: Exits BUY leg (by SELLing).
    Serialized with square_off_all so EOD/SL cannot place duplicate exits.
    Returns True if square-off succeeded or positions are already flat, False on order failure.
    Tracks all exit order lifecycle events.
    """
    buy_leg = buy_leg or buy_item
    sell_leg = sell_leg or sell_item
    mode = _resolve_mode(mode)
    symbol_base = infer_symbol_base((buy_leg or {}).get("symbol") or (sell_leg or {}).get("symbol"))

    if not exchange:
        if buy_leg and buy_leg.get("exchange"):
            exchange = buy_leg["exchange"]
        elif sell_leg and sell_leg.get("exchange"):
            exchange = sell_leg["exchange"]
        elif symbol_base == "SENSEX":
            exchange = "BFO"
        else:
            exchange = "NFO"
    
    # Get config values for retry and polling
    enable_retry = RISK_CONFIG.get("enable_order_retry", False)
    enable_polling = RISK_CONFIG.get("enable_order_polling", False)
    
    with flatten.lock:
        if flatten.is_completed(symbol_base, mode=mode):
            logger.info(f"Skipping per-trade square_off for {symbol_base} ({mode}): bulk flatten already completed.")
            return True

        print(f"\n[!] Squaring off option positions for trade {trade_id or 'TRADE'} ({mode or 'SINGLE'})...")
        logger.info(f"Squaring off option positions for trade {trade_id or 'TRADE'} ({mode or 'SINGLE'})")

        open_pos = get_open_positions(smart_api, symbol_filter=symbol_base)
        if open_pos is None:
            logger.error(f"Cannot safely square off {symbol_base}: broker position lookup failed")
            return False
        open_symbols = set()
        if open_pos:
            open_symbols = {
                pos.get("tradingsymbol", "")
                for pos in open_pos
                if int(pos.get("netqty", 0) or 0) != 0
            }

        all_ok = True

        # 1. Exit SELL leg first (if present)
        position_by_symbol = {
            pos.get("tradingsymbol", ""): pos for pos in (open_pos or [])
            if int(pos.get("netqty", 0) or 0) != 0
        }
        if sell_leg and sell_leg.get("symbol") and sell_leg.get("token"):
            position = position_by_symbol.get(sell_leg["symbol"])
            if position is None:
                logger.info(f"SELL leg {sell_leg['symbol']} already flat; skip exit order.")
            else:
                netqty = int(position.get("netqty", 0) or 0)
                sell_result = place_order_with_validation(
                    smart_api=smart_api,
                    trading_symbol=sell_leg["symbol"],
                    symbol_token=sell_leg["token"],
                    transaction_type="SELL" if netqty > 0 else "BUY",
                    quantity=abs(netqty),
                    exchange=exchange,
                    order_type_label="EXIT_BUY",
                    trade_id=trade_id,
                    symbol_base=symbol_base,
                    enable_retry=enable_retry,
                    enable_polling=enable_polling
                )
                if not sell_result['success']:
                    logger.error(f"Failed to square off SELL leg: {sell_leg['symbol']} - {sell_result.get('error', 'Unknown error')}")
                    all_ok = False
                else:
                    sell_leg["exit_price"] = sell_result.get("execution_price")
                sell_leg["exit_order_id"] = sell_result.get("order_id")

        # 2. Exit BUY leg next
        if buy_leg and buy_leg.get("symbol") and buy_leg.get("token"):
            position = position_by_symbol.get(buy_leg["symbol"])
            if position is None:
                logger.info(f"BUY leg {buy_leg['symbol']} already flat; skip exit order.")
            else:
                netqty = int(position.get("netqty", 0) or 0)
                buy_result = place_order_with_validation(
                    smart_api=smart_api,
                    trading_symbol=buy_leg["symbol"],
                    symbol_token=buy_leg["token"],
                    transaction_type="SELL" if netqty > 0 else "BUY",
                    quantity=abs(netqty),
                    exchange=exchange,
                    order_type_label="EXIT_SELL",
                    trade_id=trade_id,
                    symbol_base=symbol_base,
                    enable_retry=enable_retry,
                    enable_polling=enable_polling
                )
                if not buy_result['success']:
                    logger.error(f"Failed to square off BUY leg: {buy_leg['symbol']} - {buy_result.get('error', 'Unknown error')}")
                    all_ok = False
                else:
                    buy_leg["exit_price"] = buy_result.get("execution_price")
                buy_leg["exit_order_id"] = buy_result.get("order_id")

        return all_ok


def _compute_leg_pnl(buy_item: Optional[Dict[str, Any]], sell_item: Optional[Dict[str, Any]]) -> float:
    """Realized P&L across both legs from entry/exit prices (exit falls back to entry)."""
    total = 0.0
    for leg, default_side in ((buy_item, "BUY"), (sell_item, "SELL")):
        if not leg:
            continue
        entry = float(leg.get("entry_price", 0.0) or 0.0)
        exit_price = leg.get("exit_price")
        if exit_price is None:
            exit_price = entry
        qty = int(leg.get("quantity", 0) or 0)
        sign = 1 if str(leg.get("position_side", default_side)).upper() == "BUY" else -1
        total += (float(exit_price) - entry) * qty * sign
    return total


def close_and_record_tracked_trade(
    smart_api: SmartConnect,
    trade_id: str,
    trade: Dict[str, Any],
    exit_reason: str = "EXIT_POSITION_TIMEOUT",
    mode: Optional[str] = None,
) -> bool:
    """
    Square off a registered trade and record its realized P&L to the mode CSV.

    ``square_off`` intentionally places orders without logging the trade, so every
    forced/timeout/EOD-mode close that only calls ``square_off`` + ``remove_trade``
    silently drops the P&L. The daily loss/profit gates then keep reading ~0 and
    the strategy keeps re-entering after a limit should have stopped it. This
    helper centralises close -> compute -> record -> cooldown -> deregister so the
    loss/profit is always captured. Returns True only when the broker close
    succeeded and the trade was removed; on failure the trade is retained for
    retry.
    """
    buy_item = trade.get("buy_leg")
    sell_item = trade.get("sell_leg")
    symbol_base = trade.get("symbol_base", "NIFTY")
    exchange = trade.get("exchange", "NFO")
    mode = mode or trade.get("mode")

    ok = square_off(
        smart_api,
        buy_item=buy_item,
        sell_item=sell_item,
        exchange=exchange,
        trade_id=trade_id,
        mode=mode,
    )
    if not ok:
        logger.error(f"[{trade_id}] {exit_reason} square-off incomplete/failed; retaining trade for retry")
        return False

    total_pnl = _compute_leg_pnl(buy_item, sell_item)
    if not trade.get("is_recovered", False):
        try:
            record_trade(
                buy_item, sell_item, total_pnl, float(trade.get("max_pnl", 0.0)), exit_reason,
                symbol_base, trade.get("expiry", ""), trade.get("trade_start_time"),
                datetime.now().strftime("%H:%M:%S"), trade.get("trade_reason", ""),
                trade.get("structure", "SPREAD"), mode=mode,
            )
        except Exception as rec_err:
            logger.error(f"[{trade_id}] {exit_reason} failed to record trade P&L: {rec_err}")

    _monitoring_state.record_exit(symbol_base, exit_reason, datetime.now(), mode=mode)
    if exit_reason in ("EXIT_MAX_LOSS", "EXIT_DAILY_MAX_LOSS", "EXIT_POSITION_TIMEOUT"):
        _monitoring_state.record_stop_loss(symbol_base, datetime.now(), mode=mode)
    elif exit_reason in ("EXIT_TRAILING_PROFIT", "EXIT_MAX_PROFIT"):
        _monitoring_state.record_profit_exit(symbol_base, datetime.now(), mode=mode)

    _monitoring_state.remove_trade(trade_id)
    print(f"\n[+] Trade {trade_id} closed [{exit_reason}]: Final P&L = Rs.{total_pnl:+.2f}")
    logger.info(f"Trade {trade_id} closed [{exit_reason}]: Final P&L = Rs.{total_pnl:+.2f}")
    return True


def square_off_tracked_trade_eod(
    smart_api: SmartConnect,
    trade_id: str,
    trade: Dict[str, Any],
    mode: Optional[str] = None,
) -> bool:
    """Close, verify, log, and remove one tracked trade at its session close."""
    mode = mode or trade.get("mode")
    buy_item = trade.get("buy_leg")
    sell_item = trade.get("sell_leg")
    if not buy_item:
        logger.error(f"Cannot EOD square off {trade_id}: tracked trade has no primary leg")
        return False

    sl_order_id = buy_item.get("sl_order_id")
    if sl_order_id:
        cancel_order(smart_api, sl_order_id, variety="STOPLOSS")

    if not square_off(
        smart_api,
        buy_item=buy_item,
        sell_item=sell_item,
        exchange=trade.get("exchange", "NFO"),
        trade_id=trade_id,
        mode=mode,
    ):
        return False

    open_positions = get_open_positions(smart_api, symbol_filter=trade.get("symbol_base"))
    if open_positions is None:
        logger.error(f"EOD close for {trade_id} could not be verified; keeping it in monitoring state")
        return False
    remaining_symbols = {
        pos.get("tradingsymbol") for pos in open_positions
        if int(pos.get("netqty", 0) or 0) != 0
    }
    if buy_item.get("symbol") in remaining_symbols or (sell_item and sell_item.get("symbol") in remaining_symbols):
        logger.error(f"EOD close for {trade_id} left broker positions open; keeping it in monitoring state")
        return False

    total_pnl = 0.0
    for leg, default_side in ((buy_item, "BUY"), (sell_item, "SELL")):
        if not leg:
            continue
        exit_price = leg.get("exit_price")
        if exit_price is None:
            exit_price = leg.get("entry_price", 0.0)
        sign = 1 if leg.get("position_side", default_side).upper() == "BUY" else -1
        total_pnl += (float(exit_price) - float(leg.get("entry_price", 0.0))) * int(leg.get("quantity", 0)) * sign

    end_time = datetime.now().strftime("%H:%M:%S")
    if not trade.get("is_recovered", False):
        record_trade(
            buy_item=buy_item,
            sell_item=sell_item,
            total_pnl=total_pnl,
            max_pnl=float(trade.get("max_pnl", 0.0)),
            status="EXIT_EOD_SQUARE_OFF",
            symbol_base=trade.get("symbol_base", "NIFTY"),
            expiry=trade.get("expiry", ""),
            start_time=trade.get("trade_start_time"),
            end_time=end_time,
            trade_reason=trade.get("trade_reason", ""),
            structure=trade.get("structure", "SPREAD"),
            mode=mode,
        )
    _monitoring_state.record_exit(
        trade.get("symbol_base", "NIFTY"), "EXIT_EOD_SQUARE_OFF", datetime.now(), mode=mode
    )
    _monitoring_state.remove_trade(trade_id)
    with flatten.lock:
        flatten.mark_completed(trade.get("symbol_base", "NIFTY"), mode=mode)
    return True

def square_off_all(smart_api: SmartConnect, symbol_base: str, is_eod: bool = False, mode: Optional[str] = None) -> bool:
    """Squares off positions for the given symbol base and logs trades.
    If mode is specified, squares off ONLY that mode's specific active trades (token-specific).
    Bulk broker flattening is only performed when mode is None.

    Idempotent per symbol/day: main, P&L, and EOD watchdog can all call this safely.

    Lock strategy:
      - flatten.lock is held ONLY for the idempotency check and the final mark_completed().
      - All network I/O (position fetch, order placement) happens OUTSIDE flatten.lock so
        the P&L monitor and the EOD watchdog never block each other for more than a few
        microseconds on the idempotency guard.
    """
    symbol_base = str(symbol_base).upper().strip()
    mode = _resolve_mode(mode)

    # In dual execution mode, never bulk flatten the entire symbol on the broker.
    # Close only the specific trade(s) belonging to this mode.
    if mode:
        active = _monitoring_state.get_all_trades()
        mode_trades = [
            (tid, t) for tid, t in active.items()
            if str(t.get("symbol_base", "")).upper() == symbol_base
            and str(t.get("mode", "")).upper() == mode
        ]
        if mode_trades:
            logger.info(f"square_off_all for {symbol_base} ({mode}): exiting {len(mode_trades)} trade(s) by specific tokens.")
            close_reason = "EXIT_EOD_SQUARE_OFF" if is_eod else "EXIT_MANUAL"
            all_ok = True
            for tid, t in mode_trades:
                if not close_and_record_tracked_trade(smart_api, tid, t, exit_reason=close_reason, mode=mode):
                    all_ok = False
            if is_eod:
                with flatten.lock:
                    flatten.mark_completed(symbol_base, mode=mode)
            return all_ok
        else:
            logger.info(f"square_off_all for {symbol_base} ({mode}): no active trades registered for this mode.")
            if is_eod:
                with flatten.lock:
                    flatten.mark_completed(symbol_base, mode=mode)
            return True

    # Get config values for retry and polling
    enable_retry = RISK_CONFIG.get("enable_order_retry", False)
    enable_polling = RISK_CONFIG.get("enable_order_polling", False)

    # --- Idempotency check (held briefly) -----------------------------------
    with flatten.lock:
        if flatten.is_completed(symbol_base, mode=mode):
            logger.info(f"square_off_all skipped for {symbol_base} ({mode}): already completed today.")
            return True
    # flatten.lock released here; other threads may proceed with their own checks

    print(f"\n[!] Forcefully squaring off ALL open positions for {symbol_base}...")
    logger.info(f"Forcefully squaring off ALL open positions for {symbol_base}")

    now_hm = datetime.now().strftime("%H:%M")
    is_at_eod = is_eod or (now_hm >= "14:40")

    # --- Network I/O: all done WITHOUT flatten.lock --------------------------
    open_pos = get_open_positions(smart_api, symbol_filter=symbol_base)
    if open_pos is None:
        logger.warning(f"Failed to fetch open positions for {symbol_base}. Retrying once...")
        time.sleep(1.0)
        open_pos = get_open_positions(smart_api, symbol_filter=symbol_base)
        if open_pos is None:
            logger.error(f"Cannot square off {symbol_base}: broker position API error.")
            return False

    for pos in open_pos:
        symbol = pos.get("tradingsymbol", "")
        try:
            netqty = int(pos.get("netqty", 0))
            if netqty == 0:
                continue

            exchange = pos.get("exchange", "NFO")
            token = pos.get("symboltoken", "")
            buy_price = float(pos.get("buyavgprice", 0.0)) or float(pos.get("averageprice", 0.0))

            transaction_type = "SELL" if netqty > 0 else "BUY"
            order_type_label = "EXIT_SELL" if netqty > 0 else "EXIT_BUY"

            resp = place_order_with_validation(
                smart_api=smart_api,
                trading_symbol=symbol,
                symbol_token=token,
                transaction_type=transaction_type,
                quantity=abs(netqty),
                exchange=exchange,
                order_type_label=order_type_label,
                trade_id="EOD_SQUARE_OFF",
                symbol_base=symbol_base,
                enable_retry=enable_retry,
                enable_polling=enable_polling
            )

            if resp['success']:
                exit_price = resp['execution_price'] if resp['execution_price'] else get_ltp(smart_api, token, symbol, exchange, last_price=buy_price)
                pnl = (exit_price - buy_price) * abs(netqty) if netqty > 0 else (buy_price - exit_price) * abs(netqty)

                item = {
                    "symbol": symbol,
                    "token": token,
                    "quantity": abs(netqty),
                    "entry_price": buy_price,
                    "exit_price": exit_price,
                    "strike": "",
                }
                trade_now = datetime.now().strftime("%H:%M:%S")
                try:
                    record_trade(
                        buy_item=item if netqty > 0 else {"symbol": "", "token": "", "quantity": 0, "entry_price": 0.0, "exit_price": 0.0},
                        sell_item=item if netqty < 0 else None,
                        total_pnl=pnl,
                        max_pnl=max(0.0, pnl),
                        status="EXIT_EOD_SQUARE_OFF",
                        symbol_base=symbol_base,
                        expiry=pos.get("expirydate", ""),
                        start_time=trade_now,
                        end_time=trade_now,
                        trade_reason="EOD Market Close (3:10 PM)",
                        structure="NAKED" if netqty > 0 else "SPREAD"
                    )
                except Exception as rec_err:
                    logger.error(f"Failed to record EOD trade for {symbol}: {rec_err}")
            else:
                logger.error(f"Failed to square off {symbol}: {resp.get('error', 'Unknown error')}")
        except Exception as e:
            logger.error(f"Error squaring off {symbol}: {e}")

    # --- Verify completion and mark (brief lock re-acquire) ------------------
    remaining = get_open_positions(smart_api, symbol_filter=symbol_base)
    if remaining is None:
        logger.warning(f"Could not verify flatten for {symbol_base}; will allow retry.")
        return False
    elif remaining:
        still_open = [p.get("tradingsymbol") for p in remaining]
        logger.warning(f"Flatten incomplete for {symbol_base}, still open: {still_open}")
        return False
    else:
        if is_at_eod:
            with flatten.lock:
                flatten.mark_completed(symbol_base, mode=mode)
            logger.info(f"Bulk EOD flatten completed for {symbol_base} ({mode or 'GLOBAL'}).")
            # The monitor is shared by both strategy modes. Stop it only after
            # the broker confirms flattening and no tracked trade remains.
            if not _monitoring_state.is_current_monitor_thread():
                stop_position_monitor()
        else:
            logger.info(f"Midday square off completed for {symbol_base} ({mode or 'GLOBAL'}).")
        return True

def _consolidated_monitor_worker(smart_api: SmartConnect, default_check_interval: float = RISK_CONFIG["check_interval"]):
    """
    Consolidated background monitor thread that loops over all registered active trades,
    tracking individual and aggregate P&L, trailing profit stops, and auto square-off.
    """
    logger.info(f"Consolidated P&L monitor thread started (check interval: {default_check_interval}s).")
    iteration_count = 0
    after_hours_notice_logged = False

    while not _monitoring_state.is_stop_requested():
        try:
            active_trades = _monitoring_state.get_all_trades()
            if not active_trades:
                if _monitoring_state.wait_for_stop(1.0):
                    break
                continue

            iteration_count += 1
            now_dt = datetime.now()
            now_str = now_dt.strftime("%H:%M:%S")
            session_open = _market_session_open(now_dt)
            if session_open:
                after_hours_notice_logged = False

            # EOD square-off by each trade's actual expiry-aware closing time.
            # Failed/unverified closes stay registered for retry on the next pass.
            eod_retry_pending = False
            max_eod_attempts = int(RISK_CONFIG.get("max_eod_close_attempts", 5))
            for trade_id, trade in list(active_trades.items()):
                close_time = _trade_eod_time(trade, now_dt)
                if now_str < f"{close_time}:00":
                    continue

                # Orders placed while the exchange is closed are parked as
                # PENDING and can never fill; retrying them only stacks
                # duplicate exit orders. Keep the trade registered and wait.
                if not session_open:
                    if not after_hours_notice_logged:
                        logger.warning(
                            f"[Consolidated Monitor] Exchange closed at {now_str}; deferring "
                            f"EOD close for {len(active_trades)} trade(s) until the next session"
                        )
                        after_hours_notice_logged = True
                    continue

                attempts = int(trade.get("eod_close_attempts", 0) or 0)
                if attempts >= max_eod_attempts:
                    logger.critical(
                        f"[Consolidated Monitor] {trade_id} EOD close failed on {attempts} "
                        "attempts; stopping automatic closes. MANUAL BROKER CLOSE REQUIRED."
                    )
                    continue
                _monitoring_state.update_trade_field(trade_id, "eod_close_attempts", attempts + 1)

                trade_mode = trade.get("mode")
                logger.info(
                    f"[Consolidated Monitor] EOD {now_str} — closing {trade_id} "
                    f"({trade.get('symbol_base', '')} {trade_mode}) by {close_time} "
                    f"[attempt {attempts + 1}/{max_eod_attempts}]"
                )
                try:
                    if not square_off_tracked_trade_eod(smart_api, trade_id, trade, mode=trade_mode):
                        logger.error(f"[Consolidated Monitor] EOD close failed for {trade_id}; retaining for retry")
                        eod_retry_pending = True
                except Exception as eod_err:
                    logger.error(f"[Consolidated Monitor] Error squaring off {trade_id} at EOD: {eod_err}")
                    eod_retry_pending = True

            active_trades = _monitoring_state.get_all_trades()
            if not active_trades:
                if _monitoring_state.wait_for_stop(min(1.0, default_check_interval)):
                    break
                continue

            # Fetch broker open positions once per iteration
            open_pos = get_open_positions(smart_api)

            aggregate_pnl = 0.0
            trades_to_remove = []

            for trade_id, trade in active_trades.items():
                buy_item = trade.get("buy_leg")
                sell_item = trade.get("sell_leg")
                if not buy_item:
                    # A trade without a buy leg cannot be evaluated by the
                    # LTP/broker logic below. Repoint to the sell leg when it
                    # exists (P&L sign is handled via position_side), else drop
                    # the trade so the monitor thread never crashes on it.
                    if sell_item:
                        logger.error(
                            f"[Consolidated Monitor] {trade_id} registered without buy_leg; "
                            "using sell_leg as the primary leg"
                        )
                        buy_item = sell_item
                        buy_item.setdefault("position_side", "SELL")
                        # get_all_trades() yields copies, so write the repair back
                        # to the live state instead of the local dict.
                        _monitoring_state.update_trade_field(trade_id, "buy_leg", buy_item)
                        _monitoring_state.update_trade_field(trade_id, "sell_leg", None)
                        trade["buy_leg"] = buy_item
                        trade["sell_leg"] = None
                        sell_item = None
                    else:
                        logger.error(
                            f"[Consolidated Monitor] Trade {trade_id} has no legs at all; "
                            "removing from monitoring. MANUAL BROKER CHECK REQUIRED."
                        )
                        _monitoring_state.remove_trade(trade_id)
                        continue
                exchange = trade["exchange"]
                symbol_base = trade["symbol_base"]
                structure = trade["structure"]
                expiry = trade.get("expiry", "")
                trade_start_time = trade.get("trade_start_time", now_str)
                trade_reason = trade.get("trade_reason", "")
                max_loss = trade["max_loss"]
                max_profit = trade["max_profit"]
                profit_threshold = trade["profit_threshold"]
                min_profit_step = trade["min_profit_step"]

                trade_mode = trade.get("mode")
                if flatten.is_completed(symbol_base, mode=trade_mode):
                    _monitoring_state.remove_trade(trade_id)
                    logger.info(f"Trade {trade_id} dropped; square-off already completed for {symbol_base} ({trade_mode}).")
                    continue

                # Check if positions were manually closed on broker
                if open_pos is not None:
                    positions_by_symbol = {
                        pos.get("tradingsymbol"): pos for pos in open_pos
                        if int(pos.get("netqty", 0) or 0) != 0
                    }
                    buy_position = positions_by_symbol.get(buy_item["symbol"])
                    sell_position = positions_by_symbol.get(sell_item["symbol"]) if sell_item else None
                    buy_open = buy_position is not None
                    sell_open = True if not sell_item else sell_position is not None

                    if not buy_open or (sell_item and not sell_open):
                        # Give the broker a short window to publish newly filled
                        # positions before treating an empty position snapshot as
                        # a manual exit.
                        holding_minutes = _monitoring_state.get_trade_holding_time(trade_id) or 0.0
                        missing_checks = int(trade.get("consecutive_missing_checks", 0) or 0)
                        if holding_minutes < 2.0:
                            logger.info(f"[Consolidated Monitor] Position snapshot for {trade_id} is not visible yet; allowing fill-sync grace period")
                        else:
                            missing_checks += 1
                            # Trades arrive as copies; persist the counter so the
                            # confirmation threshold is actually reached.
                            _monitoring_state.update_trade_field(trade_id, "consecutive_missing_checks", missing_checks)
                        if holding_minutes >= 2.0 and missing_checks >= 2:
                            # One missing spread leg does not mean the whole
                            # trade is flat. Close any remaining broker legs
                            # using their actual net quantities before dropping
                            # monitoring state.
                            if buy_position:
                                buy_item["quantity"] = abs(int(buy_position.get("netqty", 0) or 0))
                            if sell_item and sell_position:
                                sell_item["quantity"] = abs(int(sell_position.get("netqty", 0) or 0))
                            if buy_item.get("sl_order_id"):
                                cancel_order(smart_api, buy_item["sl_order_id"], variety="STOPLOSS")
                            try:
                                if not square_off(
                                    smart_api, buy_item, sell_item, exchange,
                                    trade_id=trade_id, mode=trade.get("mode"),
                                ):
                                    logger.error(
                                        f"[Consolidated Monitor] Ghost exit for {trade_id}: "
                                        "remaining leg did not close; retaining trade for retry"
                                    )
                                    continue
                                post_exit_positions = get_open_positions(smart_api, symbol_filter=symbol_base)
                                if post_exit_positions is None or any(
                                    pos.get("tradingsymbol") in {buy_item.get("symbol"), sell_item.get("symbol") if sell_item else None}
                                    and int(pos.get("netqty", 0) or 0) != 0
                                    for pos in post_exit_positions
                                ):
                                    logger.error(
                                        f"[Consolidated Monitor] Ghost exit for {trade_id} could not be verified flat; "
                                        "retaining trade for retry"
                                    )
                                    continue
                            except Exception as ghost_exit_error:
                                logger.error(
                                    f"[Consolidated Monitor] Ghost exit reconciliation failed for {trade_id}: "
                                    f"{ghost_exit_error}; retaining trade for retry"
                                )
                                continue

                            logger.info(f"[Consolidated Monitor] Trade {trade_id} broker legs are flat; recording MANUAL_EXIT.")
                            print(f"\n[!] Manual/ghost exit reconciled for {trade_id}; broker legs are flat.")

                            # Fetch an exit price for each leg so spread P&L
                            # includes both the long and short sides.
                            estimated_exit_time = now_dt - timedelta(minutes=15)  # Approximate exit time (midpoint of 2 checks)
                            actual_pnl = 0.0
                            pnl_complete = True
                            for leg, default_side in ((buy_item, "BUY"), (sell_item, "SELL")):
                                if not leg:
                                    continue
                                exit_price = fetch_manual_exit_price(
                                    smart_api=smart_api,
                                    symbol=leg["symbol"],
                                    symbol_token=leg["token"],
                                    exchange=exchange,
                                    exit_time=estimated_exit_time,
                                    trade_id=trade_id,
                                    symbol_base=symbol_base,
                                )
                                if exit_price is None:
                                    pnl_complete = False
                                    exit_price = leg.get("entry_price", 0.0)
                                leg["exit_price"] = exit_price
                                side = leg.get("position_side", default_side).upper()
                                sign = 1 if side == "BUY" else -1
                                actual_pnl += (exit_price - leg.get("entry_price", 0.0)) * leg.get("quantity", 0) * sign
                            if not pnl_complete:
                                logger.warning(f"[{trade_id}] One or more historical exit prices were unavailable; missing leg P&L recorded as zero.")
                            trades_to_remove.append((trade_id, "MANUAL_EXIT", actual_pnl, trade.get("max_pnl", 0.0), False))
                            continue
                    else:
                        if int(trade.get("consecutive_missing_checks", 0) or 0) != 0:
                            _monitoring_state.update_trade_field(trade_id, "consecutive_missing_checks", 0)
                        trade["consecutive_missing_checks"] = 0

                # For recovered positions, try to use broker P&L directly for accuracy
                is_recovered = trade.get("is_recovered", False)
                total_pnl = 0.0
                leg_info = ""
                
                if is_recovered and open_pos:
                    # Try to get P&L from broker positions for recovered trades
                    broker_pnl = 0.0
                    for pos in open_pos:
                        if pos.get("tradingsymbol") == buy_item.get("symbol"):
                            broker_pnl = float(pos.get("pnl", 0))
                            break
                    
                    if broker_pnl != 0:
                        total_pnl = broker_pnl
                        leg_info = f"BUY {buy_item.get('strike')}: Rs.{broker_pnl:.2f} (Broker P&L)"
                        logger.debug(f"[{trade_id}] Using broker P&L: Rs.{broker_pnl:.2f}")
                    else:
                        # Fallback to LTP calculation if broker P&L is 0
                        logger.warning(f"[{trade_id}] Broker P&L is 0, falling back to LTP calculation")
                        is_recovered = False  # Force LTP calculation below
                
                # Initialize exit prices (used if exit is triggered)
                buy_live = buy_item.get("entry_price", 0.0)
                sell_live = sell_item.get("entry_price", 0.0) if sell_item else 0.0

                if not is_recovered:
                    # Live LTP fetching with defensive validation (reject <= 0.0)
                    buy_live = get_ltp(smart_api, buy_item["token"], buy_item["symbol"], exchange, last_price=buy_item.get("entry_price", 0.0))
                    if buy_live <= 0.0:
                        logger.warning(f"[Consolidated Monitor] Invalid LTP ({buy_live}) for {buy_item['symbol']}. Skipping check this cycle.")
                        continue

                    buy_side = buy_item.get("position_side", "BUY").upper()
                    buy_sign = 1 if buy_side == "BUY" else -1
                    buy_pnl = (buy_live - buy_item["entry_price"]) * buy_item["quantity"] * buy_sign
                    sell_pnl = 0.0

                    if sell_item:
                        sell_live = get_ltp(smart_api, sell_item["token"], sell_item["symbol"], exchange, last_price=sell_item.get("entry_price", 0.0))
                        if sell_live <= 0.0:
                            logger.warning(f"[Consolidated Monitor] Invalid LTP ({sell_live}) for {sell_item['symbol']}. Skipping check this cycle.")
                            continue
                        sell_side = sell_item.get("position_side", "SELL").upper()
                        sell_sign = 1 if sell_side == "BUY" else -1
                        sell_pnl = (sell_live - sell_item["entry_price"]) * sell_item["quantity"] * sell_sign
                        total_pnl = buy_pnl + sell_pnl
                        leg_info = f"BUY {buy_item.get('strike')}: Rs.{buy_pnl:.2f} | SELL {sell_item.get('strike')}: Rs.{sell_pnl:.2f}"
                    else:
                        total_pnl = buy_pnl
                        leg_info = f"BUY {buy_item.get('strike')}: Rs.{buy_pnl:.2f}"
                else:
                    # When using broker P&L, still fetch current LTP for exit prices
                    buy_live = get_ltp(smart_api, buy_item["token"], buy_item["symbol"], exchange, last_price=buy_item.get("entry_price", 0.0))
                    if buy_live <= 0.0:
                        logger.warning(f"[Consolidated Monitor] Invalid LTP ({buy_live}) for {buy_item['symbol']}. Using entry price as fallback.")
                        buy_live = buy_item.get("entry_price", 0.0)
                    if sell_item:
                        sell_live = get_ltp(smart_api, sell_item["token"], sell_item["symbol"], exchange, last_price=sell_item.get("entry_price", 0.0))
                        if sell_live <= 0.0:
                            logger.warning(f"[Consolidated Monitor] Invalid LTP ({sell_live}) for {sell_item['symbol']}. Using entry price as fallback.")
                            sell_live = sell_item.get("entry_price", 0.0)

                aggregate_pnl += total_pnl
                max_pnl = trade.get("max_pnl", 0.0)
                locked_profit = trade.get("locked_profit", profit_threshold)

                # Check if daily max loss would be breached if we exit now
                trade_mode = trade.get("mode")
                daily_trades, daily_loss = get_daily_trade_stats(symbol_base=symbol_base, mode=trade_mode)
                # Only add current P&L if it's a loss
                current_loss = total_pnl if total_pnl < 0 else 0
                potential_daily_loss = daily_loss + current_loss
                daily_max_loss = trade.get("daily_max_loss", RISK_CONFIG.get("daily_max_loss", -10000.0))
                if potential_daily_loss <= daily_max_loss:
                    logger.warning(f"[{trade_id}] Daily Max Loss would be breached - Potential Daily Loss: Rs.{potential_daily_loss:.2f} <= Rs.{daily_max_loss:.2f}")
                    buy_item["exit_price"] = buy_live
                    if sell_item:
                        sell_item["exit_price"] = sell_live
                    trades_to_remove.append((trade_id, "EXIT_DAILY_MAX_LOSS", total_pnl, max_pnl, True))
                    continue

                pnl_emoji = "🟢" if total_pnl >= 0 else "🔴"
                logger.info(f"[Consolidated Monitor #{iteration_count}] [{trade_id}] {pnl_emoji} P&L: Rs.{total_pnl:+.2f} | Peak: Rs.{max_pnl:+.2f} | Floor: Rs.{locked_profit:.0f} | {leg_info}")

                # Trailing profit update — write back to live state so values
                # persist across the 5-minute polling cycles (not just the copy).
                if total_pnl > max_pnl:
                    max_pnl = total_pnl
                    _monitoring_state.update_trade_field(trade_id, "max_pnl", max_pnl)
                    if max_pnl >= profit_threshold:
                        new_locked = ((max_pnl - profit_threshold) // min_profit_step) * min_profit_step + profit_threshold
                        if new_locked > locked_profit:
                            locked_profit = new_locked
                            _monitoring_state.update_trade_field(trade_id, "locked_profit", locked_profit)
                            logger.info(f"[{trade_id}] Profit reached Rs.{max_pnl:.0f} — Locked minimum profit: Rs.{locked_profit:.0f}")

                # Exit Condition 1: Stop Loss
                if total_pnl <= max_loss:
                    logger.warning(f"[{trade_id}] Stop Loss Triggered - P&L: Rs.{total_pnl:.2f} <= Rs.{max_loss:.2f}")
                    buy_item["exit_price"] = buy_live
                    if sell_item:
                        sell_item["exit_price"] = sell_live
                    trades_to_remove.append((trade_id, "EXIT_MAX_LOSS", total_pnl, max_pnl, True))
                    continue

                # Exit Condition 2: Trailing profit floor hit
                # Once the initial threshold is reached, protect its floor too.
                # Exit when a later live P&L check falls below the active floor.
                if max_pnl >= profit_threshold and total_pnl < locked_profit:
                    logger.info(f"[{trade_id}] Trailing Profit Exit - P&L dropped below Rs.{locked_profit:.0f} (Current: Rs.{total_pnl:.2f})")
                    buy_item["exit_price"] = buy_live
                    if sell_item:
                        sell_item["exit_price"] = sell_live
                    trades_to_remove.append((trade_id, "EXIT_TRAILING_PROFIT", total_pnl, max_pnl, True))
                    continue

                # Exit Condition 3: Max profit target reached
                if total_pnl >= max_profit:
                    logger.info(f"[{trade_id}] Max Profit Target Reached - P&L: Rs.{total_pnl:.2f} >= Rs.{max_profit:.2f}")
                    buy_item["exit_price"] = buy_live
                    if sell_item:
                        sell_item["exit_price"] = sell_live
                    trades_to_remove.append((trade_id, "EXIT_MAX_PROFIT", total_pnl, max_pnl, True))
                    continue

            # Process all triggered exits
            for trade_id, exit_status, total_pnl, max_pnl, do_square_off in trades_to_remove:
                trade_info = _monitoring_state.get_trade(trade_id)
                if not trade_info:
                    continue
                buy_item = trade_info.get("buy_leg")
                sell_item = trade_info.get("sell_leg")
                if not buy_item:
                    logger.error(
                        f"[{trade_id}] {exit_status} exit requested without a primary leg; "
                        "skipping order placement. MANUAL BROKER CHECK REQUIRED."
                    )
                    continue
                exchange = trade_info["exchange"]
                symbol_base = trade_info["symbol_base"]
                expiry = trade_info.get("expiry", "")
                trade_start_time = trade_info.get("trade_start_time", now_str)
                trade_reason = trade_info.get("trade_reason", "")
                structure = trade_info["structure"]

                actual_exit_status = exit_status

                # Cancel broker-side stop loss order if it exists (before square off)
                sl_order_id = buy_item.get("sl_order_id")
                if sl_order_id:
                    logger.info(f"[{trade_id}] Attempting to cancel broker-side stop loss order {sl_order_id} before square off")
                    cancel_result = cancel_order(smart_api, sl_order_id, variety="STOPLOSS")
                    if cancel_result:
                        logger.info(f"[{trade_id}] Successfully cancelled broker-side stop loss order {sl_order_id}")
                        # Small delay to ensure cancellation is processed at broker
                        time.sleep(0.5)
                    else:
                        logger.warning(f"[{trade_id}] Failed to cancel stop loss order {sl_order_id} - it may have already executed")

                if do_square_off and not session_open:
                    logger.warning(
                        f"[{trade_id}] {exit_status} triggered at {now_str} but the exchange is closed; "
                        "keeping the trade registered instead of placing an order that cannot fill"
                    )
                    continue

                if do_square_off:
                    # Verify positions still exist at broker before attempting square off
                    open_pos = get_open_positions(smart_api, symbol_filter=symbol_base)
                    buy_position_exists = False
                    sell_position_exists = False
                    position_check_failed = False

                    if open_pos:
                        buy_position_exists = any(
                            pos.get("tradingsymbol") == buy_item["symbol"] and int(pos.get("netqty", 0)) != 0
                            for pos in open_pos
                        )
                        if sell_item:
                            sell_position_exists = any(
                                pos.get("tradingsymbol") == sell_item["symbol"] and int(pos.get("netqty", 0)) != 0
                                for pos in open_pos
                            )
                    else:
                        # Position check failed (API error), but don't treat as manual exit
                        # Attempt square off anyway since exit was legitimately triggered
                        position_check_failed = True
                        logger.warning(f"[{trade_id}] Position check failed (API error), attempting square off anyway")

                    # If positions don't exist and check succeeded, treat as manual exit
                    if not position_check_failed and (not buy_position_exists or (sell_item and not sell_position_exists)):
                        logger.info(f"[{trade_id}] Positions not found at broker - treating as manual exit")
                        actual_exit_status = "MANUAL_EXIT"
                        do_square_off = False
                    else:
                        trade_mode = trade_info.get("mode")
                        sq_ok = True
                        if flatten.is_completed(symbol_base, mode=trade_mode):
                            logger.info(f"[{trade_id}] Skip per-trade square_off; bulk flatten already completed.")
                        else:
                            try:
                                sq_ok = square_off(smart_api, buy_item, sell_item, exchange, trade_id=trade_id, mode=trade_mode)
                            except Exception as sq_err:
                                logger.error(f"[{trade_id}] Error during square off: {sq_err}")
                                sq_ok = False

                        if not sq_ok:
                            logger.error(f"[{trade_id}] Square off incomplete/failed. Keeping trade in monitor to retry next cycle.")
                            continue

                    trade_end_time = datetime.now().strftime("%H:%M:%S")
                    trade_mode = trade_info.get("mode")
                    # Skip CSV logging for recovered positions (they're continuations of existing trades)
                    if not trade.get("is_recovered", False):
                        try:
                            record_trade(
                                buy_item, sell_item, total_pnl, max_pnl, actual_exit_status,
                                symbol_base, expiry, trade_start_time, trade_end_time,
                                trade_reason, structure, mode=trade_mode
                            )
                        except Exception as rec_err:
                            logger.error(f"[{trade_id}] Error recording trade: {rec_err}")
                    else:
                        logger.info(f"[{trade_id}] Recovered position exited - skipping CSV logging to avoid duplicate entries")
                else:
                    # EOD or manual exit - no square off needed
                    actual_exit_status = exit_status
                    trade_end_time = datetime.now().strftime("%H:%M:%S")
                    trade_mode = trade_info.get("mode")
                    # Skip CSV logging for recovered positions (they're continuations of existing trades)
                    if not trade.get("is_recovered", False):
                        try:
                            record_trade(
                                buy_item, sell_item, total_pnl, max_pnl, actual_exit_status,
                                symbol_base, expiry, trade_start_time, trade_end_time,
                                trade_reason, structure, mode=trade_mode
                            )
                        except Exception as rec_err:
                            logger.error(f"[{trade_id}] Error recording trade: {rec_err}")
                    else:
                        logger.info(f"[{trade_id}] Recovered position exited - skipping CSV logging to avoid duplicate entries")

                trade_mode = trade_info.get("mode")
                if actual_exit_status in ("EXIT_MAX_LOSS", "EXIT_DAILY_MAX_LOSS"):
                    _monitoring_state.record_stop_loss(symbol_base, datetime.now(), mode=trade_mode)
                elif actual_exit_status in ("EXIT_TRAILING_PROFIT", "EXIT_MAX_PROFIT"):
                    _monitoring_state.record_profit_exit(symbol_base, datetime.now(), mode=trade_mode)

                # Record exit for cooldown tracking
                _monitoring_state.record_exit(symbol_base, actual_exit_status, datetime.now(), mode=trade_mode)

                _monitoring_state.remove_trade(trade_id)
                print(f"\n[+] Trade {trade_id} closed [{actual_exit_status}]: Final P&L = Rs.{total_pnl:+.2f}")
                logger.info(f"Trade {trade_id} closed [{actual_exit_status}]: Final P&L = Rs.{total_pnl:+.2f}")

            # Log aggregate summary if trades still active
            remaining = _monitoring_state.get_all_trades()
            if remaining:
                logger.info(f"[Consolidated Monitor] Active trades: {len(remaining)} | Aggregate P&L: Rs.{aggregate_pnl:+.2f}")

            wait_interval = min(5.0, default_check_interval) if eod_retry_pending else default_check_interval
            if not eod_retry_pending:
                for trade in remaining.values():
                    close_time = _trade_eod_time(trade, datetime.now())
                    close_hour, close_minute = map(int, close_time.split(":"))
                    close_dt = datetime.now().replace(
                        hour=close_hour, minute=close_minute, second=0, microsecond=0
                    )
                    seconds_until_close = (close_dt - datetime.now()).total_seconds()
                    if seconds_until_close > 0:
                        wait_interval = min(wait_interval, seconds_until_close)
            if _monitoring_state.wait_for_stop(wait_interval):
                break

        except Exception as e:
            logger.error(f"Error in consolidated monitor loop: {e}", exc_info=True)
            if _monitoring_state.wait_for_stop(min(5.0, default_check_interval)):
                break

    logger.info("Consolidated P&L monitor worker has stopped.")


def monitor_position_pnl(
    smart_api: SmartConnect,
    buy_item: Dict[str, Any],
    sell_item: Optional[Dict[str, Any]],
    exchange: str,
    max_loss: float,
    max_profit: float,
    profit_threshold: float,
    min_profit_step: float,
    symbol_base: str,
    expiry: str,
    trade_start_time: str,
    trade_reason: str,
    structure: str,
    check_interval: float = RISK_CONFIG["check_interval"],
    mode: Optional[str] = None,
):
    """
    Backward-compatible wrapper: registers trade into MonitoringStateManager
    and ensures the single consolidated monitoring thread is running.
    """
    mode_id = str(mode or "SINGLE").upper().replace("-", "_")
    trade_id = f"{symbol_base}_{mode_id}_{datetime.now().strftime('%Y%m%d_%H%M%S_%f')}"
    trade_data = {
        "trade_id": trade_id,
        "symbol_base": symbol_base,
        "mode": mode,
        "exchange": exchange,
        "structure": structure,
        "buy_leg": buy_item,
        "sell_leg": sell_item,
        "expiry": expiry,
        "trade_start_time": trade_start_time,
        "trade_reason": trade_reason,
        "max_loss": max_loss,
        "max_profit": max_profit,
        "profit_threshold": profit_threshold,
        "min_profit_step": min_profit_step,
        "max_pnl": 0.0,
        "locked_profit": profit_threshold,
        "consecutive_missing_checks": 0,
        "flat_confirmations": 0,
        "check_interval": check_interval,
    }
    _monitoring_state.set_smart_api(smart_api)
    _monitoring_state.add_trade(trade_id, trade_data)
    _monitoring_state.ensure_monitor_running(smart_api, check_interval=check_interval)


def get_account_margin(smart_api: SmartConnect) -> Dict[str, Any]:
    """
    Fetch current account margin / funds from Angel One via rmsLimit.
    Returns dictionary with net available margin, cash, and status.
    """
    try:
        with smart_api_lock:
            resp = smart_api.rmsLimit()
        if resp and resp.get("status") and resp.get("data"):
            data = resp["data"]
            net_avail = float(data.get("net", 0.0) or 0.0)
            avail_cash = float(data.get("availablecash", 0.0) or 0.0)
            collateral = float(data.get("collateral", 0.0) or 0.0)
            return {
                "success": True,
                "net": net_avail,
                "available_cash": avail_cash,
                "collateral": collateral,
                "raw": data,
            }
        return {"success": False, "error": resp.get("message", "Unknown RMS response") if resp else "Empty response"}
    except Exception as exc:
        logger.warning(f"Failed to fetch account margin via rmsLimit: {exc}")
        return {"success": False, "error": str(exc)}


def verify_margin_precaution(smart_api: SmartConnect, lots: int = 1, structure: str = "spread", symbol_base: str = "NIFTY") -> bool:
    """
    Margin Precaution: Ensure Angel One account margin supports simultaneous long options or dual spreads.
    Logs and displays available margin status.
    """
    print("\n" + "=" * 65)
    print("  MARGIN PRECAUTION CHECK (Simultaneous Dual Execution)")
    print("=" * 65)
    margin_info = get_account_margin(smart_api)
    if margin_info.get("success"):
        net = margin_info.get("net", 0.0)
        cash = margin_info.get("available_cash", 0.0)
        print(f"  [+] Available Net Margin : Rs.{net:,.2f}")
        print(f"  [+] Available Cash       : Rs.{cash:,.2f}")
        logger.info(f"Margin Precaution: Available Net Margin: Rs.{net:,.2f}, Available Cash: Rs.{cash:,.2f}")
        if net < 50000.0:
            print("  [!] WARNING: Available margin may be low for simultaneous dual execution.")
            print("      Ensure your account has sufficient margin to hold both legs simultaneously.")
            logger.warning(f"Available margin is low (Rs.{net:,.2f}) for simultaneous dual execution.")
        else:
            print("  [✓] Margin check OK for simultaneous dual execution.")
    else:
        err = margin_info.get("error", "Could not query margin")
        print(f"  [!] Note: Could not query Angel One RMS margin directly: {err}")
        print("      Please ensure your account supports simultaneous dual positions (long options / spreads).")
        logger.info(f"RMS margin query note: {err}")
    print("=" * 65 + "\n")
    return True


def execute_daybreakout_spread(
    spread_plan: Dict[str, Any],
    smart_api: Optional[SmartConnect] = None,
    max_loss: float = RISK_CONFIG["max_loss"],
    max_profit: float = RISK_CONFIG["max_profit"],
    profit_threshold: float = RISK_CONFIG["profit_threshold"],
    min_profit_step: float = RISK_CONFIG["min_profit_step"],
    check_interval: float = RISK_CONFIG["check_interval"],
    check_existing: bool = True,
    trade_reason: str = "",
    x_factor: float = RISK_CONFIG.get("x_factor", 1.0),
    mode: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Executes a Day Breakout trade (Spread or Naked option) with consolidated parallel P&L monitoring:
    - Enforces MODE-SCOPED TRADE GATING: allows parallel REVERSE and NO-REVERSE trades
    - Specific leg token gating: checks collisions only for target contract tokens
    - BUY leg entered first; SELL leg entered second (if Spread structure)
    - Registers trade in MonitoringStateManager with consolidated P&L monitoring
    - Auto exit at max_loss (-5000), trailing min profit, or max_profit (50000)
    - Returns immediately to allow 15-minute monitor to continue
    - x_factor: Multiplier for position sizing and risk parameters (1x = base, 2x = double, etc.)
    """
    if smart_api is None:
        smart_api = login_smart_api()

    mode = mode or spread_plan.get("mode") or _resolve_mode()
    mode_tag = f" [{mode}]" if mode else ""

    # Apply x-factor to risk parameters
    if x_factor != 1.0:
        scaled_config = apply_x_factor(x_factor)
        max_loss = scaled_config["max_loss"]
        max_profit = scaled_config["max_profit"]
        profit_threshold = scaled_config["profit_threshold"]
        min_profit_step = scaled_config["min_profit_step"]
        daily_max_profit = scaled_config["daily_max_profit"]
        daily_max_loss = scaled_config["daily_max_loss"]
        fixed_loss_amount = scaled_config["fixed_loss_amount"]
        
        # Scale the plan and each executable leg. The leg dictionaries are
        # separate objects, so scaling only the plan summary leaves the order
        # quantity at its unscaled value.
        base_lots = int(spread_plan.get("lots", 0))
        scaled_lots = max(1, int(base_lots * x_factor))
        spread_plan["lots"] = scaled_lots
        spread_plan["quantity"] = int(spread_plan.get("quantity", 0) * x_factor)
        for leg_key in ("buy_leg", "sell_leg"):
            leg = spread_plan.get(leg_key)
            if not leg:
                continue
            leg_base_lots = int(leg.get("lots", base_lots))
            leg_scaled_lots = max(1, int(leg_base_lots * x_factor))
            leg["lots"] = leg_scaled_lots
            lot_size = int(leg.get("lot_size", spread_plan.get("lot_size", 0)) or 0)
            if lot_size > 0:
                leg["quantity"] = leg_scaled_lots * lot_size
            else:
                leg["quantity"] = int(leg.get("quantity", 0) * x_factor)
        
        print(f"[*] X-Factor Applied: {x_factor}x (Base values scaled)")
        print(f"    Scaled Max Loss: Rs.{max_loss:.0f} | Scaled Max Profit: Rs.{max_profit:.0f}")
        print(f"    Scaled Lots: {spread_plan['lots']} (Base: {base_lots})")
    else:
        daily_max_profit = RISK_CONFIG.get("daily_max_profit", 25000.0)
        daily_max_loss = RISK_CONFIG["daily_max_loss"]
        fixed_loss_amount = RISK_CONFIG.get("fixed_loss_amount", 9000.0)

    symbol_base = spread_plan["symbol_base"]
    exchange = spread_plan["exchange"]
    buy_item = spread_plan["buy_leg"]
    sell_item = spread_plan.get("sell_leg")
    lots = spread_plan["lots"]
    expiry = spread_plan["expiry"]
    strategy_name = spread_plan["strategy_name"]
    structure = spread_plan.get("structure", "SPREAD").upper()

    # 1. Check for existing open positions - MODE-SCOPED TRADE GATING
    if check_existing:
        if _monitoring_state.has_active_trade(symbol_base=symbol_base, mode=mode):
            msg = f"Active trade already exists in monitoring state for {symbol_base}{mode_tag}. Only one trade allowed at a time per mode."
            print(f"\n[! RISK GUARD] {msg}")
            logger.warning(f"Trade entry blocked for {symbol_base}{mode_tag}: {msg}")
            return {"status": "POSITION_EXISTS", "message": msg}

        # Check broker open positions for specific contract/token collisions
        open_pos = get_open_positions(smart_api, symbol_filter=symbol_base)
        if open_pos:
            target_symbols = {buy_item["symbol"]}
            target_tokens = {str(buy_item.get("token", ""))}
            if sell_item and sell_item.get("symbol"):
                target_symbols.add(sell_item["symbol"])
                target_tokens.add(str(sell_item.get("token", "")))

            conflicting_pos = [
                pos for pos in open_pos
                if (pos.get("tradingsymbol") in target_symbols or str(pos.get("symboltoken", "")) in target_tokens)
                and int(pos.get("netqty", 0)) != 0
            ]
            if conflicting_pos:
                msg = f"Broker reports existing open positions for target contracts of {symbol_base}{mode_tag}: {[p.get('tradingsymbol') for p in conflicting_pos]}. Only one trade allowed per contract."
                print(f"\n[! RISK GUARD] {msg}")
                for pos in conflicting_pos:
                    print(f"   * {pos.get('tradingsymbol')} | Net Qty: {pos.get('netqty')} | P&L: Rs.{pos.get('pnl', '0.00')}")
                logger.warning(f"Trade entry blocked for {symbol_base}{mode_tag}: {msg}")
                return {"status": "POSITION_EXISTS", "message": msg}

    # 2. Check Daily Risk Limits (Mode-Scoped)
    daily_trades, daily_loss = get_daily_trade_stats(symbol_base=symbol_base, mode=mode)
    daily_profit = get_daily_profit_stats(symbol_base=symbol_base, mode=mode)
    print(f"\n[*] Trading Summary{mode_tag}: {daily_trades}/{RISK_CONFIG['max_daily_trades']} trades triggered | Daily Loss: Rs.{daily_loss:.2f} | Net P&L: Rs.{daily_profit:.2f}")
    logger.info(f"Trading Summary{mode_tag}: {daily_trades}/{RISK_CONFIG['max_daily_trades']} trades triggered | Daily Loss: Rs.{daily_loss:.2f} | Net P&L: Rs.{daily_profit:.2f}")

    if daily_trades >= RISK_CONFIG["max_daily_trades"]:
        msg = f"Daily maximum trades limit reached for {symbol_base}{mode_tag} ({daily_trades}/{RISK_CONFIG['max_daily_trades']}). Aborting execution."
        print(f"\n[! RISK LIMIT] {msg}")
        logger.warning(f"Daily maximum trades limit reached for {symbol_base}{mode_tag} ({daily_trades}/{RISK_CONFIG['max_daily_trades']}) - Aborting execution")
        return {"status": "DAILY_MAX_TRADES_EXCEEDED", "message": msg}

    if daily_loss <= daily_max_loss:
        msg = f"Daily maximum loss limit reached for {symbol_base}{mode_tag} (Cumulative Loss: Rs.{daily_loss:.2f} <= Rs.{daily_max_loss:.2f}). Aborting execution."
        print(f"\n[! RISK LIMIT] {msg}")
        logger.warning(f"Daily maximum loss limit reached for {symbol_base}{mode_tag} (Cumulative Loss: Rs.{daily_loss:.2f} <= Rs.{daily_max_loss:.2f}) - Aborting execution")
        return {"status": "DAILY_MAX_LOSS_EXCEEDED", "message": msg}

    if daily_profit >= daily_max_profit or daily_loss >= daily_max_profit:
        msg = f"Daily maximum profit target reached for {symbol_base}{mode_tag} (Net P&L: Rs.{daily_profit:.2f} >= Rs.{daily_max_profit:.2f}). Aborting execution for today."
        print(f"\n[! TARGET REACHED] {msg}")
        logger.info(msg)
        return {"status": "DAILY_MAX_PROFIT_EXCEEDED", "message": msg}

    # Stop-Loss Cooldown check (wait 45-60 min after SL, mode-scoped)
    sl_cooldown_min = RISK_CONFIG.get("sl_cooldown_minutes", 45)
    last_sl_time = get_last_stop_loss_time(symbol_base=symbol_base, mode=mode)
    if last_sl_time:
        elapsed_min = (datetime.now() - last_sl_time).total_seconds() / 60.0
        if elapsed_min < sl_cooldown_min:
            remaining_min = sl_cooldown_min - elapsed_min
            msg = f"Stop-loss cooldown active for {symbol_base}{mode_tag} ({remaining_min:.0f}m remaining of {sl_cooldown_min}m cooldown). Aborting execution."
            print(f"\n[! COOLDOWN] {msg}")
            logger.warning(msg)
            return {"status": "SL_COOLDOWN_ACTIVE", "message": msg}

    # Consecutive loss circuit breaker (mode-scoped)
    max_cons_losses = RISK_CONFIG.get("max_consecutive_losses", 3)
    curr_cons = _monitoring_state.get_consecutive_losses(symbol_base, mode=mode)
    if curr_cons >= max_cons_losses:
        msg = f"Consecutive loss limit reached for {symbol_base}{mode_tag} ({curr_cons}/{max_cons_losses} losses). Aborting execution."
        print(f"\n[! RISK LIMIT] {msg}")
        logger.warning(msg)
        return {"status": "CONSECUTIVE_LOSS_LIMIT_REACHED", "message": msg}

    # Exit-type specific cooldown check (mode-scoped)
    reentry_allowed, cooldown_msg = _monitoring_state.is_reentry_allowed(symbol_base=symbol_base, mode=mode)
    if not reentry_allowed:
        msg = f"Re-entry blocked for {symbol_base}{mode_tag}: {cooldown_msg}"
        print(f"\n[! COOLDOWN] {msg}")
        logger.warning(msg)
        return {"status": "EXIT_COOLDOWN_ACTIVE", "message": msg}

    # Reset any prior flatten completion state so new trade can be monitored and squared off
    flatten.reset(symbol_base, mode=mode)

    # 3. Enter Positions - Use atomic spread execution for better reliability
    print(f"\n[+] Executing {strategy_name} [{structure}] ({lots} lots = {buy_item['quantity']} qty, Expiry: {expiry})...")
    print(f"    Trade Reason: {trade_reason}")
    print(f"    Leg 1: BUY  {buy_item['symbol']} (Token: {buy_item['token']})")
    if sell_item:
        print(f"    Leg 2: SELL {sell_item['symbol']} (Token: {sell_item['token']})")
    logger.info(f"Executing {strategy_name} [{structure}] - Lots: {lots}, Expiry: {expiry}, Reason: {trade_reason}")

    # Include the mode and microseconds so simultaneous dual-mode entries
    # cannot collide in order tracking or the consolidated P&L monitor.
    mode_id = str(mode or "SINGLE").upper().replace("-", "_")
    trade_id = f"{symbol_base}_{mode_id}_{datetime.now().strftime('%Y%m%d_%H%M%S_%f')}"

    # Get config values for order retry and polling
    enable_retry = RISK_CONFIG.get("enable_order_retry", False)
    max_retry_attempts = RISK_CONFIG.get("order_retry_max_attempts", 3)
    retry_delay = RISK_CONFIG.get("order_retry_initial_delay", 180.0)
    enable_polling = RISK_CONFIG.get("enable_order_polling", False)
    poll_interval = RISK_CONFIG.get("order_poll_interval", 15.0)
    poll_max_time = RISK_CONFIG.get("order_poll_max_time", 120.0)

    trade_start_time = None
    monitor_thread = None

    def register_profit_monitor():
        """Register the filled position before optional follow-up work can fail."""
        nonlocal trade_start_time, monitor_thread
        trade_start_time = datetime.now().strftime("%H:%M:%S")
        buy_item["start_time"] = trade_start_time
        if sell_item:
            sell_item["start_time"] = trade_start_time
        trade_data = {
            "trade_id": trade_id,
            "symbol_base": symbol_base,
            "mode": mode,
            "exchange": exchange,
            "structure": structure,
            "buy_leg": buy_item,
            "sell_leg": sell_item,
            "lots": lots,
            "quantity": buy_item.get("quantity", 0),
            "expiry": expiry,
            "strategy_name": strategy_name,
            "trade_reason": trade_reason,
            "trade_start_time": trade_start_time,
            "max_loss": max_loss,
            "max_profit": max_profit,
            "daily_max_loss": daily_max_loss,
            "profit_threshold": profit_threshold,
            "min_profit_step": min_profit_step,
            "max_pnl": 0.0,
            "locked_profit": profit_threshold,
            "consecutive_missing_checks": 0,
            "flat_confirmations": 0,
            "check_interval": check_interval,
        }
        _monitoring_state.set_smart_api(smart_api)
        if not _monitoring_state.add_trade(trade_id, trade_data):
            logger.critical(f"Could not register filled trade {trade_id} for P&L monitoring")
            raise RuntimeError(f"Filled trade {trade_id} was not registered for P&L monitoring")
        monitor_thread = _monitoring_state.ensure_monitor_running(smart_api, check_interval=check_interval)
        logger.info(f"Registered trade {trade_id} with Consolidated P&L Monitor. Active trades: {len(_monitoring_state.get_all_trades())}")
        print(f"\n[*] Registered trade {trade_id} with Consolidated P&L Monitor ({check_interval:.1f}s check interval).")
        print(f"    Trailing exit when profit crosses Rs.{profit_threshold:.0f} (Step: Rs.{min_profit_step:.0f})")
        print(f"    Stop Loss: Rs.{max_loss:.0f} | Target: Rs.{max_profit:.0f}")

    # Use atomic spread execution for spreads, enhanced order placement for naked
    if sell_item and structure == "SPREAD":
        execution_result = execute_spread_atomically(
            smart_api=smart_api,
            buy_leg=buy_item,
            sell_leg=sell_item,
            exchange=exchange,
            trade_id=trade_id,
            symbol_base=symbol_base,
            enable_retry=enable_retry,
            enable_polling=enable_polling
        )

        if not execution_result['success']:
            # A failed/ambiguous order result does not prove the broker has no
            # position. Reconcile the exact contracts before returning; if any
            # residual fill remains, register only the actual signed quantities
            # so the P&L worker can manage and close them.
            positions = get_open_positions(smart_api, symbol_filter=symbol_base)
            if positions is None:
                logger.critical(
                    f"Could not reconcile broker positions after failed spread entry {trade_id}; "
                    "manual broker check is required"
                )
                return {
                    "status": "POSITION_RECONCILIATION_FAILED",
                    "response": execution_result,
                    "trade_id": trade_id,
                }
            by_symbol = {
                p.get("tradingsymbol"): p for p in positions
                if int(p.get("netqty", 0) or 0) != 0
            }
            residual_legs = []
            for requested_leg in (buy_item, sell_item):
                if not requested_leg:
                    continue
                position = by_symbol.get(requested_leg.get("symbol"))
                if not position:
                    continue
                netqty = int(position.get("netqty", 0) or 0)
                actual_leg = dict(requested_leg)
                actual_leg["quantity"] = abs(netqty)
                actual_leg["lots"] = actual_leg["quantity"] // max(1, int(actual_leg.get("lot_size", 1) or 1))
                side_average = position.get("buyavgprice", 0.0) if netqty > 0 else position.get("sellavgprice", 0.0)
                actual_leg["entry_price"] = float(
                    position.get("averageprice", 0.0)
                    or side_average
                    or requested_leg.get("entry_price", 0.0)
                )
                actual_leg["position_side"] = "BUY" if netqty > 0 else "SELL"
                residual_legs.append(actual_leg)

            if residual_legs:
                buy_item = residual_legs[0]
                sell_item = residual_legs[1] if len(residual_legs) > 1 else None
                structure = "SPREAD" if sell_item else "NAKED"
                logger.critical(
                    f"Residual broker fill remains after failed spread entry {trade_id}; "
                    f"registering {len(residual_legs)} actual position(s) for monitoring"
                )
                register_profit_monitor()
                return {
                    "status": "RESIDUAL_POSITION_MONITORED",
                    "trade_id": trade_id,
                    "response": execution_result,
                    "monitor_thread": monitor_thread,
                }

            if execution_result['final_positions']['status'] == 'BUY_FAILED':
                return {"status": "BUY_ORDER_FAILED", "response": execution_result['buy_result']}
            elif execution_result['final_positions']['status'] == 'SELL_FAILED_ROLLED_BACK':
                return {"status": "SELL_ORDER_FAILED", "response": execution_result['sell_result'], "rollback": execution_result['rollback_performed']}
            else:
                return {"status": "EXECUTION_FAILED", "response": execution_result}

        logger.info(f"Atomic spread execution successful: {execution_result['final_positions']['status']}")
        register_profit_monitor()

    else:
        # Naked option or non-spread structure - use enhanced order placement
        buy_result = place_order_with_validation(
            smart_api=smart_api,
            trading_symbol=buy_item["symbol"],
            symbol_token=buy_item["token"],
            transaction_type="BUY",
            quantity=buy_item["quantity"],
            exchange=exchange,
            order_type_label="ENTRY_BUY",
            trade_id=trade_id,
            symbol_base=symbol_base,
            enable_retry=enable_retry,
            enable_polling=enable_polling
        )

        if not buy_result['success']:
            print("[!] BUY leg order failed. Aborting execution - monitoring will NOT start.")
            logger.error("BUY leg order failed - Aborting execution")
            return {"status": "BUY_ORDER_FAILED", "response": buy_result}

        buy_item["entry_price"] = buy_result['execution_price']
        buy_item["order_id"] = buy_result['order_id']
        entry_price = buy_item.get("entry_price")
        if entry_price is None:
            raise RuntimeError(f"Filled order {buy_item['order_id']} has no execution price; cannot start P&L monitoring safely")
        register_profit_monitor()

        # Broker-side fixed monetary loss SL order
        # trigger = entry_price - (fixed_loss_amount / quantity)
        if RISK_CONFIG.get("enable_fixed_loss_sl", False):
            fixed_loss = fixed_loss_amount
            quantity = buy_item["quantity"]
            trigger_price = entry_price - (fixed_loss / quantity)

            if trigger_price > 0:
                sl_order_id = place_stop_loss_order(
                    smart_api=smart_api,
                    trading_symbol=buy_item["symbol"],
                    symbol_token=buy_item["token"],
                    transaction_type="SELL",
                    quantity=quantity,
                    trigger_price=trigger_price,
                    exchange=exchange,
                    product_type="INTRADAY"
                )

                if sl_order_id:
                    buy_item["sl_order_id"] = sl_order_id
                    buy_item["sl_trigger_price"] = trigger_price
                    print(f"    [SL] Fixed Loss SL Order Placed: Trigger @ Rs.{trigger_price:.2f} (Fixed Loss: Rs.{fixed_loss:.0f})")
                    logger.info(f"Broker-side fixed loss SL order placed for {buy_item['symbol']} at trigger Rs.{trigger_price:.2f} (Loss: Rs.{fixed_loss:.0f})")
                else:
                    print(f"    [!] Failed to place fixed loss SL order - monitoring will handle risk management")
                    logger.warning(f"Failed to place broker-side fixed loss SL order for {buy_item['symbol']}")
            else:
                print(f"    [!] Fixed Loss SL skipped - Trigger price would be negative (Rs.{trigger_price:.2f})")
                logger.warning(f"Fixed loss SL skipped for {buy_item['symbol']} - trigger price would be negative (Rs.{trigger_price:.2f})")
    # 3b. Safe logging and recording
    try:
        print(f"\n[+] Order(s) executed successfully!")
        print(f"    BUY  : {buy_item['symbol']} @ Rs.{buy_item['entry_price']:.2f} (Order ID: {buy_item['order_id']})")
        if sell_item and sell_item.get('entry_price'):
            print(f"    SELL : {sell_item['symbol']} @ Rs.{sell_item['entry_price']:.2f} (Order ID: {sell_item['order_id']})")
        sell_log_str = f"{sell_item['symbol']} @ Rs.{sell_item['entry_price']:.2f}" if sell_item and sell_item.get('entry_price') else "N/A"
        logger.info(f"Order(s) executed successfully - BUY: {buy_item['symbol']} @ Rs.{buy_item['entry_price']:.2f}, SELL: {sell_log_str}")
    except Exception as log_err:
        logger.error(f"Non-critical error printing/logging order details: {log_err}")

    try:
        record_triggered_trade(
            symbol_base=symbol_base,
            structure=structure,
            lots=lots,
            quantity=buy_item.get("quantity", 0),
            trade_reason=trade_reason,
            buy_symbol=buy_item.get("symbol", ""),
            sell_symbol=sell_item.get("symbol", "N/A") if sell_item else "N/A",
            mode=mode or "LIVE"
        )
    except Exception as trig_err:
        logger.error(f"Non-critical error recording triggered trade: {trig_err}")

    # The monitoring thread is registered immediately after entry fill above.

    # Return immediately to allow 15-minute monitor to continue
    return {
        "status": "SUCCESS",
        "trade_id": trade_id,
        "direction": spread_plan.get("direction", ""),
        "trade_type": "BREAKOUT",
        "structure": structure.upper(),
        "trade_reason": trade_reason,
        "spread_plan": spread_plan,
        "monitor_thread": monitor_thread,
    }


def recover_existing_positions(
    smart_api: SmartConnect,
    symbol_base: str = "NIFTY",
    check_interval: float = RISK_CONFIG["check_interval"],
    max_loss: float = -5000.0,
    max_profit: float = 50000.0,
    profit_threshold: float = 3000.0,
    min_profit_step: float = 2000.0,
    daily_max_loss: float = RISK_CONFIG["daily_max_loss"],
) -> int:
    """
    Detects existing open positions and registers them in the monitoring state.
    This allows the consolidated P&L monitor to track positions that were created
    before the current strategy session started (e.g., after a restart).

    Returns the number of positions recovered.
    """
    try:
        open_pos = get_open_positions(smart_api, symbol_filter=symbol_base)
        if open_pos is None:
            logger.error(f"Could not reconcile open {symbol_base} positions for monitoring")
            return -1
        if not open_pos:
            logger.info(f"No existing positions found for {symbol_base}")
            return 0

        already_tracked_symbols = {
            leg.get("symbol")
            for trade in _monitoring_state.get_all_trades().values()
            for leg in (trade.get("buy_leg"), trade.get("sell_leg"))
            if leg and leg.get("symbol")
        }

        # Group positions by likely trades (matching expiries and symbols)
        position_groups = defaultdict(list)
        
        for pos in open_pos:
            tradingsymbol = pos.get("tradingsymbol", "")
            # Extract expiry from symbol (assuming format like NIFTY24JAN... or similar)
            # Group by the base symbol pattern to identify related legs
            position_groups[tradingsymbol[:10]].append(pos)  # Simple grouping by first 10 chars

        recovered_count = 0
        
        for group_key, positions in position_groups.items():
            if len(positions) == 0:
                continue
            
            # Try to construct a trade data structure from the positions
            # For simplicity, we'll treat each position as a separate trade
            # In a more sophisticated version, we could identify spreads
            
            for pos in positions:
                tradingsymbol = pos.get("tradingsymbol", "")
                if tradingsymbol in already_tracked_symbols:
                    continue
                netqty = int(pos.get("netqty", 0))
                avg_price = float(pos.get("averageprice", 0))
                broker_pnl = float(pos.get("pnl", 0))  # Get current P&L from broker
                
                if netqty == 0:
                    continue
                
                # Determine if this is a BUY or SELL leg
                transaction_type = "BUY" if netqty > 0 else "SELL"
                quantity = abs(netqty)
                
                # If avg_price is 0 or missing, try to calculate it from broker P&L
                if avg_price <= 0 and broker_pnl != 0:
                    current_ltp_for_calc = get_ltp(smart_api, pos.get("symboltoken", ""), tradingsymbol, pos.get("exchange", "NFO"))
                    if current_ltp_for_calc > 0:
                        if transaction_type == "BUY":
                            # P&L = (LTP - Entry) * Qty
                            # Entry = LTP - (P&L / Qty)
                            avg_price = current_ltp_for_calc - (broker_pnl / quantity)
                        else:  # SELL
                            # P&L = (Entry - LTP) * Qty
                            # Entry = LTP + (P&L / Qty)
                            avg_price = current_ltp_for_calc + (broker_pnl / quantity)
                        logger.info(f"Calculated entry price from broker P&L: Rs.{avg_price:.2f} (Broker P&L: Rs.{broker_pnl:.2f}, LTP: Rs.{current_ltp_for_calc:.2f})")
                    else:
                        logger.warning(f"Could not calculate entry price - avg_price is 0 and LTP fetch failed. P&L tracking will be inaccurate.")
                
                # Create a trade entry for monitoring
                trade_id = f"{symbol_base}_RECOVERED_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{recovered_count}"
                
                # Determine structure (simplified - assume SPREAD if multiple positions, else NAKED)
                structure = "SPREAD" if len(positions) > 1 else "NAKED"
                
                # Create leg data
                # Extract strike from symbol (e.g., SENSEX26O0172800PE -> 72800)
                strike = None
                try:
                    # Look for the numeric strike in the symbol (usually before CE/PE)
                    strike_match = re.search(r'(\d{4,5})(CE|PE)', tradingsymbol.upper())
                    if strike_match:
                        strike = int(strike_match.group(1))
                except:
                    pass
                
                # Calculate current P&L for the recovered position
                current_ltp = get_ltp(smart_api, pos.get("symboltoken", ""), tradingsymbol, pos.get("exchange", "NFO"), last_price=avg_price)
                current_pnl = 0.0
                
                # Use broker P&L if available and avg_price calculation succeeded
                if broker_pnl != 0 and avg_price > 0:
                    current_pnl = broker_pnl
                    logger.info(f"Using broker P&L for recovered position: Rs.{current_pnl:+.2f}")
                elif current_ltp > 0 and avg_price > 0:
                    # Calculate from LTP and entry price
                    if transaction_type == "BUY":
                        current_pnl = (current_ltp - avg_price) * quantity
                    else:  # SELL
                        current_pnl = (avg_price - current_ltp) * quantity
                    logger.info(f"Calculated P&L from LTP: Rs.{current_ltp:.2f}, Entry: Rs.{avg_price:.2f}, Current P&L: Rs.{current_pnl:+.2f}")
                else:
                    logger.warning(f"Could not determine initial P&L for recovered position {tradingsymbol}. P&L tracking will start from 0.")
                
                leg_item = {
                    "symbol": tradingsymbol,
                    "token": pos.get("symboltoken", ""),
                    "quantity": quantity,
                    "entry_price": avg_price,
                    "start_time": datetime.now().strftime("%H:%M:%S"),
                    "strike": strike,
                    "exchange": pos.get("exchange", "NFO"),
                    # Recovered positions can be short; the monitor derives the
                    # P&L sign from position_side, so record the broker side.
                    "position_side": "BUY" if transaction_type == "BUY" else "SELL",
                }
                
                # Determine direction based on whether it's CE or PE
                is_ce = "CE" in tradingsymbol.upper()
                direction = "BULLISH" if is_ce else "BEARISH"
                
                # Resolve lot size from current instrument contract data rather
                # than assuming a fixed size for NIFTY or SENSEX.
                try:
                    from live.daybreakout.contract_finder import get_instrument_lot_size
                    lot_size = get_instrument_lot_size(
                        symbol_base,
                        pos.get("exchange", "NFO"),
                    )
                except Exception as lot_error:
                    logger.warning(
                        f"Could not resolve lot size for recovered {symbol_base} position: {lot_error}"
                    )
                    lot_size = None
                
                # For recovered positions, if already in profit, set max_pnl to current P&L
                # so trailing profit logic works correctly from the start
                initial_max_pnl = max(0.0, current_pnl)
                initial_locked_profit = profit_threshold
                if initial_max_pnl >= profit_threshold:
                    # Already above threshold - calculate locked profit
                    initial_locked_profit = ((initial_max_pnl - profit_threshold) // min_profit_step) * min_profit_step + profit_threshold
                
                trade_data = {
                    "trade_id": trade_id,
                    "symbol_base": symbol_base,
                    "exchange": pos.get("exchange", "NFO"),
                    "structure": structure,
                    # buy_leg is the monitor's primary leg; the actual broker
                    # side is carried in position_side so short positions are
                    # evaluated with the correct P&L sign.
                    "buy_leg": leg_item,
                    "sell_leg": None,
                    "lots": quantity // lot_size if lot_size else None,
                    "quantity": quantity,
                    "expiry": "RECOVERED",  # Unknown expiry for recovered positions
                    "strategy_name": "RECOVERED_POSITION",
                    "trade_reason": "Position recovered from previous session",
                    "trade_start_time": datetime.now().strftime("%H:%M:%S"),
                    "max_loss": max_loss,
                    "max_profit": max_profit,
                    "daily_max_loss": daily_max_loss,
                    "profit_threshold": profit_threshold,
                    "min_profit_step": min_profit_step,
                    "max_pnl": initial_max_pnl,
                    "locked_profit": initial_locked_profit,
                    "consecutive_missing_checks": 0,
                    "flat_confirmations": 0,
                    "check_interval": check_interval,
                    "is_recovered": True,  # Flag to use broker P&L directly
                }
                
                # Register the recovered trade
                _monitoring_state.set_smart_api(smart_api)
                if _monitoring_state.add_trade(trade_id, trade_data):
                    recovered_count += 1
                    pnl_str = f" | Current P&L: Rs.{current_pnl:+.2f}" if current_ltp > 0 else ""
                    logger.info(f"Recovered position {tradingsymbol} ({transaction_type} {quantity} @ Rs.{avg_price:.2f}){pnl_str} for monitoring")
                    print(f"\n[+] Recovered existing position: {tradingsymbol} ({transaction_type} {quantity} @ Rs.{avg_price:.2f}){pnl_str}")
        
        if recovered_count > 0:
            # Start the monitor thread if not already running
            monitor_thread = _monitoring_state.ensure_monitor_running(smart_api, check_interval=check_interval)
            print(f"\n[*] Registered {recovered_count} recovered position(s) with Consolidated P&L Monitor")
            print(f"    Active trades currently monitored: {len(_monitoring_state.get_all_trades())}\n")
            logger.info(f"Registered {recovered_count} recovered positions with Consolidated P&L Monitor")
        else:
            print(f"\n[*] No positions could be recovered for monitoring")
            logger.info("No positions could be recovered for monitoring")
        
        return recovered_count
        
    except Exception as e:
        logger.error(f"Error during position recovery: {e}", exc_info=True)
        print(f"\n[!] Error during position recovery: {e}")
        return 0

