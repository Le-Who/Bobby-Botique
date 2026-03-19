"""Tests for app.handlers.cb_ai_actions — heavy AI action callbacks."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest


def _make_update(callback_data: str, user_id: int = 42):
    """Build a mock Update with given callback_data."""
    update = MagicMock()
    query = MagicMock()
    update.callback_query = query
    query.data = callback_data
    query.id = "abc123"
    query.from_user.id = user_id
    query.answer = AsyncMock()
    query.edit_message_text = AsyncMock()
    query.message = MagicMock()
    query.message.reply_text = AsyncMock()
    query.message.message_id = 100
    query.message.delete = AsyncMock()
    query.message.edit_text = AsyncMock()
    query.message.reply_to_message = None
    return update, query


class TestComplexSearchCallback:
    @pytest.mark.asyncio
    async def test_cancel_action_deletes_message(self):
        """When action is 'cancel', the placeholder message should be deleted."""
        from app.handlers.cb_ai_actions import complex_search_callback

        update, query = _make_update("complex_search:cancel")

        with patch("app.handlers.cb_ai_actions.set_request_id"):
            await complex_search_callback(update, MagicMock())

        query.message.delete.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_missing_original_message_shows_error(self):
        """If no original message can be found, an error should be shown."""
        from app.handlers.cb_ai_actions import complex_search_callback

        update, query = _make_update("complex_search:confirm")
        query.message.reply_to_message = None

        context = MagicMock()
        context.user_data = {}

        with patch("app.handlers.cb_ai_actions.set_request_id"):
            await complex_search_callback(update, context)

        query.message.edit_text.assert_awaited_once()
        error_text = query.message.edit_text.call_args[0][0]
        assert "не удалось" in error_text.lower() or "оригинальное" in error_text.lower()


class TestFallbackCallback:
    @pytest.mark.asyncio
    async def test_cancel_action_edits_message(self):
        """When action is 'cancel', the message should show cancellation text."""
        from app.handlers.cb_ai_actions import fallback_callback

        update, query = _make_update("fallback:cancel")

        with patch("app.handlers.cb_ai_actions.set_request_id"):
            await fallback_callback(update, MagicMock())

        query.message.edit_text.assert_awaited_once()
        assert "отмен" in query.message.edit_text.call_args[0][0].lower()

    @pytest.mark.asyncio
    async def test_missing_original_message_shows_error(self):
        from app.handlers.cb_ai_actions import fallback_callback

        update, query = _make_update("fallback:confirm")
        query.message.reply_to_message = None
        context = MagicMock()
        context.user_data = {}

        with patch("app.handlers.cb_ai_actions.set_request_id"):
            await fallback_callback(update, context)

        query.message.edit_text.assert_awaited_once()
        error_text = query.message.edit_text.call_args[0][0]
        assert "не удалось" in error_text.lower() or "оригинальное" in error_text.lower()


class TestRetryLastCallback:
    @pytest.mark.asyncio
    async def test_no_last_message_shows_error(self):
        from app.handlers.cb_ai_actions import retry_last_callback

        update, query = _make_update("retry_last")

        with (
            patch(
                "app.handlers.cb_ai_actions.get_user_chat",
                new_callable=AsyncMock,
                return_value=MagicMock(),
            ),
            patch("app.handlers.cb_ai_actions.state"),
            patch("app.state.ensure_state_loaded", new_callable=AsyncMock),
            patch("app.state.get_last_sent_message", return_value=None),
        ):
            await retry_last_callback(update, MagicMock())

        query.edit_message_text.assert_awaited_once()
        error_text = query.edit_message_text.call_args[0][0]
        assert "нет запроса" in error_text.lower()


class TestBusyStateToast:
    """P4: when user lock is held, handlers must show toast via a SINGLE query.answer().

    Telegram only processes the first answerCallbackQuery per callback_query_id.
    If the handler calls query.answer() twice (once empty, once with text),
    the second call is silently ignored and the user never sees the toast.
    """

    @pytest.mark.asyncio
    async def test_complex_search_busy_shows_toast(self):
        from app.handlers.cb_ai_actions import complex_search_callback

        update, query = _make_update("complex_search:confirm")
        original = MagicMock()
        original.from_user.id = 42
        query.message.reply_to_message = original
        context = MagicMock()
        context.user_data = {}

        lock = AsyncMock()
        lock.locked = MagicMock(return_value=True)

        with (
            patch("app.handlers.cb_ai_actions.set_request_id"),
            patch("app.handlers.cb_ai_actions.set_user_context"),
            patch("app.handlers.cb_ai_actions.state.get_user_lock", return_value=lock),
        ):
            await complex_search_callback(update, context)

        # Must be called exactly once — Telegram ignores subsequent calls
        assert query.answer.await_count == 1, f"query.answer() called {query.answer.await_count} times, expected 1"
        # That single call must carry the toast text
        first_call_args = query.answer.call_args_list[0]
        assert "⏳" in str(first_call_args), f"First answer() must have toast: {first_call_args}"

    @pytest.mark.asyncio
    async def test_fallback_busy_shows_toast(self):
        from app.handlers.cb_ai_actions import fallback_callback

        update, query = _make_update("fallback:confirm:gpt-4")
        original = MagicMock()
        original.from_user.id = 42
        query.message.reply_to_message = original
        context = MagicMock()
        context.user_data = {}

        lock = AsyncMock()
        lock.locked = MagicMock(return_value=True)

        with (
            patch("app.handlers.cb_ai_actions.set_request_id"),
            patch("app.handlers.cb_ai_actions.set_user_context"),
            patch("app.handlers.cb_ai_actions.state.get_user_lock", return_value=lock),
        ):
            await fallback_callback(update, context)

        assert query.answer.await_count == 1, f"query.answer() called {query.answer.await_count} times, expected 1"
        first_call_args = query.answer.call_args_list[0]
        assert "⏳" in str(first_call_args), f"First answer() must have toast: {first_call_args}"

    @pytest.mark.asyncio
    async def test_retry_busy_shows_toast(self):
        from app.handlers.cb_ai_actions import retry_last_callback

        update, query = _make_update("retry_last")

        lock = AsyncMock()
        lock.locked = MagicMock(return_value=True)

        with (
            patch("app.handlers.cb_ai_actions.set_request_id"),
            patch("app.handlers.cb_ai_actions.set_user_context"),
            patch(
                "app.handlers.cb_ai_actions.get_user_chat",
                new_callable=AsyncMock,
                return_value=MagicMock(),
            ),
            patch("app.handlers.cb_ai_actions.state.get_user_lock", return_value=lock),
            patch("app.state.ensure_state_loaded", new_callable=AsyncMock),
            patch("app.state.get_last_sent_message", return_value="some previous text"),
        ):
            await retry_last_callback(update, MagicMock())

        assert query.answer.await_count == 1, f"query.answer() called {query.answer.await_count} times, expected 1"
        first_call_args = query.answer.call_args_list[0]
        assert "⏳" in str(first_call_args), f"First answer() must have toast: {first_call_args}"
        # No placeholder message should have been created
        query.message.reply_text.assert_not_awaited()


class TestExports:
    def test_all_exports(self):
        from app.handlers.cb_ai_actions import __all__

        assert "complex_search_callback" in __all__
        assert "fallback_callback" in __all__
        assert "retry_last_callback" in __all__
