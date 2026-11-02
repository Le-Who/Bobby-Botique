"""Sequential chat fallbacks must receive time before the route deadline."""

import asyncio
from dataclasses import replace
from unittest.mock import AsyncMock

import pytest

from app.errors import ErrorCode
from app.providers.router import ProviderRouter
from app.providers.stream_types import (
    FailurePhase,
    FinishReason,
    GenerationRequest,
    GroundingReport,
    KeyDisposition,
    PromptRole,
    PromptTurn,
    ProviderKind,
    RetryDisposition,
    RouteUsed,
    StreamCompleted,
    StreamFailed,
    TextDelta,
    TextPart,
    TokenUsage,
    Workload,
)

PRIMARY = "gemini-3.1-flash-lite"
BACKUP = "gemini-3.5-flash-lite"


def _request():
    return GenerationRequest(
        models=(PRIMARY, BACKUP),
        turns=(PromptTurn(PromptRole.USER, (TextPart("hello"),)),),
        allow_deferred=False,
        route_strategy="sequential",
        allow_model_fallback=False,
    )


def _completion(model):
    return StreamCompleted(
        finish_reason=FinishReason.from_raw("STOP"),
        usage=TokenUsage(total=5),
        grounding=GroundingReport(),
        route=RouteUsed(provider=ProviderKind.GEMINI, requested_model=model, actual_model=model),
    )


def _failure(code=ErrorCode.OVERLOADED, key=KeyDisposition.TRANSIENT_FAILURE):
    return StreamFailed(
        code=code,
        phase=FailurePhase.BEFORE_TEXT,
        retry=RetryDisposition.TRY_NEXT_KEY,
        key=key,
        diagnostic="Synthetic provider failure",
    )


def _install_transport(monkeypatch, stream):
    calls = []
    active = set()
    closed = []
    health = AsyncMock()

    async def lookup(model, *, excluded_hashes):
        for index in range(8):
            key = f"{model}:{index}"
            if key not in excluded_hashes:
                return {"api_key": key, "key_hash": key}
        return None

    class Provider:
        provider_name = "gemini"

        def __init__(self, key):
            self.key = key

        async def stream(self, request, *, model_name):
            calls.append((model_name, self.key))
            active.add(self.key)
            try:
                async for event in stream(model_name, self.key, active):
                    yield event
            finally:
                try:
                    await asyncio.sleep(0)
                finally:
                    active.remove(self.key)
                    closed.append(self.key)

    monkeypatch.setattr("app.agent_use_cases.get_available_gemini_key", lookup)
    monkeypatch.setattr("app.agent_use_cases.reserve_gemini_key_usage", AsyncMock(return_value=True))
    monkeypatch.setattr("app.repos.keys.get_key_status_manager", lambda: health)
    monkeypatch.setattr("app.providers.base.get_provider_for_model", lambda model, key: Provider(key))
    monkeypatch.setattr("app.providers.gemini.is_vertex_client_available", lambda: False)
    return calls, active, closed, health


@pytest.mark.asyncio
@pytest.mark.parametrize("allow_key_race", [True, False])
async def test_overloaded_primary_moves_to_configured_backup_without_exhausting_keys(monkeypatch, allow_key_race):
    async def stream(model, key, active):
        if model == PRIMARY:
            yield _failure()
        else:
            assert active == {key}, "Sequential fallback must await primary cleanup"
            yield TextDelta("backup answer")
            yield _completion(model)

    calls, active, closed, health = _install_transport(monkeypatch, stream)
    request = replace(_request(), allow_key_race=allow_key_race)
    events = [event async for event in ProviderRouter().stream(request)]

    assert events == [TextDelta("backup answer"), _completion(BACKUP)]
    assert [model for model, _ in calls] == [PRIMARY] * (2 if allow_key_race else 1) + [BACKUP]
    assert health.suspend_key.await_count == (2 if allow_key_race else 1)
    assert all(call.args[2] == "transient" for call in health.suspend_key.await_args_list)
    assert not active and len(closed) == len(calls)


@pytest.mark.asyncio
@pytest.mark.parametrize("primary_behavior", ["silent", "overload_and_silent"])
async def test_silent_primary_leaves_time_for_sequential_backup(monkeypatch, primary_behavior):
    monkeypatch.setattr(GenerationRequest, "first_text_timeout_seconds", property(lambda self: 0.2))

    async def stream(model, key, active):
        if model == PRIMARY:
            if primary_behavior == "overload_and_silent" and key.endswith(":0"):
                yield _failure()
            else:
                await asyncio.Event().wait()
        else:
            assert active == {key}, "Timed-out providers must drain before sequential fallback"
            yield TextDelta("backup answer")
            yield _completion(model)

    calls, active, closed, health = _install_transport(monkeypatch, stream)
    events = [event async for event in ProviderRouter().stream(_request())]

    assert events == [TextDelta("backup answer"), _completion(BACKUP)]
    assert calls[-1][0] == BACKUP
    assert health.suspend_key.await_count == (1 if primary_behavior == "overload_and_silent" else 0)
    assert not active and len(closed) == len(calls)


@pytest.mark.asyncio
async def test_primary_key_lookup_cannot_consume_the_backup_time(monkeypatch):
    from app import agent_use_cases

    monkeypatch.setattr(GenerationRequest, "first_text_timeout_seconds", property(lambda self: 0.2))
    lookup_closed = asyncio.Event()

    async def stream(model, key, active):
        assert lookup_closed.is_set()
        yield TextDelta("backup answer")
        yield _completion(model)

    calls, active, _, health = _install_transport(monkeypatch, stream)
    original_lookup = agent_use_cases.get_available_gemini_key

    async def lookup(model, *, excluded_hashes):
        if model == PRIMARY:
            try:
                await asyncio.Event().wait()
            finally:
                lookup_closed.set()
        return await original_lookup(model, excluded_hashes=excluded_hashes)

    monkeypatch.setattr("app.agent_use_cases.get_available_gemini_key", lookup)
    events = [event async for event in ProviderRouter().stream(_request())]

    assert events == [TextDelta("backup answer"), _completion(BACKUP)]
    assert [model for model, _ in calls] == [BACKUP]
    health.suspend_key.assert_not_awaited()
    assert not active


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "code,disposition",
    [(ErrorCode.RATE_LIMIT, KeyDisposition.RATE_LIMITED), (ErrorCode.INVALID_KEY, KeyDisposition.INVALID)],
)
async def test_key_specific_failures_keep_rotating_on_the_primary(monkeypatch, code, disposition):
    async def stream(model, key, active):
        if key.endswith((":0", ":1")):
            yield _failure(code, disposition)
        else:
            yield TextDelta("primary answer")
            yield _completion(model)

    calls, active, _, _ = _install_transport(monkeypatch, stream)
    events = [event async for event in ProviderRouter().stream(_request())]

    assert events == [TextDelta("primary answer"), _completion(PRIMARY)]
    assert [model for model, _ in calls] == [PRIMARY, PRIMARY, PRIMARY]
    assert not active


@pytest.mark.asyncio
async def test_overload_does_not_discard_a_successful_sibling(monkeypatch):
    async def stream(model, key, active):
        if key.endswith(":0"):
            yield _failure()
        else:
            yield TextDelta("primary answer")
            yield _completion(model)

    calls, active, _, _ = _install_transport(monkeypatch, stream)
    events = [event async for event in ProviderRouter().stream(_request())]

    assert events == [TextDelta("primary answer"), _completion(PRIMARY)]
    assert [model for model, _ in calls] == [PRIMARY, PRIMARY]
    assert not active


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["single_model", "deferred", "inline"])
async def test_other_serial_requests_keep_their_primary_budget(monkeypatch, mode):
    monkeypatch.setattr(GenerationRequest, "first_text_timeout_seconds", property(lambda self: 0.2))

    async def stream(model, key, active):
        await asyncio.sleep(0.12)
        yield TextDelta("primary answer")
        yield _completion(model)

    calls, active, _, _ = _install_transport(monkeypatch, stream)
    request = _request()
    if mode == "single_model":
        request = replace(request, models=(PRIMARY,))
    elif mode == "deferred":
        request = replace(request, allow_deferred=True)
    else:
        request = replace(request, workload=Workload.INLINE)
    events = [event async for event in ProviderRouter().stream(request)]

    assert events == [TextDelta("primary answer"), _completion(PRIMARY)]
    assert all(model == PRIMARY for model, _ in calls)
    assert not active


@pytest.mark.asyncio
async def test_cancelling_primary_wait_drains_calls_without_starting_backup(monkeypatch):
    started = asyncio.Event()

    async def stream(model, key, active):
        started.set()
        await asyncio.Event().wait()
        yield TextDelta("unreachable")

    calls, active, closed, health = _install_transport(monkeypatch, stream)

    async def collect():
        return [event async for event in ProviderRouter().stream(_request())]

    task = asyncio.create_task(collect())
    try:
        await asyncio.wait_for(started.wait(), 1)
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    assert all(model == PRIMARY for model, _ in calls)
    assert not active and len(closed) == len(calls)
    health.suspend_key.assert_not_awaited()
