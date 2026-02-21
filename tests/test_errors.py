"""
Tests for app/errors.py - Error handling utilities.
"""

import pytest
from unittest.mock import AsyncMock, MagicMock

from app.errors import (
    # Constants
    GENERIC_ERROR,
    OVERLOADED_ERROR,
    QUOTA_ERROR,
    TIMEOUT_ERROR,
    # Functions
    user_friendly_error,
    is_error_message,
    is_retryable_error,
    is_key_related_error,
    build_retry_and_roles_keyboard,
    build_roles_keyboard,
    # Classes
    APIError,
    # Context manager - tested in async tests
    handle_api_errors,
)


class TestUserFriendlyError:
    """Tests for user_friendly_error function."""

    def test_overloaded_503(self):
        assert user_friendly_error("Service 503") == OVERLOADED_ERROR

    def test_overloaded_unavailable(self):
        assert user_friendly_error("Service unavailable") == OVERLOADED_ERROR

    def test_overloaded_keyword(self):
        assert user_friendly_error("Server overloaded") == OVERLOADED_ERROR

    def test_quota_error(self):
        assert user_friendly_error("Quota exceeded") == QUOTA_ERROR

    def test_limit_error(self):
        assert user_friendly_error("Rate limit reached") == QUOTA_ERROR

    def test_timeout_error(self):
        assert user_friendly_error("Request timeout") == TIMEOUT_ERROR

    def test_generic_error(self):
        assert user_friendly_error("Some unknown error") == GENERIC_ERROR

    def test_exception_input(self):
        exc = ValueError("Quota exceeded")
        assert user_friendly_error(exc) == QUOTA_ERROR

    def test_empty_string(self):
        assert user_friendly_error("") == GENERIC_ERROR


class TestIsErrorMessage:
    """Tests for is_error_message function."""

    def test_timeout_emoji(self):
        assert is_error_message("⏰ Timeout") is True

    def test_error_emoji(self):
        assert is_error_message("❌ Error occurred") is True

    def test_quota_emoji(self):
        assert is_error_message("🚫 Quota exceeded") is True

    def test_normal_message(self):
        assert is_error_message("Hello world") is False

    def test_empty_string(self):
        assert is_error_message("") is False

    def test_none(self):
        assert is_error_message(None) is False


class TestIsRetryableError:
    """Tests for is_retryable_error function."""

    def test_timeout_retryable(self):
        assert is_retryable_error("⏰ Timeout") is True

    def test_overloaded_retryable(self):
        assert is_retryable_error("🔄 Server overloaded") is True

    def test_503_retryable(self):
        assert is_retryable_error("Error 503") is True

    def test_rate_limit_retryable(self):
        assert is_retryable_error("rate limit exceeded") is True

    def test_quota_not_retryable(self):
        # Quota errors are key-related, not retryable by same key
        assert is_retryable_error("quota exceeded") is False

    def test_empty_not_retryable(self):
        assert is_retryable_error("") is False


class TestIsKeyRelatedError:
    """Tests for is_key_related_error function."""

    def test_quota_is_key_related(self):
        assert is_key_related_error("Quota exceeded") is True

    def test_invalid_api_key(self):
        assert is_key_related_error("Invalid API key") is True

    def test_unauthorized(self):
        assert is_key_related_error("Unauthorized") is True

    def test_503_not_key_related(self):
        # 503 is server issue, not key issue
        assert is_key_related_error("503 Service unavailable") is False

    def test_timeout_not_key_related(self):
        assert is_key_related_error("Request timeout") is False


class TestAPIError:
    """Tests for APIError exception class."""

    def test_basic_creation(self):
        err = APIError("Test error")
        assert err.raw_error == "Test error"
        assert err.retryable is False
        assert err.key_related is False
        assert err.user_message == GENERIC_ERROR

    def test_with_flags(self):
        err = APIError("Rate limit", retryable=True, key_related=True)
        assert err.retryable is True
        assert err.key_related is True

    def test_from_exception_quota(self):
        exc = ValueError("Quota exceeded for model")
        err = APIError.from_exception(exc)
        assert err.key_related is True
        assert err.user_message == QUOTA_ERROR

    def test_from_exception_timeout(self):
        exc = TimeoutError("Request timed out")
        err = APIError.from_exception(exc)
        assert err.retryable is True
        assert err.user_message == TIMEOUT_ERROR


class TestKeyboardBuilders:
    """Tests for keyboard builder functions."""

    def test_retry_and_roles_keyboard(self):
        kb = build_retry_and_roles_keyboard(include_roles=True)
        assert len(kb.inline_keyboard) == 2
        assert kb.inline_keyboard[0][0].callback_data == "retry_last"
        assert kb.inline_keyboard[1][0].callback_data == "open_roles"

    def test_retry_only_keyboard(self):
        kb = build_retry_and_roles_keyboard(include_roles=False)
        assert len(kb.inline_keyboard) == 1
        assert kb.inline_keyboard[0][0].callback_data == "retry_last"

    def test_roles_keyboard(self):
        kb = build_roles_keyboard()
        assert len(kb.inline_keyboard) == 1
        assert kb.inline_keyboard[0][0].callback_data == "open_roles"


@pytest.mark.asyncio
class TestHandleApiErrors:
    """Async tests for handle_api_errors context manager."""

    async def test_no_error_passthrough(self):
        """When no error, code executes normally."""
        mock_message = MagicMock()
        mock_message.edit_text = AsyncMock()

        executed = False
        async with handle_api_errors(mock_message, "Test"):
            executed = True

        assert executed is True
        mock_message.edit_text.assert_not_called()

    async def test_error_shows_message(self):
        """When error occurs, user-friendly message is shown."""
        mock_message = MagicMock()
        mock_message.edit_text = AsyncMock()

        async with handle_api_errors(mock_message, "Test"):
            raise ValueError("Some error")

        mock_message.edit_text.assert_called_once()
        call_args = mock_message.edit_text.call_args
        assert call_args[0][0] == GENERIC_ERROR

    async def test_retryable_error_shows_retry_button(self):
        """Retryable errors show retry keyboard."""
        mock_message = MagicMock()
        mock_message.edit_text = AsyncMock()

        async with handle_api_errors(mock_message, "Test"):
            raise ValueError("503 Service unavailable")

        call_args = mock_message.edit_text.call_args
        assert call_args[1]["reply_markup"] is not None

    async def test_reraise_option(self):
        """With reraise=True, exception is re-raised after handling."""
        mock_message = MagicMock()
        mock_message.edit_text = AsyncMock()

        with pytest.raises(ValueError):
            async with handle_api_errors(mock_message, "Test", reraise=True):
                raise ValueError("Test error")

    async def test_error_callback_sync(self):
        """Sync error callback is called."""
        mock_message = MagicMock()
        mock_message.edit_text = AsyncMock()
        callback_mock = MagicMock()

        async with handle_api_errors(mock_message, "Test", on_error=callback_mock):
            raise ValueError("Test")

        callback_mock.assert_called_once()

    async def test_error_callback_async(self):
        """Async error callback is awaited."""
        mock_message = MagicMock()
        mock_message.edit_text = AsyncMock()
        callback_mock = AsyncMock()

        async with handle_api_errors(mock_message, "Test", on_error=callback_mock):
            raise ValueError("Test")

        callback_mock.assert_awaited_once()


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
