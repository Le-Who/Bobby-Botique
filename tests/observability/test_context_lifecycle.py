from __future__ import annotations

import contextvars

from app.observability.context import (
    current_context,
    export_job_context,
    request_scope,
    restore_job_context,
    span_scope,
)


def test_nested_request_scope_restores_every_field():
    """A nested request must not leak its actor or trace into its parent."""
    empty = current_context()
    with request_scope(request_id="1" * 32, user_id=42, chat_id=84) as outer:
        with request_scope(request_id="2" * 32, user_id=7, chat_id=8):
            inner = current_context()
            assert inner.request_id == "2" * 32
            assert inner.user_id == 7
        assert current_context() == outer
    assert current_context() == empty


def test_span_scope_uses_hex_id_and_restores_parent():
    """A child span must carry an explicit parent and leave no residue after exit."""
    with request_scope(request_id="a" * 32, user_id=42) as request_context:
        with span_scope("provider.request") as child:
            assert child.trace_id == request_context.trace_id
            assert child.parent_span_id == request_context.span_id
            assert len(child.span_id or "") == 16
            assert all(char in "0123456789abcdef" for char in child.span_id or "")
        assert current_context() == request_context


def test_job_context_round_trip_links_origin_and_resets():
    """A durable job must preserve its origin while receiving a new execution identity."""
    with request_scope(request_id="a" * 32, user_id=42, chat_id=84):
        portable = export_job_context()

    with restore_job_context(portable, task_id="synthetic-job", execution_id="b" * 32) as restored:
        assert restored.request_id == "a" * 32
        assert restored.user_id == 42
        assert restored.chat_id == 84
        assert restored.task_id == "synthetic-job"
        assert restored.execution_id == "b" * 32
        assert restored.parent_span_id == portable["span_id"]

    assert current_context().task_id is None


def test_generated_request_and_trace_ids_are_opaque_hex():
    with request_scope() as context:
        assert len(context.request_id or "") == 32
        assert len(context.trace_id or "") == 32
        assert all(char in "0123456789abcdef" for char in context.request_id or "")
        assert all(char in "0123456789abcdef" for char in context.trace_id or "")


def test_child_span_exposes_operation_and_parent_link():
    with request_scope(request_id="a" * 32):
        with span_scope("provider.request") as child:
            assert child.operation == "provider.request"
            assert child.parent_span_id is not None


def test_linked_request_uses_origin_trace_without_reusing_http_request_id():
    with request_scope(request_id="h" * 32) as http_context:
        origin_trace_id = http_context.trace_id
        origin_span_id = http_context.span_id

    with request_scope(trace_id=origin_trace_id, parent_span_id=origin_span_id) as update_context:
        assert update_context.request_id != "h" * 32
        assert update_context.trace_id == origin_trace_id
        assert update_context.parent_span_id == origin_span_id


def test_request_scope_can_be_finalized_by_framework_cleanup_context():
    """ASGI teardown may close a request scope from a copied async Context."""
    origin = contextvars.Context()
    cleanup = contextvars.Context()
    scope = request_scope(request_id="f" * 32)

    origin.run(scope.__enter__)
    cleanup.run(scope.__exit__, None, None, None)

    assert current_context().request_id is None
