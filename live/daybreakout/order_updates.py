"""Shared Angel One order-update WebSocket with a small thread-safe event cache."""

import json
import threading
from typing import Any, Dict, Optional

import websocket

from live.daybreakout.logger import logger


class OrderUpdateStream:
    """Own one order-update connection and make broker events available by order ID."""

    URI = "wss://tns.angelone.in/smart-order-update"

    def __init__(self, auth_token: str, api_key: str, client_code: str, feed_token: str):
        # The order-update endpoint expects the JWT using the standard Bearer
        # authorization scheme. SmartConnect versions differ on whether the
        # returned access token already includes that scheme.
        auth_token = str(auth_token or "").strip()
        if auth_token and not auth_token.lower().startswith("bearer "):
            auth_token = f"Bearer {auth_token}"
        self.headers = {
            "Authorization": auth_token,
            "x-api-key": api_key,
            "x-client-code": client_code,
            "x-feed-token": feed_token,
        }
        self._lock = threading.Lock()
        self._events: Dict[str, Dict[str, Any]] = {}
        self._versions: Dict[str, int] = {}
        self._changed = threading.Condition(self._lock)
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._ws = None

    @staticmethod
    def _decode(message: Any) -> Any:
        if isinstance(message, bytes):
            message = message.decode("utf-8", errors="replace")
        if isinstance(message, str):
            try:
                return json.loads(message)
            except (TypeError, ValueError):
                return None
        return message

    @classmethod
    def _flatten(cls, payload: Any):
        """Yield dict records from common Angel payload wrappers and JSON arrays."""
        payload = cls._decode(payload)
        if isinstance(payload, list):
            for item in payload:
                yield from cls._flatten(item)
        elif isinstance(payload, dict):
            yield payload
            for key in ("data", "orderData", "orderUpdate", "payload"):
                nested = payload.get(key)
                if isinstance(nested, (dict, list)):
                    yield from cls._flatten(nested)

    def _on_message(self, _ws, message):
        found = False
        updated_order_ids = set()
        for record in self._flatten(message):
            order_id = record.get("orderid", record.get("orderId", record.get("order_id")))
            if not order_id or str(order_id) in updated_order_ids:
                continue
            updated_order_ids.add(str(order_id))
            with self._changed:
                self._events[str(order_id)] = dict(record)
                self._versions[str(order_id)] = self._versions.get(str(order_id), 0) + 1
                self._changed.notify_all()
            found = True
        if found:
            logger.info("Received Angel order update")
        else:
            logger.debug("Received non-order WebSocket message")

    def _run(self):
        delay = 2
        while not self._stop.is_set():
            self._ws = websocket.WebSocketApp(
                self.URI,
                header=self.headers,
                on_message=self._on_message,
                on_open=lambda _ws: logger.info("Angel order-update WebSocket connected"),
                on_error=lambda _ws, error: logger.warning(f"Angel order-update WebSocket error: {error}"),
                on_close=lambda *_args: logger.warning("Angel order-update WebSocket disconnected"),
            )
            try:
                self._ws.run_forever(ping_interval=10, ping_timeout=5)
            except Exception as exc:
                logger.warning(f"Angel order-update WebSocket failed: {exc}")
            self._ws = None
            if not self._stop.is_set():
                self._stop.wait(delay)
                delay = min(delay * 2, 30)

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._run, name="AngelOrderUpdates", daemon=True)
        self._thread.start()

    def latest(self, order_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            event = self._events.get(str(order_id))
            return dict(event) if event else None

    def snapshot(self, order_id: str):
        with self._lock:
            key = str(order_id)
            event = self._events.get(key)
            return self._versions.get(key, 0), dict(event) if event else None

    def wait_next(self, order_id: str, after_version: int, timeout: float):
        """Wait for a new event; returns its version and payload."""
        key = str(order_id)
        with self._changed:
            self._changed.wait_for(
                lambda: self._versions.get(key, 0) > after_version or self._stop.is_set(),
                timeout=max(0, timeout),
            )
            event = self._events.get(key)
            return self._versions.get(key, 0), dict(event) if event else None

    def close(self, timeout: float = 5.0):
        self._stop.set()
        with self._changed:
            self._changed.notify_all()
        ws = self._ws
        if ws is not None:
            try:
                ws.close()
            except Exception:
                pass
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=timeout)
