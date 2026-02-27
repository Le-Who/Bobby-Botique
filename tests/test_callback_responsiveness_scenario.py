import asyncio
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app import state
from app.handlers import callbacks


class DummyQuery:
    def __init__(self, user_id: int, data: str):
        self.data = data
        self.from_user = SimpleNamespace(id=user_id)
        self._answers = []
        self.message = SimpleNamespace()
        self.edit_message_text = AsyncMock()

    async def answer(self, text=None, show_alert=False):
        self._answers.append((text, show_alert))


class DummyUpdate:
    def __init__(self, query: DummyQuery):
        self.callback_query = query


@pytest.mark.asyncio
async def test_user_b_settings_callback_not_blocked_by_user_a_long_request(monkeypatch):
    """
    Regression scenario:
    - user A holds per-user lock for a long-running request
    - user B triggers lightweight settings callback (model selection)
    Expectation: user B callback is processed immediately and does not wait for user A flow.
    """
    user_a = 111
    user_b = 222

    # Minimal settings/config stubs used by model_button_callback
    monkeypatch.setattr(
        callbacks,
        "settings",
        SimpleNamespace(
            AVAILABLE_MODELS=["gemini-2.5-flash"], OPENROUTER_AVAILABLE_MODELS=[]
        ),
    )
    monkeypatch.setattr(callbacks, "get_openrouter_keys", lambda: [])
    monkeypatch.setattr(callbacks, "get_model_hash", lambda model_name: "hash-ok")

    chat_state = SimpleNamespace(model=None)

    mock_get = AsyncMock(return_value=chat_state)
    mock_update = AsyncMock()
    monkeypatch.setattr(callbacks, "get_user_chat", mock_get)
    monkeypatch.setattr(callbacks, "update_user_chat", mock_update)
    monkeypatch.setattr(
        callbacks.menus,
        "get_model_menu_content",
        lambda _chat_state, _ctx: ("ok", None, None),
    )

    query_b = DummyQuery(user_b, "model:0:hash-ok")
    update_b = DummyUpdate(query_b)
    context_b = SimpleNamespace()

    async def long_request_user_a():
        async with state.get_user_lock(user_a):
            await asyncio.sleep(0.30)

    task_a = asyncio.create_task(long_request_user_a())
    await asyncio.sleep(0.02)  # let A acquire lock

    start = time.perf_counter()
    await callbacks.model_button_callback(update_b, context_b)
    elapsed = time.perf_counter() - start

    await task_a

    # Callback must finish well before long request completion.
    assert elapsed < 0.15
    mock_update.assert_awaited_once()
    query_b.edit_message_text.assert_awaited_once()
    assert any(msg and "Модель изменена" in msg for msg, _ in query_b._answers)
