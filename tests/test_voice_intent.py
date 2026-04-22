from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from app.voice_intent import detect_tts_intent


@pytest.mark.asyncio
async def test_detect_tts_intent_from_user_text():
    decision = await detect_tts_intent(user_text="Пожалуйста, озвучь ответ про погоду")
    assert decision.explicit_tts is True
    assert decision.source == "user_text"


@pytest.mark.asyncio
async def test_detect_tts_intent_from_caption():
    decision = await detect_tts_intent(caption="Прочитай вслух, что на картинке")
    assert decision.explicit_tts is True
    assert decision.source == "user_text"


@pytest.mark.asyncio
async def test_detect_tts_intent_for_single_forwarded_short_command():
    forwarded_entries = [SimpleNamespace(text="Озвучь текст", is_forwarded=True, is_user_authored=False)]
    decision = await detect_tts_intent(
        llm_context="<forwarded_dialogue>Озвучь текст</forwarded_dialogue>",
        forwarded_entries=forwarded_entries,
    )
    assert decision.explicit_tts is True
    assert decision.source == "single_forward"


@pytest.mark.asyncio
async def test_detect_tts_intent_uses_classifier_for_ambiguous_forward():
    forwarded_entries = [
        SimpleNamespace(
            text="Он сказал озвучь ответ позже, а потом обсуждение ушло в сторону",
            is_forwarded=True,
            is_user_authored=False,
        )
    ]
    classifier_result = SimpleNamespace(explicit_tts=False, confidence=0.2, source="classifier", reason="opencode_no")
    with patch("app.voice_intent._classify_ambiguous_tts_intent", new_callable=AsyncMock) as mock_classifier:
        mock_classifier.return_value = classifier_result
        decision = await detect_tts_intent(
            llm_context="<forwarded_dialogue>Он сказал озвучь ответ позже</forwarded_dialogue>",
            forwarded_entries=forwarded_entries,
        )
    assert decision.explicit_tts is False
    mock_classifier.assert_awaited_once()


@pytest.mark.asyncio
async def test_detect_tts_intent_prefers_user_text_over_forwarded_content():
    user_entries = [SimpleNamespace(text="озвучь", is_forwarded=False, is_user_authored=True)]
    forwarded_entries = [
        SimpleNamespace(text="длинная пересланная переписка без команды", is_forwarded=True, is_user_authored=False)
    ]
    decision = await detect_tts_intent(
        llm_context="озвучь\n<forwarded_dialogue>...</forwarded_dialogue>",
        user_entries=user_entries,
        forwarded_entries=forwarded_entries,
    )
    assert decision.explicit_tts is True
    assert decision.source == "user_text"
