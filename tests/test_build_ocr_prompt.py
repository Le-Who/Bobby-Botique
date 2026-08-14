"""Unit tests for OCR-specific prompt and model selection in ai_photo."""

from unittest.mock import MagicMock, patch

from app.config import GEMINI_ECONOMY_MODEL, GEMINI_PRIMARY_MODEL
from app.handlers.ai_photo import _build_ocr_prompt, _pick_ocr_model


def test_build_ocr_prompt_with_caption():
    """OCR prompt should incorporate the original caption and dictate extraction rules."""
    caption = "Распознай эту табличку"
    prompt = _build_ocr_prompt(caption)
    assert "Распознай эту табличку" in prompt
    assert "без какого-либо описания" in prompt.lower() or "только текст" in prompt.lower()


def test_build_ocr_prompt_without_caption():
    """OCR prompt without caption should have generic extraction rules."""
    prompt = _build_ocr_prompt(None)
    assert "распознай" in prompt.lower() or "извлеки" in prompt.lower()
    assert "без какого-либо описания" in prompt.lower() or "только текст" in prompt.lower()


def test_pick_ocr_model_prefers_current_primary():
    """_pick_ocr_model should prefer the current primary model if available."""
    mock_settings = MagicMock()
    mock_settings.AVAILABLE_MODELS = [GEMINI_ECONOMY_MODEL, GEMINI_PRIMARY_MODEL]

    with patch("app.handlers.ai_photo.settings", mock_settings):
        model = _pick_ocr_model()
        assert model == GEMINI_PRIMARY_MODEL


def test_pick_ocr_model_falls_back_to_current_economy():
    """_pick_ocr_model should use the current economy model if primary is unavailable."""
    mock_settings = MagicMock()
    mock_settings.AVAILABLE_MODELS = [GEMINI_ECONOMY_MODEL]

    with patch("app.handlers.ai_photo.settings", mock_settings):
        model = _pick_ocr_model()
        assert model == GEMINI_ECONOMY_MODEL


def test_pick_ocr_model_ignores_invalid_models_and_uses_primary_default():
    """_pick_ocr_model must reject syntactically invalid Gemini model IDs."""
    mock_settings = MagicMock()
    mock_settings.AVAILABLE_MODELS = ["not-gemini", "gemini bad id"]
    mock_settings.DEFAULT_MODEL = "gemini bad id"

    with patch("app.handlers.ai_photo.settings", mock_settings):
        model = _pick_ocr_model()
        assert model == GEMINI_PRIMARY_MODEL


def test_pick_ocr_model_final_fallback():
    """_pick_ocr_model should fall back to the first available model or default model."""
    mock_settings = MagicMock()
    mock_settings.AVAILABLE_MODELS = [GEMINI_ECONOMY_MODEL]
    mock_settings.DEFAULT_MODEL = GEMINI_ECONOMY_MODEL

    with patch("app.handlers.ai_photo.settings", mock_settings):
        model = _pick_ocr_model()
        assert model == GEMINI_ECONOMY_MODEL
