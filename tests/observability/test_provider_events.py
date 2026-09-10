from __future__ import annotations

import asyncio

import pytest

from app.errors import ErrorCode
from app.providers.stream_types import (
    FailurePhase,
    FinishReason,
    GroundingReport,
    KeyDisposition,
    RetryDisposition,
    RouteUsed,
    StreamCompleted,
    StreamFailed,
    TextDelta,
    TokenUsage,
)


def test_adapter_exception_helper_records_traceback_with_selected_key(monkeypatch):
    from app.observability import provider_events

    captured: list[tuple[str, BaseException, dict]] = []

    def capture_exception(event, error, **kwargs):
        captured.append((event, error, kwargs))
        return "adapter-error-id"

    monkeypatch.setattr(provider_events, "record_exception", capture_exception)
    error = RuntimeError("synthetic adapter failure")

    error_id = provider_events.record_provider_exception(
        error,
        provider="openrouter",
        model="synthetic-model",
        api_key="synthetic-key-9876",
        failure_phase="before_text",
    )

    assert error_id == "adapter-error-id"
    assert captured[0][0] == "provider.call_failed"
    assert captured[0][2]["fields"]["key_suffix"] == "9876"
    assert captured[0][2]["fields"]["failure_phase"] == "before_text"


@pytest.mark.asyncio
async def test_observed_stream_emits_started_first_text_and_typed_failure(monkeypatch):
    from app.observability import provider_events

    captured: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        provider_events,
        "emit",
        lambda event, **fields: captured.append((event, fields)),
    )

    async def source():
        yield TextDelta("hello")
        yield StreamFailed(
            code=ErrorCode.TIMEOUT,
            phase=FailurePhase.AFTER_TEXT,
            retry=RetryDisposition.RETRY_LATER,
            key=KeyDisposition.TRANSIENT_FAILURE,
            diagnostic="timed out after visible text",
        )

    secret = "synthetic-provider-key-ABCD"
    observed = [
        event
        async for event in provider_events.observe_provider_stream(
            source(),
            provider="gemini",
            requested_model="gemini-requested",
            actual_model="gemini-actual",
            api_key=secret,
            key_hash="f" * 64,
            attempt_number=2,
            max_attempts=3,
            race_id="race-1",
        )
    ]

    assert len(observed) == 2
    assert [name for name, _ in captured] == [
        "provider.attempt_started",
        "provider.first_text",
        "provider.attempt_finished",
    ]
    started = captured[0][1]
    finished = captured[-1][1]
    assert started["key_suffix"] == "ABCD"
    assert started["key_fingerprint"] == "f" * 16
    assert secret not in repr(captured)
    assert finished["attempt_id"] == started["attempt_id"]
    assert finished["outcome"] == "failed"
    assert finished["reason_code"] == ErrorCode.TIMEOUT.value
    assert finished["failure_phase"] == "after_text"
    assert finished["retry_disposition"] == "retry_later"
    assert finished["key_disposition"] == "transient_failure"


@pytest.mark.asyncio
async def test_observed_stream_records_exception_once_and_re_raises(monkeypatch):
    from app.observability import provider_events

    captured: list[tuple[str, dict]] = []
    exceptions: list[tuple[str, BaseException, dict]] = []
    monkeypatch.setattr(provider_events, "emit", lambda event, **fields: captured.append((event, fields)))

    def capture_exception(event, error, **kwargs):
        exceptions.append((event, error, kwargs))
        return "e" * 32

    monkeypatch.setattr(provider_events, "record_exception", capture_exception)

    async def source():
        if False:
            yield TextDelta("unreachable")
        raise RuntimeError("synthetic provider failure")

    with pytest.raises(RuntimeError, match="synthetic provider failure"):
        async for _ in provider_events.observe_provider_stream(
            source(),
            provider="openrouter",
            requested_model="requested",
            actual_model="actual",
            api_key="provider-key-WXYZ",
        ):
            pass

    assert len(exceptions) == 1
    assert exceptions[0][0] == "provider.attempt_failed"
    assert exceptions[0][2]["fields"]["key_suffix"] == "WXYZ"
    assert captured[-1][0] == "provider.attempt_finished"
    assert captured[-1][1]["error_id"] == "e" * 32
    assert captured[-1][1]["outcome"] == "failed"


@pytest.mark.asyncio
async def test_observed_stream_logs_race_loser_cancellation(monkeypatch):
    from app.observability import provider_events

    captured: list[tuple[str, dict]] = []
    monkeypatch.setattr(provider_events, "emit", lambda event, **fields: captured.append((event, fields)))

    async def source():
        await asyncio.sleep(60)
        yield TextDelta("late")

    async def consume() -> None:
        async for _ in provider_events.observe_provider_stream(
            source(),
            provider="gemini",
            requested_model="model",
            actual_model="model",
            api_key="provider-key-1234",
            race_id="race-cancel",
        ):
            pass

    task = asyncio.create_task(consume())
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert captured[-1][0] == "provider.attempt_finished"
    assert captured[-1][1]["outcome"] == "cancelled"
    assert captured[-1][1]["cancellation_reason"] == "race_loser_or_parent_cancelled"


@pytest.mark.asyncio
async def test_empty_completion_records_failed_attempt_terminal(monkeypatch):
    from app.observability import provider_events
    from app.providers.stream_types import ProviderKind

    captured: list[tuple[str, dict]] = []
    monkeypatch.setattr(provider_events, "emit", lambda event, **fields: captured.append((event, fields)))

    async def source():
        yield StreamCompleted(
            finish_reason=FinishReason.from_raw("stop"),
            usage=TokenUsage(),
            grounding=GroundingReport(),
            route=RouteUsed(
                provider=ProviderKind.OPENROUTER,
                requested_model="requested",
                actual_model="actual",
            ),
        )

    observed = [
        event
        async for event in provider_events.observe_provider_stream(
            source(),
            provider="openrouter",
            requested_model="requested",
            actual_model="actual",
            api_key="provider-key-1234",
        )
    ]

    assert isinstance(observed[0], StreamCompleted)
    finished = [fields for name, fields in captured if name == "provider.attempt_finished"]
    assert len(finished) == 1
    assert finished[0]["outcome"] == "failed"
    assert finished[0]["reason_code"] == ErrorCode.EMPTY_RESPONSE.value
    assert finished[0]["failure_phase"] == FailurePhase.BEFORE_TEXT.value
