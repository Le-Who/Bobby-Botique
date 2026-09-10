from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from app.errors import ErrorCode
from app.observability.delivery_events import delivery_outcome_fields
from app.providers.stream_types import (
    FailurePhase,
    FinishReason,
    GroundingReport,
    KeyDisposition,
    ProviderKind,
    RetryDisposition,
    RouteUsed,
    StreamCompleted,
    StreamFailed,
    TokenUsage,
)
from app.response_delivery.outcomes import CompleteDelivery, DeferredDelivery, FailedDelivery, PartialDelivery
from app.response_delivery.renderer import DeliveryKind, DeliveryReceipt, TelegramMessageRef


def _receipt(kind: DeliveryKind = DeliveryKind.MESSAGE) -> DeliveryReceipt:
    return DeliveryReceipt(
        kind=kind,
        message_ids=(10, 11),
        final_message=TelegramMessageRef(chat_id=42, message_id=11),
        publication_url="https://telegra.ph/private-id",
    )


def _completed() -> StreamCompleted:
    return StreamCompleted(
        finish_reason=FinishReason.from_raw("STOP"),
        usage=TokenUsage(total=7),
        grounding=GroundingReport(),
        route=RouteUsed(ProviderKind.GEMINI, "requested", "actual"),
    )


def test_delivery_outcomes_have_distinct_terminal_semantics_without_public_url():
    failed_terminal = StreamFailed(
        code=ErrorCode.TIMEOUT,
        phase=FailurePhase.AFTER_TEXT,
        retry=RetryDisposition.RETRY_LATER,
        key=KeyDisposition.TRANSIENT_FAILURE,
        diagnostic="timeout",
        error_id="e" * 32,
    )
    outcomes = [
        CompleteDelivery("content", "shown", _completed(), False, _receipt()),
        PartialDelivery("content", "shown", failed_terminal, False, _receipt(DeliveryKind.SPLIT)),
        FailedDelivery(ErrorCode.INVALID_REQUEST, "notice", _receipt(DeliveryKind.FAILURE), upstream_error_id="u" * 32),
        DeferredDelivery("task-1", "later", _receipt(DeliveryKind.DEFERRED)),
    ]

    fields = [delivery_outcome_fields(outcome) for outcome in outcomes]

    assert [item["delivery_status"] for item in fields] == ["sent", "partial", "failure_notice_sent", "deferred"]
    assert fields[1]["upstream_error_id"] == "e" * 32
    assert fields[2]["upstream_error_id"] == "u" * 32
    assert fields[3]["task_id"] == "task-1"
    assert all("publication_url" not in item for item in fields)
    assert all(item["publication_present"] is True for item in fields)


@pytest.mark.asyncio
async def test_delivery_facade_emits_exactly_one_terminal_event(monkeypatch):
    from app.response_delivery import delivery as delivery_module

    outcome = FailedDelivery(ErrorCode.TIMEOUT, "notice", _receipt(DeliveryKind.FAILURE))
    facade = delivery_module.TelegramResponseDelivery()
    facade._deliver_impl = AsyncMock(return_value=outcome)
    captured: list[tuple[str, dict]] = []
    monkeypatch.setattr(delivery_module, "emit", lambda event, **fields: captured.append((event, fields)))

    result = await facade.deliver(None, None, presentation=None)

    assert result is outcome
    assert [name for name, _ in captured] == ["delivery.started", "delivery.finished"]
    assert captured[-1][1]["delivery_status"] == "failure_notice_sent"


@pytest.mark.asyncio
async def test_delivery_transport_exception_gets_separate_error_id(monkeypatch):
    from app.response_delivery import delivery as delivery_module

    facade = delivery_module.TelegramResponseDelivery()
    error = RuntimeError("synthetic transport error")
    facade._deliver_impl = AsyncMock(side_effect=error)
    captured: list[tuple[str, dict]] = []
    monkeypatch.setattr(delivery_module, "emit", lambda event, **fields: captured.append((event, fields)))
    monkeypatch.setattr(delivery_module, "record_exception", lambda *args, **kwargs: "t" * 32)

    with pytest.raises(RuntimeError, match="synthetic transport error"):
        await facade.deliver(None, None, presentation=None)

    assert captured[-1][0] == "delivery.finished"
    assert captured[-1][1]["delivery_status"] == "transport_failed"
    assert captured[-1][1]["error_id"] == "t" * 32
