"""Tests for app.handlers.cb_navigation — navigation and menu callbacks."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest


def _make_update(callback_data: str, user_id: int = 42):
    """Build a mock Update with given callback_data."""
    update = MagicMock()
    query = MagicMock()
    update.callback_query = query
    query.data = callback_data
    query.from_user.id = user_id
    query.answer = AsyncMock()
    query.edit_message_text = AsyncMock()
    query.edit_message_reply_markup = AsyncMock()
    query.message = MagicMock()
    query.message.reply_text = AsyncMock()
    return update, query


class TestNewTopicCallback:
    @pytest.mark.asyncio
    async def test_busy_user_rejected(self):
        from app.handlers.cb_navigation import new_topic_callback

        update, query = _make_update("new_topic")

        with patch("app.handlers.cb_navigation._is_user_busy", return_value=True):
            await new_topic_callback(update, MagicMock())

        query.answer.assert_awaited()

    @pytest.mark.asyncio
    async def test_clears_chat_context(self):
        from app.handlers.cb_navigation import new_topic_callback

        update, query = _make_update("new_topic")

        mock_chat_state = MagicMock()
        mock_chat_state.history = [{"role": "user", "parts": ["hello"]}]
        mock_chat_state.token_count = 100
        mock_chat_state.is_deep_dive = True
        mock_chat_state.deep_dive_thread_id = "abc"
        mock_chat_state.context_summary = "some summary"

        with (
            patch("app.handlers.cb_navigation._is_user_busy", return_value=False),
            patch(
                "app.handlers.cb_navigation.get_user_chat",
                new_callable=AsyncMock,
                return_value=mock_chat_state,
            ),
            patch("app.handlers.cb_navigation.update_user_chat", new_callable=AsyncMock) as mock_update,
        ):
            await new_topic_callback(update, MagicMock())

        # Chat state should be cleared
        assert mock_chat_state.history == []
        assert mock_chat_state.token_count == 0
        assert mock_chat_state.system_prompt is None
        assert mock_chat_state.context_summary is None
        mock_update.assert_awaited_once()


class TestModelMenuCallback:
    @pytest.mark.asyncio
    async def test_edits_message_with_menu(self):
        from app.handlers.cb_navigation import model_menu_callback

        update, query = _make_update("model_menu")
        context = MagicMock()

        mock_chat_state = MagicMock()

        with (
            patch(
                "app.handlers.cb_navigation.get_user_chat",
                new_callable=AsyncMock,
                return_value=mock_chat_state,
            ),
            patch("app.handlers.cb_navigation.menus") as mock_menus,
        ):
            mock_menus.get_model_menu_content.return_value = (
                "Model Menu",
                "Markdown",
                MagicMock(),
            )

            await model_menu_callback(update, context)

        query.answer.assert_awaited_once()
        query.edit_message_text.assert_awaited_once()


class TestToggleSearchCallback:
    @pytest.mark.asyncio
    async def test_toggles_search_enabled(self):
        from app.handlers.cb_navigation import toggle_search_callback

        update, query = _make_update("toggle_search")

        mock_chat_state = MagicMock()
        mock_chat_state.search_enabled = False

        with (
            patch(
                "app.handlers.cb_navigation.get_user_chat",
                new_callable=AsyncMock,
                return_value=mock_chat_state,
            ),
            patch("app.handlers.cb_navigation.update_user_chat", new_callable=AsyncMock) as mock_update,
        ):
            await toggle_search_callback(update, MagicMock())

        assert mock_chat_state.search_enabled is True
        mock_update.assert_awaited_once()


class TestDeepDiveCallback:
    @pytest.mark.asyncio
    async def test_busy_user_rejected(self):
        from app.handlers.cb_navigation import deep_dive_callback

        update, query = _make_update("deep_dive:exit")

        with patch("app.handlers.cb_navigation._is_user_busy", return_value=True):
            await deep_dive_callback(update, MagicMock())

        query.answer.assert_awaited()


class TestExports:
    def test_all_exports_present(self):
        from app.handlers.cb_navigation import __all__

        expected = [
            "deep_dive_callback",
            "help_callback",
            "help_topic_callback",
            "model_menu_callback",
            "new_chat_callback",
            "new_topic_callback",
            "open_conversations_callback",
            "open_documents_callback",
            "toggle_search_callback",
        ]
        for name in expected:
            assert name in __all__, f"{name} missing from __all__"
