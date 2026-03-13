"""Tests for app.tracing — lightweight trace/span context management."""

from unittest.mock import patch

from app.tracing import bind_request_span, get_trace_context


class TestGetTraceContext:
    """get_trace_context returns the current contextvar state."""

    def test_default_context_has_none_values(self):
        ctx = get_trace_context()
        assert ctx["trace_id"] is None
        assert ctx["span_id"] is None

    def test_context_within_span(self):
        with bind_request_span(request_id="req-123", span_name="test"):
            ctx = get_trace_context()
            assert ctx["trace_id"] == "req-123"
            assert ctx["span_id"] is not None
            assert ctx["span_id"].startswith("test-")

    def test_context_reset_after_span(self):
        with bind_request_span(request_id="req-456"):
            pass
        ctx = get_trace_context()
        assert ctx["trace_id"] is None
        assert ctx["span_id"] is None


class TestBindRequestSpan:
    """bind_request_span should set and restore contextvars."""

    def test_yields_correct_context(self):
        with bind_request_span(request_id="abc", span_name="op") as ctx:
            assert ctx["request_id"] == "abc"
            assert ctx["trace_id"] == "abc"
            assert ctx["span_id"].startswith("op-")

    def test_generates_trace_id_when_no_request_id(self):
        with patch("app.tracing.get_request_id", return_value=None):
            with bind_request_span() as ctx:
                assert ctx["trace_id"] is not None
                assert len(ctx["trace_id"]) == 32  # uuid4 hex

    def test_nested_spans_independent(self):
        with bind_request_span(request_id="outer", span_name="a") as _outer_ctx:
            with bind_request_span(request_id="inner", span_name="b") as inner_ctx:
                assert inner_ctx["trace_id"] == "inner"
                assert inner_ctx["span_id"].startswith("b-")
            # After inner exits, outer should be restored
            restored = get_trace_context()
            assert restored["trace_id"] == "outer"

    def test_span_id_contains_name(self):
        with bind_request_span(span_name="my_operation") as ctx:
            assert "my_operation-" in ctx["span_id"]

    def test_span_id_unique_per_call(self):
        span_ids = set()
        for _ in range(10):
            with bind_request_span(span_name="x") as ctx:
                span_ids.add(ctx["span_id"])
        assert len(span_ids) == 10
