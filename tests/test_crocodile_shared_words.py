"""Explicit Crocodile model selection must govern generated word content."""

from collections import OrderedDict
from contextlib import nullcontext
from unittest.mock import AsyncMock

import pytest

from app.games import daily_ai, judgement_cache, word_bank


@pytest.fixture(autouse=True)
def isolated_generation(monkeypatch):
    for name in ("_GENERATED_CACHE", "_GENERATED_INFLIGHT", "_PROVISIONAL_GENERATED"):
        monkeypatch.setattr(word_bank, name, {})
    monkeypatch.setattr(daily_ai, "get_daily_text_model", AsyncMock(return_value="gemini-2.5-pro"))
    monkeypatch.setattr(judgement_cache, "_redis_hash_get", AsyncMock(return_value=None))
    monkeypatch.setattr(judgement_cache, "_redis_hash_set", AsyncMock())
    monkeypatch.setattr(judgement_cache, "_generated_words_store", OrderedDict())
    monkeypatch.setattr(word_bank, "enqueue_bank_hint_prewarm", AsyncMock())
    monkeypatch.setattr(word_bank, "record_result", AsyncMock())
    monkeypatch.setattr(word_bank, "acquire_foreground_slot", AsyncMock(side_effect=lambda *args: nullcontext()))


@pytest.mark.asyncio
async def test_selected_model_generates_bank_and_isolates_model_cache(monkeypatch):
    generate = AsyncMock(
        side_effect=[
            '["apple", "pear", "plum", "melon", "kiwi"]',
            '["fork", "knife", "spoon", "plate", "cup"]',
        ]
    )
    monkeypatch.setattr(daily_ai, "generate_daily_text", generate)
    first = await word_bank.generate_words_for_category("custom", lang="en", topic_id="custom:test")
    assert first == ["apple", "pear", "plum", "melon", "kiwi"]
    assert generate.call_args.args[1] == "gemini-2.5-pro"
    word_bank._GENERATED_CACHE.clear()
    assert await word_bank.generate_words_for_category("custom", lang="en", topic_id="custom:test") == first
    assert generate.await_count == 1
    daily_ai.get_daily_text_model.return_value = "gemini-2.5-flash"
    assert await word_bank.generate_words_for_category("custom", lang="en", topic_id="custom:test") == [
        "fork",
        "knife",
        "spoon",
        "plate",
        "cup",
    ]
    assert generate.call_args.args[1] == "gemini-2.5-flash"


@pytest.mark.asyncio
async def test_fast_word_selected_model_bypasses_auto_race(monkeypatch):
    generate = AsyncMock(return_value='{"word":"telescope"}')
    monkeypatch.setattr(daily_ai, "generate_daily_text", generate)
    assert await word_bank._generate_single_word_fast("astronomy", "en") == "telescope"
    assert generate.call_args.args[1] == "gemini-2.5-pro"


@pytest.mark.asyncio
async def test_custom_category_selected_model(monkeypatch):
    monkeypatch.setattr(judgement_cache, "get_cached_word_category", AsyncMock(return_value=None))
    monkeypatch.setattr(judgement_cache, "cache_word_category", AsyncMock())
    generate = AsyncMock(return_value='{"category":"Техника"}')
    monkeypatch.setattr(daily_ai, "generate_daily_text", generate)
    assert await word_bank.resolve_custom_word_category("неизвестный прибор") == "Техника"
    assert generate.call_args.args[1] == "gemini-2.5-pro"


@pytest.mark.asyncio
async def test_builtin_word_never_reads_model_setting():
    result = await word_bank.pick_random_word_for_topic(word_bank.resolve_topic("животные"))
    assert result[0] in word_bank.WORD_BANK["ru"]["Животные"]
    daily_ai.get_daily_text_model.assert_not_awaited()


@pytest.mark.asyncio
async def test_custom_picker_ignores_automatic_bank_and_keeps_selected_snapshot(monkeypatch):
    topic = word_bank.resolve_topic("unusual artifacts")
    word_bank._GENERATED_CACHE[f"topic:{topic.topic_id}"] = ["old"] * 5
    monkeypatch.setattr(daily_ai, "generate_daily_text", AsyncMock(return_value='{"word":"telescope"}'))
    pending = []
    monkeypatch.setattr(word_bank, "submit_task", pending.append)
    try:
        assert await word_bank.pick_random_word_for_topic(topic) == ("telescope", "en", topic.category, True)
    finally:
        for coroutine in pending:
            coroutine.close()


@pytest.mark.asyncio
async def test_selected_model_failure_does_not_fall_back_to_router(monkeypatch):
    monkeypatch.setattr(daily_ai, "generate_daily_text", AsyncMock(side_effect=RuntimeError("unavailable")))
    assert await word_bank._generate_single_word_fast("astronomy", "en") is None
    assert await word_bank.generate_words_for_category("astronomy", lang="en") is None
    assert all(
        call.args[1:] == ("ai_studio", "gemini-2.5-pro") for call in word_bank.acquire_foreground_slot.await_args_list
    )


@pytest.mark.asyncio
async def test_auto_picker_keeps_auto_snapshot_during_setting_change(monkeypatch):
    daily_ai.get_daily_text_model.return_value = ""

    async def changing_cache(*args, **kwargs):
        daily_ai.get_daily_text_model.return_value = "gemini-2.5-pro"
        return None

    seen = []

    async def fast_word(category, lang, *, model=None):
        seen.append(model)
        return "telescope"

    monkeypatch.setattr(judgement_cache, "get_cached_generated_words", changing_cache)
    monkeypatch.setattr(word_bank, "_generate_single_word_fast", fast_word)
    monkeypatch.setattr(word_bank, "submit_task", lambda coroutine: coroutine.close())
    await word_bank.pick_random_word_for_topic(word_bank.resolve_topic("unusual artifacts"))
    assert seen == [""]


@pytest.mark.asyncio
@pytest.mark.parametrize("background", [False, True])
async def test_selected_bank_respects_budget_denial(monkeypatch, background):
    monkeypatch.setattr(word_bank, "acquire_foreground_slot", AsyncMock(return_value=None))
    monkeypatch.setattr(word_bank, "acquire_background_slot", AsyncMock(return_value=None))
    generate = AsyncMock(return_value='["apple", "pear", "plum", "melon", "kiwi"]')
    monkeypatch.setattr(daily_ai, "generate_daily_text", generate)
    assert await word_bank.generate_words_for_category("custom", lang="en", background=background) is None
    generate.assert_not_awaited()


@pytest.mark.asyncio
async def test_selected_fast_word_respects_budget_denial(monkeypatch):
    monkeypatch.setattr(word_bank, "acquire_foreground_slot", AsyncMock(return_value=None))
    generate = AsyncMock(return_value='{"word":"telescope"}')
    monkeypatch.setattr(daily_ai, "generate_daily_text", generate)
    assert await word_bank._generate_single_word_fast("astronomy", "en") is None
    generate.assert_not_awaited()
