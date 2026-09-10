"""Versioned structured-log envelope and exception serialization."""

from __future__ import annotations

import hashlib
import traceback
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

type JsonValue = None | bool | int | float | str | list[JsonValue] | dict[str, JsonValue]

SCHEMA_VERSION = 1
MAX_EXCEPTION_FRAMES = 32
MAX_EXCEPTION_CHAIN = 8

_REPO_ROOT = Path(__file__).resolve().parents[2]


def utc_timestamp(epoch_seconds: float | None = None) -> str:
    """Return a millisecond-resolution RFC3339 UTC timestamp."""
    value = datetime.now(UTC) if epoch_seconds is None else datetime.fromtimestamp(epoch_seconds, UTC)
    return value.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def repository_path(pathname: str) -> str:
    """Prefer a stable repository-relative source path."""
    try:
        return Path(pathname).resolve().relative_to(_REPO_ROOT).as_posix()
    except OSError, ValueError:
        return Path(pathname).as_posix()


def _stack_frames(error: BaseException) -> list[JsonValue]:
    frames = traceback.extract_tb(error.__traceback__)[-MAX_EXCEPTION_FRAMES:]
    return [
        {
            "file": repository_path(frame.filename),
            "line": frame.lineno,
            "function": frame.name,
        }
        for frame in frames
    ]


def _exception_message(error: BaseException, *, include_message: bool) -> dict[str, JsonValue]:
    try:
        message = str(error)
    except Exception:
        return {"message": "[unavailable]"}
    if include_message:
        return {"message": message}
    return {
        "message": "[redacted]",
        "message_length": len(message),
        "message_fingerprint": hashlib.sha256(message.encode("utf-8")).hexdigest()[:16],
    }


def serialize_exception(
    error: BaseException,
    *,
    _remaining: int = MAX_EXCEPTION_CHAIN,
    include_message: bool = False,
) -> dict[str, JsonValue]:
    """Serialize actionable exception evidence without frame locals or live traceback objects."""
    result: dict[str, JsonValue] = {
        "type": type(error).__name__,
        "stack": _stack_frames(error),
        **_exception_message(error, include_message=include_message),
    }
    if _remaining <= 0:
        result["chain_truncated"] = True
        return result

    cause = error.__cause__
    if cause is not None:
        result["cause"] = serialize_exception(
            cause,
            _remaining=_remaining - 1,
            include_message=include_message,
        )
    elif error.__context__ is not None and not error.__suppress_context__:
        result["context"] = serialize_exception(
            error.__context__,
            _remaining=_remaining - 1,
            include_message=include_message,
        )

    if isinstance(error, BaseExceptionGroup):
        members = list(error.exceptions[:MAX_EXCEPTION_CHAIN])
        result["exceptions"] = [
            serialize_exception(
                item,
                _remaining=_remaining - 1,
                include_message=include_message,
            )
            for item in members
        ]
        if len(error.exceptions) > len(members):
            result["exceptions_truncated"] = len(error.exceptions) - len(members)
    return result


def exception_from_log_value(value: object) -> BaseException | None:
    """Extract an exception from stdlib/structlog ``exc_info`` values."""
    if isinstance(value, BaseException):
        return value
    if isinstance(value, tuple) and len(value) == 3 and isinstance(value[1], BaseException):
        return value[1]
    if value is True:
        import sys

        current = sys.exc_info()[1]
        return current if isinstance(current, BaseException) else None
    return None


def json_compatible_mapping(value: Mapping[str, Any]) -> dict[str, JsonValue]:
    """Convert ordinary structured fields; strict bounded conversion is added in Task 2."""
    result: dict[str, JsonValue] = {}
    for key, item in value.items():
        if item is None or isinstance(item, (bool, int, float, str)):
            result[str(key)] = item
        elif isinstance(item, list):
            result[str(key)] = [
                entry if isinstance(entry, (type(None), bool, int, float, str)) else str(entry) for entry in item
            ]
        elif isinstance(item, dict):
            result[str(key)] = json_compatible_mapping(item)
        else:
            result[str(key)] = str(item)
    return result
