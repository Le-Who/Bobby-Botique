"""Real resolver contracts for model-scoped keys and hedged route ownership."""

from unittest.mock import AsyncMock

import pytest

from app.agent_use_cases import AgentRequestUseCase
from app.providers.router import ProviderRouter
from app.providers.stream_types import (
    FinishReason,
    GenerationRequest,
    GroundingReport,
    PromptRole,
    PromptTurn,
    ProviderKind,
    RouteUsed,
    StreamCompleted,
    TextDelta,
    TextPart,
    TokenUsage,
    Workload,
)

PRIMARY = "gemini-3.6-flash"
BACKUP = "gemini-3.5-flash"
LAST = "gemini-3.5-flash-lite"


def completion(model):
    return StreamCompleted(
        finish_reason=FinishReason.from_raw("STOP"),
        usage=TokenUsage(total=7),
        grounding=GroundingReport(),
        route=RouteUsed(provider=ProviderKind.GEMINI, requested_model=model, actual_model=model),
    )


def install_key_pool(monkeypatch, model_keys):
    """Replace only storage and provider transport; run the real key resolver."""
    lookups = []
    reservations = []
    calls = []

    async def lookup(model, *, excluded_hashes):
        lookups.append((model, frozenset(excluded_hashes)))
        for key in model_keys.get(model, ()):
            if key not in excluded_hashes:
                return {"api_key": key, "key_hash": key}
        return None

    async def reserve(key, model):
        reservations.append((key, model))
        return True

    class Provider:
        provider_name = "gemini"

        def __init__(self, key):
            self.key = key

        async def stream(self, request, *, model_name):
            calls.append((self.key, model_name))
            yield TextDelta(f"answer from {model_name}")
            yield completion(model_name)

    monkeypatch.setattr("app.agent_use_cases.get_available_gemini_key", lookup)
    monkeypatch.setattr("app.agent_use_cases.reserve_gemini_key_usage", reserve)
    monkeypatch.setattr("app.agent_use_cases.invalidate_key_cache", AsyncMock())
    monkeypatch.setattr("app.agent_use_cases.get_use_openrouter", lambda: False)
    monkeypatch.setattr(
        "app.agent_use_cases._gemini_fallback_priority",
        lambda model: {PRIMARY: [BACKUP, LAST], BACKUP: [LAST]}.get(model, []),
    )
    monkeypatch.setattr("app.repos.keys.get_key_status_manager", lambda: AsyncMock())
    monkeypatch.setattr("app.providers.base.get_provider_for_model", lambda model, key: Provider(key))
    return lookups, reservations, calls


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "model_keys,expected_model,expected_key",
    [
        ({PRIMARY: ["primary-key"], BACKUP: ["backup-key"]}, PRIMARY, "primary-key"),
        ({BACKUP: ["backup-key"], LAST: ["last-key"]}, BACKUP, "backup-key"),
    ],
    ids=["usable-primary", "first-fallback"],
)
async def test_companion_key_lookup_cannot_replace_the_selected_model(
    monkeypatch, model_keys, expected_model, expected_key
):
    # Resolving a companion on a different model must not reroute the first key.
    _lookups, reservations, calls = install_key_pool(monkeypatch, model_keys)
    request = GenerationRequest(
        models=(PRIMARY,),
        turns=(PromptTurn(PromptRole.USER, (TextPart("hello"),)),),
        workload=Workload.INLINE,
    )

    events = [event async for event in ProviderRouter().stream(request)]

    assert events == [TextDelta(f"answer from {expected_model}"), completion(expected_model)]
    assert calls == [(expected_key, expected_model)]
    assert reservations == [(expected_key, expected_model)]


@pytest.mark.asyncio
async def test_hedged_lanes_resolve_only_their_model_without_duplicate_fallback_attempts(monkeypatch):
    # A keyless primary must end its lane, leaving fallback work to its own lane.
    lookups, reservations, calls = install_key_pool(monkeypatch, {BACKUP: ["backup-key"]})
    monkeypatch.setattr("app.providers.router._ordered_gemini_fallback_models", lambda model: [])
    monkeypatch.setattr("app.providers.router.MODEL_HEDGE_DELAY_SECONDS", 0)
    attempt_events = []
    monkeypatch.setattr(
        "app.observability.provider_events.emit",
        lambda event, **fields: attempt_events.append((event, fields)),
    )
    request = GenerationRequest(
        models=(PRIMARY, BACKUP),
        turns=(PromptTurn(PromptRole.USER, (TextPart("hello"),)),),
        allow_deferred=False,
    )

    events = [event async for event in ProviderRouter().stream(request)]

    assert events == [TextDelta(f"answer from {BACKUP}"), completion(BACKUP)]
    assert calls == [("backup-key", BACKUP)]
    assert reservations == [("backup-key", BACKUP)]
    assert lookups == [(PRIMARY, frozenset()), (BACKUP, frozenset()), (BACKUP, frozenset({"backup-key"}))]
    assert [
        (fields["requested_model"], fields["actual_model"])
        for event, fields in attempt_events
        if event == "provider.attempt_started"
    ] == [(BACKUP, BACKUP)]


@pytest.mark.asyncio
async def test_exact_openrouter_resolution_cannot_expand_an_explicit_model_plan(monkeypatch):
    lookups = []

    async def lookup(model, *, excluded_hashes):
        lookups.append(model)
        if model == "vendor/selected":
            return None
        return {"api_key": "fallback-key", "key_hash": "fallback-hash"}

    monkeypatch.setattr("app.agent_use_cases.get_available_openrouter_key", lookup)

    result = await AgentRequestUseCase().resolve_exact_ai_request("vendor/selected")

    assert result == (None, None, "all_exhausted")
    assert lookups == ["vendor/selected"]
