"""Tests for app.utils.multimodal_processor — constants and configuration validation."""

from unittest.mock import AsyncMock

import pytest


class TestMultimodalProcessorConstants:
    """Verify multimodal processor configuration constants are sane."""

    def test_transcription_model_is_flash_lite(self):
        from app.utils.multimodal_processor import TRANSCRIPTION_MODEL

        assert "flash-lite" in TRANSCRIPTION_MODEL.lower()

    def test_image_model_is_flash_lite(self):
        from app.utils.multimodal_processor import IMAGE_DESCRIPTION_MODEL

        assert "flash-lite" in IMAGE_DESCRIPTION_MODEL.lower()

    def test_thinking_config_high_is_high(self):
        from app.utils.multimodal_processor import THINKING_CONFIG_HIGH

        assert THINKING_CONFIG_HIGH.thinking_level is not None
        assert "high" in str(THINKING_CONFIG_HIGH.thinking_level).lower()

    def test_thinking_config_medium_is_medium(self):
        from app.utils.multimodal_processor import THINKING_CONFIG_MEDIUM

        assert THINKING_CONFIG_MEDIUM.thinking_level is not None
        assert "medium" in str(THINKING_CONFIG_MEDIUM.thinking_level).lower()

    def test_voice_system_prompt_contains_transcription_instructions(self):
        from app.utils.multimodal_processor import _VOICE_SYSTEM_PROMPT

        assert "transcri" in _VOICE_SYSTEM_PROMPT.lower()
        assert "commentary" in _VOICE_SYSTEM_PROMPT.lower()

    def test_image_system_prompt_contains_visual_instructions(self):
        from app.utils.multimodal_processor import _IMAGE_SYSTEM_PROMPT

        assert "image" in _IMAGE_SYSTEM_PROMPT.lower() or "visual" in _IMAGE_SYSTEM_PROMPT.lower()

    def test_document_summary_prompt_exists(self):
        from app.utils.multimodal_processor import _DOCUMENT_SUMMARY_PROMPT

        assert "document" in _DOCUMENT_SUMMARY_PROMPT.lower()
        assert "summarize" in _DOCUMENT_SUMMARY_PROMPT.lower()

    def test_resilience_policy_has_retries(self):
        from app.utils.multimodal_processor import _MEDIA_RESILIENCE

        assert _MEDIA_RESILIENCE.max_retries >= 2
        assert _MEDIA_RESILIENCE.timeout_s > 0

    @pytest.mark.asyncio
    async def test_transcribe_voice_rejects_empty_bytes(self):
        """transcribe_voice should return (None, 'conversational') for empty audio without calling API."""
        from app.utils.multimodal_processor import transcribe_voice

        result = await transcribe_voice(b"", "fake-key")
        assert result == (None, "conversational", None)

    @pytest.mark.asyncio
    async def test_transcribe_voice_normalizes_legacy_model_override(self, monkeypatch):
        """Manual ASR model overrides must not send deprecated Gemini chat model IDs upstream."""
        from app.utils import multimodal_processor as mp
        from app.utils.multimodal_processor import TRANSCRIPTION_MODEL, transcribe_voice

        fake_pollinations = type(
            "FakePollinations",
            (),
            {"transcribe_audio": AsyncMock(return_value=None)},
        )()
        monkeypatch.setattr(
            "app.providers.pollinations.get_pollinations_provider",
            lambda: fake_pollinations,
        )
        generate_mock = AsyncMock(return_value="hello\nINTENT:CONVERSATIONAL")
        monkeypatch.setattr(mp, "_generate_with_resilience", generate_mock)

        result = await transcribe_voice(
            b"voice-bytes",
            "fake-key",
            model="gemini-2.5-pro",
        )

        assert result == ("hello", "conversational", None)
        assert generate_mock.await_args.kwargs["model"] == TRANSCRIPTION_MODEL

    @pytest.mark.asyncio
    async def test_describe_image_rejects_empty_bytes(self):
        """describe_image should return None for empty image without calling API."""
        from app.utils.multimodal_processor import describe_image

        result = await describe_image(b"", "fake-key")
        assert result is None

    @pytest.mark.asyncio
    async def test_summarize_document_rejects_short_text(self):
        """summarize_document_text should return None for too-short text."""
        from app.utils.multimodal_processor import summarize_document_text

        result = await summarize_document_text("short", "fake-key")
        assert result is None


class TestBackwardCompatImports:
    """Verify backward-compat shim in audio_processor.py works."""

    def test_audio_processor_exports_transcribe_voice(self):
        from app.utils.audio_processor import transcribe_voice  # noqa: F401

    def test_audio_processor_exports_constants(self):
        from app.utils.audio_processor import THINKING_CONFIG  # noqa: F401

        assert "high" in str(THINKING_CONFIG.thinking_level).lower()
