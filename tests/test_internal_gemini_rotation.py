"""Local RPM refusal rotates credentials before any provider dispatch."""

import hashlib
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest


def _key(value):
    return {"api_key": value, "key_hash": hashlib.sha256(value.encode()).hexdigest()}


@pytest.fixture
def rotation(monkeypatch):
    from app.repos import keys

    events = []
    initial, replacement = _key("initial"), _key("replacement")

    async def reserve(key_hash, model):
        events.append(("reserve", key_hash, model))
        return key_hash == replacement["key_hash"]

    async def select(model_name, **kwargs):
        assert initial["key_hash"] in kwargs.get("excluded_hashes", ())
        return replacement

    health = SimpleNamespace(record_success=AsyncMock(), suspend_key=AsyncMock())
    monkeypatch.setattr(keys, "reserve_gemini_key_usage", reserve)
    monkeypatch.setattr(keys, "reserve_gemini_minute_usage", reserve)
    monkeypatch.setattr(keys, "get_available_gemini_key", select)
    monkeypatch.setattr(keys, "get_key_status_manager", lambda: health)
    return SimpleNamespace(events=events, initial=initial, replacement=replacement, reserve=reserve, health=health)


@pytest.mark.asyncio
@pytest.mark.parametrize("synthesis", [False, True])
async def test_research_rotates_without_restarting_contents_or_budget(monkeypatch, rotation, synthesis):
    from app.core import agentic
    from app.core.research_budget import ResearchBudget

    contents = [{"role": "user", "parts": ["synthetic research"]}]
    observations = []

    def client(key):
        async def generate(**kwargs):
            rotation.events.append(("sdk", key, kwargs["model"]))
            assert kwargs["contents"] is contents
            return SimpleNamespace(usage_metadata=SimpleNamespace(total_token_count=10))

        return SimpleNamespace(aio=SimpleNamespace(models=SimpleNamespace(generate_content=generate)))

    real_attempt = agentic.start_workload_attempt

    def observed_attempt(**kwargs):
        observations.append(kwargs["api_key"])
        return real_attempt(**kwargs)

    monkeypatch.setattr(agentic, "reserve_gemini_key_usage", rotation.reserve)
    monkeypatch.setattr(agentic, "get_cached_genai_client", client)
    monkeypatch.setattr(agentic, "start_workload_attempt", observed_attempt)
    agent = agentic.AgenticSearch("gemini-test", "initial")
    budget = ResearchBudget()
    await agent._generate_budgeted(contents=contents, config=None, budget=budget, synthesis=synthesis)
    assert rotation.events == [
        ("reserve", rotation.initial["key_hash"], "gemini-test"),
        ("reserve", rotation.replacement["key_hash"], "gemini-test"),
        ("sdk", "replacement", "gemini-test"),
    ]
    assert agent.api_key == "replacement"
    assert observations == ["replacement"]
    assert budget.tokens_known == 10
    assert budget.unknown_usage_calls == 0
    assert budget.attempts_used == 0
    rotation.health.suspend_key.assert_not_awaited()


@pytest.mark.asyncio
async def test_roles_rotate_same_gemini_model_without_double_reservation(monkeypatch, rotation):
    from app.handlers import ai_core

    async def reserve(key_hash, model, use_openrouter):
        assert use_openrouter is False
        return await rotation.reserve(key_hash, model)

    async def respond(api_key, history, model, *args, **kwargs):
        rotation.events.append(("sdk", api_key, model))
        assert kwargs["provider_max_retries"] == 1
        return "role result", 1

    monkeypatch.setattr(ai_core, "_agent_use_case", SimpleNamespace(reserve_key_usage=reserve, get_ai_response=respond))
    assert await ai_core._get_ai_response("initial", [], "gemini-test", use_openrouter=False) == ("role result", 1)
    assert rotation.events == [
        ("reserve", rotation.initial["key_hash"], "gemini-test"),
        ("reserve", rotation.replacement["key_hash"], "gemini-test"),
        ("sdk", "replacement", "gemini-test"),
    ]
    rotation.health.suspend_key.assert_not_awaited()


@pytest.mark.asyncio
async def test_openrouter_roles_do_not_switch_to_gemini_on_refusal(monkeypatch):
    from app.handlers import ai_core

    replacement = AsyncMock()
    respond = AsyncMock()
    monkeypatch.setattr("app.repos.keys.reserve_available_gemini_key", replacement)
    monkeypatch.setattr(
        ai_core,
        "_agent_use_case",
        SimpleNamespace(reserve_key_usage=AsyncMock(return_value=False), get_ai_response=respond),
    )
    await ai_core._get_ai_response("initial", [], "gemini-test", use_openrouter=True)
    replacement.assert_not_awaited()
    respond.assert_not_awaited()


@pytest.mark.asyncio
async def test_daily_rotates_and_records_replacement_health(monkeypatch, rotation):
    from app.games import daily_ai

    monkeypatch.setattr(
        "app.repos.keys.get_available_gemini_key", AsyncMock(side_effect=[rotation.initial, rotation.replacement])
    )

    def client(key):
        async def generate(**kwargs):
            rotation.events.append(("sdk", key, kwargs["model"]))
            return SimpleNamespace(text='{"word":"synthetic"}')

        return SimpleNamespace(aio=SimpleNamespace(models=SimpleNamespace(generate_content=generate)))

    monkeypatch.setattr("app.providers.gemini.get_cached_genai_client", client)
    assert await daily_ai.generate_daily_text("synthetic", "gemini-test") == '{"word":"synthetic"}'
    assert rotation.events == [
        ("reserve", rotation.initial["key_hash"], "gemini-test"),
        ("reserve", rotation.replacement["key_hash"], "gemini-test"),
        ("sdk", "replacement", "gemini-test"),
    ]
    rotation.health.record_success.assert_awaited_once_with(rotation.replacement["key_hash"], "gemini-test")
    rotation.health.suspend_key.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("replacement_available", [False, True])
async def test_embeddings_admit_minutes_and_rotate_without_daily_accounting(
    monkeypatch, rotation, replacement_available
):
    from app.repos import memory

    daily = AsyncMock(side_effect=AssertionError("Embedding must retain its separate daily contract"))
    monkeypatch.setattr("app.repos.keys.reserve_gemini_key_usage", daily)
    if not replacement_available:
        monkeypatch.setattr("app.repos.keys.get_available_gemini_key", AsyncMock(return_value=None))

    def client(key):
        async def embed(**kwargs):
            rotation.events.append(("sdk", key, kwargs["model"]))
            return SimpleNamespace(embeddings=[SimpleNamespace(values=[0.25, 0.5])])

        return SimpleNamespace(aio=SimpleNamespace(models=SimpleNamespace(embed_content=embed)))

    monkeypatch.setattr(memory, "get_cached_genai_client", client)
    result = await memory._get_embedding("synthetic", "initial")
    expected = [("reserve", rotation.initial["key_hash"], memory.EMBEDDING_MODEL)]
    if replacement_available:
        expected.extend(
            [
                ("reserve", rotation.replacement["key_hash"], memory.EMBEDDING_MODEL),
                ("sdk", "replacement", memory.EMBEDDING_MODEL),
            ]
        )
    assert rotation.events == expected
    assert result == ([0.25, 0.5] if replacement_available else None)
    daily.assert_not_awaited()
    rotation.health.suspend_key.assert_not_awaited()
