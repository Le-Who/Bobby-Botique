from __future__ import annotations

import time

from app.observability.context import request_scope
from app.observability.ingress import WebhookOriginStore, sanitize_client_request_id


def test_client_request_id_is_metadata_not_a_server_correlation_id():
    assert sanitize_client_request_id("client-123") == "client-123"
    assert sanitize_client_request_id("bad\nheader") is None
    assert sanitize_client_request_id("x" * 65) is None


def test_webhook_origin_is_bounded_consumed_once_and_preserves_trace_link():
    store = WebhookOriginStore(capacity=2, ttl_seconds=300)

    with request_scope(request_id="a" * 32) as first:
        store.remember(1)
    with request_scope(request_id="b" * 32):
        store.remember(2)
    with request_scope(request_id="c" * 32):
        store.remember(3)

    assert store.consume(1) is None
    origin = store.consume(2)
    assert origin is not None
    assert origin["trace_id"] == "b" * 32
    assert origin["span_id"] is not None
    assert store.consume(2) is None
    assert first.trace_id == "a" * 32


def test_webhook_origin_expires_and_discard_is_explicit(monkeypatch):
    now = 100.0
    monkeypatch.setattr(time, "monotonic", lambda: now)
    store = WebhookOriginStore(capacity=2, ttl_seconds=5)

    with request_scope(request_id="a" * 32):
        store.remember(1)
        store.remember(2)
    now = 106.0
    assert store.consume(1) is None

    store.discard(2)
    assert store.consume(2) is None


async def test_successful_http_request_emits_canonical_terminal_outcome(monkeypatch):
    from app import web

    captured: list[tuple[str, dict[str, object]]] = []
    monkeypatch.setattr(web, "emit", lambda event, **fields: captured.append((event, fields)))
    monkeypatch.setattr(web.database, "is_database_connected", lambda: True)

    response = await web.quart_app.test_client().get("/health")

    assert response.status_code == 200
    terminal = [fields for event, fields in captured if event == "http.request_finished"]
    assert len(terminal) == 1
    assert terminal[0]["outcome"] == "succeeded"
