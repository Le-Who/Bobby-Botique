"""Crocodile auxiliary generation accounts for unsuccessful dispatches too."""

import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["success", "timeout", "rate_limit", "invalid", "refused", "quota_store_error"])
async def test_judge_reserves_before_dispatch_without_post_success_increment(monkeypatch, outcome):
    from app.games import judge

    events = []
    tasks = []

    @asynccontextmanager
    async def lease():
        yield

    async def reserve(*args, **kwargs):
        events.append("reserve")
        if outcome == "quota_store_error":
            raise ConnectionError("offline quota store")
        return outcome != "refused"

    async def generate(**kwargs):
        events.append("sdk")
        if outcome == "timeout":
            raise TimeoutError("synthetic timeout")
        if outcome == "rate_limit":
            from google.genai.errors import APIError

            raise APIError(
                429, {"error": {"message": "Quota exceeded " + "private-details " * 100 + "retryDelay: 47s"}}
            )
        return SimpleNamespace(
            text='{"status":"hot","score":0.8,"hint":"close"}' if outcome == "success" else '{"status":"invalid"}'
        )

    use_case = SimpleNamespace(
        resolve_ai_request=AsyncMock(
            side_effect=[
                ({"api_key": "fake-key", "key_hash": "fake-hash"}, "gemini-test", None),
                (None, None, None),
                (None, None, None),
            ]
        ),
        reserve_key_usage=reserve,
        increment_key_usage=AsyncMock(),
    )
    health = SimpleNamespace(record_success=AsyncMock(), suspend_key=AsyncMock())
    client = SimpleNamespace(aio=SimpleNamespace(models=SimpleNamespace(generate_content=generate)))
    monkeypatch.setattr("app.agent_use_cases.AgentRequestUseCase", lambda: use_case)
    monkeypatch.setattr("app.providers.gemini.get_cached_genai_client", lambda _: client)
    monkeypatch.setattr("app.providers.gemini.get_vertex_client", lambda: None)
    monkeypatch.setattr("app.repos.keys.get_key_status_manager", lambda: health)
    monkeypatch.setattr(judge, "acquire_foreground_slot", AsyncMock(side_effect=lambda *args: lease()))
    monkeypatch.setattr(judge, "record_result", AsyncMock())
    monkeypatch.setattr(judge, "_primary_circuit_open_until", 0.0)
    monkeypatch.setattr(judge, "submit_task", lambda coro: tasks.append(asyncio.create_task(coro)))
    result = await judge._race_generate("synthetic", "guess")
    await asyncio.gather(*tasks)
    assert (result is not None) is (outcome == "success")
    assert events == (["reserve"] if outcome in {"refused", "quota_store_error"} else ["reserve", "sdk"])
    use_case.increment_key_usage.assert_not_awaited()
    if outcome in {"refused", "quota_store_error", "invalid"}:
        health.suspend_key.assert_not_awaited()
    if outcome in {"refused", "quota_store_error"}:
        assert judge._primary_circuit_open_until == 0.0
    if outcome == "rate_limit":
        from app.errors import extract_retry_after_seconds

        diagnostic = health.suspend_key.await_args.args[3]
        assert extract_retry_after_seconds(diagnostic) == 47
        assert "private-details" not in diagnostic


@pytest.mark.asyncio
@pytest.mark.parametrize("allowed", [False, True])
async def test_automatic_category_reserves_each_call(monkeypatch, allowed):
    from app.games import daily_ai, word_bank

    events = []
    monkeypatch.setattr(daily_ai, "get_daily_text_model_for", AsyncMock(return_value=""))
    monkeypatch.setattr("app.games.judgement_cache.cache_word_category", AsyncMock())
    monkeypatch.setattr("app.games.judgement_cache.get_cached_word_category", AsyncMock(return_value=None))

    async def reserve(*args, **kwargs):
        events.append("reserve")
        return allowed

    async def respond(**kwargs):
        events.append("sdk")
        return "Техника", 2

    use_case = SimpleNamespace(
        resolve_ai_request=AsyncMock(return_value=({"api_key": "fake", "key_hash": "fake-hash"}, "gemini-test", None)),
        reserve_key_usage=reserve,
    )
    monkeypatch.setattr("app.agent_use_cases.AgentRequestUseCase", lambda: use_case)
    monkeypatch.setattr("app.providers.router.get_ai_response", respond)
    await word_bank.resolve_custom_word_category("неизвестный прибор")
    assert events == (["reserve", "sdk"] if allowed else ["reserve", "reserve"])
