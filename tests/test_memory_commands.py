"""Privacy and ownership tests for the Telegram memory explorer."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.handlers.memory_commands import _send_memory_page, memory_callback_handler, memory_command


def _update(*, user_id=42, chat_type="private", data=None):
    message = SimpleNamespace(
        chat=SimpleNamespace(type=chat_type),
        reply_text=AsyncMock(),
        edit_text=AsyncMock(),
    )
    query = None
    if data is not None:
        query = SimpleNamespace(data=data, message=message, answer=AsyncMock())
    return SimpleNamespace(
        effective_user=SimpleNamespace(id=user_id),
        effective_chat=SimpleNamespace(type=chat_type),
        message=message,
        callback_query=query,
    )


@pytest.mark.asyncio
async def test_memory_command_refuses_to_render_personal_snippets_in_group():
    update = _update(chat_type="group")
    core = memory_command.__wrapped__.__wrapped__

    with patch("app.handlers.memory_commands._send_memory_page", new_callable=AsyncMock) as send_page:
        await core(update, SimpleNamespace())

    send_page.assert_not_awaited()
    update.message.reply_text.assert_awaited_once()


@pytest.mark.asyncio
async def test_memory_buttons_are_bound_to_menu_owner():
    target = SimpleNamespace(reply_text=AsyncMock())
    memories = [{"id": 7, "content": "A private remembered fact", "created_at": None, "source_type": "fact"}]
    with (
        patch("app.repos.memory.list_memories", new_callable=AsyncMock, return_value=memories),
        patch(
            "app.repos.memory.get_memory_stats",
            new_callable=AsyncMock,
            return_value={"total_memories": 1},
        ),
    ):
        await _send_memory_page(target, 42, page=0)

    markup = target.reply_text.await_args.kwargs["reply_markup"]
    callback_data = [button.callback_data for row in markup.inline_keyboard for button in row]
    assert "mem:42:del:7:0" in callback_data
    assert "mem:42:noop" in callback_data


@pytest.mark.asyncio
async def test_foreign_memory_callback_is_rejected_before_repository_access():
    update = _update(user_id=99, data="mem:42:del:7:0")
    core = memory_callback_handler.__wrapped__.__wrapped__

    with patch("app.repos.memory.delete_memory", new_callable=AsyncMock) as delete:
        await core(update, SimpleNamespace())

    delete.assert_not_awaited()
    update.callback_query.answer.assert_awaited_once()
    assert update.callback_query.answer.await_args.kwargs["show_alert"] is True


@pytest.mark.asyncio
async def test_deleting_last_memory_replaces_stale_private_panel():
    update = _update(user_id=42, data="mem:42:del:7:0")
    core = memory_callback_handler.__wrapped__.__wrapped__

    with (
        patch("app.repos.memory.delete_memory", new_callable=AsyncMock, return_value=True),
        patch("app.repos.memory.list_memories", new_callable=AsyncMock, return_value=[]),
        patch(
            "app.repos.memory.get_memory_stats",
            new_callable=AsyncMock,
            return_value={"total_memories": 0},
        ),
    ):
        await core(update, SimpleNamespace())

    update.callback_query.message.edit_text.assert_awaited_once()
    assert "нет сохранённых" in update.callback_query.message.edit_text.await_args.args[0]
    update.callback_query.message.reply_text.assert_not_awaited()


@pytest.mark.asyncio
async def test_memory_page_clamps_after_last_page_shrinks():
    target = SimpleNamespace(edit_text=AsyncMock(), reply_text=AsyncMock())
    remaining = [
        {
            "id": 8,
            "content": "Only remaining fact",
            "created_at": None,
            "source_type": "fact",
        }
    ]

    with (
        patch(
            "app.repos.memory.list_memories",
            new_callable=AsyncMock,
            side_effect=[[], remaining],
        ) as list_memories,
        patch(
            "app.repos.memory.get_memory_stats",
            new_callable=AsyncMock,
            return_value={"total_memories": 1},
        ),
    ):
        await _send_memory_page(target, 42, page=2)

    assert list_memories.await_args_list[0].kwargs["offset"] == 10
    assert list_memories.await_args_list[1].kwargs["offset"] == 0
    markup = target.edit_text.await_args.kwargs["reply_markup"]
    labels = [button.text for row in markup.inline_keyboard for button in row]
    assert "1/1" in labels
    assert "3/1" not in labels
