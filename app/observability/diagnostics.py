"""Time-bounded, selector-scoped diagnostic logging policy."""

from __future__ import annotations

import threading
from datetime import UTC, datetime

from app.observability.config import LoggingSettings
from app.observability.context import LogContext, current_context
from app.observability.events import emit

_STATE_LOCK = threading.Lock()
_ANNOUNCED_ACTIVE: tuple[object, ...] | None = None
_ANNOUNCED_EXPIRED: tuple[object, ...] | None = None


def _signature(settings: LoggingSettings) -> tuple[object, ...]:
    return (
        settings.diagnostic_incident_id,
        settings.diagnostic_subsystem,
        settings.diagnostic_until,
        settings.diagnostic_request_id,
        settings.diagnostic_user_id,
    )


def _has_target_selector(settings: LoggingSettings) -> bool:
    return bool(settings.diagnostic_request_id or settings.diagnostic_user_id)


def announce_diagnostic_state(
    settings: LoggingSettings,
    *,
    now: datetime | None = None,
) -> None:
    """Emit activation/expiry once per configured diagnostic scope."""
    global _ANNOUNCED_ACTIVE, _ANNOUNCED_EXPIRED

    current = (now or datetime.now(UTC)).astimezone(UTC)
    signature = _signature(settings)
    complete = bool(
        settings.diagnostic_incident_id
        and settings.diagnostic_subsystem
        and settings.diagnostic_until
        and _has_target_selector(settings)
    )
    event_name: str | None = None
    with _STATE_LOCK:
        if settings.diagnostic_active and signature != _ANNOUNCED_ACTIVE:
            _ANNOUNCED_ACTIVE = signature
            event_name = "diagnostic.enabled"
        elif (
            complete
            and settings.diagnostic_until is not None
            and current >= settings.diagnostic_until
            and signature != _ANNOUNCED_EXPIRED
        ):
            _ANNOUNCED_EXPIRED = signature
            event_name = "diagnostic.expired"

    if event_name is not None:
        emit(
            event_name,
            level="warning",
            operation="logging.diagnostic_scope",
            incident_id=settings.diagnostic_incident_id,
            subsystem=settings.diagnostic_subsystem,
            target_request_id=settings.diagnostic_request_id,
            target_user_id=settings.diagnostic_user_id,
            diagnostic_until=(
                settings.diagnostic_until.isoformat().replace("+00:00", "Z") if settings.diagnostic_until else None
            ),
        )


def diagnostic_scope_matches(
    settings: LoggingSettings,
    context: LogContext,
    *,
    subsystem: str,
) -> bool:
    """Return true only when all configured selectors match the current event."""
    if settings.content_mode != "preview" or not settings.diagnostic_active:
        return False
    if settings.diagnostic_subsystem != subsystem:
        return False
    if settings.diagnostic_request_id and settings.diagnostic_request_id != context.request_id:
        return False
    if settings.diagnostic_user_id is not None and settings.diagnostic_user_id != context.user_id:
        return False
    return _has_target_selector(settings)


def diagnostic_preview_allowed(*, subsystem: str) -> bool:
    """Resolve current configuration and context at event time, including expiry."""
    settings = LoggingSettings.from_environ()
    announce_diagnostic_state(settings)
    return diagnostic_scope_matches(settings, current_context(), subsystem=subsystem)


__all__ = [
    "announce_diagnostic_state",
    "diagnostic_preview_allowed",
    "diagnostic_scope_matches",
]
