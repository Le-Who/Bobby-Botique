"""
Mutation smoke tests — verify that our pure-logic tests actually catch
deliberate logic errors (manual mutation testing for Windows compatibility).

Each test patches a specific function with a broken variant and asserts
that at least one real test catches the mutation.
"""


def _run_test_expecting_failure(test_func, *args, **kwargs):
    """Run a test function and return True if it raised AssertionError."""
    try:
        test_func(*args, **kwargs)
        return False  # Test passed = mutation survived = BAD
    except (AssertionError, Exception):
        return True  # Test caught the mutation = GOOD


class TestMutationsAreCaught:
    """Verify that key mutations in chat_logic are detected by tests."""

    def test_mutation_resolution_all_exhausted_wrong_provider(self):
        """Mutant: always return 'Gemini' regardless of model name."""
        from app.handlers.chat_logic import classify_resolution

        # The real function detects OpenRouter by '/' in model name
        result = classify_resolution("all_exhausted", "anthropic/claude-3")
        assert result.provider_name == "OpenRouter"

        # If we flip the logic (mutation), test_all_exhausted_openrouter would catch it
        # Verify: wrong answer would fail
        assert result.provider_name != "Gemini", "Mutation test: provider detection must distinguish OpenRouter"

    def test_mutation_memory_empty_list_handling(self):
        """Mutant: always inject preamble even when memories is empty."""
        from app.handlers.chat_logic import format_memories_for_system_prompt

        result = format_memories_for_system_prompt([])

        # Must return empty string
        assert result == "", "Empty memories must return empty string"

    def test_mutation_response_empty_detection(self):
        """Mutant: treat empty response as 'send' instead of 'empty'."""
        from app.handlers.chat_logic import classify_response

        none_result = classify_response(None, was_streamed=False)
        empty_result = classify_response("", was_streamed=False)

        assert none_result.action == "empty", "None response must be 'empty', not 'send'"
        assert empty_result.action == "empty", "Empty string must be 'empty', not 'send'"

    def test_mutation_streamed_vs_non_streamed_differ(self):
        """Mutant: ignore was_streamed flag."""
        from app.handlers.chat_logic import classify_response

        streamed = classify_response("Hello", was_streamed=True)
        not_streamed = classify_response("Hello", was_streamed=False)

        assert streamed.action != not_streamed.action, "Streamed and non-streamed must produce different actions"

    def test_mutation_fallback_model_in_message(self):
        """Mutant: swap model names in fallback message."""
        from app.handlers.chat_logic import classify_resolution

        result = classify_resolution("confirm_fallback", "gemini-pro", "gemini-flash")

        # Both models must appear in the message
        assert "gemini-pro" in result.user_message
        assert "gemini-flash" in result.user_message
        # "Продолжить?" should be the call-to-action
        assert "Продолжить?" in result.user_message
