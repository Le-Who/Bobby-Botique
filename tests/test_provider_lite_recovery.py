"""A transient Lite failure must not discard another potentially healthy call."""

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
    RequestScope,
    RetryDisposition,
    RouteUsed,
    StreamCompleted,
    StreamFailed,
    TextDelta,
    TextPart,
    TokenUsage,
    Workload,
)

MODEL = "gemini-3.1-flash-lite"


def completion():
    return StreamCompleted(
        finish_reason=FinishReason.from_raw("STOP"),
        usage=TokenUsage(total=5),
        grounding=GroundingReport(),
        route=RouteUsed(provider=ProviderKind.GEMINI, requested_model=MODEL, actual_model=MODEL),
    )


def overloaded():
    return StreamFailed(
        code=ErrorCode.OVERLOADED,
        phase=FailurePhase.BEFORE_TEXT,
        retry=RetryDisposition.TRY_NEXT_KEY,
        key=KeyDisposition.TRANSIENT_FAILURE,
        diagnostic="Synthetic HTTP 503",
    )


def install(monkeypatch, behavior, *, delay=0.01):
    class Pool:
        def __init__(self):
            self.reserved = []
            self.health = AsyncMock()

        async def resolve_exact_ai_request(self, model, *, excluded_key_hashes):
            for index in range(10):
                key = str(index)
                if key not in excluded_key_hashes:
                    return {"key_hash": key, "api_key": f"synthetic-lite-recovery-key-{key}"}, model, None
            return None, None, "all_exhausted"

        async def reserve_key_usage(self, key, model, provider):
            self.reserved.append(key)
            return True

    pool = Pool()
    active = set()
    calls = []
    closed = []
    tasks = []

    class Provider:
        provider_name = "gemini"

        def __init__(self, key):
            self.key = key.rsplit("-", 1)[-1]

        async def stream(self, request, *, model_name):
            calls.append(self.key)
            active.add(self.key)
            tasks.append(asyncio.current_task())
            try:
                async for event in behavior(self.key, active):
                    yield event
            finally:
                try:
                    await asyncio.sleep(0)
                finally:
                    active.remove(self.key)
                    closed.append(self.key)

    monkeypatch.setattr("app.agent_use_cases.AgentRequestUseCase", lambda: pool)
    monkeypatch.setattr("app.repos.keys.get_key_status_manager", lambda: pool.health)
    monkeypatch.setattr("app.providers.base.get_provider_for_model", lambda model, key: Provider(key))
    monkeypatch.setattr("app.providers.gemini.is_vertex_client_available", lambda: False)
    monkeypatch.setattr("app.providers.router.KEY_RACE_HEDGE_DELAY_SECONDS", delay)
    request = GenerationRequest(
        models=(MODEL,),
        turns=(PromptTurn(PromptRole.USER, (TextPart("hello"),)),),
        route_strategy="sequential",
        allow_model_fallback=False,
        allow_deferred=False,
    )
    return ProviderRouter(), request, pool, active, calls, closed, tasks


async def collect(events):
    return [event async for event in events]


@pytest.mark.asyncio
@pytest.mark.parametrize("winner", ["0", "2"])
async def test_503_starts_next_key_immediately_and_keeps_original_eligible(monkeypatch, winner):
    replacement_started = asyncio.Event()

    async def behavior(key, active):
        if key == "1":
            yield overloaded()
            return
        if key == "2":
            assert "0" in active
            replacement_started.set()
        await replacement_started.wait()
        if key == winner:
            yield TextDelta("recovered")
            yield completion()
        else:
            await asyncio.Event().wait()

    router, request, pool, active, calls, closed, tasks = install(monkeypatch, behavior, delay=60)
    events = await asyncio.wait_for(collect(router.stream(request)), 1)
    assert events == [TextDelta("recovered"), completion()]
    assert calls == ["0", "1", "2"]
    assert pool.reserved == calls
    assert pool.health.suspend_key.await_count == 1
    assert pool.health.suspend_key.await_args.args[0] == "1"
    assert not active and sorted(closed) == sorted(calls)
    assert all(task.done() for task in tasks)


@pytest.mark.asyncio
async def test_successive_soft_hedges_keep_every_silent_call_alive(monkeypatch):
    async def behavior(key, active):
        if key == "3":
            assert active == {"0", "1", "2", "3"}
            yield TextDelta("fourth key")
            yield completion()
        else:
            await asyncio.Event().wait()

    router, request, pool, active, calls, closed, tasks = install(monkeypatch, behavior)
    events = await asyncio.wait_for(collect(router.stream(request)), 1)
    assert events == [TextDelta("fourth key"), completion()]
    assert calls == ["0", "1", "2", "3"]
    pool.health.suspend_key.assert_not_awaited()
    assert not active and sorted(closed) == sorted(calls)
    assert all(task.done() for task in tasks)


@pytest.mark.asyncio
async def test_failed_reserve_is_replaced_while_originals_still_wait(monkeypatch):
    async def behavior(key, active):
        if key == "2":
            yield overloaded()
        elif key == "3":
            assert {"0", "1"} <= active
            yield TextDelta("replacement of reserve")
            yield completion()
        else:
            await asyncio.Event().wait()

    router, request, pool, active, calls, closed, tasks = install(monkeypatch, behavior)
    events = await asyncio.wait_for(collect(router.stream(request)), 1)
    assert events == [TextDelta("replacement of reserve"), completion()]
    assert calls == ["0", "1", "2", "3"]
    assert pool.health.suspend_key.await_count == 1
    assert pool.health.suspend_key.await_args.args[0] == "2"
    assert not active and sorted(closed) == sorted(calls)
    assert all(task.done() for task in tasks)


@pytest.mark.asyncio
async def test_all_silent_attempts_stay_bounded_and_cancel_together(monkeypatch):
    last_started = asyncio.Event()

    async def behavior(key, active):
        if key == "5":
            last_started.set()
        await asyncio.Event().wait()
        yield TextDelta("unreachable")

    router, request, pool, active, calls, closed, tasks = install(monkeypatch, behavior)
    consumer = asyncio.create_task(collect(router.stream(request)))
    try:
        await asyncio.wait_for(last_started.wait(), 1)
        await asyncio.sleep(0.05)
        assert len(calls) == 6 and not consumer.done()
    finally:
        consumer.cancel()
        with pytest.raises(asyncio.CancelledError):
            await consumer
    assert not active and sorted(closed) == sorted(calls)
    assert all(task.done() for task in tasks)
    pool.health.suspend_key.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("code", [ErrorCode.OVERLOADED, ErrorCode.TIMEOUT, ErrorCode.NETWORK])
async def test_failed_wave_retries_automatically_without_new_user_admission(monkeypatch, code):
    network_calls = 0

    async def behavior(key, active):
        nonlocal network_calls
        network_calls += 1
        if network_calls <= 6:
            yield replace(overloaded(), code=code)
        else:
            yield TextDelta("automatic recovery")
            yield completion()

    router, request, pool, active, calls, closed, tasks = install(monkeypatch, behavior)
    monkeypatch.setattr("app.providers.router._interactive_retry_delay", lambda round_number: 0.01, raising=False)
    limiter = AsyncMock(return_value=True)
    monkeypatch.setattr(router._rate_limiter, "check_rate_limit", limiter)
    request = replace(request, scope=RequestScope(user_id=123))
    events = await asyncio.wait_for(collect(router.stream(request)), 1)
    assert events == [TextDelta("automatic recovery"), completion()]
    assert network_calls >= 7 and len(pool.reserved) == len(calls)
    limiter.assert_awaited_once_with(123)
    assert not active and sorted(closed) == sorted(calls)
    assert all(task.done() for task in tasks)


@pytest.mark.asyncio
async def test_after_text_failure_is_not_retried_or_concatenated(monkeypatch):
    failure = replace(
        overloaded(), code=ErrorCode.TIMEOUT, phase=FailurePhase.AFTER_TEXT, retry=RetryDisposition.DO_NOT_RETRY
    )

    async def behavior(key, active):
        if key == "0":
            yield TextDelta("partial")
            yield failure
        else:
            await asyncio.Event().wait()

    router, request, _, active, calls, closed, tasks = install(monkeypatch, behavior)
    events = await asyncio.wait_for(collect(router.stream(request)), 1)
    assert events == [TextDelta("partial"), failure]
    assert not active and sorted(closed) == sorted(calls)
    assert all(task.done() for task in tasks)


@pytest.mark.asyncio
async def test_denied_reserve_is_skipped_without_losing_task_identity(monkeypatch):
    async def behavior(key, active):
        if key == "3":
            assert {"0", "1"} <= active
            yield TextDelta("admitted replacement")
            yield completion()
        else:
            await asyncio.Event().wait()

    router, request, pool, active, calls, closed, tasks = install(monkeypatch, behavior)
    reserve = pool.reserve_key_usage

    async def admit(key, model, provider):
        return False if key == "2" else await reserve(key, model, provider)

    monkeypatch.setattr(pool, "reserve_key_usage", admit)
    events = await asyncio.wait_for(collect(router.stream(request)), 1)
    assert events == [TextDelta("admitted replacement"), completion()]
    assert calls == ["0", "1", "3"]
    assert pool.reserved == calls
    assert not active and sorted(closed) == sorted(calls)
    assert all(task.done() for task in tasks)
    pool.health.suspend_key.assert_not_awaited()


@pytest.mark.asyncio
async def test_initial_cooldown_recovers_without_a_manual_retry(monkeypatch):
    async def behavior(key, active):
        yield TextDelta("key available now")
        yield completion()

    router, request, pool, active, calls, closed, tasks = install(monkeypatch, behavior)
    resolve = pool.resolve_exact_ai_request
    lookups = 0

    async def temporarily_empty(model, *, excluded_key_hashes):
        nonlocal lookups
        lookups += 1
        if lookups == 1:
            return None, None, "all_exhausted"
        return await resolve(model, excluded_key_hashes=excluded_key_hashes)

    monkeypatch.setattr(pool, "resolve_exact_ai_request", temporarily_empty)
    monkeypatch.setattr("app.providers.router._interactive_retry_delay", lambda round_number: 0.01)
    events = await asyncio.wait_for(collect(router.stream(request)), 1)
    assert events == [TextDelta("key available now"), completion()]
    assert not active and sorted(closed) == sorted(calls)
    assert all(task.done() for task in tasks)


@pytest.mark.asyncio
async def test_cancelling_automatic_backoff_does_not_start_another_round(monkeypatch):
    waiting = asyncio.Event()

    async def behavior(key, active):
        yield overloaded()

    router, request, pool, active, calls, closed, tasks = install(monkeypatch, behavior)

    def delay(round_number):
        waiting.set()
        return 60.0

    monkeypatch.setattr("app.providers.router._interactive_retry_delay", delay)
    consumer = asyncio.create_task(collect(router.stream(request)))
    try:
        await asyncio.wait_for(waiting.wait(), 1)
        assert len(calls) == 6 and not active
    finally:
        consumer.cancel()
        with pytest.raises(asyncio.CancelledError):
            await consumer
    assert len(pool.reserved) == len(calls) == 6
    assert sorted(closed) == sorted(calls)
    assert all(task.done() for task in tasks)


@pytest.mark.asyncio
async def test_temporary_reserve_unavailability_is_rechecked_while_originals_live(monkeypatch):
    async def behavior(key, active):
        if key == "2":
            assert {"0", "1"} <= active
            yield TextDelta("key cooldown ended")
            yield completion()
        else:
            await asyncio.Event().wait()

    router, request, pool, active, calls, closed, tasks = install(monkeypatch, behavior)
    resolve = pool.resolve_exact_ai_request
    reserve_checks = 0

    async def temporarily_empty(model, *, excluded_key_hashes):
        nonlocal reserve_checks
        if len(excluded_key_hashes) >= 2:
            reserve_checks += 1
            if reserve_checks == 1:
                return None, None, "all_exhausted"
        return await resolve(model, excluded_key_hashes=excluded_key_hashes)

    monkeypatch.setattr(pool, "resolve_exact_ai_request", temporarily_empty)
    events = await asyncio.wait_for(collect(router.stream(request)), 1)
    assert events == [TextDelta("key cooldown ended"), completion()]
    assert reserve_checks >= 2 and calls == ["0", "1", "2"]
    assert not active and sorted(closed) == sorted(calls)
    assert all(task.done() for task in tasks)


@pytest.mark.asyncio
async def test_invalid_request_is_returned_without_automatic_retry(monkeypatch):
    failure = replace(
        overloaded(), code=ErrorCode.INVALID_REQUEST, retry=RetryDisposition.DO_NOT_RETRY, key=KeyDisposition.UNCHANGED
    )

    async def behavior(key, active):
        yield failure

    router, request, _, active, calls, closed, tasks = install(monkeypatch, behavior)
    events = await asyncio.wait_for(collect(router.stream(request)), 1)
    assert events == [failure]
    assert not active and sorted(closed) == sorted(calls)
    assert all(task.done() for task in tasks)


@pytest.mark.parametrize(
    "models,workload,deferred,expected",
    [
        ((MODEL,), Workload.INTERACTIVE, False, True),
        (("gemini-3.5-flash-lite",), Workload.INTERACTIVE, False, True),
        (("models/gemini-3.5-flash-lite", MODEL), Workload.INTERACTIVE, False, True),
        ((MODEL, "gemini-3.8-flash"), Workload.INTERACTIVE, False, False),
        ((MODEL,), Workload.INTERACTIVE, True, False),
        ((MODEL,), Workload.INLINE, False, False),
        ((MODEL,), Workload.QUICK_SEARCH, False, False),
        ((MODEL,), Workload.DEFERRED_RETRY, False, False),
    ],
)
def test_automatic_recovery_policy_is_limited_to_owned_interactive_lite_chats(models, workload, deferred, expected):
    request = GenerationRequest(
        models=models,
        turns=(PromptTurn(PromptRole.USER, (TextPart("hello"),)),),
        workload=workload,
        allow_deferred=deferred,
    )
    assert request.recovers_transient_errors is expected


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "last_code,last_key",
    [
        (ErrorCode.RATE_LIMIT, KeyDisposition.RATE_LIMITED),
        (ErrorCode.QUOTA_EXCEEDED, KeyDisposition.EXHAUSTED),
        (ErrorCode.INVALID_KEY, KeyDisposition.INVALID),
    ],
)
async def test_mixed_transient_wave_does_not_lose_recovery_to_its_last_error(monkeypatch, last_code, last_key):
    network_calls = 0

    async def behavior(key, active):
        nonlocal network_calls
        network_calls += 1
        if network_calls < 6:
            yield overloaded()
        elif network_calls == 6:
            yield replace(overloaded(), code=last_code, key=last_key)
        else:
            yield TextDelta("mixed wave recovered")
            yield completion()

    router, request, _, active, calls, closed, tasks = install(monkeypatch, behavior)
    monkeypatch.setattr("app.providers.router._interactive_retry_delay", lambda round_number: 0.01)
    events = await asyncio.wait_for(collect(router.stream(request)), 1)
    assert events == [TextDelta("mixed wave recovered"), completion()]
    assert not active and sorted(closed) == sorted(calls)
    assert all(task.done() for task in tasks)


@pytest.mark.asyncio
async def test_per_user_rate_rejection_does_not_enter_provider_recovery(monkeypatch):
    async def behavior(key, active):
        pytest.fail("User admission rejected this logical request")
        yield TextDelta("unreachable")

    router, request, _, active, calls, _, _ = install(monkeypatch, behavior)
    monkeypatch.setattr(router._rate_limiter, "check_rate_limit", AsyncMock(return_value=False))
    request = replace(request, scope=RequestScope(user_id=123))
    events = await asyncio.wait_for(collect(router.stream(request)), 1)
    assert len(events) == 1 and isinstance(events[0], StreamFailed)
    assert events[0].code is ErrorCode.RATE_LIMIT
    assert not calls and not active


@pytest.mark.asyncio
async def test_hedged_primary_replaces_503_without_discarding_its_live_sibling(monkeypatch):
    async def behavior(key, active):
        if key == "0":
            yield overloaded()
        elif key == "2":
            assert "1" in active and len(active) <= 2
            yield TextDelta("same-model replacement")
            yield completion()
        else:
            await asyncio.Event().wait()

    router, request, pool, active, calls, closed, tasks = install(monkeypatch, behavior, delay=60)
    monkeypatch.setattr("app.providers.router.MODEL_HEDGE_DELAY_SECONDS", 60)
    request = replace(request, models=(MODEL, "gemini-3.5-flash-lite"), route_strategy="hedged")
    events = await asyncio.wait_for(collect(router.stream(request)), 1)
    assert events == [TextDelta("same-model replacement"), completion()]
    assert calls == ["0", "1", "2"]
    assert not active and sorted(closed) == sorted(calls)
    assert all(task.done() for task in tasks)
