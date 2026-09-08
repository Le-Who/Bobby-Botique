"""Process overrides must not bleed into other Crocodile generation roles."""

from contextlib import nullcontext
from datetime import date
from unittest.mock import AsyncMock

import pytest

from app.games import crocodile_daily, daily_ai, hinting, judge, judgement_cache, word_bank
from app.repos import crocodile_daily as repo


@pytest.fixture
def role_settings(monkeypatch):
    values = {
        "daily_croc_text_model": "gemini-2.5-flash",
        "daily_croc_text_model_words": "gemini-2.5-pro",
        "daily_croc_text_model_category": "gemini-3.1-flash-lite",
        "daily_croc_text_model_hints": "gemini-3.5-flash-lite",
        "daily_croc_text_model_judge": "gemini-3.6-flash",
        "daily_croc_text_model_image_prompt": "gemini-3.1-pro",
    }

    async def setting(key, default=""):
        return values.get(key, default)

    monkeypatch.setattr("app.repos.settings_repo.get_global_setting", setting)
    return values


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "process, expected",
    [
        ("words", "gemini-2.5-pro"),
        ("category", "gemini-3.1-flash-lite"),
        ("hints", "gemini-3.5-flash-lite"),
        ("judge", "gemini-3.6-flash"),
        ("image_prompt", "gemini-3.1-pro"),
    ],
)
async def test_process_override_wins_over_legacy(role_settings, process, expected):
    resolver = getattr(daily_ai, "get_daily_text_model_for", None)
    assert resolver is not None, "process model resolver is missing"
    assert await resolver(process) == expected


@pytest.mark.asyncio
@pytest.mark.parametrize("override", ["", "  ", "gpt-5", "pollinations/gemini", None])
async def test_invalid_or_empty_override_inherits_legacy(role_settings, override):
    role_settings["daily_croc_text_model_hints"] = override
    resolver = getattr(daily_ai, "get_daily_text_model_for", None)
    assert resolver is not None, "process model resolver is missing"
    assert await resolver("hints") == "gemini-2.5-flash"
    role_settings["daily_croc_text_model"] = "gpt-5"
    assert await resolver("hints") == ""


@pytest.mark.asyncio
async def test_unknown_process_rejected(role_settings):
    resolver = getattr(daily_ai, "get_daily_text_model_for", None)
    assert resolver is not None, "process model resolver is missing"
    with pytest.raises(ValueError, match="process"):
        await resolver("unknown")


@pytest.mark.asyncio
async def test_classic_words_and_category_use_independent_roles(monkeypatch, role_settings):
    monkeypatch.setattr(judgement_cache, "get_cached_word_category", AsyncMock(return_value=None))
    monkeypatch.setattr(judgement_cache, "cache_word_category", AsyncMock())
    monkeypatch.setattr(word_bank, "acquire_foreground_slot", AsyncMock(side_effect=lambda *a: nullcontext()))
    monkeypatch.setattr(word_bank, "record_result", AsyncMock())
    generate = AsyncMock(side_effect=['{"word":"telescope"}', '{"category":"Техника"}'])
    monkeypatch.setattr(daily_ai, "generate_daily_text", generate)
    assert await word_bank._generate_single_word_fast("astronomy", "en") == "telescope"
    assert generate.call_args.args[1] == "gemini-2.5-pro"
    assert await word_bank.resolve_custom_word_category("неизвестный прибор") == "Техника"
    assert generate.call_args.args[1] == "gemini-3.1-flash-lite"


@pytest.mark.asyncio
async def test_classic_hints_batch_and_judge_use_independent_roles(monkeypatch, role_settings):
    monkeypatch.setattr(judgement_cache, "get_cached_judgement", AsyncMock(return_value=None))
    monkeypatch.setattr(judgement_cache, "cache_judgement", AsyncMock())
    generate = AsyncMock(
        side_effect=[
            '{"hints":["один","два","три"]}',
            '{"items":[{"word":"кот","hints":["один","два","три"]}]}',
            '{"status":"warm","score":0.6,"hint":"Подумай ещё"}',
        ]
    )
    monkeypatch.setattr(daily_ai, "generate_daily_text", generate)
    assert await judge.generate_hints("кот", "животные") == ["один", "два", "три"]
    assert generate.call_args.args[1] == "gemini-3.5-flash-lite"
    assert (await hinting._generate_batched_hints(("кот", "тигр"), "животные"))["кот"] == ["один", "два", "три"]
    assert generate.call_args.args[1] == "gemini-3.5-flash-lite"
    assert (await judge.judge_guess("кот", "тигр"))[1].score == 0.6
    assert generate.call_args.args[1] == "gemini-3.6-flash"


@pytest.mark.asyncio
async def test_image_description_uses_own_role_and_cache(monkeypatch, role_settings):
    monkeypatch.setattr(word_bank, "_PROMPT_TRANSLATION_CACHE", {})
    generate = AsyncMock(
        side_effect=[
            '{"visual_description":"a ceramic teapot"}',
            '{"visual_description":"a steel kettle"}',
        ]
    )
    monkeypatch.setattr(daily_ai, "generate_daily_text", generate)
    assert await crocodile_daily._translate_word_for_prompt("чайник") == "a ceramic teapot"
    assert generate.call_args.args[1] == "gemini-3.1-pro"
    role_settings["daily_croc_text_model_hints"] = "gemini-2.5-pro"
    assert await crocodile_daily._translate_word_for_prompt("чайник") == "a ceramic teapot"
    assert generate.await_count == 1
    role_settings["daily_croc_text_model_image_prompt"] = "gemini-2.5-pro"
    assert await crocodile_daily._translate_word_for_prompt("чайник") == "a steel kettle"
    assert generate.call_args.args[1] == "gemini-2.5-pro"


@pytest.mark.asyncio
async def test_daily_words_and_hints_use_same_role_settings(monkeypatch, role_settings):
    generate = AsyncMock(side_effect=['{"word":"самовар"}', '{"hints":["Ручка","Носик","Кипяток"]}'])
    monkeypatch.setattr(daily_ai, "generate_daily_text", generate)
    monkeypatch.setattr(repo, "set_puzzle_hints", AsyncMock())
    topic = word_bank.resolve_topic("предметы")
    assert (await repo._pick_daily_word(topic, used_words=set(), difficulty="easy"))[0] == "самовар"
    assert generate.call_args.args[1] == "gemini-2.5-pro"
    puzzle = repo.DailyPuzzle(date(2026, 9, 8), "чайник", "предметы", "ru")
    assert await crocodile_daily.get_daily_hints(puzzle) == ["Ручка", "Носик", "Кипяток"]
    assert generate.call_args.args[1] == "gemini-3.5-flash-lite"


@pytest.mark.asyncio
async def test_prewarmed_hints_are_isolated_only_by_hints_model(monkeypatch, role_settings):
    monkeypatch.setattr(judgement_cache, "_hints_store", judgement_cache.OrderedDict())
    monkeypatch.setattr(judgement_cache, "_redis_hash_get", AsyncMock(return_value=None))
    monkeypatch.setattr(judgement_cache, "_persist_hints", AsyncMock())
    generate = AsyncMock(
        side_effect=[
            '{"items":[{"word":"кот","hints":["один","два","три"]},{"word":"тигр","hints":["лес","полосы","хищник"]}]}',
            '{"hints":["новый","второй","третий"]}',
        ]
    )
    monkeypatch.setattr(daily_ai, "generate_daily_text", generate)
    hinting.reset_hint_runtime_state_for_tests()
    try:
        await hinting._prewarm_topic_hints(("кот", "тигр"), "животные", topic_id="animals")
        assert generate.call_args.args[1] == "gemini-3.5-flash-lite"
        role_settings["daily_croc_text_model_judge"] = "gemini-2.5-pro"
        assert await hinting.get_or_generate_cached_hints("кот", "животные", topic_id="animals") == [
            "один",
            "два",
            "три",
        ]
        assert generate.await_count == 1
        role_settings["daily_croc_text_model_hints"] = "gemini-2.5-pro"
        assert await hinting.get_or_generate_cached_hints("кот", "животные", topic_id="animals") == [
            "новый",
            "второй",
            "третий",
        ]
        assert generate.call_args.args[1] == "gemini-2.5-pro"
    finally:
        hinting.reset_hint_runtime_state_for_tests()
