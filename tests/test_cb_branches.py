"""Tests for app.handlers.cb_branches — conversation branching callbacks."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.database import ChatState


def _make_update(user_id: int = 42):
    """Build a mock Update with callback_query for branch tests."""
    update = MagicMock()
    update.effective_user.id = user_id
    query = MagicMock()
    query.answer = AsyncMock()
    query.message.reply_text = AsyncMock()
    update.callback_query = query
    return update, query


def _make_chat_state(branch_id=None, history=None):
    return ChatState(
        history=history or [{"role": "user", "parts": ["hello"]}, {"role": "model", "parts": ["hi"]}],
        model="gemini-flash",
        token_count=100,
        search_enabled=False,
        system_prompt=None,
        branch_id=branch_id,
    )


class TestBranchCreateCallback:
    @pytest.mark.asyncio
    async def test_creates_branch_and_sets_id(self):
        from app.handlers.cb_branches import branch_create_callback

        update, query = _make_update()
        chat_state = _make_chat_state()

        with (
            patch("app.handlers.cb_branches.get_user_chat", new_callable=AsyncMock, return_value=chat_state),
            patch("app.handlers.cb_branches.create_branch", new_callable=AsyncMock, return_value=99),
            patch("app.handlers.cb_branches.update_user_chat", new_callable=AsyncMock) as mock_save,
        ):
            await branch_create_callback(update, MagicMock())

        assert chat_state.branch_id == 99
        mock_save.assert_awaited_once_with(42, chat_state)
        query.message.reply_text.assert_awaited_once()
        reply_text = query.message.reply_text.call_args[0][0]
        assert "Ветка создана" in reply_text

    @pytest.mark.asyncio
    async def test_rejects_if_already_in_branch(self):
        from app.handlers.cb_branches import branch_create_callback

        update, query = _make_update()
        chat_state = _make_chat_state(branch_id=5)

        with patch("app.handlers.cb_branches.get_user_chat", new_callable=AsyncMock, return_value=chat_state):
            await branch_create_callback(update, MagicMock())

        query.answer.assert_awaited()
        # Should show alert about already being in a branch
        alert_calls = [c for c in query.answer.call_args_list if c.kwargs.get("show_alert")]
        assert len(alert_calls) >= 1

    @pytest.mark.asyncio
    async def test_handles_no_chat_state(self):
        from app.handlers.cb_branches import branch_create_callback

        update, query = _make_update()

        with patch("app.handlers.cb_branches.get_user_chat", new_callable=AsyncMock, return_value=None):
            await branch_create_callback(update, MagicMock())

        alert_calls = [c for c in query.answer.call_args_list if c.kwargs.get("show_alert")]
        assert len(alert_calls) >= 1

    @pytest.mark.asyncio
    async def test_handles_branch_creation_failure(self):
        from app.handlers.cb_branches import branch_create_callback

        update, query = _make_update()
        chat_state = _make_chat_state()

        with (
            patch("app.handlers.cb_branches.get_user_chat", new_callable=AsyncMock, return_value=chat_state),
            patch("app.handlers.cb_branches.create_branch", new_callable=AsyncMock, return_value=None),
        ):
            await branch_create_callback(update, MagicMock())

        alert_calls = [c for c in query.answer.call_args_list if c.kwargs.get("show_alert")]
        assert len(alert_calls) >= 1


class TestBranchReturnCallback:
    @pytest.mark.asyncio
    async def test_restores_original_history(self):
        from app.handlers.cb_branches import branch_return_callback

        update, query = _make_update()
        original = [{"role": "user", "parts": ["original message"]}]
        chat_state = _make_chat_state(branch_id=10, history=[{"role": "user", "parts": ["branch msg"]}])

        with (
            patch("app.handlers.cb_branches.get_user_chat", new_callable=AsyncMock, return_value=chat_state),
            patch("app.handlers.cb_branches.restore_branch", new_callable=AsyncMock, return_value=original),
            patch("app.handlers.cb_branches.update_user_chat", new_callable=AsyncMock) as mock_save,
            patch("app.handlers.cb_branches.delete_branch", new_callable=AsyncMock) as mock_delete,
        ):
            await branch_return_callback(update, MagicMock())

        assert chat_state.history == original
        assert chat_state.branch_id is None
        mock_save.assert_awaited_once_with(42, chat_state)
        mock_delete.assert_awaited_once_with(10, 42)
        reply_text = query.message.reply_text.call_args[0][0]
        assert "Вернулись" in reply_text

    @pytest.mark.asyncio
    async def test_already_in_main_shows_info(self):
        from app.handlers.cb_branches import branch_return_callback

        update, query = _make_update()
        chat_state = _make_chat_state(branch_id=None)

        with patch("app.handlers.cb_branches.get_user_chat", new_callable=AsyncMock, return_value=chat_state):
            await branch_return_callback(update, MagicMock())

        alert_calls = [c for c in query.answer.call_args_list if c.kwargs.get("show_alert")]
        assert len(alert_calls) >= 1

    @pytest.mark.asyncio
    async def test_handles_restore_failure(self):
        from app.handlers.cb_branches import branch_return_callback

        update, query = _make_update()
        chat_state = _make_chat_state(branch_id=10)

        with (
            patch("app.handlers.cb_branches.get_user_chat", new_callable=AsyncMock, return_value=chat_state),
            patch("app.handlers.cb_branches.restore_branch", new_callable=AsyncMock, return_value=None),
        ):
            await branch_return_callback(update, MagicMock())

        alert_calls = [c for c in query.answer.call_args_list if c.kwargs.get("show_alert")]
        assert len(alert_calls) >= 1


class TestExports:
    def test_all_exports(self):
        from app.handlers.cb_branches import __all__

        assert "branch_create_callback" in __all__
        assert "branch_return_callback" in __all__
