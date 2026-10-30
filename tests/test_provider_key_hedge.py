"""A slow key stays eligible while a bounded reserve request runs."""

import asyncio
from types import SimpleNamespace

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
)

MODEL = "gemini-3.8-flash"


def completed():
    return StreamCompleted(
        finish_reason=FinishReason.from_raw("STOP"),
        usage=TokenUsage(total=7),
        grounding=GroundingReport(),
        route=RouteUsed(provider=ProviderKind.GEMINI, requested_model=MODEL, actual_model=MODEL),
    )


class KeyPool:
    def __init__(self, keys):
        self.keys = keys
        self.reserved = []
        self.suspended = []
        self.successes = []
        self.reserve_checked = asyncio.Event()

    async def resolve_ai_request(self, model, *, excluded_key_hashes, **kwargs):
        return await self.resolve_exact_ai_request(model, excluded_key_hashes=excluded_key_hashes)

    async def resolve_exact_ai_request(self, model, *, excluded_key_hashes):
        if len(excluded_key_hashes) >= 2:
            self.reserve_checked.set()
        for key in self.keys:
            if key not in excluded_key_hashes:
                return {"api_key": key, "key_hash": key}, model, None
        return None, None, "all_exhausted"

    async def reserve_key_usage(self, key, model, _provider):
        self.reserved.append(key)
        return True

    async def suspend_key(self, key, model, category, diagnostic):
        self.suspended.append((key, category))

    async def record_success(self, key, model):
        self.successes.append(key)


def install(monkeypatch, script, keys=("first", "second", "reserve", "fourth"), budget=0.2):
    pool = KeyPool(keys)
    started = []
    closed = set()
    tasks = []

    class Provider:
        provider_name = "gemini"

        def __init__(self, key):
            self.key = key

        async def stream(self, request, *, model_name):
            started.append(self.key)
            tasks.append(asyncio.current_task())
            try:
                async for event in script(self.key, pool, closed):
                    yield event
            finally:
                await asyncio.sleep(0)
                closed.add(self.key)

    monkeypatch.setattr("app.agent_use_cases.AgentRequestUseCase", lambda: pool)
    monkeypatch.setattr("app.repos.keys.get_key_status_manager", lambda: pool)
    monkeypatch.setattr("app.providers.base.get_provider_for_model", lambda model, key: Provider(key))
    monkeypatch.setattr("app.providers.router.KEY_RACE_HEDGE_DELAY_SECONDS", 0.01, raising=False)
    monkeypatch.setattr(GenerationRequest, "provider_timeout_seconds", property(lambda self: budget))
    monkeypatch.setattr(GenerationRequest, "first_text_timeout_seconds", property(lambda self: budget), raising=False)
    request = GenerationRequest(
        models=(MODEL,),
        turns=(PromptTurn(PromptRole.USER, (TextPart("hello"),)),),
        allow_deferred=False,
        allow_model_fallback=False,
        route_strategy="sequential",
    )
    return ProviderRouter(), request, pool, started, closed, tasks


@pytest.mark.asyncio
@pytest.mark.parametrize("winner", ["first", "reserve"])
async def test_soft_timeout_keeps_original_eligible_and_awaits_every_loser(monkeypatch, winner):
    reserve_started = asyncio.Event()

    async def script(key, pool, closed):
        if key == "reserve":
            assert not closed, "The original requests were cancelled at the soft deadline"
            reserve_started.set()
        await reserve_started.wait()
        if key == winner:
            yield TextDelta(f"answer from {key}")
            yield completed()
        else:
            await asyncio.Event().wait()

    router, request, pool, started, closed, tasks = install(monkeypatch, script)
    events = await asyncio.wait_for(collect(router.stream(request)), 1)

    assert events == [TextDelta(f"answer from {winner}"), completed()]
    assert started == ["first", "second", "reserve"]
    assert pool.reserved == started
    assert pool.suspended == []
    assert pool.successes == [winner]
    assert closed == set(started)
    assert all(task.done() for task in tasks)


@pytest.mark.asyncio
async def test_missing_reserve_does_not_discard_a_pending_original(monkeypatch):
    async def script(key, pool, closed):
        await pool.reserve_checked.wait()
        if key == "first":
            yield TextDelta("late original")
            yield completed()
        else:
            await asyncio.Event().wait()

    router, request, pool, started, closed, tasks = install(monkeypatch, script, keys=("first", "second"))
    events = await asyncio.wait_for(collect(router.stream(request)), 1)

    assert events == [TextDelta("late original"), completed()]
    assert started == ["first", "second"]
    assert pool.suspended == []
    assert closed == set(started)
    assert all(task.done() for task in tasks)


@pytest.mark.asyncio
@pytest.mark.parametrize("keys", [("first",), ("first", "second", "reserve", "fourth")])
async def test_hard_deadline_is_bounded_without_penalizing_silent_keys(monkeypatch, keys):
    async def script(key, pool, closed):
        await asyncio.Event().wait()
        yield TextDelta("unreachable")

    router, request, pool, started, closed, tasks = install(monkeypatch, script, keys=keys, budget=0.05)
    events = await asyncio.wait_for(collect(router.stream(request)), 1)

    assert len(events) == 1 and isinstance(events[0], StreamFailed)
    assert events[0].code is ErrorCode.TIMEOUT
    assert events[0].key is KeyDisposition.UNCHANGED
    assert events[0].retry is RetryDisposition.RETRY_LATER
    assert started == list(keys[:3])
    assert pool.suspended == []
    assert closed == set(started)
    assert all(task.done() for task in tasks)


@pytest.mark.asyncio
async def test_actual_provider_failure_remains_penalized_when_original_wins_late(monkeypatch):
    reserve_started = asyncio.Event()

    async def script(key, pool, closed):
        if key == "second":
            yield StreamFailed(
                code=ErrorCode.OVERLOADED,
                phase=FailurePhase.BEFORE_TEXT,
                retry=RetryDisposition.TRY_NEXT_KEY,
                key=KeyDisposition.TRANSIENT_FAILURE,
                diagnostic="synthetic overload",
            )
            return
        if key == "reserve":
            reserve_started.set()
        await reserve_started.wait()
        if key == "first":
            yield TextDelta("original recovered")
            yield completed()
        else:
            await asyncio.Event().wait()

    router, request, pool, started, closed, tasks = install(monkeypatch, script)
    events = await asyncio.wait_for(collect(router.stream(request)), 1)

    assert events == [TextDelta("original recovered"), completed()]
    assert pool.suspended == [("second", "transient")]
    assert pool.successes == ["first"]
    assert closed == set(started)
    assert all(task.done() for task in tasks)


@pytest.mark.asyncio
async def test_slow_reserve_lookup_cannot_block_original_text_and_is_awaited(monkeypatch):
    lookup_started = asyncio.Event()
    lookup_closed = asyncio.Event()

    async def script(key, pool, closed):
        await lookup_started.wait()
        if key == "first":
            yield TextDelta("original while storage hangs")
            yield completed()
        else:
            await asyncio.Event().wait()

    router, request, pool, started, closed, tasks = install(monkeypatch, script)
    resolve = pool.resolve_exact_ai_request

    async def slow_resolve(model, *, excluded_key_hashes):
        if len(excluded_key_hashes) >= 2:
            lookup_started.set()
            try:
                await asyncio.Event().wait()
            finally:
                await asyncio.sleep(0)
                lookup_closed.set()
        return await resolve(model, excluded_key_hashes=excluded_key_hashes)

    monkeypatch.setattr(pool, "resolve_exact_ai_request", slow_resolve)
    events = await asyncio.wait_for(collect(router.stream(request)), 1)

    assert events == [TextDelta("original while storage hangs"), completed()]
    assert started == ["first", "second"]
    assert lookup_closed.is_set()
    assert closed == set(started)
    assert all(task.done() for task in tasks)


@pytest.mark.asyncio
async def test_denied_reserve_quota_preserves_original_without_dispatching_reserve(monkeypatch):
    quota_checked = asyncio.Event()

    async def script(key, pool, closed):
        await quota_checked.wait()
        if key == "first":
            yield TextDelta("original after quota denial")
            yield completed()
        else:
            await asyncio.Event().wait()

    router, request, pool, started, closed, tasks = install(monkeypatch, script)
    reserve = pool.reserve_key_usage

    async def reserve_quota(key, model, provider):
        if key == "reserve":
            quota_checked.set()
            return False
        return await reserve(key, model, provider)

    monkeypatch.setattr(pool, "reserve_key_usage", reserve_quota)
    events = await asyncio.wait_for(collect(router.stream(request)), 1)

    assert events == [TextDelta("original after quota denial"), completed()]
    assert started == ["first", "second"]
    assert pool.reserved == started
    assert pool.suspended == []
    assert closed == set(started)
    assert all(task.done() for task in tasks)


@pytest.mark.asyncio
async def test_caller_cancellation_awaits_all_overlapping_attempts(monkeypatch):
    reserve_started = asyncio.Event()

    async def script(key, pool, closed):
        if key == "reserve":
            reserve_started.set()
        await asyncio.Event().wait()
        yield TextDelta("unreachable")

    router, request, pool, started, closed, tasks = install(monkeypatch, script)
    consumer = asyncio.create_task(collect(router.stream(request)))
    try:
        await asyncio.wait_for(reserve_started.wait(), 1)
    finally:
        consumer.cancel()
        with pytest.raises(asyncio.CancelledError):
            await consumer

    assert started == ["first", "second", "reserve"]
    assert pool.suspended == []
    assert closed == set(started)
    assert all(task.done() for task in tasks)


@pytest.mark.asyncio
async def test_hedges_share_existing_total_attempt_budget_across_rotations(monkeypatch):
    keys = tuple(f"key-{index}" for index in range(10))
    reserve_started = asyncio.Event()

    async def script(key, pool, closed):
        index = keys.index(key)
        if index == 2:
            reserve_started.set()
        if index < 3:
            await reserve_started.wait()
        else:
            assert set(keys[:index]).issubset(closed), "A reserve raced another reserve"
        yield StreamFailed(
            code=ErrorCode.OVERLOADED,
            phase=FailurePhase.BEFORE_TEXT,
            retry=RetryDisposition.TRY_NEXT_KEY,
            key=KeyDisposition.TRANSIENT_FAILURE,
            diagnostic="synthetic overload",
        )

    router, request, pool, started, closed, tasks = install(monkeypatch, script, keys=keys)
    events = await asyncio.wait_for(collect(router.stream(request)), 1)

    assert len(events) == 1 and isinstance(events[0], StreamFailed)
    assert events[0].code is ErrorCode.OVERLOADED
    assert started == list(keys[:6])
    assert pool.reserved == started
    assert len(pool.suspended) == 6
    assert closed == set(started)
    assert all(task.done() for task in tasks)


@pytest.mark.asyncio
async def test_key_rotation_does_not_restart_model_first_text_budget(monkeypatch):
    from app.providers import router as router_module

    now = 100.0
    remaining = []

    async def script(key, pool, closed):
        nonlocal now
        if key in {"first", "second"}:
            now = 100.04
            yield StreamFailed(
                code=ErrorCode.OVERLOADED,
                phase=FailurePhase.BEFORE_TEXT,
                retry=RetryDisposition.TRY_NEXT_KEY,
                key=KeyDisposition.TRANSIENT_FAILURE,
                diagnostic="synthetic overload",
            )
        else:
            await asyncio.Event().wait()
            yield TextDelta("unreachable")

    async def wait_for(awaitable, *, timeout):
        nonlocal now
        remaining.append(timeout)
        try:
            return await asyncio.wait_for(awaitable, timeout)
        except TimeoutError:
            now += timeout
            raise

    router, request, pool, started, closed, tasks = install(monkeypatch, script, budget=0.05)
    monkeypatch.setattr(router_module, "KEY_RACE_HEDGE_DELAY_SECONDS", 1.0)
    monkeypatch.setattr(
        router_module,
        "asyncio",
        SimpleNamespace(
            **{**vars(asyncio), "get_running_loop": lambda: SimpleNamespace(time=lambda: now), "wait_for": wait_for}
        ),
    )
    events = await asyncio.wait_for(collect(router.stream(request)), 1)

    assert len(events) == 1 and isinstance(events[0], StreamFailed)
    assert events[0].code is ErrorCode.TIMEOUT
    assert remaining[0] == pytest.approx(0.05)
    assert remaining[1:]
    assert all(value == pytest.approx(0.01) for value in remaining[1:])
    assert started == ["first", "second", "reserve"]
    assert pool.suspended == [("first", "transient"), ("second", "transient")]
    assert closed == set(started)
    assert all(task.done() for task in tasks)


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["initial_lookup", "initial_quota", "rotation_lookup", "rotation_quota"])
async def test_key_preparation_obeys_model_deadline_and_awaits_cleanup(monkeypatch, stage):
    blocked_closed = asyncio.Event()

    async def script(key, pool, closed):
        yield StreamFailed(
            code=ErrorCode.OVERLOADED,
            phase=FailurePhase.BEFORE_TEXT,
            retry=RetryDisposition.TRY_NEXT_KEY,
            key=KeyDisposition.TRANSIENT_FAILURE,
            diagnostic="synthetic overload",
        )

    router, request, pool, started, closed, tasks = install(monkeypatch, script, budget=0.03)
    resolve = pool.resolve_exact_ai_request
    reserve = pool.reserve_key_usage

    async def block():
        try:
            await asyncio.Event().wait()
        finally:
            await asyncio.sleep(0)
            blocked_closed.set()

    async def lookup(model, *, excluded_key_hashes):
        if stage == "initial_lookup" or (
            stage == "rotation_lookup" and {"first", "second"}.issubset(excluded_key_hashes)
        ):
            await block()
        return await resolve(model, excluded_key_hashes=excluded_key_hashes)

    async def quota(key, model, provider):
        if stage == "initial_quota" or (stage == "rotation_quota" and key == "reserve"):
            await block()
        return await reserve(key, model, provider)

    monkeypatch.setattr(pool, "resolve_exact_ai_request", lookup)
    monkeypatch.setattr(pool, "reserve_key_usage", quota)
    events = await asyncio.wait_for(collect(router.stream(request)), 0.5)

    assert len(events) == 1 and isinstance(events[0], StreamFailed)
    assert events[0].code is ErrorCode.TIMEOUT
    assert events[0].key is KeyDisposition.UNCHANGED
    assert blocked_closed.is_set()
    assert started == (["first", "second"] if stage.startswith("rotation") else [])
    assert closed == set(started)
    assert all(task.done() for task in tasks)


@pytest.mark.asyncio
async def test_failure_health_write_cannot_hold_rotation_past_model_deadline(monkeypatch):
    health_closed = asyncio.Event()

    async def script(key, pool, closed):
        yield StreamFailed(
            code=ErrorCode.OVERLOADED,
            phase=FailurePhase.BEFORE_TEXT,
            retry=RetryDisposition.TRY_NEXT_KEY,
            key=KeyDisposition.TRANSIENT_FAILURE,
            diagnostic="synthetic overload",
        )

    async def suspend(*args):
        try:
            await asyncio.Event().wait()
        finally:
            await asyncio.sleep(0)
            health_closed.set()

    router, request, pool, started, closed, tasks = install(monkeypatch, script, budget=0.03)
    monkeypatch.setattr(pool, "suspend_key", suspend)
    events = await asyncio.wait_for(collect(router.stream(request)), 0.5)

    assert len(events) == 1 and isinstance(events[0], StreamFailed)
    assert events[0].code is ErrorCode.OVERLOADED
    assert health_closed.is_set()
    assert started == ["first", "second"]
    assert closed == set(started)
    assert all(task.done() for task in tasks)


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["record_success", "suspend_key"])
async def test_slow_health_bookkeeping_cannot_hide_selected_winner(monkeypatch, method):
    failed = asyncio.Event()
    health_closed = asyncio.Event()

    async def script(key, pool, closed):
        if key == "first":
            yield StreamFailed(
                code=ErrorCode.OVERLOADED,
                phase=FailurePhase.BEFORE_TEXT,
                retry=RetryDisposition.TRY_NEXT_KEY,
                key=KeyDisposition.TRANSIENT_FAILURE,
                diagnostic="synthetic overload",
            )
            failed.set()
        else:
            await failed.wait()
            while "first" not in closed:
                await asyncio.sleep(0)
            yield TextDelta("selected winner")
            yield completed()

    async def health_write(*args):
        try:
            await asyncio.Event().wait()
        finally:
            await asyncio.sleep(0)
            health_closed.set()

    router, request, pool, started, closed, tasks = install(monkeypatch, script, budget=0.03)
    monkeypatch.setattr(pool, method, health_write)
    events = await asyncio.wait_for(collect(router.stream(request)), 0.5)

    assert events == [TextDelta("selected winner"), completed()]
    assert health_closed.is_set()
    assert started == ["first", "second"]
    assert closed == set(started)
    assert all(task.done() for task in tasks)


async def collect(stream):
    return [event async for event in stream]


@pytest.mark.asyncio
async def test_first_visible_text_does_not_wait_for_key_health_persistence(monkeypatch):
    release_health = asyncio.Event()

    async def script(key, pool, closed):
        if key == "first":
            yield TextDelta("ready now")
            yield completed()
        else:
            await asyncio.Event().wait()

    async def record_success(*args):
        await release_health.wait()

    router, request, pool, started, closed, tasks = install(monkeypatch, script, budget=0.2)
    monkeypatch.setattr(pool, "record_success", record_success)
    stream = router.stream(request)
    try:
        assert await asyncio.wait_for(anext(stream), 0.03) == TextDelta("ready now")
        release_health.set()
        assert await collect(stream) == [completed()]
    finally:
        release_health.set()
        await stream.aclose()
    assert closed == set(started)
    assert all(task.done() for task in tasks)


@pytest.mark.asyncio
async def test_switching_models_shares_the_original_first_text_deadline(monkeypatch):
    from dataclasses import replace

    from app.providers import router as router_module

    now = 100.0
    calls = 0
    remaining = []

    async def script(key, pool, closed):
        nonlocal now, calls
        calls += 1
        if calls <= 2:
            now = 100.04
            yield StreamFailed(
                code=ErrorCode.OVERLOADED,
                phase=FailurePhase.BEFORE_TEXT,
                retry=RetryDisposition.TRY_NEXT_KEY,
                key=KeyDisposition.TRANSIENT_FAILURE,
                diagnostic="synthetic overload",
            )
        else:
            await asyncio.Event().wait()
            yield TextDelta("unreachable")

    async def wait_for(awaitable, *, timeout):
        nonlocal now
        remaining.append(timeout)
        try:
            return await asyncio.wait_for(awaitable, timeout)
        except TimeoutError:
            now += timeout
            raise

    router, request, pool, started, closed, tasks = install(monkeypatch, script, budget=0.05)
    request = replace(request, models=(MODEL, "gemini-3.5-flash-lite"))
    monkeypatch.setattr(router_module, "KEY_RACE_HEDGE_DELAY_SECONDS", 1.0)
    monkeypatch.setattr(
        router_module,
        "asyncio",
        SimpleNamespace(
            **{**vars(asyncio), "get_running_loop": lambda: SimpleNamespace(time=lambda: now), "wait_for": wait_for}
        ),
    )
    events = await asyncio.wait_for(collect(router.stream(request)), 0.5)

    assert len(events) == 1 and isinstance(events[0], StreamFailed)
    assert events[0].code is ErrorCode.TIMEOUT
    assert remaining[0] == pytest.approx(0.05)
    assert all(value == pytest.approx(0.01) for value in remaining[1:])
    assert calls == 3
    assert closed == set(started)
    assert all(task.done() for task in tasks)


@pytest.mark.asyncio
async def test_stalled_winner_returns_partial_timeout_without_starting_another_attempt(monkeypatch):
    async def script(key, pool, closed):
        if key == "first":
            yield TextDelta("partial answer")
        await asyncio.Event().wait()

    router, request, pool, started, closed, tasks = install(monkeypatch, script, budget=0.2)
    monkeypatch.setattr(GenerationRequest, "stream_idle_timeout_seconds", property(lambda self: 0.02))
    events = await asyncio.wait_for(collect(router.stream(request)), 0.5)

    assert events[0] == TextDelta("partial answer")
    assert len(events) == 2 and isinstance(events[1], StreamFailed)
    assert events[1].code is ErrorCode.TIMEOUT
    assert events[1].phase is FailurePhase.AFTER_TEXT
    assert events[1].key is KeyDisposition.UNCHANGED
    assert events[1].retry is RetryDisposition.DO_NOT_RETRY
    assert started == ["first", "second"]
    assert pool.suspended == []
    assert closed == set(started)
    assert all(task.done() for task in tasks)


@pytest.mark.asyncio
async def test_loser_cleanup_does_not_restart_winner_visible_text_idle_budget(monkeypatch):
    from app.providers import router as router_module

    now = 100.0
    loser_closed = asyncio.Event()
    remaining = []

    async def script(key, pool, closed):
        nonlocal now
        if key == "first":
            yield TextDelta("partial answer")
            await asyncio.Event().wait()
        else:
            try:
                await asyncio.Event().wait()
            finally:
                await asyncio.sleep(0)
                now += 0.04
                loser_closed.set()

    async def wait_for(awaitable, *, timeout):
        if loser_closed.is_set():
            remaining.append(timeout)
        return await asyncio.wait_for(awaitable, timeout)

    router, request, pool, started, closed, tasks = install(monkeypatch, script, budget=0.2)
    monkeypatch.setattr(GenerationRequest, "stream_idle_timeout_seconds", property(lambda self: 0.05))
    monkeypatch.setattr(
        router_module,
        "asyncio",
        SimpleNamespace(
            **{**vars(asyncio), "get_running_loop": lambda: SimpleNamespace(time=lambda: now), "wait_for": wait_for}
        ),
    )
    events = await asyncio.wait_for(collect(router.stream(request)), 0.5)

    assert remaining and all(value == pytest.approx(0.01) for value in remaining)
    assert events[0] == TextDelta("partial answer")
    assert len(events) == 2 and isinstance(events[1], StreamFailed)
    assert events[1].phase is FailurePhase.AFTER_TEXT
    assert closed == set(started)
    assert all(task.done() for task in tasks)


@pytest.mark.asyncio
async def test_cancellation_during_loser_cleanup_also_drains_winner_health_task(monkeypatch):
    loser_cleaning = asyncio.Event()
    health_started = asyncio.Event()
    health_closed = asyncio.Event()
    release_loser = asyncio.Event()
    release_health = asyncio.Event()
    health_tasks = []

    async def script(key, pool, closed):
        if key == "first":
            yield TextDelta("answer")
            yield completed()
        else:
            try:
                await asyncio.Event().wait()
            finally:
                loser_cleaning.set()
                await release_loser.wait()

    async def record_success(*args):
        health_tasks.append(asyncio.current_task())
        health_started.set()
        try:
            await release_health.wait()
        finally:
            await asyncio.sleep(0)
            health_closed.set()

    router, request, pool, started, closed, tasks = install(monkeypatch, script, budget=0.2)
    monkeypatch.setattr(pool, "record_success", record_success)
    consumer = asyncio.create_task(collect(router.stream(request)))
    try:
        await asyncio.wait_for(loser_cleaning.wait(), 0.5)
        await asyncio.wait_for(health_started.wait(), 0.5)
        consumer.cancel()
        # Let the resource finalizer finish normally; owner cancellation must
        # not act as a second cancellation that interrupts this finalizer.
        release_loser.set()
        with pytest.raises(asyncio.CancelledError):
            await consumer
        drained_before_test_cleanup = health_closed.is_set()
    finally:
        release_loser.set()
        release_health.set()
        await asyncio.gather(consumer, *health_tasks, return_exceptions=True)

    assert drained_before_test_cleanup
    assert all(task.done() for task in tasks)
