"""Structured API request lifecycle events with monotonic timing."""

from __future__ import annotations

import logging
import time
import uuid
from dataclasses import dataclass
from typing import Any

from app.observability.redaction import provider_key_fields, sanitize_event
from app.observability.schema import serialize_exception

_PROTECTED_FIELDS = {
    "schema_version",
    "timestamp",
    "level",
    "event",
    "event_id",
    "service",
    "environment",
    "release",
    "instance_id",
    "hostname",
    "logger",
    "source",
    "request_id",
    "trace_id",
    "span_id",
    "parent_span_id",
    "user_id",
    "chat_id",
    "attempt_id",
    "api",
}
_MISSING = object()


@dataclass(frozen=True, slots=True)
class APICallTiming:
    """Opaque request timing handle; it never retains the raw provider key."""

    started_ns: int
    attempt_id: str
    key_present: bool | None = None
    key_suffix: str | None = None
    key_fingerprint: str | None = None

    def key_fields(self) -> dict[str, bool | str | None]:
        if self.key_present is None:
            return {}
        return {
            "key_present": self.key_present,
            "key_suffix": self.key_suffix,
            "key_fingerprint": self.key_fingerprint,
        }


class APILogger:
    """Compatibility facade that emits searchable structured lifecycle events."""

    def __init__(self) -> None:
        self.logger = logging.getLogger("api_logger")
        self.logger.setLevel(logging.INFO)

    @staticmethod
    def _safe_fields(api: str, fields: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
        raw_key = fields.pop("api_key", _MISSING)
        key_hash = fields.pop("key_hash", None)
        for protected in _PROTECTED_FIELDS:
            fields.pop(protected, None)

        identity: dict[str, Any] = {}
        if raw_key is not _MISSING:
            identity = provider_key_fields(
                api,
                raw_key if isinstance(raw_key, str) else None,
                key_hash=key_hash if isinstance(key_hash, str) else None,
            )
        sanitized = sanitize_event({**fields, **identity})
        safe_identity = {key: sanitized.pop(key) for key in identity}
        return sanitized, safe_identity

    def log_request(self, api: str, **fields: Any) -> APICallTiming:
        """Log request start and return an opaque monotonic timing handle."""
        safe_fields, identity = self._safe_fields(api, dict(fields))
        timing = APICallTiming(
            started_ns=time.monotonic_ns(),
            attempt_id=uuid.uuid4().hex,
            key_present=identity.get("key_present"),
            key_suffix=identity.get("key_suffix"),
            key_fingerprint=identity.get("key_fingerprint"),
        )
        self.logger.info(
            "API request started",
            extra={
                "_event_name": "api.request_started",
                "operation": "api.request",
                "api": api,
                "attempt_id": timing.attempt_id,
                **safe_fields,
                **identity,
            },
            stacklevel=2,
        )
        return timing

    def log_response(
        self,
        api: str,
        start_time: APICallTiming | float,
        *,
        success: bool = True,
        error_message: str | None = None,
        **fields: Any,
    ) -> float:
        """Log a terminal request event and return elapsed seconds."""
        safe_fields, explicit_identity = self._safe_fields(api, dict(fields))
        sanitized_error = sanitize_event({"error_message": error_message})["error_message"]
        legacy_timing = not isinstance(start_time, APICallTiming)
        if isinstance(start_time, APICallTiming):
            duration = max(0.0, (time.monotonic_ns() - start_time.started_ns) / 1_000_000_000)
            attempt_id = start_time.attempt_id
            identity = {**start_time.key_fields(), **explicit_identity}
        else:
            valid_epoch = isinstance(start_time, (int, float)) and start_time > 0
            duration = max(0.0, time.time() - float(start_time)) if valid_epoch else 0.0
            attempt_id = uuid.uuid4().hex
            identity = explicit_identity

        extra = {
            "_event_name": "api.request_finished",
            "operation": "api.request",
            "api": api,
            "attempt_id": attempt_id,
            "duration_ms": round(duration * 1000, 2),
            "outcome": "succeeded" if success else "failed",
            "error_message": sanitized_error,
            "legacy_timing": legacy_timing,
            **safe_fields,
            **identity,
        }
        log_method = self.logger.info if success else self.logger.error
        log_method(
            "API request completed" if success else "API request failed",
            extra=extra,
            stacklevel=2,
        )
        return duration

    def log_error(
        self,
        api: str,
        error: Exception,
        context: dict[str, Any] | None = None,
    ) -> str:
        """Log one exception with a stable ID and a real traceback tuple."""
        safe_fields, identity = self._safe_fields(api, dict(context or {}))
        error_id = uuid.uuid4().hex
        self.logger.error(
            "API request failed",
            extra={
                "_event_name": "api.request_failed",
                "operation": "api.request",
                "api": api,
                "error_id": error_id,
                "_exception_snapshot": serialize_exception(error, include_message=False),
                **safe_fields,
                **identity,
            },
            stacklevel=2,
        )
        return error_id


api_logger = APILogger()
