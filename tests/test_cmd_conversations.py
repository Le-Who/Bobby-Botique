"""Tests for app.handlers.cmd_conversations — conversation CRUD commands."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


def make_update(user_id=123, args=None):
    """Create a minimal mock Update + Context."""
    update = MagicMock()
    update.effective_user.id = user_id
    update.effective_chat.id = 456
    update.message.reply_text = AsyncMock()

    context = MagicMock()
    context.args = args or []
    return update, context


# ── /save ─────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_save_conversation_with_title():
    """save_conversation_command with explicit title."""
    update, context = make_update(args=["My", "Conversation"])

    with (
        patch(
            "app.utils.decorators.is_authorized",
            new_callable=AsyncMock,
            return_value=True,
        ),
        patch(
            "app.handlers.cmd_conversations.get_user_chat",
            new_callable=AsyncMock,
            return_value=SimpleNamespace(history=[], system_prompt=None),
        ),
        patch(
            "app.handlers.cmd_conversations.save_conversation",
            new_callable=AsyncMock,
            return_value=42,
        ),
    ):
        from app.handlers.cmd_conversations import save_conversation_command

        await save_conversation_command(update, context)

    update.message.reply_text.assert_awaited_once()
    reply_text = update.message.reply_text.call_args[0][0]
    assert "42" in reply_text
    assert "✅" in reply_text


@pytest.mark.asyncio
async def test_save_conversation_failure():
    """save_conversation_command handles DB failure."""
    update, context = make_update(args=["Title"])

    with (
        patch(
            "app.utils.decorators.is_authorized",
            new_callable=AsyncMock,
            return_value=True,
        ),
        patch(
            "app.handlers.cmd_conversations.get_user_chat",
            new_callable=AsyncMock,
            return_value=SimpleNamespace(history=[], system_prompt=None),
        ),
        patch(
            "app.handlers.cmd_conversations.save_conversation",
            new_callable=AsyncMock,
            return_value=None,
        ),
    ):
        from app.handlers.cmd_conversations import save_conversation_command

        await save_conversation_command(update, context)

    reply_text = update.message.reply_text.call_args[0][0]
    assert "❌" in reply_text


# ── /conversations ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_conversations_command_empty():
    """conversations_command shows menu content."""
    update, context = make_update()

    with (
        patch(
            "app.utils.decorators.is_authorized",
            new_callable=AsyncMock,
            return_value=True,
        ),
        patch("app.handlers.cmd_conversations.menus") as mock_menus,
    ):
        mock_menus.get_conversations_menu_content = AsyncMock(return_value=("📂 Нет бесед", None, None))

        from app.handlers.cmd_conversations import conversations_command

        await conversations_command(update, context)

    update.message.reply_text.assert_awaited_once_with("📂 Нет бесед")


# ── /switch ───────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_switch_conversation_no_args():
    """switch_conversation_command without args shows usage."""
    update, context = make_update()

    with patch("app.utils.decorators.is_authorized", new_callable=AsyncMock, return_value=True):
        from app.handlers.cmd_conversations import switch_conversation_command

        await switch_conversation_command(update, context)

    reply_text = update.message.reply_text.call_args[0][0]
    assert "Использование" in reply_text


@pytest.mark.asyncio
async def test_switch_conversation_success():
    """switch_conversation_command with valid ID."""
    update, context = make_update(args=["5"])

    with (
        patch(
            "app.utils.decorators.is_authorized",
            new_callable=AsyncMock,
            return_value=True,
        ),
        patch(
            "app.handlers.cmd_conversations.switch_to_conversation",
            new_callable=AsyncMock,
            return_value=True,
        ) as switch,
        patch("app.handlers.cmd_conversations.role_conv_metrics") as mock_metrics,
    ):
        mock_metrics.record_conversation_switched = AsyncMock()

        from app.handlers.cmd_conversations import switch_conversation_command

        await switch_conversation_command(update, context)

    switch.assert_awaited_once_with(123, 5)
    mock_metrics.record_conversation_switched.assert_awaited_once_with()
    update.message.reply_text.assert_awaited_once_with("✅ Переключились на беседу 5")


# ── /delete ───────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_delete_conversation_no_args():
    """delete_conversation_command without args shows usage."""
    update, context = make_update()

    with patch("app.utils.decorators.is_authorized", new_callable=AsyncMock, return_value=True):
        from app.handlers.cmd_conversations import delete_conversation_command

        await delete_conversation_command(update, context)

    reply_text = update.message.reply_text.call_args[0][0]
    assert "Использование" in reply_text


@pytest.mark.asyncio
async def test_delete_conversation_shows_confirmation():
    """delete_conversation_command shows confirmation buttons."""
    update, context = make_update(args=["7"])

    with patch("app.utils.decorators.is_authorized", new_callable=AsyncMock, return_value=True):
        from app.handlers.cmd_conversations import delete_conversation_command

        await delete_conversation_command(update, context)

    call_kwargs = update.message.reply_text.call_args[1]
    assert "reply_markup" in call_kwargs
    reply_text = update.message.reply_text.call_args[0][0]
    assert "7" in reply_text
    assert "удалить" in reply_text.lower()
    assert [[button.callback_data for button in row] for row in call_kwargs["reply_markup"].inline_keyboard] == [
        ["conv_delete_confirm:7"],
        ["conv_delete_cancel"],
    ]


@pytest.mark.asyncio
async def test_delete_confirmation_mutates_only_scoped_conversation_and_replay_fails():
    from app.handlers.cb_conversations import conv_delete_confirm_callback

    update, context = make_update(user_id=123)
    query = update.callback_query
    query.from_user.id = 123
    query.data = "conv_delete_confirm:7"
    query.answer = AsyncMock()
    query.edit_message_text = AsyncMock()
    saved = {(123, 7), (999, 7), (123, 8)}

    async def delete(user_id, conv_id):
        key = (user_id, conv_id)
        if key not in saved:
            return False
        saved.remove(key)
        return True

    with (
        patch("app.handlers.cb_conversations.delete_conversation", side_effect=delete) as delete_call,
        patch(
            "app.handlers.cb_conversations.role_conv_metrics.record_conversation_deleted", new_callable=AsyncMock
        ) as metrics,
        patch(
            "app.handlers.cb_conversations.menus.get_conversations_menu_content",
            new_callable=AsyncMock,
            return_value=("Remaining", "HTML", None),
        ) as menu,
    ):
        await conv_delete_confirm_callback(update, context)
        assert saved == {(999, 7), (123, 8)}
        delete_call.assert_awaited_once_with(123, 7)
        metrics.assert_awaited_once_with()
        menu.assert_awaited_once_with(123, 1)
        query.edit_message_text.assert_awaited_once_with("Remaining", parse_mode="HTML", reply_markup=None)
        query.answer.assert_awaited_once_with("✅ Беседа 7 удалена")

        query.answer.reset_mock()
        await conv_delete_confirm_callback(update, context)
        assert delete_call.await_count == 2
        assert saved == {(999, 7), (123, 8)}
        metrics.assert_awaited_once_with()
        menu.assert_awaited_once_with(123, 1)
        query.answer.assert_awaited_once_with("❌ Ошибка при удалении беседы")


@pytest.mark.asyncio
@pytest.mark.parametrize("saved", [{(999, 7)}, set()], ids=["wrong_owner", "missing"])
async def test_delete_confirmation_wrong_owner_or_missing_has_no_success_effects(saved):
    from app.handlers.cb_conversations import conv_delete_confirm_callback

    update, context = make_update(user_id=123)
    query = update.callback_query
    query.from_user.id = 123
    query.data = "conv_delete_confirm:7"
    query.answer = AsyncMock()
    query.edit_message_text = AsyncMock()
    before = saved.copy()

    async def scoped_delete(user_id, conv_id):
        key = (user_id, conv_id)
        if key not in saved:
            return False
        saved.remove(key)
        return True

    with (
        patch("app.handlers.cb_conversations.delete_conversation", side_effect=scoped_delete) as delete,
        patch(
            "app.handlers.cb_conversations.role_conv_metrics.record_conversation_deleted", new_callable=AsyncMock
        ) as metrics,
        patch("app.handlers.cb_conversations.menus.get_conversations_menu_content", new_callable=AsyncMock) as menu,
    ):
        await conv_delete_confirm_callback(update, context)
    delete.assert_awaited_once_with(123, 7)
    assert saved == before
    metrics.assert_not_awaited()
    menu.assert_not_awaited()
    query.answer.assert_awaited_once_with("❌ Ошибка при удалении беседы")
