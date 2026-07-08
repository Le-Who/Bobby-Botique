"""Tests for app.handlers.cb_models — model selection callbacks."""

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
    return update, query


class TestModelButtonCallback:
    @pytest.mark.asyncio
    async def test_model_none_is_ignored(self):
        """Clicking the separator (model_none) should be a no-op."""
        from app.handlers.cb_models import model_button_callback

        update, query = _make_update("model_none")

        with (
            patch("app.handlers.cb_models._is_user_busy", return_value=False),
            patch("app.handlers.cb_models.get_user_chat", new_callable=AsyncMock),
            patch("app.handlers.cb_models.update_user_chat", new_callable=AsyncMock),
        ):
            await model_button_callback(update, MagicMock())

        # answer() is called, but edit_message_text should NOT be called
        query.answer.assert_awaited_once()
        query.edit_message_text.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_busy_user_gets_toast(self):
        """Busy users should see a toast and the request should be rejected."""
        from app.handlers.cb_models import model_button_callback

        update, query = _make_update("model:0")

        with patch("app.handlers.cb_models._is_user_busy", return_value=True):
            await model_button_callback(update, MagicMock())

        query.answer.assert_awaited()
        # Should show alert toast
        call_kwargs = query.answer.call_args
        assert call_kwargs[1].get("show_alert") is True or (len(call_kwargs[0]) >= 1 and call_kwargs[0][0])

    @pytest.mark.asyncio
    async def test_valid_model_index_updates_chat_state(self):
        """A valid model:index callback should update user's model choice."""
        from app.handlers.cb_models import model_button_callback

        update, query = _make_update("model:0")

        mock_chat_state = MagicMock()
        mock_chat_state.model = None

        with (
            patch("app.handlers.cb_models._is_user_busy", return_value=False),
            patch(
                "app.handlers.cb_models.get_user_chat",
                new_callable=AsyncMock,
                return_value=mock_chat_state,
            ),
            patch("app.handlers.cb_models.update_user_chat", new_callable=AsyncMock) as mock_update,
            patch("app.handlers.cb_models.settings") as mock_settings,
            patch("app.handlers.cb_models.get_openrouter_keys", return_value=None),
            patch("app.handlers.cb_models.menus") as mock_menus,
        ):
            mock_settings.AVAILABLE_MODELS = ["gemini-3.1-flash-lite"]
            mock_settings.OPENROUTER_AVAILABLE_MODELS = []
            mock_menus.get_model_menu_content.return_value = (
                "text",
                "Markdown",
                MagicMock(),
            )

            await model_button_callback(update, MagicMock())

        assert mock_chat_state.model == "gemini-3.1-flash-lite"
        mock_update.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_invalid_model_index_shows_error(self):
        """Out-of-range model index should show error message."""
        from app.handlers.cb_models import model_button_callback

        update, query = _make_update("model:999")

        with (
            patch("app.handlers.cb_models._is_user_busy", return_value=False),
            patch("app.handlers.cb_models.settings") as mock_settings,
            patch("app.handlers.cb_models.get_openrouter_keys", return_value=None),
        ):
            mock_settings.AVAILABLE_MODELS = ["gemini-3.1-flash-lite"]
            mock_settings.OPENROUTER_AVAILABLE_MODELS = []

            await model_button_callback(update, MagicMock())

        query.edit_message_text.assert_awaited_once()
        call_args = query.edit_message_text.call_args
        assert "ошибка" in call_args[0][0].lower() or "error" in call_args[0][0].lower()


class TestSwitchModelCallback:
    @pytest.mark.asyncio
    async def test_busy_user_gets_toast(self):
        from app.handlers.cb_models import switch_model_callback

        update, query = _make_update("switch_model:gemini-3.1-flash-lite")

        with patch("app.handlers.cb_models._is_user_busy", return_value=True):
            await switch_model_callback(update, MagicMock())

        query.answer.assert_awaited()

    @pytest.mark.asyncio
    async def test_unavailable_model_shows_warning(self):
        from app.handlers.cb_models import switch_model_callback

        update, query = _make_update("switch_model:nonexistent-model")

        with (
            patch("app.handlers.cb_models._is_user_busy", return_value=False),
            patch("app.handlers.cb_models.settings") as mock_settings,
        ):
            mock_settings.AVAILABLE_MODELS = ["gemini-3.1-flash-lite"]
            mock_settings.OPENROUTER_AVAILABLE_MODELS = []

            await switch_model_callback(update, MagicMock())

        query.edit_message_text.assert_awaited_once()
        assert "недоступна" in query.edit_message_text.call_args[0][0].lower()

    @pytest.mark.asyncio
    async def test_valid_switch_updates_state(self):
        from app.handlers.cb_models import switch_model_callback

        update, query = _make_update("switch_model:gemini-3.1-flash-lite")

        mock_chat_state = MagicMock()

        with (
            patch("app.handlers.cb_models._is_user_busy", return_value=False),
            patch("app.handlers.cb_models.settings") as mock_settings,
            patch(
                "app.handlers.cb_models.get_user_chat",
                new_callable=AsyncMock,
                return_value=mock_chat_state,
            ),
            patch("app.handlers.cb_models.update_user_chat", new_callable=AsyncMock) as mock_update,
        ):
            mock_settings.AVAILABLE_MODELS = ["gemini-3.1-flash-lite"]
            mock_settings.OPENROUTER_AVAILABLE_MODELS = []

            await switch_model_callback(update, MagicMock())

        assert mock_chat_state.model == "gemini-3.1-flash-lite"
        mock_update.assert_awaited_once()


class TestExports:
    def test_all_exports(self):
        from app.handlers.cb_models import __all__

        assert "model_button_callback" in __all__
        assert "switch_model_callback" in __all__
