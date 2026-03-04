import contextvars
import uuid
from collections.abc import Iterator
from contextlib import contextmanager

from app.request_context import get_request_id

_trace_id_var: contextvars.ContextVar[str | None] = contextvars.ContextVar("trace_id", default=None)
_span_id_var: contextvars.ContextVar[str | None] = contextvars.ContextVar("span_id", default=None)


def get_trace_context() -> dict[str, str | None]:
    return {
        "request_id": get_request_id(),
        "trace_id": _trace_id_var.get(),
        "span_id": _span_id_var.get(),
    }


@contextmanager
def bind_request_span(request_id: str | None = None, span_name: str = "request") -> Iterator[dict[str, str | None]]:
    """Bind a lightweight trace/span context that is correlated with request_id.

    Contract:
    - request_id: primary correlation id propagated through handlers/web
    - trace_id: defaults to request_id, fallback random hex
    - span_id: per-scope random short id
    """
    rid = request_id or get_request_id()
    trace_id = rid or uuid.uuid4().hex
    span_id = f"{span_name}-{uuid.uuid4().hex[:8]}"

    trace_token = _trace_id_var.set(trace_id)
    span_token = _span_id_var.set(span_id)
    try:
        yield {
            "request_id": rid,
            "trace_id": trace_id,
            "span_id": span_id,
        }
    finally:
        _trace_id_var.reset(trace_token)
        _span_id_var.reset(span_token)
