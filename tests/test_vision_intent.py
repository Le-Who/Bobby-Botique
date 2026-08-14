"""Unit tests for vision intent classification."""

from unittest.mock import AsyncMock, patch

import pytest

from app.utils import vision_intent
from app.utils.vision_intent import classify_vision_intent


@pytest.fixture(autouse=True)
def clear_intent_cache():
    """Keep cached classifications from leaking between independent scenarios."""
    vision_intent._INTENT_CACHE.clear()
    yield
    vision_intent._INTENT_CACHE.clear()


@pytest.mark.asyncio
async def test_classify_vision_intent_empty():
    """Empty or None caption should return 'describe' immediately."""
    assert await classify_vision_intent(None) == "describe"
    assert await classify_vision_intent("") == "describe"
    assert await classify_vision_intent("   ") == "describe"

@pytest.mark.asyncio
async def test_classify_vision_intent_regex_ocr_ru():
    """Russian OCR keywords should trigger 'ocr' immediately."""
    assert await classify_vision_intent("перепиши текст с картинки") == "ocr"
    assert await classify_vision_intent("распознай текст") == "ocr"
    assert await classify_vision_intent("что тут написано?") == "ocr"
    assert await classify_vision_intent("какой текст на фото?") == "ocr"
    assert await classify_vision_intent("сделай перевод текста") == "ocr"
    assert await classify_vision_intent("скопируй текст отсюда") == "ocr"

@pytest.mark.asyncio
async def test_classify_vision_intent_regex_ocr_en():
    """English OCR keywords should trigger 'ocr' immediately."""
    assert await classify_vision_intent("extract text from this image") == "ocr"
    assert await classify_vision_intent("transcribe please") == "ocr"
    assert await classify_vision_intent("read text") == "ocr"
    assert await classify_vision_intent("copy text from photo") == "ocr"

@pytest.mark.asyncio
async def test_classify_vision_intent_regex_describe():
    """Explicit describe keywords should trigger 'describe' immediately without LLM."""
    assert await classify_vision_intent("что нарисовано?") == "describe"
    assert await classify_vision_intent("опиши это фото") == "describe"
    assert await classify_vision_intent("что это такое?") == "describe"
    assert await classify_vision_intent("tell me what is this") == "describe"

@pytest.mark.asyncio
async def test_classify_vision_intent_llm_fallback_ocr():
    """Ambiguous caption should fall back to LLM, returning 'ocr' if LLM says so."""
    # Ambiguous caption that doesn't match any regex directly
    ambiguous = "какие символы видны?"
    
    with patch("app.utils.vision_intent._call_llm_for_intent", new_callable=AsyncMock) as mock_llm:
        mock_llm.return_value = "ocr"
        res = await classify_vision_intent(ambiguous)
        assert res == "ocr"
        mock_llm.assert_awaited_once_with(ambiguous)

@pytest.mark.asyncio
async def test_classify_vision_intent_llm_fallback_describe():
    """Ambiguous caption should fall back to LLM, returning 'describe' if LLM says so."""
    ambiguous = "красиво ли получилось?"
    
    with patch("app.utils.vision_intent._call_llm_for_intent", new_callable=AsyncMock) as mock_llm:
        mock_llm.return_value = "describe"
        res = await classify_vision_intent(ambiguous)
        assert res == "describe"
        mock_llm.assert_awaited_once_with(ambiguous)

@pytest.mark.asyncio
async def test_classify_vision_intent_error_fallback():
    """When LLM call fails, it should gracefully fall back to 'describe'."""
    ambiguous = "какие символы видны?"
    
    with patch("app.utils.vision_intent._call_llm_for_intent", new_callable=AsyncMock, side_effect=Exception("API failure")):
        res = await classify_vision_intent(ambiguous)
        assert res == "describe"
