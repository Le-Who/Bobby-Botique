"""Unit tests for OCR-specific prompt and model selection in ai_photo."""

from unittest.mock import MagicMock, patch

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


def test_pick_ocr_model_prefer_3_5():
    """_pick_ocr_model should prefer gemini-3.5-flash if available."""
    mock_settings = MagicMock()
    mock_settings.AVAILABLE_MODELS = ["gemini-3.1-flash-lite", "gemini-3.5-flash"]

    with patch("app.handlers.ai_photo.settings", mock_settings):
        model = _pick_ocr_model()
        assert model == "gemini-3.5-flash"


def test_pick_ocr_model_fallback_3_1_lite():
    """_pick_ocr_model should fall back to gemini-3.1-flash-lite if 3.5 is not available."""
    mock_settings = MagicMock()
    mock_settings.AVAILABLE_MODELS = ["gemini-3.1-flash-lite"]

    with patch("app.handlers.ai_photo.settings", mock_settings):
        model = _pick_ocr_model()
        assert model == "gemini-3.1-flash-lite"


def test_pick_ocr_model_ignores_stale_models_and_uses_primary_default():
    """_pick_ocr_model should never select stale Gemini chat models."""
    mock_settings = MagicMock()
    mock_settings.AVAILABLE_MODELS = ["gemini-2.5-flash", "gemini-3-flash-preview"]
    mock_settings.DEFAULT_MODEL = "gemini-2.5-flash"

    with patch("app.handlers.ai_photo.settings", mock_settings):
        model = _pick_ocr_model()
        assert model == "gemini-3.5-flash"


def test_pick_ocr_model_final_fallback():
    """_pick_ocr_model should fall back to the first available model or default model."""
    mock_settings = MagicMock()
    mock_settings.AVAILABLE_MODELS = ["gemini-3.1-flash-lite"]
    mock_settings.DEFAULT_MODEL = "gemini-3.1-flash-lite"

    with patch("app.handlers.ai_photo.settings", mock_settings):
        model = _pick_ocr_model()
        assert model == "gemini-3.1-flash-lite"
