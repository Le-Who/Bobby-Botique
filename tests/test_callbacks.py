from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.handlers import cb_documents
from tests.factories import make_telegram_context, make_telegram_update


@pytest.mark.asyncio
async def test_document_callback_structure(monkeypatch):
    update = make_telegram_update(user_id=12345)
    query = SimpleNamespace(
        data="doc:list",
        from_user=update.effective_user,
        answer=AsyncMock(),
        edit_message_text=AsyncMock(),
    )
    update.callback_query = query
    get_menu = AsyncMock(return_value=("text", "mode", "markup"))
    monkeypatch.setattr(cb_documents.menus, "get_documents_menu_content", get_menu)

    await cb_documents.document_callback(update, make_telegram_context())

    query.answer.assert_awaited_once_with()
    get_menu.assert_awaited_once_with(12345)
    query.edit_message_text.assert_awaited_once_with("text", parse_mode="mode", reply_markup="markup")
