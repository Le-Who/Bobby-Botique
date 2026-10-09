"""Ordinary chat-logic regression tests; these do not measure mutation coverage."""


class TestChatLogicRegressionContracts:
    """Regression contracts for provider, memory and response classification."""

    def test_resolution_all_exhausted_identifies_openrouter(self):
        """Exhausted OpenRouter models retain the correct provider identity."""
        from app.handlers.chat_logic import classify_resolution

        # The real function detects OpenRouter by '/' in model name
        result = classify_resolution("all_exhausted", "anthropic/claude-3")
        assert result.provider_name == "OpenRouter"

    def test_memory_empty_list_produces_no_prompt(self):
        """Empty memory lists produce no system-prompt addition."""
        from app.handlers.chat_logic import format_memories_for_system_prompt

        result = format_memories_for_system_prompt([])

        # Must return empty string
        assert result == "", "Empty memories must return empty string"

    def test_response_empty_detection(self):
        """Missing response text produces the empty action."""
        from app.handlers.chat_logic import classify_response

        none_result = classify_response(None, was_streamed=False)
        empty_result = classify_response("", was_streamed=False)

        assert none_result.action == "empty", "None response must be 'empty', not 'send'"
        assert empty_result.action == "empty", "Empty string must be 'empty', not 'send'"

    def test_streamed_vs_non_streamed_differ(self):
        """Streamed responses need a different delivery action."""
        from app.handlers.chat_logic import classify_response

        streamed = classify_response("Hello", was_streamed=True)
        not_streamed = classify_response("Hello", was_streamed=False)

        assert streamed.action != not_streamed.action, "Streamed and non-streamed must produce different actions"

    def test_fallback_model_in_message(self):
        """Fallback messages identify both original and fallback models."""
        from app.handlers.chat_logic import classify_resolution

        result = classify_resolution("confirm_fallback", "gemini-pro", "gemini-flash")

        # Both models must appear in the message
        assert "gemini-pro" in result.user_message
        assert "gemini-flash" in result.user_message
        # "Продолжить?" should be the call-to-action
        assert "Продолжить?" in result.user_message
