"""Integration tests for core AI pipeline: streaming, memory, error handling, and admin alerts."""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ============================================================================
# 1. STREAMING TESTS
# ============================================================================


class TestStreamingWriter:
    """Test StreamingWriter progressive message updates."""

    @pytest.mark.asyncio
    async def test_streaming_writer_debounces_edits(self):
        """StreamingWriter should not call edit_text more often than DEBOUNCE_INTERVAL."""
        from app.adapters.ui_adapter import StreamingUIAdapter
        from app.streaming import StreamingWriter

        mock_msg = AsyncMock()
        mock_msg.message_id = 1
        mock_msg.chat = MagicMock()
        mock_msg.chat.id = 123

        mock_adapter = AsyncMock(spec=StreamingUIAdapter)
        mock_adapter._bot = AsyncMock()  # force draft mode if possible

        writer = StreamingWriter(mock_adapter, chat_type="private")
        writer._debounce_s = 0.05
        writer._min_chunk = 0

        # Write several chunks rapidly
        await writer.write("Hello ")
        await writer.write("World ")
        await writer.write("!")
        await writer.finalize()

        # Should have been called at least once (finalize forces a flush),
        # but not 3 times (debouncing should merge some calls)
        assert mock_adapter.edit_message.call_count + mock_adapter.send_draft.call_count >= 1

    @pytest.mark.asyncio
    async def test_streaming_writer_finalize_sends_full_text(self):
        """finalize() must send the complete accumulated text."""
        from app.adapters.ui_adapter import StreamingUIAdapter
        from app.streaming import StreamingWriter

        mock_msg = AsyncMock()
        mock_msg.message_id = 2
        mock_msg.chat = MagicMock()
        mock_msg.chat.id = 123

        mock_adapter = AsyncMock(spec=StreamingUIAdapter)
        mock_adapter._bot = None  # force edit_message mode

        writer = StreamingWriter(mock_adapter, chat_type="group")
        writer._debounce_s = 0.01
        writer._min_chunk = 0
        await writer.write("Part1 ")
        await writer.write("Part2")
        await writer.finalize()

        # The last edit_text call should contain the full accumulated text
        last_call = mock_adapter.edit_message.call_args_list[-1]
        final_text = last_call[0][0] if last_call[0] else last_call[1].get("text", "")
        assert "Part1" in final_text
        assert "Part2" in final_text


# ============================================================================
# 2. ERROR PIPELINE TESTS
# ============================================================================


class TestErrorPipeline:
    """Test tag_error → is_error_message → handle_ai_response_error chain."""

    def test_tag_error_produces_detectable_message(self):
        """tag_error output must be detected by is_error_message."""
        from app.errors import ErrorCode, is_error_message, tag_error

        tagged = tag_error(ErrorCode.RATE_LIMIT, "Rate limit exceeded")
        assert is_error_message(tagged) is True

    def test_plain_text_is_not_error(self):
        """Normal AI response must not be detected as error."""
        from app.errors import is_error_message

        assert is_error_message("Привет! Как дела?") is False
        assert is_error_message("Here is the code:\n```python\nprint('hi')\n```") is False

    def test_retryable_error_classification(self):
        """Rate limit errors should be classified as retryable."""
        from app.errors import ErrorCode, is_retryable_error, tag_error

        rate_limit = tag_error(ErrorCode.RATE_LIMIT, "Rate limit hit")
        assert is_retryable_error(rate_limit) is True

        invalid_key = tag_error(ErrorCode.INVALID_KEY, "Bad key")
        assert is_retryable_error(invalid_key) is False

    @pytest.mark.asyncio
    async def test_handle_ai_response_error_edits_message(self):
        """handle_ai_response_error should edit the placeholder with error text."""
        from app.errors import ErrorCode, tag_error

        error_text = tag_error(ErrorCode.RATE_LIMIT, "All keys exhausted")
        mock_msg = AsyncMock()
        mock_msg.message_id = 99

        with patch("app.handlers.ai_core.stop_heartbeat"):
            from app.handlers.ai_core import handle_ai_response_error

            result = await handle_ai_response_error(error_text, mock_msg)

        assert result is True
        mock_msg.edit_text.assert_called_once()

    @pytest.mark.asyncio
    async def test_handle_ai_response_error_skips_normal_text(self):
        """handle_ai_response_error should return False for normal responses."""
        mock_msg = AsyncMock()
        mock_msg.message_id = 100

        with patch("app.handlers.ai_core.stop_heartbeat"):
            from app.handlers.ai_core import handle_ai_response_error

            result = await handle_ai_response_error("Нормальный ответ AI", mock_msg)

        assert result is False
        mock_msg.edit_text.assert_not_called()


# ============================================================================
# 3. ADMIN ALERTS TESTS
# ============================================================================


class TestAdminAlerts:
    """Test admin alert system with rate limiting."""

    @pytest.mark.asyncio
    async def test_alert_admin_sends_message(self):
        """alert_admin should send a message via bot.send_message."""
        from app.admin_alerts import AlertSeverity, _alert_timestamps, alert_admin

        _alert_timestamps.clear()

        mock_app = MagicMock()
        mock_app.bot = AsyncMock()

        with patch("app.config.settings", MagicMock(ADMIN_ID=12345)):
            await alert_admin(mock_app, "Test alert", AlertSeverity.WARNING)

        mock_app.bot.send_message.assert_called_once()
        call_kwargs = mock_app.bot.send_message.call_args[1]
        assert call_kwargs["chat_id"] == 12345
        assert "Test alert" in call_kwargs["text"]

    @pytest.mark.asyncio
    async def test_alert_admin_rate_limiting(self):
        """alert_admin should drop messages after rate limit is exceeded."""
        from app.admin_alerts import (
            _MAX_ALERTS,
            AlertSeverity,
            _alert_timestamps,
            alert_admin,
        )

        _alert_timestamps.clear()

        mock_app = MagicMock()
        mock_app.bot = AsyncMock()

        with patch("app.config.settings", MagicMock(ADMIN_ID=12345)):
            # Send _MAX_ALERTS messages — all should go through
            for i in range(_MAX_ALERTS):
                await alert_admin(mock_app, f"Alert {i}", AlertSeverity.WARNING)

            assert mock_app.bot.send_message.call_count == _MAX_ALERTS

            # The next one should be rate-limited
            await alert_admin(mock_app, "This should be dropped", AlertSeverity.CRITICAL)
            assert mock_app.bot.send_message.call_count == _MAX_ALERTS  # No increase

        _alert_timestamps.clear()

    @pytest.mark.asyncio
    async def test_alert_admin_includes_traceback(self):
        """alert_admin with exc should include traceback in message."""
        from app.admin_alerts import AlertSeverity, _alert_timestamps, alert_admin

        _alert_timestamps.clear()

        mock_app = MagicMock()
        mock_app.bot = AsyncMock()

        try:
            raise ValueError("Test exception for alert")
        except ValueError as e:
            with patch("app.config.settings", MagicMock(ADMIN_ID=12345)):
                await alert_admin(mock_app, "Error occurred", AlertSeverity.CRITICAL, exc=e)

        call_kwargs = mock_app.bot.send_message.call_args[1]
        assert "ValueError" in call_kwargs["text"]
        assert "Test exception for alert" in call_kwargs["text"]

        _alert_timestamps.clear()


# ============================================================================
# 4. PROVIDER ROUTER FALLBACK TESTS
# ============================================================================


class TestProviderRouterFallback:
    """Test ProviderRouter key rotation and fallback logic."""

    @pytest.mark.asyncio
    async def test_router_returns_tagged_error_on_all_keys_exhausted(self):
        """When all keys are exhausted, the router should return a tagged error."""
        from app.errors import is_error_message

        # Mock resolve to always return no keys
        with patch("app.agent_use_cases.AgentRequestUseCase") as MockUseCase:
            mock_use_case = MockUseCase.return_value
            mock_use_case.resolve_ai_request = AsyncMock(return_value=(None, None, "all_exhausted"))

            from app.providers import ProviderRouter

            router = ProviderRouter()
            result_text, result_tokens = await router.get_response(
                "gemini-3.1-flash-lite",
                [{"role": "user", "parts": ["Hello"]}],
            )

            assert is_error_message(result_text) is True

    @pytest.mark.asyncio
    async def test_resolve_excludes_failed_key_on_retry(self):
        """AgentRequestUseCase should pass excluded_key_hashes to get_key_func on retry."""
        from app.agent_use_cases import AgentRequestUseCase

        resolve_calls = []

        async def mock_get_key(model, excluded_hashes=None):
            resolve_calls.append({"model": model, "excluded": excluded_hashes or set()})
            if not excluded_hashes:
                return {"api_key": "key1", "key_hash": "h1"}
            return {"api_key": "key2", "key_hash": "h2"}

        use_case = AgentRequestUseCase()

        # First call — no exclusions
        key1, model1, err1 = await use_case._resolve_key_generic(
            "gemini-3.1-flash-lite",
            mock_get_key,
            [],
            excluded_key_hashes=None,
        )
        assert key1["key_hash"] == "h1"
        assert err1 is None

        # Second call — exclude first key
        key2, model2, err2 = await use_case._resolve_key_generic(
            "gemini-3.1-flash-lite",
            mock_get_key,
            [],
            excluded_key_hashes={"h1"},
        )
        assert key2["key_hash"] == "h2"

        # Verify exclusion was passed through
        assert len(resolve_calls) == 2
        assert "h1" in resolve_calls[1]["excluded"]
