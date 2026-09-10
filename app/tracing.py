from collections.abc import Iterator
from contextlib import contextmanager

from app.observability.context import current_context, span_scope
from app.request_context import get_request_id


def get_trace_context() -> dict[str, str | int | None]:
    context = current_context()
    return {
        "request_id": get_request_id(),
        "trace_id": context.trace_id,
        "span_id": context.span_id,
        "parent_span_id": context.parent_span_id,
        "task_id": context.task_id,
        "execution_id": context.execution_id,
        "operation": context.operation,
        "client_request_id": context.client_request_id,
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
    with span_scope(span_name, request_id=rid) as context:
        yield {
            "request_id": context.request_id,
            "trace_id": context.trace_id,
            "span_id": context.span_id,
            "parent_span_id": context.parent_span_id,
        }
