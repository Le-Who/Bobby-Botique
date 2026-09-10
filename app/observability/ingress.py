"""Bounded correlation helpers for HTTP and Telegram ingress boundaries."""

from __future__ import annotations

import threading
import time
from collections import OrderedDict
from dataclasses import dataclass

from app.observability.context import current_context
from app.observability.schema import JsonValue


def sanitize_client_request_id(value: str | None) -> str | None:
    """Keep an inbound request label as untrusted metadata, never as trace identity."""
    if not isinstance(value, str) or not value or len(value) > 64:
        return None
    if not value.isprintable() or any(ord(char) < 32 or ord(char) == 127 for char in value):
        return None
    return value


@dataclass(frozen=True, slots=True)
class _Origin:
    expires_at: float
    context: dict[str, JsonValue]


class WebhookOriginStore:
    """One-shot bounded update-id → HTTP origin links for the PTB queue handoff."""

    def __init__(self, *, capacity: int = 4096, ttl_seconds: float = 300.0) -> None:
        if capacity <= 0 or ttl_seconds <= 0:
            raise ValueError("Webhook origin limits must be positive")
        self._capacity = capacity
        self._ttl_seconds = ttl_seconds
        self._items: OrderedDict[int, _Origin] = OrderedDict()
        self._lock = threading.Lock()

    def remember(self, update_id: int) -> None:
        if not isinstance(update_id, int) or isinstance(update_id, bool):
            return
        context = current_context()
        portable: dict[str, JsonValue] = {
            "trace_id": context.trace_id,
            "span_id": context.span_id,
            "request_id": context.request_id,
        }
        now = time.monotonic()
        with self._lock:
            self._expire_locked(now)
            self._items.pop(update_id, None)
            self._items[update_id] = _Origin(now + self._ttl_seconds, portable)
            while len(self._items) > self._capacity:
                self._items.popitem(last=False)

    def consume(self, update_id: int) -> dict[str, JsonValue] | None:
        now = time.monotonic()
        with self._lock:
            self._expire_locked(now)
            origin = self._items.pop(update_id, None)
        return None if origin is None else dict(origin.context)

    def discard(self, update_id: int) -> None:
        with self._lock:
            self._items.pop(update_id, None)

    def _expire_locked(self, now: float) -> None:
        while self._items:
            update_id, origin = next(iter(self._items.items()))
            if origin.expires_at > now:
                break
            self._items.pop(update_id, None)


webhook_origins = WebhookOriginStore()

__all__ = ["WebhookOriginStore", "sanitize_client_request_id", "webhook_origins"]
