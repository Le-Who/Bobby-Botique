"""Tests for app.handlers.cb_feedback — feedback and noop callbacks."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest


@pytest.fixture()
def _update_with_feedback():
    """Build a mock Update whose callback_query has feedback data."""
    from telegram import InlineKeyboardMarkup, Message

    update = MagicMock()
    query = MagicMock()
    update.callback_query = query
    query.from_user.id = 42
    query.answer = AsyncMock()

    # spec=Message so that isinstance(msg, Message) is True inside _handle_vote
    msg = MagicMock(spec=Message)
    msg.message_id = 999
    query.message = msg

    # Default: existing keyboard with a feedback row
    feedback_btn = MagicMock()
    feedback_btn.callback_data = "feedback:up"
    other_btn = MagicMock()
    other_btn.callback_data = "open_roles"

    # spec=InlineKeyboardMarkup so isinstance guard in _handle_reveal passes
    msg.reply_markup = MagicMock(spec=InlineKeyboardMarkup)
    msg.reply_markup.inline_keyboard = [
        [feedback_btn],
        [other_btn],
    ]
    msg.edit_reply_markup = AsyncMock()
    return update, query


class TestNoopCallback:
    @pytest.mark.asyncio
    async def test_noop_answers_query(self):
        from app.handlers.cb_feedback import _noop_callback

        update = MagicMock()
        update.callback_query.answer = AsyncMock()

        await _noop_callback(update, MagicMock())

        update.callback_query.answer.assert_awaited_once()


class TestFeedbackCallback:
    @pytest.mark.asyncio
    async def test_thumbs_up_calls_save(self, _update_with_feedback):
        from app.handlers.cb_feedback import feedback_callback

        update, query = _update_with_feedback
        query.data = "feedback:up"

        with patch("app.handlers.cb_feedback.save_feedback", new_callable=AsyncMock) as mock_save:
            await feedback_callback(update, MagicMock())

        mock_save.assert_awaited_once_with(42, 999, "up")
        query.answer.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_thumbs_down_calls_save(self, _update_with_feedback):
        from app.handlers.cb_feedback import feedback_callback

        update, query = _update_with_feedback
        query.data = "feedback:down"

        with patch("app.handlers.cb_feedback.save_feedback", new_callable=AsyncMock) as mock_save:
            await feedback_callback(update, MagicMock())

        mock_save.assert_awaited_once_with(42, 999, "down")

    @pytest.mark.asyncio
    async def test_feedback_replaces_keyboard_row(self, _update_with_feedback):
        from app.handlers.cb_feedback import feedback_callback

        update, query = _update_with_feedback
        query.data = "feedback:up"

        with patch("app.handlers.cb_feedback.save_feedback", new_callable=AsyncMock):
            await feedback_callback(update, MagicMock())

        # The feedback row should be replaced, edit_reply_markup called
        query.message.edit_reply_markup.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_save_failure_does_not_raise(self, _update_with_feedback):
        """Feedback save errors should be logged but not propagate."""
        from app.handlers.cb_feedback import feedback_callback

        update, query = _update_with_feedback
        query.data = "feedback:up"

        with patch(
            "app.handlers.cb_feedback.save_feedback",
            new_callable=AsyncMock,
            side_effect=Exception("DB down"),
        ):
            # Should not raise
            await feedback_callback(update, MagicMock())

        query.answer.assert_awaited_once()


class TestExports:
    def test_all_exports(self):
        from app.handlers.cb_feedback import __all__

        assert "feedback_callback" in __all__
        assert "_noop_callback" in __all__
