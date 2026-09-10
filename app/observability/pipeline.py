"""Shared processors for structlog and standard-library logging records."""

from __future__ import annotations

import json
import logging
import os
import socket
import uuid
from collections.abc import Mapping, MutableMapping
from typing import Any

from app.observability.redaction import sanitize_event
from app.observability.schema import (
    JsonValue,
    exception_from_log_value,
    json_compatible_mapping,
    repository_path,
    serialize_exception,
    utc_timestamp,
)
from app.request_context import get_chat_id, get_request_id, get_user_id
from app.tracing import get_trace_context

_INSTANCE_ID = uuid.uuid4().hex
_RESERVED_FIELDS = {
    "schema_version",
    "timestamp",
    "level",
    "event",
    "message",
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
    "task_id",
    "execution_id",
    "client_request_id",
    "exception",
}
_PROCESSOR_FIELDS = {
    "_record",
    "_from_structlog",
    "exc_info",
    "stack_info",
    "pathname",
    "filename",
    "func_name",
    "lineno",
    "logger",
    "level",
    "timestamp",
}
_ROOT_CAUSE_FIELDS = (
    "schema_version",
    "timestamp",
    "level",
    "event",
    "message",
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
    "task_id",
    "execution_id",
    "operation",
    "attempt_id",
    "race_id",
    "provider",
    "requested_model",
    "actual_model",
    "key_present",
    "key_suffix",
    "key_fingerprint",
    "outcome",
    "reason_code",
    "failure_phase",
    "retry_disposition",
    "key_disposition",
    "error_id",
    "duration_ms",
    "exception",
)


def _source(record: logging.LogRecord | None, fields: Mapping[str, Any]) -> dict[str, JsonValue]:
    if record is not None:
        return {
            "file": repository_path(record.pathname),
            "function": record.funcName,
            "line": record.lineno,
        }
    pathname = fields.get("pathname") or fields.get("filename") or "unknown"
    line = fields.get("lineno")
    return {
        "file": repository_path(str(pathname)),
        "function": str(fields.get("func_name") or "unknown"),
        "line": line if isinstance(line, int) else 0,
    }


def normalize_event(logger: object, method_name: str, event_dict: MutableMapping[str, Any]) -> dict[str, JsonValue]:
    """Build the same versioned envelope for structlog and foreign stdlib records."""
    record_value = event_dict.get("_record")
    record = record_value if isinstance(record_value, logging.LogRecord) else None
    message_value = event_dict.get("event", "")
    message = message_value if isinstance(message_value, str) else str(message_value)
    event_name = event_dict.get("_event_name")
    if not isinstance(event_name, str) or not event_name:
        event_name = "legacy.log"

    trace = get_trace_context()
    logger_name = record.name if record is not None else getattr(logger, "name", None)
    if not isinstance(logger_name, str) or not logger_name:
        raw_logger = event_dict.get("logger")
        logger_name = raw_logger if isinstance(raw_logger, str) and raw_logger else "root"
    level = record.levelname.lower() if record is not None else method_name.lower()
    timestamp = utc_timestamp(record.created if record is not None else None)

    exception_value = event_dict.get("exc_info")
    if exception_value is None and record is not None:
        exception_value = record.exc_info
    error = exception_from_log_value(exception_value)
    exception_snapshot = event_dict.get("_exception_snapshot")

    extras = {
        key: value
        for key, value in event_dict.items()
        if key not in _RESERVED_FIELDS and key not in _PROCESSOR_FIELDS and not key.startswith("_")
    }
    normalized_extras = json_compatible_mapping(extras)
    collisions = sorted(
        key
        for key in event_dict
        if key in _RESERVED_FIELDS and key not in {"event", "message", "logger", "level", "timestamp", "exception"}
    )

    envelope: dict[str, JsonValue] = {
        "schema_version": 1,
        "timestamp": timestamp,
        "level": level,
        "event": event_name,
        "message": message,
        "event_id": uuid.uuid4().hex,
        "service": os.environ.get("SERVICE_NAME", "gemaibotv2"),
        "environment": os.environ.get("APP_ENV", "unknown"),
        "release": os.environ.get("APP_RELEASE", "unknown"),
        "instance_id": _INSTANCE_ID,
        "hostname": os.environ.get("HOSTNAME", socket.gethostname()),
        "logger": logger_name,
        "source": _source(record, event_dict),
        "request_id": get_request_id(),
        "trace_id": trace["trace_id"],
        "span_id": trace["span_id"],
        "parent_span_id": trace["parent_span_id"],
        "user_id": get_user_id(),
        "chat_id": get_chat_id(),
        "task_id": trace["task_id"],
        "execution_id": trace["execution_id"],
        "operation": trace["operation"],
        "client_request_id": trace["client_request_id"],
        **normalized_extras,
    }
    if collisions:
        collision_values: list[JsonValue] = list(collisions)
        envelope["reserved_field_collisions"] = collision_values
    if isinstance(exception_snapshot, Mapping):
        envelope["exception"] = json_compatible_mapping(exception_snapshot)
    elif error is not None:
        envelope["exception"] = serialize_exception(error)
    return sanitize_event(envelope)


def remove_processor_meta(
    _logger: object,
    _method_name: str,
    event_dict: MutableMapping[str, Any],
) -> MutableMapping[str, Any]:
    """Remove ProcessorFormatter metadata when present on either logging path."""
    event_dict.pop("_record", None)
    event_dict.pop("_from_structlog", None)
    return event_dict


def _json_size(event: Mapping[str, Any]) -> int:
    return len(json.dumps(event, ensure_ascii=False, separators=(",", ":"), default=str).encode())


def enforce_event_budget(max_bytes: int):
    """Return a processor that preserves root-cause fields within a hard byte cap."""
    if max_bytes < 1024:
        raise ValueError("LOG_EVENT_MAX_BYTES must be at least 1024")

    def processor(
        _logger: object,
        _method_name: str,
        event_dict: MutableMapping[str, Any],
    ) -> MutableMapping[str, Any]:
        if _json_size(event_dict) <= max_bytes:
            return event_dict

        original_size = _json_size(event_dict)
        compact: dict[str, Any] = {
            key: event_dict[key] for key in _ROOT_CAUSE_FIELDS if key in event_dict and event_dict[key] is not None
        }
        compact["event_truncated"] = True
        compact["original_event_bytes"] = original_size

        exception = compact.get("exception")
        if isinstance(exception, dict):
            compact["exception"] = {
                key: exception[key]
                for key in ("type", "message", "stack", "cause", "context", "chain_truncated")
                if key in exception
            }

        for text_field in ("message",):
            value = compact.get(text_field)
            if isinstance(value, str) and len(value) > 256:
                compact[text_field] = f"{value[:256]}…[truncated]"

        while _json_size(compact) > max_bytes:
            exception = compact.get("exception")
            if isinstance(exception, dict) and exception.get("stack"):
                stack = exception["stack"]
                if isinstance(stack, list) and len(stack) > 1:
                    exception["stack"] = stack[-max(1, len(stack) // 2) :]
                    exception["stack_truncated"] = True
                    continue
            removable = next(
                (
                    key
                    for key in ("source", "hostname", "instance_id", "parent_span_id", "span_id", "trace_id")
                    if key in compact
                ),
                None,
            )
            if removable is not None:
                compact.pop(removable)
                continue
            message = compact.get("message")
            if isinstance(message, str) and len(message) > 32:
                compact["message"] = f"{message[:32]}…"
                continue
            break

        event_dict.clear()
        event_dict.update(compact)
        return event_dict

    return processor


def standalone_event_bytes(
    event: str,
    *,
    level: str,
    message: str,
    fields: Mapping[str, Any] | None = None,
) -> bytes:
    """Serialize a pipeline-internal event without recursively using logging."""
    envelope: dict[str, Any] = {
        "schema_version": 1,
        "timestamp": utc_timestamp(),
        "level": level,
        "event": event,
        "message": message,
        "event_id": uuid.uuid4().hex,
        "service": os.environ.get("SERVICE_NAME", "gemaibotv2"),
        "environment": os.environ.get("APP_ENV", "unknown"),
        "release": os.environ.get("APP_RELEASE", "unknown"),
        "instance_id": _INSTANCE_ID,
        "hostname": os.environ.get("HOSTNAME", socket.gethostname()),
        "logger": "app.observability.writer",
        "source": {"file": "app/observability/writer.py", "function": "_run", "line": 0},
        "request_id": None,
        "trace_id": None,
        "span_id": None,
        "parent_span_id": None,
        "user_id": None,
        "chat_id": None,
        **dict(fields or {}),
    }
    sanitized = sanitize_event(envelope)
    return json.dumps(sanitized, ensure_ascii=False, separators=(",", ":")).encode()
