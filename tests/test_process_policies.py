from dataclasses import replace
from types import MappingProxyType, SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from app.providers.router import ProviderRouter
from app.providers.stream_types import GenerationRequest, PromptRole, PromptTurn, TextPart


def test_explicit_sequential_plan_does_not_start_model_hedging():
    request = GenerationRequest(
        models=("gemini-3.5-flash", "gemini-3.1-flash-lite"),
        turns=(PromptTurn(PromptRole.USER, (TextPart("test"),)),),
        allow_deferred=False,
        route_strategy="sequential",
        allow_model_fallback=False,
    )
    assert ProviderRouter._interactive_hedge_models(request) == []
    hedge = replace(request, route_strategy="hedged")
    assert ProviderRouter._interactive_hedge_models(hedge) == list(request.models)


def test_policy_validation_accepts_new_ids_but_rejects_incompatible_hedge():
    from app.process_policies import validate_policy

    value = {"models": ["gemini-future-model"], "strategy": "sequential", "inherit_user_model": False}
    assert validate_policy("chat", value)["models"] == ["gemini-future-model"]
    with pytest.raises(ValueError, match="hedg"):
        validate_policy("chat", {**value, "models": ["vendor/model"], "strategy": "hedged"})


def test_policy_rejects_unknown_fields_empty_models_and_unknown_process():
    from app.process_policies import validate_policy

    with pytest.raises(ValueError):
        validate_policy("chat", {"models": [], "strategy": "sequential", "inherit_user_model": False})
    with pytest.raises(ValueError):
        validate_policy("chat", {"models": ["gemini-test"], "unlimited": True})
    with pytest.raises(KeyError):
        validate_policy("not-a-process", {"models": ["gemini-test"]})


@pytest.mark.asyncio
async def test_exact_key_resolution_does_not_switch_model_when_keys_exhausted():
    from app.agent_use_cases import AgentRequestUseCase

    with patch("app.agent_use_cases.get_available_gemini_key", new=AsyncMock(return_value=None)) as keys:
        result = await AgentRequestUseCase().resolve_exact_ai_request("gemini-future-model")
    assert result[0] is None
    assert keys.await_count == 1
    assert keys.call_args.args[0] == "gemini-future-model"


@pytest.mark.asyncio
async def test_factory_applies_immutable_admin_chain_and_revision():
    from app.providers.request_factory import generation_request_from_history

    snapshot = SimpleNamespace(
        revision=7,
        values=MappingProxyType(
            {
                "process:chat": MappingProxyType(
                    {"models": ("gemini-new", "gemini-backup"), "strategy": "sequential", "inherit_user_model": False}
                )
            }
        ),
    )
    with patch("app.runtime_settings.store.get_snapshot", new=AsyncMock(return_value=snapshot)):
        request = await generation_request_from_history(
            process_id="chat", models=("gemini-original",), history=[{"role": "user", "parts": ["hello"]}]
        )
    assert request.models == ("gemini-new", "gemini-backup")
    assert request.policy_revision == 7
    assert request.allow_model_fallback is False


@pytest.mark.asyncio
async def test_text_adapter_uses_exact_stream_for_override_and_preserves_usage():
    from app.process_policies import ResolvedPolicy, execute_text_process
    from app.providers.stream_types import (
        FinishReason,
        GroundingReport,
        ProviderKind,
        RouteUsed,
        StreamCompleted,
        TextDelta,
        TokenUsage,
    )

    class Router:
        def __init__(self):
            self.request = None

        async def stream(self, request):
            self.request = request
            yield TextDelta("Answer")
            yield StreamCompleted(
                finish_reason=FinishReason.from_raw("STOP"),
                usage=TokenUsage(total=10),
                grounding=GroundingReport(),
                route=RouteUsed(
                    provider=ProviderKind.GEMINI, requested_model=request.models[0], actual_model=request.models[0]
                ),
            )

    router = Router()
    policy = ResolvedPolicy(("gemini-chosen", "gemini-backup"), "sequential", True, 9)
    with patch("app.process_policies.resolve_process", new=AsyncMock(return_value=policy)):
        text, tokens = await execute_text_process(
            "summary", ("gemini-old",), [{"role": "user", "parts": ["hello"]}], router=router
        )
    assert (text, tokens) == ("Answer", 10)
    assert router.request.models == policy.models
    assert router.request.allow_model_fallback is False


@pytest.mark.asyncio
async def test_inline_override_does_not_start_duplicate_legacy_standby():
    from app.handlers.inline import _generate_inline_answer
    from app.process_policies import ResolvedPolicy

    policy = ResolvedPolicy(("gemini-configured", "gemini-backup"), "sequential", True, 1)
    with (
        patch("app.process_policies.resolve_process", new=AsyncMock(return_value=policy)),
        patch("app.handlers.inline._stream_inline_fast", new=AsyncMock(return_value=("Answer", []))) as stream,
        patch("app.handlers.inline._stream_inline_primary", new=AsyncMock()) as primary,
    ):
        result = await _generate_inline_answer("gemini-original", "question", [], None, 1, True)
    assert result[0] == "Answer"
    stream.assert_awaited_once()
    primary.assert_not_awaited()
