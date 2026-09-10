"""Single token-reset context store for requests, spans, and durable jobs."""

from __future__ import annotations

import contextvars
import uuid
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, replace
from typing import Any, cast

from app.observability.schema import JsonValue


@dataclass(frozen=True, slots=True)
class LogContext:
    request_id: str | None = None
    trace_id: str | None = None
    span_id: str | None = None
    parent_span_id: str | None = None
    user_id: int | None = None
    chat_id: int | None = None
    task_id: str | None = None
    execution_id: str | None = None
    operation: str | None = None
    client_request_id: str | None = None


_EMPTY_CONTEXT = LogContext()
_context_var: contextvars.ContextVar[LogContext] = contextvars.ContextVar(
    "observability_context",
    default=_EMPTY_CONTEXT,
)


def current_context() -> LogContext:
    return _context_var.get()


def replace_current_context(**changes: str | int | None) -> LogContext:
    """Compatibility mutation for legacy setter APIs within the current async context."""
    allowed = {"request_id", "user_id", "chat_id", "operation"}
    unknown = changes.keys() - allowed
    if unknown:
        raise TypeError(f"Unsupported context fields: {', '.join(sorted(unknown))}")
    updated = replace(current_context(), **cast(Any, changes))
    _context_var.set(updated)
    return updated


@contextmanager
def request_scope(
    *,
    request_id: str | None = None,
    trace_id: str | None = None,
    parent_span_id: str | None = None,
    user_id: int | None = None,
    chat_id: int | None = None,
    operation: str | None = None,
    client_request_id: str | None = None,
) -> Iterator[LogContext]:
    rid = request_id or uuid.uuid4().hex
    context = LogContext(
        request_id=rid,
        trace_id=trace_id or rid,
        span_id=uuid.uuid4().hex[:16],
        parent_span_id=parent_span_id,
        user_id=user_id,
        chat_id=chat_id,
        operation=operation,
        client_request_id=client_request_id,
    )
    previous = current_context()
    _context_var.set(context)
    try:
        yield context
    finally:
        _context_var.set(previous)


@contextmanager
def span_scope(
    name: str,
    *,
    request_id: str | None = None,
    **fields: JsonValue,
) -> Iterator[LogContext]:
    """Create a child span while preserving request/actor/job ownership."""
    del fields
    if not name.strip():
        raise ValueError("Span name must not be blank")
    parent = current_context()
    rid = request_id or parent.request_id
    trace_id = request_id or parent.trace_id or rid or uuid.uuid4().hex
    context = replace(
        parent,
        request_id=rid,
        trace_id=trace_id,
        parent_span_id=parent.span_id,
        span_id=uuid.uuid4().hex[:16],
        operation=name,
    )
    previous = current_context()
    _context_var.set(context)
    try:
        yield context
    finally:
        _context_var.set(previous)


def export_job_context() -> dict[str, JsonValue]:
    context = current_context()
    return {
        "schema_version": 1,
        "request_id": context.request_id,
        "trace_id": context.trace_id,
        "span_id": context.span_id,
        "user_id": context.user_id,
        "chat_id": context.chat_id,
        "operation": context.operation,
    }


def _optional_string(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _optional_int(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


@contextmanager
def restore_job_context(
    data: Mapping[str, object],
    *,
    task_id: str,
    execution_id: str,
) -> Iterator[LogContext]:
    """Restore only the allowlisted portable origin context for one job execution."""
    origin_span = _optional_string(data.get("span_id"))
    trace_id = _optional_string(data.get("trace_id")) or uuid.uuid4().hex
    context = LogContext(
        request_id=_optional_string(data.get("request_id")),
        trace_id=trace_id,
        span_id=uuid.uuid4().hex[:16],
        parent_span_id=origin_span,
        user_id=_optional_int(data.get("user_id")),
        chat_id=_optional_int(data.get("chat_id")),
        task_id=task_id,
        execution_id=execution_id,
        operation="job.execute",
    )
    previous = current_context()
    _context_var.set(context)
    try:
        yield context
    finally:
        _context_var.set(previous)
