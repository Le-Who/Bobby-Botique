"""
Tests for ProviderRouter and KeyStatusManager.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.providers import ProviderRouter


class TestProviderRouter:
    """Tests for ProviderRouter.get_response."""

    @pytest.mark.asyncio
    async def test_successful_response(self):
        router = ProviderRouter()

        mock_use_case = MagicMock()
        mock_use_case.resolve_ai_request = AsyncMock(
            return_value=({"api_key": "key1", "key_hash": "hash1"}, "gemini-3.1-flash-lite", None)
        )
        mock_use_case.get_ai_response = AsyncMock(return_value=("Hello!", 10))
        mock_use_case.increment_key_usage = AsyncMock()

        mock_status_mgr = MagicMock()
        mock_status_mgr.record_success = AsyncMock()

        with (
            patch("app.agent_use_cases.AgentRequestUseCase", return_value=mock_use_case),
            patch("app.repos.keys.get_key_status_manager", return_value=mock_status_mgr),
        ):
            text, tokens = await router.get_response(
                "gemini-3.1-flash-lite",
                [{"role": "user", "parts": ["hi"]}],
            )

        assert text == "Hello!"
        assert tokens == 10
        mock_status_mgr.record_success.assert_called_once_with("hash1", "gemini-3.1-flash-lite")

    @pytest.mark.asyncio
    async def test_all_keys_exhausted(self):
        router = ProviderRouter()

        mock_use_case = MagicMock()
        mock_use_case.resolve_ai_request = AsyncMock(return_value=(None, None, "all_exhausted"))

        with (
            patch("app.agent_use_cases.AgentRequestUseCase", return_value=mock_use_case),
            patch("app.repos.keys.get_key_status_manager"),
        ):
            text, tokens = await router.get_response(
                "gemini-3.1-flash-lite",
                [{"role": "user", "parts": ["hi"]}],
            )

        assert "🚫" in text
        assert tokens is None

    @pytest.mark.asyncio
    async def test_key_failure_triggers_retry_and_suspend(self):
        """When a key fails with a key-related error, it should be suspended
        and the router should retry with a different key."""
        router = ProviderRouter()

        mock_use_case = MagicMock()

        # First call returns a key that produces an error, second call succeeds
        mock_use_case.resolve_ai_request = AsyncMock(
            side_effect=[
                ({"api_key": "key1", "key_hash": "hash1"}, "gemini-3.1-flash-lite", None),
                ({"api_key": "key2", "key_hash": "hash2"}, "gemini-3.1-flash-lite", None),
            ]
        )
        mock_use_case.get_ai_response = AsyncMock(
            side_effect=[
                ("🔑 Неверный API ключ.", None),  # First key fails (permanent error)
                ("Hello!", 10),  # Second key succeeds
            ]
        )
        mock_use_case.increment_key_usage = AsyncMock()

        mock_status_mgr = MagicMock()
        mock_status_mgr.suspend_key = AsyncMock()
        mock_status_mgr.record_success = AsyncMock()

        with (
            patch("app.agent_use_cases.AgentRequestUseCase", return_value=mock_use_case),
            patch("app.repos.keys.get_key_status_manager", return_value=mock_status_mgr),
        ):
            text, tokens = await router.get_response(
                "gemini-3.1-flash-lite",
                [{"role": "user", "parts": ["hi"]}],
                max_key_retries=3,
            )

        assert text == "Hello!"
        assert tokens == 10
        # First key should have been suspended with "permanent" category
        mock_status_mgr.suspend_key.assert_called_once_with(
            "hash1",
            "gemini-3.1-flash-lite",
            "permanent",
            "🔑 Неверный API ключ."[:200],
        )
        # Second key should have been recorded as success
        mock_status_mgr.record_success.assert_called_once_with("hash2", "gemini-3.1-flash-lite")

    @pytest.mark.asyncio
    async def test_quota_error_suspends_with_quota_category(self):
        """Quota exceeded should suspend with 'quota' category."""
        router = ProviderRouter()

        mock_use_case = MagicMock()
        mock_use_case.resolve_ai_request = AsyncMock(
            side_effect=[
                ({"api_key": "key1", "key_hash": "hash1"}, "gemini-3.1-flash-lite", None),
                (None, None, "all_exhausted"),
            ]
        )
        mock_use_case.get_ai_response = AsyncMock(
            return_value=("🚫 Достигнут лимит запросов к API (Quota Exceeded).", None)
        )

        mock_status_mgr = MagicMock()
        mock_status_mgr.suspend_key = AsyncMock()

        with (
            patch("app.agent_use_cases.AgentRequestUseCase", return_value=mock_use_case),
            patch("app.repos.keys.get_key_status_manager", return_value=mock_status_mgr),
        ):
            text, tokens = await router.get_response(
                "gemini-3.1-flash-lite",
                [{"role": "user", "parts": ["hi"]}],
                max_key_retries=2,
            )

        # Verify the key was suspended with "quota" category
        mock_status_mgr.suspend_key.assert_called_once()
        call_args = mock_status_mgr.suspend_key.call_args
        assert call_args[0][2] == "quota"  # error_category

    @pytest.mark.asyncio
    async def test_excluded_keys_passed_to_resolve(self):
        """After a key failure, the failed key hash should be passed as excluded
        to the next resolve_ai_request call."""
        router = ProviderRouter()

        mock_use_case = MagicMock()
        mock_use_case.resolve_ai_request = AsyncMock(
            side_effect=[
                ({"api_key": "key1", "key_hash": "hash1"}, "gemini-3.1-flash-lite", None),
                ({"api_key": "key2", "key_hash": "hash2"}, "gemini-3.1-flash-lite", None),
            ]
        )
        mock_use_case.get_ai_response = AsyncMock(
            side_effect=[
                ("🔑 Invalid API key", None),
                ("OK", 5),
            ]
        )
        mock_use_case.increment_key_usage = AsyncMock()

        mock_status_mgr = MagicMock()
        mock_status_mgr.suspend_key = AsyncMock()
        mock_status_mgr.record_success = AsyncMock()

        with (
            patch("app.agent_use_cases.AgentRequestUseCase", return_value=mock_use_case),
            patch("app.repos.keys.get_key_status_manager", return_value=mock_status_mgr),
        ):
            text, tokens = await router.get_response(
                "gemini-3.1-flash-lite",
                [{"role": "user", "parts": ["hi"]}],
                max_key_retries=3,
            )

        assert text == "OK"
        # Second resolve call should have "hash1" in excluded_key_hashes
        second_call = mock_use_case.resolve_ai_request.call_args_list[1]
        excluded = second_call.kwargs.get("excluded_key_hashes", set())
        assert "hash1" in excluded

    @pytest.mark.asyncio
    async def test_openrouter_detection(self):
        router = ProviderRouter()

        mock_use_case = MagicMock()
        mock_use_case.resolve_ai_request = AsyncMock(return_value=(None, None, "all_exhausted"))

        with (
            patch("app.agent_use_cases.AgentRequestUseCase", return_value=mock_use_case),
            patch("app.repos.keys.get_key_status_manager"),
        ):
            text, _ = await router.get_response(
                "openai/gpt-4o",
                [{"role": "user", "parts": ["hi"]}],
            )

        assert "OpenRouter" in text

    @pytest.mark.asyncio
    async def test_transient_error_not_suspended(self):
        """503/timeout errors should NOT suspend the key."""
        router = ProviderRouter()

        mock_use_case = MagicMock()
        mock_use_case.resolve_ai_request = AsyncMock(
            side_effect=[
                ({"api_key": "key1", "key_hash": "hash1"}, "gemini-3.1-flash-lite", None),
                (None, None, "all_exhausted"),
            ]
        )
        # The 🔄 emoji is a transient error — not key-related
        mock_use_case.get_ai_response = AsyncMock(return_value=("⏰ Превышено время ожидания ответа от API.", None))

        mock_status_mgr = MagicMock()
        mock_status_mgr.suspend_key = AsyncMock()

        with (
            patch("app.agent_use_cases.AgentRequestUseCase", return_value=mock_use_case),
            patch("app.repos.keys.get_key_status_manager", return_value=mock_status_mgr),
        ):
            await router.get_response(
                "gemini-3.1-flash-lite",
                [{"role": "user", "parts": ["hi"]}],
                max_key_retries=2,
            )

        # Timeout is NOT key-related, so suspend_key should NOT be called
        mock_status_mgr.suspend_key.assert_not_called()

    @pytest.mark.asyncio
    async def test_model_fallback_on_all_keys_permanent_failure(self):
        """When all keys fail with permanent errors for one model,
        the router should try fallback models from AVAILABLE_MODELS."""
        router = ProviderRouter()

        mock_use_case = MagicMock()

        # Main loop: 3 keys all fail with permanent errors
        # Then fallback: resolve succeeds for fallback model
        mock_use_case.resolve_ai_request = AsyncMock(
            side_effect=[
                ({"api_key": "key1", "key_hash": "hash1"}, "gemini-3-flash-preview", None),
                ({"api_key": "key2", "key_hash": "hash2"}, "gemini-3-flash-preview", None),
                ({"api_key": "key3", "key_hash": "hash3"}, "gemini-3-flash-preview", None),
                # Fallback call for "gemini-2.5-flash"
                ({"api_key": "key1", "key_hash": "hash1"}, "gemini-2.5-flash", None),
            ]
        )
        mock_use_case.get_ai_response = AsyncMock(
            side_effect=[
                ("🔑 Неверный API ключ.", None),  # key1 permanent fail
                ("🔑 Неверный API ключ.", None),  # key2 permanent fail
                ("🔑 Неверный API ключ.", None),  # key3 permanent fail
                ("Fallback response!", 15),  # fallback model succeeds
            ]
        )
        mock_use_case.increment_key_usage = AsyncMock()

        mock_status_mgr = MagicMock()
        mock_status_mgr.suspend_key = AsyncMock()
        mock_status_mgr.record_success = AsyncMock()

        mock_settings = MagicMock()
        mock_settings.AVAILABLE_MODELS = [
            "gemini-3-flash-preview",
            "gemini-2.5-flash",
            "gemini-flash-latest",
        ]

        with (
            patch("app.agent_use_cases.AgentRequestUseCase", return_value=mock_use_case),
            patch("app.repos.keys.get_key_status_manager", return_value=mock_status_mgr),
            patch("app.providers.router.settings", mock_settings),
        ):
            text, tokens = await router.get_response(
                "gemini-3-flash-preview",
                [{"role": "user", "parts": ["hi"]}],
                max_key_retries=3,
            )

        assert text == "Fallback response!"
        assert tokens == 15
        # All 3 original keys should have been suspended
        assert mock_status_mgr.suspend_key.call_count == 3
        # Fallback key should have been recorded as success
        mock_status_mgr.record_success.assert_called_once_with("hash1", "gemini-2.5-flash")

    @pytest.mark.asyncio
    async def test_no_model_fallback_on_non_permanent_errors(self):
        """Model fallback should NOT trigger for quota/rate-limit errors."""
        router = ProviderRouter()

        mock_use_case = MagicMock()
        mock_use_case.resolve_ai_request = AsyncMock(
            side_effect=[
                ({"api_key": "key1", "key_hash": "hash1"}, "gemini-3.1-flash-lite", None),
                ({"api_key": "key2", "key_hash": "hash2"}, "gemini-3.1-flash-lite", None),
                ({"api_key": "key3", "key_hash": "hash3"}, "gemini-3.1-flash-lite", None),
            ]
        )
        mock_use_case.get_ai_response = AsyncMock(
            side_effect=[
                ("🔑 Неверный API ключ.", None),  # permanent
                ("🚫 Достигнут лимит запросов к API (Quota Exceeded).", None),  # quota (NOT permanent)
                ("🔑 Неверный API ключ.", None),  # permanent
            ]
        )

        mock_status_mgr = MagicMock()
        mock_status_mgr.suspend_key = AsyncMock()

        with (
            patch("app.agent_use_cases.AgentRequestUseCase", return_value=mock_use_case),
            patch("app.repos.keys.get_key_status_manager", return_value=mock_status_mgr),
        ):
            text, tokens = await router.get_response(
                "gemini-3.1-flash-lite",
                [{"role": "user", "parts": ["hi"]}],
                max_key_retries=3,
            )

        # Should NOT have fallen back — quota error means model is fine, key is the problem
        assert "🚫" in text
        assert "не сработали" in text
        assert tokens is None
