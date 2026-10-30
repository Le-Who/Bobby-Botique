"""Only the primary attempt may race; reserves dispatch one provider call."""

import asyncio
from collections import Counter

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

PRIMARY = "gemini-3.8-flash"
BACKUP = "gemini-3.5-flash-lite"


def complete(model):
    return StreamCompleted(
        finish_reason=FinishReason.from_raw("STOP"),
        usage=TokenUsage(total=7),
        grounding=GroundingReport(),
        route=RouteUsed(provider=ProviderKind.GEMINI, requested_model=PRIMARY, actual_model=model),
    )


def overload():
    return StreamFailed(
        code=ErrorCode.OVERLOADED,
        phase=FailurePhase.BEFORE_TEXT,
        retry=RetryDisposition.TRY_NEXT_KEY,
        key=KeyDisposition.TRANSIENT_FAILURE,
        diagnostic="synthetic overload",
    )


def install(monkeypatch, script):
    started = []
    closed = []
    active = Counter()
    peak = Counter()

    class Pool:
        async def resolve_ai_request(self, model, *, excluded_key_hashes, **kwargs):
            return await self.resolve_exact_ai_request(model, excluded_key_hashes=excluded_key_hashes)

        async def resolve_exact_ai_request(self, model, *, excluded_key_hashes):
            for index in range(8):
                key = f"{model}:synthetic-{index}"
                if key not in excluded_key_hashes:
                    return {"api_key": key, "key_hash": key}, model, None
            return None, None, "all_exhausted"

        async def reserve_key_usage(self, *args):
            return True

        async def suspend_key(self, *args):
            pass

        async def record_success(self, *args):
            pass

    class Provider:
        provider_name = "gemini"

        def __init__(self, key):
            self.key = key

        async def stream(self, request, *, model_name):
            started.append((model_name, self.key))
            active[model_name] += 1
            peak[model_name] = max(peak[model_name], active[model_name])
            try:
                async for event in script(model_name, self.key, started):
                    yield event
            finally:
                await asyncio.sleep(0)
                active[model_name] -= 1
                closed.append((model_name, self.key))

    pool = Pool()
    monkeypatch.setattr("app.agent_use_cases.AgentRequestUseCase", lambda: pool)
    monkeypatch.setattr("app.repos.keys.get_key_status_manager", lambda: pool)
    monkeypatch.setattr("app.providers.base.get_provider_for_model", lambda model, key: Provider(key))
    monkeypatch.setattr("app.providers.gemini.is_vertex_client_available", lambda: False)
    monkeypatch.setattr("app.providers.router.MODEL_HEDGE_DELAY_SECONDS", 0.01)
    monkeypatch.setattr("app.providers.router.KEY_RACE_HEDGE_DELAY_SECONDS", 0.5, raising=False)
    monkeypatch.setattr(GenerationRequest, "first_text_timeout_seconds", property(lambda self: 3.0))
    return started, closed, active, peak


def request(models, strategy="sequential"):
    return GenerationRequest(
        models=models,
        turns=(PromptTurn(PromptRole.USER, (TextPart("synthetic request"),)),),
        route_strategy=strategy,
        allow_model_fallback=False,
        allow_deferred=False,
    )


@pytest.mark.asyncio
async def test_key_rotation_after_failed_primary_pair_dispatches_one_reserve(monkeypatch):
    pair_started = asyncio.Event()

    async def script(model, key, started):
        if key.endswith(("-0", "-1")):
            if len(started) == 2:
                pair_started.set()
            await pair_started.wait()
            yield overload()
        else:
            yield TextDelta("single reserve answer")
            yield complete(model)

    started, closed, active, peak = install(monkeypatch, script)
    events = [event async for event in ProviderRouter().stream(request((PRIMARY,)))]

    assert events == [TextDelta("single reserve answer"), complete(PRIMARY)]
    assert len(started) == 3
    assert peak[PRIMARY] == 2
    assert sorted(closed) == sorted(started)
    assert not any(active.values())


@pytest.mark.asyncio
async def test_exhausted_model_rotates_to_single_key_fallback(monkeypatch):
    async def script(model, key, started):
        if model == PRIMARY:
            yield overload()
        else:
            yield TextDelta("fallback model")
            yield complete(model)

    started, closed, active, peak = install(monkeypatch, script)
    events = [event async for event in ProviderRouter().stream(request((PRIMARY, BACKUP)))]

    assert events == [TextDelta("fallback model"), complete(BACKUP)]
    assert Counter(model for model, _ in started) == {PRIMARY: 6, BACKUP: 1}
    assert peak[BACKUP] == 1
    assert sorted(closed) == sorted(started)
    assert not any(active.values())


@pytest.mark.asyncio
async def test_independent_requests_keep_independent_primary_races(monkeypatch):
    both_pairs_started = asyncio.Event()

    async def script(model, key, started):
        if len(started) == 4:
            both_pairs_started.set()
        await both_pairs_started.wait()
        if key.endswith("-1"):
            await asyncio.Event().wait()
        yield TextDelta("independent answer")
        yield complete(model)

    started, closed, active, peak = install(monkeypatch, script)

    async def consume():
        return [event async for event in ProviderRouter().stream(request((PRIMARY,)))]

    results = await asyncio.wait_for(asyncio.gather(consume(), consume()), 3)
    assert results == [[TextDelta("independent answer"), complete(PRIMARY)]] * 2
    assert peak[PRIMARY] == 4
    assert len(closed) == len(started) == 4
    assert not any(active.values())


@pytest.mark.asyncio
async def test_inline_outer_model_race_does_not_spawn_inner_key_races(monkeypatch):
    from unittest.mock import AsyncMock

    from app.handlers import inline

    async def script(model, key, started):
        if model == PRIMARY:
            await asyncio.Event().wait()
        else:
            yield TextDelta("ready inline reserve")
            yield complete(model)

    started, closed, active, peak = install(monkeypatch, script)
    monkeypatch.setattr(inline, "get_global_setting", AsyncMock(return_value="low"))
    monkeypatch.setattr(inline, "_should_use_inline_primary", lambda query: True)

    result = await asyncio.wait_for(
        inline._generate_inline_answer(
            preferred_model=PRIMARY,
            user_query="synthetic request",
            history=[{"role": "user", "parts": ["synthetic request"]}],
            system_instruction=None,
            user_id=None,
            enable_web_search=False,
        ),
        3,
    )
    assert result == ("ready inline reserve", [], inline._INLINE_FALLBACK_MODEL)
    assert Counter(model for model, _ in started) == {PRIMARY: 1, inline._INLINE_FALLBACK_MODEL: 1}
    assert sorted(closed) == sorted(started)
    assert not any(active.values())


@pytest.mark.asyncio
async def test_model_reserve_never_starts_its_own_key_race(monkeypatch):
    primary_pair_started = asyncio.Event()

    async def script(model, key, started):
        if model == PRIMARY:
            if sum(name == PRIMARY for name, _ in started) == 2:
                primary_pair_started.set()
            await asyncio.Event().wait()
        else:
            await primary_pair_started.wait()
            yield TextDelta("one backup key")
            yield complete(model)

    started, closed, active, peak = install(monkeypatch, script)
    events = [event async for event in ProviderRouter().stream(request((PRIMARY, BACKUP), "hedged"))]

    assert events == [TextDelta("one backup key"), complete(BACKUP)]
    assert Counter(model for model, _ in started) == {PRIMARY: 2, BACKUP: 1}
    assert peak[BACKUP] == 1
    assert sorted(closed) == sorted(started)
    assert not any(active.values())


@pytest.mark.asyncio
@pytest.mark.parametrize("as_reserve", [False, True])
async def test_vertex_remains_eligible_without_adding_a_parallel_reserve(monkeypatch, as_reserve):
    vertex_model = "gemini-3.1-flash-lite"

    async def script(model, key, started):
        if key == "vertex":
            yield TextDelta("vertex answer")
            yield complete(model)
        elif as_reserve:
            yield overload()
        else:
            await asyncio.Event().wait()

    started, closed, active, peak = install(monkeypatch, script)
    monkeypatch.setattr("app.providers.gemini.is_vertex_client_available", lambda: True)
    models = (PRIMARY, vertex_model) if as_reserve else (vertex_model,)
    strategy = "hedged" if as_reserve else "sequential"
    events = [event async for event in ProviderRouter().stream(request(models, strategy))]

    assert events == [TextDelta("vertex answer"), complete(vertex_model)]
    assert peak[vertex_model] == (1 if as_reserve else 2)
    assert sum(key == "vertex" for _, key in started) == 1
    assert sorted(closed) == sorted(started)
    assert not any(active.values())


@pytest.mark.asyncio
async def test_silent_model_reserve_cannot_spawn_more_keys_after_soft_timer(monkeypatch):
    from contextlib import aclosing

    reserve_started = asyncio.Event()

    async def script(model, key, started):
        if model == BACKUP:
            reserve_started.set()
        await asyncio.Event().wait()
        yield TextDelta("unreachable")

    started, closed, active, peak = install(monkeypatch, script)
    monkeypatch.setattr("app.providers.router.KEY_RACE_HEDGE_DELAY_SECONDS", 0.01)

    async def consume():
        async with aclosing(ProviderRouter().stream(request((PRIMARY, BACKUP), "hedged"))) as stream:
            return [event async for event in stream]

    consumer = asyncio.create_task(consume())
    try:
        await asyncio.wait_for(reserve_started.wait(), 3)
        await asyncio.sleep(0.04)
        assert Counter(model for model, _ in started) == {PRIMARY: 2, BACKUP: 1}
    finally:
        consumer.cancel()
        await asyncio.gather(consumer, return_exceptions=True)
    assert sorted(closed) == sorted(started)
    assert not any(active.values())


@pytest.mark.asyncio
async def test_implicit_model_fallback_cannot_reopen_primary_race_via_vertex(monkeypatch):
    from dataclasses import replace

    from app.providers.stream_types import GroundingMode
    from app.repos import keys

    vertex_model = "gemini-3.1-flash-lite"

    async def script(model, key, started):
        yield TextDelta("resolved fallback")
        yield complete(model)

    started, closed, active, peak = install(monkeypatch, script)
    pool = keys.get_key_status_manager()

    async def resolve_fallback(model, *, excluded_key_hashes, **kwargs):
        return await pool.resolve_exact_ai_request(vertex_model, excluded_key_hashes=excluded_key_hashes)

    monkeypatch.setattr(pool, "resolve_ai_request", resolve_fallback)
    monkeypatch.setattr("app.providers.gemini.is_vertex_client_available", lambda: True)
    generation = replace(
        request((PRIMARY,)), allow_model_fallback=True, grounding=GroundingMode.PROVIDER_SEARCH_REQUIRED
    )
    events = [event async for event in ProviderRouter().stream(generation)]

    assert events == [TextDelta("resolved fallback"), complete(vertex_model)]
    assert started == [(vertex_model, f"{vertex_model}:synthetic-0")]
    assert peak[vertex_model] == 1
    assert sorted(closed) == sorted(started)
    assert not any(active.values())


@pytest.mark.asyncio
async def test_cancel_during_loser_cleanup_waits_for_provider_finalizer(monkeypatch):
    from contextlib import aclosing

    loser_started = asyncio.Event()
    cleanup_started = asyncio.Event()
    release_cleanup = asyncio.Event()
    cleanup_finished = asyncio.Event()

    async def script(model, key, started):
        if key.endswith("-0"):
            await loser_started.wait()
            yield TextDelta("winner")
            yield complete(model)
        else:
            loser_started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cleanup_started.set()
                await release_cleanup.wait()
                cleanup_finished.set()

    started, closed, active, peak = install(monkeypatch, script)

    async def consume():
        async with aclosing(ProviderRouter().stream(request((PRIMARY,)))) as stream:
            return [event async for event in stream]

    consumer = asyncio.create_task(consume())
    try:
        await asyncio.wait_for(cleanup_started.wait(), 3)
        consumer.cancel()
        await asyncio.sleep(0)
        await asyncio.sleep(0)
    finally:
        release_cleanup.set()
        await asyncio.gather(consumer, return_exceptions=True)

    assert cleanup_finished.is_set(), "Owner cancellation interrupted provider resource cleanup"
    assert consumer.cancelled()
    assert sorted(closed) == sorted(started)
    assert not any(active.values())
