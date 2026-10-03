import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app import state
from app.handlers import cb_models


class DummyQuery:
    def __init__(self, user_id: int, data: str):
        self.data = data
        self.from_user = SimpleNamespace(id=user_id)
        self._answers: list[tuple[str | None, bool]] = []
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
        cb_models,
        "settings",
        SimpleNamespace(
            AVAILABLE_MODELS=["gemini-2.5-flash"],
            OPENCODE_AVAILABLE_MODELS=[],
            OPENROUTER_AVAILABLE_MODELS=[],
        ),
    )
    monkeypatch.setattr(cb_models, "get_openrouter_keys", list)
    monkeypatch.setattr(cb_models, "get_model_hash", lambda model_name: "hash-ok")

    chat_state = SimpleNamespace(model=None)

    mock_get = AsyncMock(return_value=chat_state)
    mock_update = AsyncMock()
    monkeypatch.setattr(cb_models, "get_user_chat", mock_get)
    monkeypatch.setattr(cb_models, "update_user_chat", mock_update)
    monkeypatch.setattr(
        cb_models.menus,
        "get_model_menu_content",
        lambda _chat_state, _ctx: ("ok", None, None),
    )

    query_b = DummyQuery(user_b, "model:0:hash-ok")
    update_b = DummyUpdate(query_b)
    context_b = SimpleNamespace()

    acquired = asyncio.Event()
    release = asyncio.Event()

    async def long_request_user_a():
        async with state.get_user_lock(user_a):
            acquired.set()
            await release.wait()

    task_a = asyncio.create_task(long_request_user_a())
    try:
        await asyncio.wait_for(acquired.wait(), timeout=2)
        await asyncio.wait_for(cb_models.model_button_callback(update_b, context_b), timeout=2)
        assert not task_a.done()
        assert state.get_user_lock(user_a).locked()
        mock_update.assert_awaited_once()
        query_b.edit_message_text.assert_awaited_once()
        assert any(msg and "Модель изменена" in msg for msg, _ in query_b._answers)
    finally:
        release.set()
        await task_a
