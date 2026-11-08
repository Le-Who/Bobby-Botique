"""Quota diagnosis retains provider evidence without error bodies or project IDs."""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from google.genai.errors import APIError

from app.agent_use_cases import AgentRequestUseCase
from app.errors import ErrorCode, tag_error
from app.providers import gemini
from app.providers.base import AIResponse
from app.providers.stream_types import GenerationRequest, PromptRole, PromptTurn, TextPart


@pytest.mark.parametrize("structured", [False, True])
@pytest.mark.parametrize("streaming", [False, True])
async def test_quota_event_keeps_only_allowlisted_provider_evidence(monkeypatch, caplog, structured, streaming):
    body = {"code": 429, "status": "RESOURCE_EXHAUSTED", "message": "private-error-body"}
    if structured:
        body["details"] = [
            {
                "@type": "type.googleapis.com/google.rpc.QuotaFailure",
                "violations": [
                    {
                        "quotaMetric": "generativelanguage.googleapis.com/generate_content_free_tier_input_token_count",
                        "quotaId": "GenerateContentInputTokensPerModelPerMinute-FreeTier",
                        "quotaDimensions": {
                            "model": "gemini-3.5-flash-lite",
                            "location": "global",
                            "project": "private-project",
                        },
                        "quotaValue": "250000",
                        "description": "private-description",
                    }
                ],
            },
            {"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "47s"},
        ]
    error = APIError(429, {"error": body})
    client = SimpleNamespace(
        aio=SimpleNamespace(
            models=SimpleNamespace(
                generate_content=AsyncMock(side_effect=error),
                generate_content_stream=AsyncMock(side_effect=error),
            )
        )
    )
    monkeypatch.setattr(gemini, "get_cached_genai_client", lambda _: client)
    with caplog.at_level("INFO"):
        provider = gemini.GeminiProvider("synthetic-test-credential")
        if streaming:
            req = GenerationRequest(
                models=("gemini-3.5-flash-lite",), turns=(PromptTurn(PromptRole.USER, (TextPart("synthetic"),)),)
            )
            _events = [event async for event in provider.stream(req, model_name=req.models[0])]
        else:
            await provider._execute_request(
                [{"role": "user", "parts": ["synthetic"]}], "gemini-3.5-flash-lite", None, None, None, 5
            )
    events = [r for r in caplog.records if getattr(r, "_event_name", "") == "provider.quota_rejected"]
    assert len(events) == 1
    event = events[0]
    assert event.provider_status == "RESOURCE_EXHAUSTED"
    assert event.quota_evidence == ("structured" if structured else "unspecified")
    if structured:
        assert event.retry_after_seconds == 47
        assert event.quota_violations == [
            {
                "metric": "generativelanguage.googleapis.com/generate_content_free_tier_input_token_count",
                "id": "GenerateContentInputTokensPerModelPerMinute-FreeTier",
                "model": "gemini-3.5-flash-lite",
                "location": "global",
                "limit": 250000,
            }
        ]
    else:
        assert event.retry_after_seconds is None
        assert event.quota_violations == []
    serialized = json.dumps(vars(event), default=str)
    assert "private-error-body" not in serialized
    assert "private-description" not in serialized
    assert "private-project" not in serialized
    assert "synthetic-test-credential" not in serialized
    assert "private-error-body" not in caplog.text


async def test_legacy_agent_preserves_failure_object_for_health_but_keeps_tuple_contract(monkeypatch):
    response = AIResponse(
        text=tag_error(ErrorCode.RATE_LIMIT, "temporary"),
        token_count=0,
        success=False,
        error_message="429 RESOURCE_EXHAUSTED retryDelay:127s",
        provider="gemini",
        model="gemini-test",
    )
    provider = SimpleNamespace(provider_name="gemini", get_response=AsyncMock(return_value=response))
    monkeypatch.setattr("app.providers.get_provider_for_model", lambda *args: provider)
    received = []
    result = await AgentRequestUseCase().get_ai_response(
        "synthetic",
        [],
        "gemini-test",
        use_openrouter=False,
        on_provider_error=received.append,
    )
    assert result == (response.text, None)
    assert received == [response]


async def test_legacy_agent_without_observer_does_not_require_provider_name(monkeypatch):
    response = AIResponse(text=tag_error(ErrorCode.RATE_LIMIT, "temporary"), token_count=0, success=False)
    provider = SimpleNamespace(get_response=AsyncMock(return_value=response))
    monkeypatch.setattr("app.providers.get_provider_for_model", lambda *args: provider)
    result = await AgentRequestUseCase().get_ai_response("synthetic", [], "gemini-test", use_openrouter=False)
    assert result == (response.text, None)


@pytest.mark.parametrize("streaming", [False, True])
async def test_partial_api_error_without_optional_metadata_keeps_rate_classification(monkeypatch, caplog, streaming):
    class PartialAPIError(APIError):
        def __init__(self):
            self.code = 429

        def __str__(self):
            return "429 RESOURCE_EXHAUSTED"

    client = SimpleNamespace(
        aio=SimpleNamespace(
            models=SimpleNamespace(
                generate_content=AsyncMock(side_effect=PartialAPIError()),
                generate_content_stream=AsyncMock(side_effect=PartialAPIError()),
            )
        )
    )
    monkeypatch.setattr(gemini, "get_cached_genai_client", lambda _: client)
    provider = gemini.GeminiProvider("synthetic-test-credential")
    with caplog.at_level("INFO"):
        if streaming:
            request = GenerationRequest(
                models=("gemini-test",), turns=(PromptTurn(PromptRole.USER, (TextPart("synthetic"),)),)
            )
            events = [event async for event in provider.stream(request, model_name="gemini-test")]
            assert events[-1].code is ErrorCode.RATE_LIMIT
        else:
            response = await provider._execute_request(
                [{"role": "user", "parts": ["synthetic"]}], "gemini-test", None, None, None, 5
            )
            assert not response.success
            assert "[RATE_LIMIT]" in response.text
    event = next(r for r in caplog.records if getattr(r, "_event_name", "") == "provider.quota_rejected")
    assert event.provider_status == "unknown"
    assert event.quota_evidence == "unspecified"


@pytest.mark.parametrize("model", ["gemini-test", "models/gemini-test", "gemma-test", "anthropic/claude-test"])
async def test_unspecified_cooldown_change_is_scoped_to_google_models(monkeypatch, model):
    from datetime import UTC, datetime

    from app.repos import keys

    now = datetime(2026, 11, 8, 7, 46, tzinfo=UTC)

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return now.astimezone(tz)

    query = AsyncMock(side_effect=[[{"failure_count": 0}], []])
    monkeypatch.setattr(keys, "datetime", Clock)
    monkeypatch.setattr(keys, "db_query", query)
    monkeypatch.setattr(keys, "invalidate_key_cache", AsyncMock())
    await keys.KeyStatusManager().suspend_key("fake", model, "rate_limit")
    seconds = (query.await_args.args[1][2] - now).total_seconds()
    assert seconds == (15 if "/" in model and not model.startswith("models/") else 60)
