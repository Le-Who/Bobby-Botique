"""Overrides reach formerly hardcoded instructions without replacing input data."""

from unittest.mock import AsyncMock

import pytest

from app import prompt_registry
from app.runtime_settings.lifecycle import load_controlled_prompts


@pytest.fixture
def registry(monkeypatch):
    load_controlled_prompts()
    value = prompt_registry.PromptRegistry()
    monkeypatch.setattr(prompt_registry, "_registry_instance", value)
    return value


def test_horoscope_prompt_override_preserves_user_and_transit_literals(registry):
    from app.intent_router import _build_horoscope_system_instruction

    data = 'query {day_ru}, JSON: {"n": 1}'
    with prompt_registry.prompt_scope(
        overrides={"horoscope.system": "Edited {user_text} / {signs_str} / {day_ru} / {astro_context}"}, revision=10
    ):
        text = _build_horoscope_system_instruction(
            user_text=data, signs_str="Aries", day_ru="tomorrow", astro_context="Keep {user_text}"
        )
    assert text == f"Edited {data} / Aries / tomorrow / Keep {{user_text}}"


async def test_daily_image_scene_override_uses_word_topic_and_difficulty(registry, monkeypatch):
    from app.games import crocodile_daily, daily_ai

    monkeypatch.setattr(daily_ai, "get_daily_text_model_for", AsyncMock(return_value="gemini-test"))
    monkeypatch.setattr(crocodile_daily, "_translate_word_for_prompt", AsyncMock(return_value="cat {topic}"))
    with prompt_registry.prompt_scope(
        overrides={"crocodile.image.scene": "Scene {display_word}; topic={topic}; {tension}"}, revision=11
    ):
        text = await crocodile_daily._build_daily_image_prompt("кот", "animals", difficulty="easy")
    assert text == "Scene cat {topic}; topic=animals; Make the composition immediately readable."


async def test_whisper_intent_wrapper_override_reaches_classification(registry, monkeypatch):
    from app.providers import pollinations
    from app.utils import multimodal_processor as media

    transcribe = AsyncMock(return_value="Keep {asr_instruction}, literal {raw_text}")
    monkeypatch.setattr(
        pollinations, "get_pollinations_provider", lambda: type("Provider", (), {"transcribe_audio": transcribe})()
    )
    classify = AsyncMock(return_value="transcribed\nINTENT:CONVERSATIONAL")
    monkeypatch.setattr(media, "_classify_intent_with_fallback", classify)
    with prompt_registry.prompt_scope(
        overrides={"media.intent.text": "Edited classifier\n{asr_instruction}\nQuoted input: {raw_text}"}, revision=12
    ):
        result = await media._transcribe_pollinations(b"audio", None)
    sent = classify.await_args.kwargs["prompt_parts"][0].text
    assert sent.startswith("Edited classifier\n")
    assert "Quoted input: Keep {asr_instruction}, literal {raw_text}" in sent
    assert result[0] == "transcribed"


def test_additional_instructions_are_exposed_with_required_variables(registry):
    rows = {row["id"]: row for row in registry.prompt_catalog()}
    expected = {
        "horoscope.system": {"user_text", "signs_str", "day_ru", "astro_context"},
        "crocodile.image.scene": {"display_word", "topic", "tension"},
        "media.intent.text": {"asr_instruction", "raw_text"},
        "inline.tabs": set(),
        "inline.search.enabled": set(),
        "inline.search.disabled": set(),
        "chat.continue": set(),
    }
    for name, variables in expected.items():
        assert set(rows[name]["variables"]) == variables
