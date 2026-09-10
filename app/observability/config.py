"""Logging configuration resolved without importing application settings."""

from __future__ import annotations

import logging
import os
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

_TRUE = {"1", "true", "yes", "on"}
_FALSE = {"0", "false", "no", "off"}
_LEVELS = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}


def _bool_value(value: str | None) -> bool | None:
    if value is None:
        return None
    normalized = value.strip().casefold()
    if normalized in _TRUE:
        return True
    if normalized in _FALSE:
        return False
    return None


def _positive_int(
    source: Mapping[str, str],
    name: str,
    default: int,
    invalid: list[str],
    *,
    minimum: int = 1,
    maximum: int | None = None,
) -> int:
    raw = source.get(name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError:
        invalid.append(name)
        return default
    if value < minimum or (maximum is not None and value > maximum):
        invalid.append(name)
        return default
    return value


def _parse_until(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(UTC)


@dataclass(frozen=True, slots=True)
class LoggingSettings:
    format: str
    level_name: str
    level: int
    service_name: str
    environment: str
    release: str
    content_mode: str
    event_max_bytes: int
    queue_max_events: int
    queue_max_bytes: int
    diagnostic_request_id: str | None
    diagnostic_user_id: int | None
    diagnostic_subsystem: str | None
    diagnostic_until: datetime | None
    diagnostic_incident_id: str | None
    diagnostic_key_suffix: bool
    diagnostic_active: bool
    invalid_parameters: tuple[str, ...] = ()
    conflicts: tuple[str, ...] = ()

    @classmethod
    def from_environ(cls) -> LoggingSettings:
        return cls.from_mapping(os.environ)

    @classmethod
    def from_mapping(
        cls,
        source: Mapping[str, str],
        *,
        now: datetime | None = None,
    ) -> LoggingSettings:
        invalid: list[str] = []
        conflicts: list[str] = []

        explicit_format = source.get("LOG_FORMAT")
        if explicit_format is not None:
            log_format = explicit_format.strip().casefold()
            if log_format not in {"json", "text"}:
                invalid.append("LOG_FORMAT")
                log_format = "json"
        else:
            structured = _bool_value(source.get("STRUCTURED_LOGGING"))
            pretty = _bool_value(source.get("LOG_PRETTY"))
            if source.get("STRUCTURED_LOGGING") is not None and structured is None:
                invalid.append("STRUCTURED_LOGGING")
            if source.get("LOG_PRETTY") is not None and pretty is None:
                invalid.append("LOG_PRETTY")
            log_format = "text" if pretty is True and structured is not True else "json"

        structured = _bool_value(source.get("STRUCTURED_LOGGING"))
        pretty = _bool_value(source.get("LOG_PRETTY"))
        if explicit_format is not None:
            if structured is not None and (structured != (log_format == "json")):
                conflicts.append("STRUCTURED_LOGGING")
            if pretty is True and log_format != "text":
                conflicts.append("LOG_PRETTY")

        level_name = source.get("LOG_LEVEL", "INFO").strip().upper()
        if level_name not in _LEVELS:
            invalid.append("LOG_LEVEL")
            level_name = "INFO"

        content_mode = source.get("LOG_CONTENT_MODE", "metadata").strip().casefold()
        if content_mode not in {"metadata", "preview"}:
            invalid.append("LOG_CONTENT_MODE")
            content_mode = "metadata"

        event_max_bytes = _positive_int(
            source,
            "LOG_EVENT_MAX_BYTES",
            32_768,
            invalid,
            minimum=1_024,
            maximum=262_144,
        )
        queue_max_events = _positive_int(source, "LOG_QUEUE_MAX_EVENTS", 4_096, invalid)
        queue_max_bytes = _positive_int(source, "LOG_QUEUE_MAX_BYTES", 8_388_608, invalid)

        diagnostic_user_id: int | None = None
        raw_user_id = source.get("LOG_DIAGNOSTIC_USER_ID")
        if raw_user_id:
            try:
                candidate = int(raw_user_id)
            except ValueError:
                candidate = 0
            if candidate > 0:
                diagnostic_user_id = candidate
            else:
                invalid.append("LOG_DIAGNOSTIC_USER_ID")

        diagnostic_request_id = source.get("LOG_DIAGNOSTIC_REQUEST_ID") or None
        diagnostic_subsystem = source.get("LOG_DIAGNOSTIC_SUBSYSTEM") or None
        diagnostic_incident_id = source.get("LOG_DIAGNOSTIC_INCIDENT_ID") or None
        diagnostic_until = _parse_until(source.get("LOG_DIAGNOSTIC_UNTIL"))
        key_suffix_value = _bool_value(source.get("LOG_DIAGNOSTIC_KEY_SUFFIX"))
        if source.get("LOG_DIAGNOSTIC_KEY_SUFFIX") is not None and key_suffix_value is None:
            invalid.append("LOG_DIAGNOSTIC_KEY_SUFFIX")
        diagnostic_key_suffix = key_suffix_value is True

        current = (now or datetime.now(UTC)).astimezone(UTC)
        complete_diagnostic = all((diagnostic_subsystem, diagnostic_incident_id, diagnostic_until))
        # A key suffix is emitted for every selected provider key by contract; it
        # is not a target selector and must never enable broad content previews.
        has_selector = bool(diagnostic_request_id or diagnostic_user_id)
        bounded_until = bool(diagnostic_until and current < diagnostic_until <= current + timedelta(minutes=15))
        diagnostic_active = complete_diagnostic and has_selector and bounded_until

        return cls(
            format=log_format,
            level_name=level_name,
            level=getattr(logging, level_name),
            service_name=source.get("SERVICE_NAME", "gemaibotv2") or "gemaibotv2",
            environment=source.get("APP_ENV", "unknown") or "unknown",
            release=source.get("APP_RELEASE", "unknown") or "unknown",
            content_mode=content_mode,
            event_max_bytes=event_max_bytes,
            queue_max_events=queue_max_events,
            queue_max_bytes=queue_max_bytes,
            diagnostic_request_id=diagnostic_request_id,
            diagnostic_user_id=diagnostic_user_id,
            diagnostic_subsystem=diagnostic_subsystem,
            diagnostic_until=diagnostic_until,
            diagnostic_incident_id=diagnostic_incident_id,
            diagnostic_key_suffix=diagnostic_key_suffix,
            diagnostic_active=diagnostic_active,
            invalid_parameters=tuple(dict.fromkeys(invalid)),
            conflicts=tuple(dict.fromkeys(conflicts)),
        )
