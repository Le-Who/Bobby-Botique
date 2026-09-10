"""Versioned structured-log envelope and exception serialization."""

from __future__ import annotations

import hashlib
import traceback
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

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
    """Convert built-in containers without invoking untrusted conversion hooks."""

    def safe_name(key: object) -> str:
        return key if isinstance(key, str) else f"<{type(key).__name__}>"

    def convert(item: object, *, depth: int = 0) -> JsonValue:
        if item is None or isinstance(item, (bool, int, float, str)):
            return item
        if isinstance(item, bytes):
            return {"type": "bytes", "size": len(item)}
        if depth >= 5:
            return "[max-depth]"
        if type(item) is dict:
            mapping = cast(dict[object, object], item)
            entries = list(mapping.items())[:32]
            mapping_result = {safe_name(key): convert(entry, depth=depth + 1) for key, entry in entries}
            if len(mapping) > len(entries):
                mapping_result["truncated_fields"] = len(mapping) - len(entries)
            return mapping_result
        if type(item) in (list, tuple, set, frozenset):
            collection = cast(list[object] | tuple[object, ...] | set[object] | frozenset[object], item)
            collection_entries = list(collection)[:32]
            list_result = [convert(entry, depth=depth + 1) for entry in collection_entries]
            if len(collection) > len(collection_entries):
                list_result.append(f"[truncated {len(collection) - len(collection_entries)} items]")
            return list_result
        return f"<{type(item).__name__}>"

    if type(value) is not dict:
        return {"value": f"<{type(value).__name__}>"}
    result: dict[str, JsonValue] = {}
    for key, item in list(value.items())[:32]:
        result[safe_name(key)] = convert(item)
    if len(value) > len(result):
        result["truncated_fields"] = len(value) - len(result)
    return result
