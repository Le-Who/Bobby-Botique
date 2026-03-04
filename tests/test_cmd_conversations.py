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
        patch("app.utils.decorators.is_authorized", new_callable=AsyncMock, return_value=True),
        patch(
            "app.handlers.cmd_conversations.get_user_chat",
            new_callable=AsyncMock,
            return_value=SimpleNamespace(history=[], system_prompt=None),
        ),
        patch("app.handlers.cmd_conversations.save_conversation", new_callable=AsyncMock, return_value=42),
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
        patch("app.utils.decorators.is_authorized", new_callable=AsyncMock, return_value=True),
        patch(
            "app.handlers.cmd_conversations.get_user_chat",
            new_callable=AsyncMock,
            return_value=SimpleNamespace(history=[], system_prompt=None),
        ),
        patch("app.handlers.cmd_conversations.save_conversation", new_callable=AsyncMock, return_value=None),
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
        patch("app.utils.decorators.is_authorized", new_callable=AsyncMock, return_value=True),
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
        patch("app.utils.decorators.is_authorized", new_callable=AsyncMock, return_value=True),
        patch("app.handlers.cmd_conversations.switch_to_conversation", new_callable=AsyncMock, return_value=True),
        patch("app.handlers.cmd_conversations.role_conv_metrics") as mock_metrics,
    ):
        mock_metrics.record_conversation_switched = AsyncMock()

        from app.handlers.cmd_conversations import switch_conversation_command

        await switch_conversation_command(update, context)

    reply_text = update.message.reply_text.call_args[0][0]
    assert "✅" in reply_text


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
