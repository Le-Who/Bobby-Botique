"""Direct research and legacy role generation share pre-dispatch admission."""

import asyncio
import hashlib
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["success", "timeout", "cancelled", "refused", "store_error"])
async def test_research_reserves_each_attempt_before_dispatch(monkeypatch, outcome):
    from app.core import agentic
    from app.core.research_budget import ResearchBudget

    events = []

    async def reserve(key_hash, model):
        assert key_hash == hashlib.sha256(b"fake-key").hexdigest()
        assert model == "gemini-test"
        events.append("reserve")
        if outcome == "store_error":
            raise ConnectionError("quota store unavailable")
        return outcome != "refused"

    async def generate(**kwargs):
        events.append("sdk")
        if outcome == "timeout":
            raise TimeoutError("synthetic")
        if outcome == "cancelled":
            raise asyncio.CancelledError
        return SimpleNamespace(usage_metadata=SimpleNamespace(total_token_count=10))

    monkeypatch.setattr("app.repos.keys.reserve_gemini_key_usage", reserve)
    monkeypatch.setattr("app.repos.keys.reserve_available_gemini_key", AsyncMock(return_value=None))
    monkeypatch.setattr(agentic, "reserve_gemini_key_usage", reserve, raising=False)
    client = SimpleNamespace(aio=SimpleNamespace(models=SimpleNamespace(generate_content=generate)))
    monkeypatch.setattr(agentic, "get_cached_genai_client", lambda _: client)
    agent = agentic.AgenticSearch("gemini-test", "fake-key")
    budget = ResearchBudget()
    if outcome == "success":
        await agent._generate_budgeted(contents=[], config=None, budget=budget)
        await agent._generate_budgeted(contents=[], config=None, budget=budget, synthesis=True)
        assert events == ["reserve", "sdk", "reserve", "sdk"]
        assert budget.tokens_known == 20
    else:
        with pytest.raises((Exception, asyncio.CancelledError)):
            await agent._generate_budgeted(contents=[], config=None, budget=budget)
        assert events == (["reserve"] if outcome in {"refused", "store_error"} else ["reserve", "sdk"])
        assert budget.unknown_usage_calls == (0 if outcome in {"refused", "store_error"} else 1)


@pytest.mark.asyncio
@pytest.mark.parametrize("allowed", [False, True])
async def test_legacy_role_boundary_reserves_once_and_disables_nested_retries(monkeypatch, allowed):
    from app.handlers import ai_core

    events = []

    async def reserve(key_hash, model, use_openrouter):
        assert key_hash == hashlib.sha256(b"fake-key").hexdigest()
        assert model == "gemini-test"
        events.append("reserve")
        return allowed

    async def respond(*args, **kwargs):
        assert kwargs["provider_max_retries"] == 1
        events.append("sdk")
        return "checked", 1

    monkeypatch.setattr(ai_core, "_agent_use_case", SimpleNamespace(reserve_key_usage=reserve, get_ai_response=respond))
    monkeypatch.setattr("app.repos.keys.reserve_available_gemini_key", AsyncMock(return_value=None))
    result, _ = await ai_core._get_ai_response("fake-key", [], "gemini-test")
    assert events == (["reserve", "sdk"] if allowed else ["reserve"])
    if allowed:
        assert result == "checked"
