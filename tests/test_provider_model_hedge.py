from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from app.providers import ProviderRouter
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
)


def _request() -> GenerationRequest:
    return GenerationRequest(
        models=("gemini-3.5-flash-lite", "gemini-3.1-flash-lite"),
        turns=(PromptTurn(PromptRole.USER, (TextPart("hello"),)),),
        allow_deferred=False,
    )


def _completion(model: str) -> StreamCompleted:
    return StreamCompleted(
        finish_reason=FinishReason.from_raw("STOP"),
        usage=TokenUsage(total=5),
        grounding=GroundingReport(),
        route=RouteUsed(provider=ProviderKind.GEMINI, requested_model=model, actual_model=model),
    )


class ModelKeys:
    async def resolve_ai_request(self, preferred_model, *, excluded_key_hashes, **kwargs):
        key_hash = f"key:{preferred_model}"
        if key_hash in excluded_key_hashes:
            return None, None, "all_exhausted"
        return {"api_key": key_hash, "key_hash": key_hash}, preferred_model, None

    async def reserve_key_usage(self, key_hash, model_name, _use_openrouter):
        return True


@pytest.mark.asyncio
async def test_interactive_fallback_starts_while_slow_primary_is_still_pending(monkeypatch) -> None:
    from app.providers import router as router_module

    monkeypatch.setattr(router_module, "MODEL_HEDGE_DELAY_SECONDS", 0.01, raising=False)
    primary_done = asyncio.Event()
    overlap = []

    class Provider:
        provider_name = "gemini"

        async def stream(self, request, *, model_name):
            if model_name == "gemini-3.5-flash-lite":
                try:
                    await asyncio.sleep(0.15)
                    yield StreamFailed(
                        code=router_module.ErrorCode.OVERLOADED,
                        phase=FailurePhase.BEFORE_TEXT,
                        retry=RetryDisposition.TRY_NEXT_KEY,
                        key=KeyDisposition.TRANSIENT_FAILURE,
                        diagnostic="HTTP 503",
                    )
                finally:
                    primary_done.set()
            else:
                assert request.models[0] == "gemini-3.5-flash-lite"
                overlap.append(not primary_done.is_set())
                yield TextDelta("backup answer")
                yield _completion(model_name)

    with (
        patch("app.agent_use_cases.AgentRequestUseCase", return_value=ModelKeys()),
        patch("app.repos.keys.get_key_status_manager", return_value=AsyncMock()),
        patch("app.providers.base.get_provider_for_model", return_value=Provider()),
    ):
        events = [event async for event in ProviderRouter().stream(_request())]

    assert overlap == [True]
    assert primary_done.is_set()
    assert events == [TextDelta("backup answer"), _completion("gemini-3.1-flash-lite")]


@pytest.mark.asyncio
async def test_fast_primary_does_not_launch_fallback(monkeypatch) -> None:
    from app.providers import router as router_module

    monkeypatch.setattr(router_module, "MODEL_HEDGE_DELAY_SECONDS", 0.05)
    started = []

    class Provider:
        provider_name = "gemini"

        async def stream(self, request, *, model_name):
            started.append(model_name)
            yield TextDelta("primary answer")
            yield _completion(model_name)

    with (
        patch("app.agent_use_cases.AgentRequestUseCase", return_value=ModelKeys()),
        patch("app.repos.keys.get_key_status_manager", return_value=AsyncMock()),
        patch("app.providers.base.get_provider_for_model", return_value=Provider()),
    ):
        events = [event async for event in ProviderRouter().stream(_request())]

    assert started == ["gemini-3.5-flash-lite"]
    assert events == [TextDelta("primary answer"), _completion("gemini-3.5-flash-lite")]


@pytest.mark.asyncio
async def test_failed_model_lanes_start_next_fallback_without_waiting_for_full_chain(monkeypatch) -> None:
    from app.providers import router as router_module

    monkeypatch.setattr(router_module, "MODEL_HEDGE_DELAY_SECONDS", 0.01)
    started = []
    request = GenerationRequest(
        models=("gemini-3.5-flash-lite", "gemini-3.1-flash-lite", "gemini-3.6-flash"),
        turns=(PromptTurn(PromptRole.USER, (TextPart("hello"),)),),
        allow_deferred=False,
        scope=RequestScope(user_id=7, chat_id=7),
    )

    class Provider:
        provider_name = "gemini"

        async def stream(self, request, *, model_name):
            started.append(model_name)
            if model_name != "gemini-3.6-flash":
                yield StreamFailed(
                    code=router_module.ErrorCode.OVERLOADED,
                    phase=FailurePhase.BEFORE_TEXT,
                    retry=RetryDisposition.TRY_NEXT_KEY,
                    key=KeyDisposition.TRANSIENT_FAILURE,
                    diagnostic="HTTP 503",
                )
            else:
                yield TextDelta("third model")
                yield _completion(model_name)

    router = ProviderRouter()
    limiter = AsyncMock()
    limiter.check_rate_limit.return_value = True
    router._rate_limiter = limiter
    with (
        patch("app.agent_use_cases.AgentRequestUseCase", return_value=ModelKeys()),
        patch("app.repos.keys.get_key_status_manager", return_value=AsyncMock()),
        patch("app.providers.base.get_provider_for_model", return_value=Provider()),
    ):
        events = [event async for event in router.stream(request)]

    assert started[:3] == ["gemini-3.5-flash-lite", "gemini-3.1-flash-lite", "gemini-3.6-flash"]
    assert events == [TextDelta("third model"), _completion("gemini-3.6-flash")]
    limiter.check_rate_limit.assert_awaited_once_with(7)


@pytest.mark.asyncio
async def test_all_failed_model_lanes_emit_one_failure_terminal(monkeypatch) -> None:
    from app.providers import router as router_module

    monkeypatch.setattr(router_module, "MODEL_HEDGE_DELAY_SECONDS", 0.01)
    monkeypatch.setattr(router_module, "_ordered_gemini_fallback_models", lambda model: [])

    class Provider:
        provider_name = "gemini"

        async def stream(self, request, *, model_name):
            yield StreamFailed(
                code=router_module.ErrorCode.OVERLOADED,
                phase=FailurePhase.BEFORE_TEXT,
                retry=RetryDisposition.TRY_NEXT_KEY,
                key=KeyDisposition.TRANSIENT_FAILURE,
                diagnostic="HTTP 503",
            )

    with (
        patch("app.agent_use_cases.AgentRequestUseCase", return_value=ModelKeys()),
        patch("app.repos.keys.get_key_status_manager", return_value=AsyncMock()),
        patch("app.providers.base.get_provider_for_model", return_value=Provider()),
    ):
        events = [event async for event in ProviderRouter().stream(_request())]

    assert len(events) == 1
    assert isinstance(events[0], StreamFailed)


@pytest.mark.asyncio
async def test_closing_hedged_stream_awaits_active_provider_cleanup(monkeypatch) -> None:
    from app.providers import router as router_module

    monkeypatch.setattr(router_module, "MODEL_HEDGE_DELAY_SECONDS", 0.05)
    closed = asyncio.Event()

    class Provider:
        provider_name = "gemini"

        async def stream(self, request, *, model_name):
            try:
                yield TextDelta("first text")
                await asyncio.Event().wait()
            finally:
                closed.set()

    with (
        patch("app.agent_use_cases.AgentRequestUseCase", return_value=ModelKeys()),
        patch("app.repos.keys.get_key_status_manager", return_value=AsyncMock()),
        patch("app.providers.base.get_provider_for_model", return_value=Provider()),
    ):
        stream = ProviderRouter().stream(_request())
        assert await anext(stream) == TextDelta("first text")
        await stream.aclose()

    assert closed.is_set()


@pytest.mark.asyncio
async def test_key_race_first_text_deadline_does_not_reset_after_one_key_fails(monkeypatch) -> None:
    from app.providers import router as router_module

    monkeypatch.setattr(router_module, "KEY_RACE_FIRST_TEXT_TIMEOUT_SECONDS", 0.05, raising=False)
    monkeypatch.setattr(router_module, "_ordered_gemini_fallback_models", lambda model: [])

    class TwoKeys:
        async def resolve_ai_request(self, preferred_model, *, excluded_key_hashes, **kwargs):
            for key in ("first", "second"):
                if key not in excluded_key_hashes:
                    return {"api_key": key, "key_hash": key}, preferred_model, None
            return None, None, "all_exhausted"

        async def reserve_key_usage(self, key_hash, model_name, _use_openrouter):
            return True

    class Provider:
        provider_name = "gemini"

        def __init__(self, key):
            self.key = key

        async def stream(self, request, *, model_name):
            if self.key == "first":
                await asyncio.sleep(0.03)
                yield StreamFailed(
                    code=router_module.ErrorCode.OVERLOADED,
                    phase=FailurePhase.BEFORE_TEXT,
                    retry=RetryDisposition.TRY_NEXT_KEY,
                    key=KeyDisposition.TRANSIENT_FAILURE,
                    diagnostic="HTTP 503",
                )
            else:
                await asyncio.Event().wait()
                yield TextDelta("never")

    request = GenerationRequest(
        models=("gemini-3.5-flash-lite",),
        turns=(PromptTurn(PromptRole.USER, (TextPart("hello"),)),),
    )
    router = ProviderRouter()
    with (
        patch("app.agent_use_cases.AgentRequestUseCase", return_value=TwoKeys()),
        patch("app.repos.keys.get_key_status_manager", return_value=AsyncMock()),
        patch("app.providers.base.get_provider_for_model", side_effect=lambda model, key: Provider(key)),
        patch.object(router, "_pick_transient_fallback_model", return_value=None),
    ):
        events = await asyncio.wait_for(_collect(router.stream(request)), timeout=0.2)

    assert len(events) == 1 and isinstance(events[0], StreamFailed)
    assert events[0].code is router_module.ErrorCode.TIMEOUT


async def _collect(stream):
    return [event async for event in stream]
