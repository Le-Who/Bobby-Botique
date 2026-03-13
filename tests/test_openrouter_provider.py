"""Tests for app.providers.openrouter — message conversion and error handling."""

import pytest

from app.providers.openrouter import OpenRouterProvider, _has_multimodal_content


@pytest.fixture
def provider():
    return OpenRouterProvider(api_key="test-key")


# ── _build_messages ──────────────────────────────────────────────────────────


class TestBuildMessages:
    """_build_messages converts Gemini-format history to OpenAI-format."""

    @pytest.mark.asyncio
    async def test_system_instruction_added(self, provider):
        messages = await provider._build_messages([], system_instruction="Be helpful")
        assert len(messages) == 1
        assert messages[0]["role"] == "system"
        assert messages[0]["content"] == "Be helpful"

    @pytest.mark.asyncio
    async def test_no_system_instruction(self, provider):
        messages = await provider._build_messages([], system_instruction=None)
        assert len(messages) == 0

    @pytest.mark.asyncio
    async def test_model_role_converted_to_assistant(self, provider):
        history = [
            {"role": "user", "parts": ["Hello"]},
            {"role": "model", "parts": ["Hi there"]},
        ]
        messages = await provider._build_messages(history, system_instruction=None)
        assert messages[0]["role"] == "user"
        assert messages[1]["role"] == "assistant"

    @pytest.mark.asyncio
    async def test_string_parts_extracted(self, provider):
        history = [{"role": "user", "parts": ["What is Python?"]}]
        messages = await provider._build_messages(history, system_instruction=None)
        assert messages[0]["content"] == "What is Python?"

    @pytest.mark.asyncio
    async def test_empty_parts_skipped(self, provider):
        history = [{"role": "user", "parts": []}]
        messages = await provider._build_messages(history, system_instruction=None)
        assert len(messages) == 0

    @pytest.mark.asyncio
    async def test_non_dict_items_skipped(self, provider):
        history = ["not a dict", {"role": "user", "parts": ["valid"]}]
        messages = await provider._build_messages(history, system_instruction=None)
        assert len(messages) == 1
        assert messages[0]["content"] == "valid"

    @pytest.mark.asyncio
    async def test_whitespace_only_parts_skipped(self, provider):
        history = [{"role": "user", "parts": ["  ", "Hello"]}]
        messages = await provider._build_messages(history, system_instruction=None)
        assert len(messages) == 1
        assert messages[0]["content"] == "Hello"

    @pytest.mark.asyncio
    async def test_parts_not_list_normalized(self, provider):
        history = [{"role": "user", "parts": "single string"}]
        messages = await provider._build_messages(history, system_instruction=None)
        assert len(messages) == 1


# ── _has_multimodal_content ──────────────────────────────────────────────────


class TestHasMultimodalContent:
    """Detect multimodal (image) parts in history."""

    def test_text_only_returns_false(self):
        history = [{"parts": ["text only"]}]
        assert _has_multimodal_content(history) is False

    def test_bytes_returns_true(self):
        history = [{"parts": [b"\x89PNG"]}]
        assert _has_multimodal_content(history) is True

    def test_bytearray_returns_true(self):
        history = [{"parts": [bytearray(b"\x89PNG")]}]
        assert _has_multimodal_content(history) is True

    def test_empty_history_returns_false(self):
        assert _has_multimodal_content([]) is False
