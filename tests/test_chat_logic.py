"""Tests for app.handlers.chat_logic — pure functions, zero mocking needed."""

import pytest

from app.handlers.chat_logic import (
    ResolutionResult,
    ResponseAction,
    build_memory_context,
    classify_resolution,
    classify_response,
)


# ═══════════════════════════════════════════════════════════════════════════════
# classify_resolution
# ═══════════════════════════════════════════════════════════════════════════════


class TestClassifyResolution:
    """Test the resolution branching logic extracted from _handle_regular_chat."""

    def test_proceed_on_direct_resolution(self):
        result = classify_resolution("direct", "gemini-2.0-flash")
        assert result.action == "proceed"
        assert result.user_message is None

    def test_all_exhausted_gemini(self):
        result = classify_resolution("all_exhausted", "gemini-2.0-flash")
        assert result.action == "all_exhausted"
        assert result.provider_name == "Gemini"
        assert "Gemini" in result.user_message
        assert "исчерпаны" in result.user_message

    def test_all_exhausted_openrouter(self):
        """OpenRouter models contain '/' in name."""
        result = classify_resolution("all_exhausted", "anthropic/claude-3")
        assert result.action == "all_exhausted"
        assert result.provider_name == "OpenRouter"
        assert "OpenRouter" in result.user_message

    def test_all_exhausted_none_model_defaults_to_gemini(self):
        result = classify_resolution("all_exhausted", None)
        assert result.provider_name == "Gemini"

    def test_confirm_fallback(self):
        result = classify_resolution("confirm_fallback", "gemini-pro", "gemini-flash")
        assert result.action == "confirm_fallback"
        assert result.fallback_model == "gemini-flash"
        assert "gemini-pro" in result.user_message
        assert "gemini-flash" in result.user_message
        assert "Продолжить?" in result.user_message

    def test_unknown_resolution_proceeds(self):
        """Any unrecognized resolution string should proceed normally."""
        result = classify_resolution("some_new_status", "model")
        assert result.action == "proceed"


# ═══════════════════════════════════════════════════════════════════════════════
# build_memory_context
# ═══════════════════════════════════════════════════════════════════════════════


class TestBuildMemoryContext:
    """Test memory injection into conversation history."""

    def test_empty_memories_returns_original_history(self):
        history = [{"role": "user", "parts": ["Hello"]}]
        result = build_memory_context([], history)
        assert result is history  # Same object, not a copy

    def test_memories_prepended_to_history(self):
        history = [{"role": "user", "parts": ["What is AI?"]}]
        memories = [{"content": "User likes Python"}]
        result = build_memory_context(memories, history)

        assert len(result) == 3  # memory_msg + ack_msg + original
        assert result[0]["role"] == "user"
        assert "воспоминания" in result[0]["parts"][0].lower()
        assert result[1]["role"] == "model"
        assert result[2] == history[0]

    def test_memories_truncated_to_max_length(self):
        long_content = "A" * 1000
        memories = [{"content": long_content}]
        result = build_memory_context(memories, [], max_content_length=50)

        mem_text = result[0]["parts"][0]
        # The injected text should contain at most 50 chars of the memory
        assert "A" * 50 in mem_text
        assert "A" * 51 not in mem_text

    def test_multiple_memories_all_included(self):
        memories = [
            {"content": "Fact 1"},
            {"content": "Fact 2"},
            {"content": "Fact 3"},
        ]
        result = build_memory_context(memories, [])
        mem_text = result[0]["parts"][0]
        assert "Fact 1" in mem_text
        assert "Fact 2" in mem_text
        assert "Fact 3" in mem_text

    def test_does_not_mutate_original_history(self):
        history = [{"role": "user", "parts": ["Hi"]}]
        original_len = len(history)
        build_memory_context([{"content": "memo"}], history)
        assert len(history) == original_len


# ═══════════════════════════════════════════════════════════════════════════════
# classify_response
# ═══════════════════════════════════════════════════════════════════════════════


class TestClassifyResponse:
    """Test response routing logic."""

    def test_none_response_is_empty(self):
        result = classify_response(None, was_streamed=False)
        assert result.action == "empty"

    def test_empty_string_is_empty(self):
        result = classify_response("", was_streamed=False)
        assert result.action == "empty"

    def test_success_non_streamed_sends(self):
        result = classify_response("Here is your answer!", was_streamed=False)
        assert result.action == "send"

    def test_success_streamed_attaches_buttons(self):
        result = classify_response("Streamed answer", was_streamed=True)
        assert result.action == "attach_buttons"

    @pytest.mark.parametrize(
        "error_text",
        [
            "503 Service Unavailable",
            "500 Internal Server Error",
            "429 Too Many Requests",
            "Error: something went wrong",
            "Unavailable right now",
        ],
    )
    def test_error_responses_classified(self, error_text):
        result = classify_response(error_text, was_streamed=False)
        assert result.action == "error"

    def test_normal_text_containing_error_word_in_middle(self):
        """'error' in the middle of normal text should NOT trigger error action."""
        result = classify_response("The user made an error in their logic", was_streamed=False)
        assert result.action == "send"
