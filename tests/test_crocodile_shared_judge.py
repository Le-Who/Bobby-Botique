import asyncio
from unittest.mock import AsyncMock

import pytest

from app.games import daily_ai, hinting, judge, judgement_cache


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    monkeypatch.setattr(daily_ai, "get_daily_text_model", AsyncMock(return_value="gemini-3.6-flash"))
    monkeypatch.setattr(judgement_cache, "_store", judgement_cache.OrderedDict())
    monkeypatch.setattr(judgement_cache, "_hints_store", judgement_cache.OrderedDict())
    monkeypatch.setattr(judgement_cache, "_redis_hash_get", AsyncMock(return_value=None))
    monkeypatch.setattr(judgement_cache, "_persist", AsyncMock())
    monkeypatch.setattr(judgement_cache, "_persist_hints", AsyncMock())
    monkeypatch.setattr(judge, "_race_generate", AsyncMock(return_value=None))
    hinting.reset_hint_runtime_state_for_tests()
    yield
    hinting.reset_hint_runtime_state_for_tests()


@pytest.mark.asyncio
async def test_selected_model_judges_semantics_and_separates_cache(monkeypatch):
    calls = []

    async def generate(prompt, model, timeout):
        calls.append(model)
        assert "кот" in prompt and "тигр" in prompt and "score" in prompt
        score = 0.6 if model == "gemini-3.6-flash" else 0.1
        return f'{{"status":"warm","score":{score},"hint":"Продолжай думать"}}'

    monkeypatch.setattr(daily_ai, "generate_daily_text", generate)
    first = await judge.judge_guess("кот", "тигр", category="животные")
    assert first[1].score == 0.6
    await asyncio.sleep(0)
    assert (await judge.judge_guess("кот", "тигр", category="животные"))[1].cached
    monkeypatch.setattr(daily_ai, "get_daily_text_model", AsyncMock(return_value="gemini-3.5-flash-lite"))
    assert (await judge.judge_guess("кот", "тигр", category="животные"))[1].score == 0.1
    assert calls == ["gemini-3.6-flash", "gemini-3.5-flash-lite"]


@pytest.mark.asyncio
async def test_auto_judge_keeps_existing_lane(monkeypatch):
    monkeypatch.setattr(daily_ai, "get_daily_text_model", AsyncMock(return_value=""))
    judge._race_generate.return_value = judge.GuessJudgement(status="hot", score=0.8, hint="Близко")
    direct = AsyncMock(side_effect=AssertionError("Auto must use existing lane"))
    monkeypatch.setattr(daily_ai, "generate_daily_text", direct)
    status, result = await judge.judge_guess("кот", "тигр")
    assert status == "hot" and result.score == 0.8
    direct.assert_not_awaited()


@pytest.mark.asyncio
async def test_selected_hints_failure_uses_only_local_fallback(monkeypatch):
    monkeypatch.setattr(daily_ai, "generate_daily_text", AsyncMock(side_effect=TimeoutError()))
    router = AsyncMock(side_effect=AssertionError("selected model must not use router"))
    monkeypatch.setattr("app.providers.get_provider_router", router)
    hints = await judge.generate_hints("кот", "животные")
    assert len(hints) == 3 and "3" in hints[1]
    assert await hinting._generate_batched_hints(("кот", "тигр"), "животные") == {}
    router.assert_not_called()


@pytest.mark.asyncio
async def test_hint_generation_freezes_auto_choice_before_cache_lookup(monkeypatch):
    monkeypatch.setattr(daily_ai, "get_daily_text_model", AsyncMock(return_value=""))

    async def cache_miss(*args, **kwargs):
        monkeypatch.setattr(daily_ai, "get_daily_text_model", AsyncMock(return_value="gemini-3.6-flash"))
        return None

    async def generate(word, category, mode, *, model=None):
        return ["auto", "second", "third"] if model == "" else ["selected", "second", "third"]

    monkeypatch.setattr(judgement_cache, "get_cached_hints", cache_miss)
    monkeypatch.setattr(judge, "generate_hints", generate)
    assert await hinting.get_or_generate_cached_hints("кот", "животные") == ["auto", "second", "third"]


@pytest.mark.asyncio
@pytest.mark.parametrize("payload", [TimeoutError(), '{"score":5}', "not json"])
async def test_selected_judge_failure_never_falls_back(monkeypatch, payload):
    call = AsyncMock(side_effect=payload) if isinstance(payload, Exception) else AsyncMock(return_value=payload)
    monkeypatch.setattr(daily_ai, "generate_daily_text", call)
    assert (await judge.judge_guess("кот", "тигр"))[0] == "judge_unavailable"
    call.assert_awaited_once()
    judge._race_generate.assert_not_awaited()


@pytest.mark.asyncio
async def test_selected_local_exact_requires_no_generation(monkeypatch):
    call = AsyncMock(side_effect=AssertionError("network"))
    monkeypatch.setattr(daily_ai, "generate_daily_text", call)
    assert (await judge.judge_guess("кот", "кот"))[0] == "exact_match"
    call.assert_not_awaited()


@pytest.mark.asyncio
async def test_selected_hints_and_prewarm_use_direct_model_and_distinct_cache(monkeypatch):
    calls = []

    async def generate(prompt, model, timeout):
        calls.append(model)
        if '"items"' in prompt:
            return '{"items":[{"word":"кот","hints":["один","два","три"]},{"word":"тигр","hints":["лес","полосы","хищник"]}]}'
        return '{"hints":["новый","второй","третий"]}'

    monkeypatch.setattr(daily_ai, "generate_daily_text", generate)
    await hinting._prewarm_topic_hints(("кот", "тигр"), "животные", topic_id="animals")
    assert await hinting.get_or_generate_cached_hints("кот", "животные", topic_id="animals") == ["один", "два", "три"]
    monkeypatch.setattr(daily_ai, "get_daily_text_model", AsyncMock(return_value="gemini-3.5-flash-lite"))
    assert await hinting.get_or_generate_cached_hints("кот", "животные", topic_id="animals") == [
        "новый",
        "второй",
        "третий",
    ]
    assert calls == ["gemini-3.6-flash", "gemini-3.5-flash-lite"]
