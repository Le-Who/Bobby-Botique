"""Public event emission helpers."""

from __future__ import annotations

import logging
import re
import uuid
from collections.abc import Mapping

from app.observability.redaction import sanitize_event
from app.observability.schema import JsonValue, serialize_exception

type Level = str

_EVENT_NAME = re.compile(r"^[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)+$")
_LEVELS = {
    "debug": logging.DEBUG,
    "info": logging.INFO,
    "warning": logging.WARNING,
    "error": logging.ERROR,
    "critical": logging.CRITICAL,
}
_LOGGER = logging.getLogger("app.observability")


def _validate_event_name(event: str) -> None:
    if not _EVENT_NAME.fullmatch(event):
        raise ValueError(f"Invalid observability event name: {event!r}")


def emit(
    event: str,
    **fields: JsonValue,
) -> None:
    """Emit one stable domain event through the shared logging pipeline."""
    _validate_event_name(event)
    level_value = fields.pop("level", "info")
    level = level_value if isinstance(level_value, str) else ""
    message_value = fields.pop("message", "")
    message = message_value if isinstance(message_value, str) else ""
    numeric_level = _LEVELS.get(level)
    if numeric_level is None:
        raise ValueError(f"Invalid observability level: {level!r}")
    safe_fields = sanitize_event(fields)
    safe_message_value = sanitize_event({"message": message or event})["message"]
    safe_message = safe_message_value if isinstance(safe_message_value, str) else event
    try:
        from app.observability.metrics import operational_metrics

        operational_metrics.observe(event, safe_fields)
    except Exception:
        # Observability must not change business behavior, including on metrics bugs.
        pass
    _LOGGER.log(
        numeric_level,
        safe_message,
        extra={"_event_name": event, **safe_fields},
        stacklevel=2,
    )


def record_exception(
    event: str,
    error: BaseException,
    *,
    operation: str,
    level: Level = "error",
    fields: Mapping[str, JsonValue] | None = None,
) -> str:
    """Emit an exception with a stable error ID, even outside an active except block."""
    _validate_event_name(event)
    numeric_level = _LEVELS.get(level)
    if numeric_level is None:
        raise ValueError(f"Invalid observability level: {level!r}")
    error_id = uuid.uuid4().hex
    safe_fields = sanitize_event(dict(fields or {}))
    extra: dict[str, JsonValue] = {
        "_event_name": event,
        "operation": operation,
        "error_id": error_id,
        "_exception_snapshot": serialize_exception(error, include_message=False),
        **safe_fields,
    }
    try:
        from app.observability.metrics import operational_metrics

        operational_metrics.observe(event, extra)
    except Exception:
        pass
    _LOGGER.log(
        numeric_level,
        f"{type(error).__name__} in {operation}",
        extra=extra,
        stacklevel=2,
    )
    return error_id
