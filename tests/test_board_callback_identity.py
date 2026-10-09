"""Board close must authorize the board belonging to the callback message."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.handlers.board_handler import handle_board_close_callback


@pytest.fixture
def close_input(monkeypatch):
    board = {"id": 11, "creator_id": 101, "topic": "Ideas", "entries": [], "last_summary": ""}
    lookup = AsyncMock(return_value=board)
    close = AsyncMock(return_value=True)
    monkeypatch.setattr("app.repos.boards_repo.get_board_by_inline_msg", lookup)
    monkeypatch.setattr("app.repos.boards_repo.close_board", close)
    query = SimpleNamespace(
        data="board_close:11", inline_message_id="inline-A", from_user=SimpleNamespace(id=101), answer=AsyncMock()
    )
    bot = SimpleNamespace(edit_message_text=AsyncMock())
    return SimpleNamespace(
        query=query,
        board=board,
        lookup=lookup,
        close=close,
        bot=bot,
        update=SimpleNamespace(callback_query=query),
        context=SimpleNamespace(bot=bot),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid", ["foreign_payload", "missing_actor", "missing_creator", "foreign_actor", "stale"])
async def test_close_rejects_unbound_or_unauthorized_board(close_input, invalid):
    case = close_input
    if invalid == "foreign_payload":
        case.query.data = "board_close:22"
    elif invalid == "missing_actor":
        case.query.from_user = None
    elif invalid == "missing_creator":
        case.board["creator_id"] = None
    elif invalid == "foreign_actor":
        case.query.from_user.id = 202
    else:
        case.lookup.return_value = None

    await handle_board_close_callback(case.update, case.context)

    case.lookup.assert_awaited_once_with("inline-A")
    case.close.assert_not_awaited()
    case.bot.edit_message_text.assert_not_awaited()
    assert any(call.kwargs.get("show_alert") for call in case.query.answer.await_args_list)


@pytest.mark.asyncio
@pytest.mark.parametrize("payload", ["board_close", "board_close:nope", "board_close:0", "board_close:-1", "other:11"])
async def test_close_rejects_malformed_payload_without_repository_call(close_input, payload):
    close_input.query.data = payload
    await handle_board_close_callback(close_input.update, close_input.context)
    close_input.lookup.assert_not_awaited()
    close_input.close.assert_not_awaited()
    close_input.bot.edit_message_text.assert_not_awaited()


@pytest.mark.asyncio
async def test_creator_closes_exact_message_board(close_input):
    await handle_board_close_callback(close_input.update, close_input.context)
    close_input.close.assert_awaited_once_with(11)
    edit = close_input.bot.edit_message_text.await_args.kwargs
    assert edit["inline_message_id"] == "inline-A"
    assert "закрыта" in edit["text"]
    assert edit["reply_markup"].inline_keyboard[0][0].callback_data == "inline_noop"


@pytest.mark.asyncio
async def test_failed_close_does_not_publish_closed_ui(close_input):
    close_input.close.return_value = False
    await handle_board_close_callback(close_input.update, close_input.context)
    close_input.close.assert_awaited_once_with(11)
    close_input.bot.edit_message_text.assert_not_awaited()
    assert any(call.kwargs.get("show_alert") for call in close_input.query.answer.await_args_list)
