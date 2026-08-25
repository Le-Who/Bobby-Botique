from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.handlers.cb_fwd_save import fwd_save_callback
from tests.factories import make_chat_state


def _callback_update(user_id: int = 42):
    message = SimpleNamespace(
        reply_markup=None,
        reply_text=AsyncMock(),
        edit_reply_markup=AsyncMock(),
    )
    query = SimpleNamespace(answer=AsyncMock(), message=message)
    update = SimpleNamespace(
        callback_query=query,
        effective_user=SimpleNamespace(id=user_id),
    )
    return update, query


@pytest.mark.asyncio
async def test_fwd_save_acknowledges_callback_once_and_reports_disabled_memory():
    update, query = _callback_update()
    chat_state = make_chat_state(ltm_enabled=False)

    with patch(
        "app.handlers.cb_fwd_save.get_user_chat",
        new_callable=AsyncMock,
        return_value=chat_state,
    ):
        await fwd_save_callback(update, MagicMock())

    query.answer.assert_awaited_once()
    query.message.reply_text.assert_awaited_once()
    assert "выключена" in query.message.reply_text.await_args.args[0]


@pytest.mark.asyncio
async def test_fwd_save_reports_stale_consent_without_answering_twice():
    update, query = _callback_update()
    chat_state = make_chat_state(
        history=[{"role": "model", "parts": ["A sufficiently long response to persist safely."]}],
        ltm_enabled=True,
        memory_epoch=7,
    )

    with (
        patch(
            "app.handlers.cb_fwd_save.get_user_chat",
            new_callable=AsyncMock,
            return_value=chat_state,
        ),
        patch(
            "app.repos.keys.get_available_gemini_key",
            new_callable=AsyncMock,
            return_value={"api_key": "test-key"},
        ),
        patch(
            "app.repos.memory.store_memory",
            new_callable=AsyncMock,
            return_value=None,
        ) as store_memory,
    ):
        await fwd_save_callback(update, MagicMock())

    query.answer.assert_awaited_once()
    query.message.reply_text.assert_awaited_once()
    assert "устарела" in query.message.reply_text.await_args.args[0]
    assert store_memory.await_args.kwargs["expected_epoch"] == 7
