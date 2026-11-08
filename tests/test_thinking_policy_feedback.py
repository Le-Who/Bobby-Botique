"""User-facing reasoning controls describe effective policy and saved preference."""

import inspect
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.handlers import cb_navigation, commands
from app.runtime_settings import lifecycle
from app.runtime_settings.store import SettingsSnapshot
from tests.factories import make_chat_state


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["query", "set", "settings", "callback"])
async def test_reasoning_feedback_never_claims_saved_high_over_admin_none(monkeypatch, action):
    chat = make_chat_state(model="gemini-previous-user-choice", thinking_level="high")
    message = SimpleNamespace(reply_text=AsyncMock())
    query = SimpleNamespace(
        from_user=SimpleNamespace(id=81200),
        edit_message_text=AsyncMock(),
        answer=AsyncMock(),
    )
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=81200, language_code="ru"),
        message=message,
        callback_query=query,
    )
    monkeypatch.setattr(commands, "get_user_chat", AsyncMock(return_value=chat))
    monkeypatch.setattr(cb_navigation, "get_user_chat", AsyncMock(return_value=chat))
    monkeypatch.setattr(cb_navigation, "_is_user_busy", lambda _: False)
    save = AsyncMock()
    monkeypatch.setattr("app.repos.chats.update_thinking_level", save)
    snapshot = SettingsSnapshot(
        125,
        {
            "live_route:provider": "openai",
            "process:chat": {
                "models": ["openai-direct/gpt-6-luna"],
                "strategy": "sequential",
                "inherit_user_model": False,
                "reasoning_effort": "none",
            },
        },
    )
    token = lifecycle._operation_snapshot.set(snapshot)
    try:
        if action in {"query", "set"}:
            await inspect.unwrap(commands.thinking_command)(
                update, SimpleNamespace(args=["high"] if action == "set" else [])
            )
            text = message.reply_text.await_args.args[0]
        elif action == "settings":
            await inspect.unwrap(commands.settings_command)(update, SimpleNamespace())
            text = message.reply_text.await_args.args[0]
        else:
            await cb_navigation.settings_thinking_callback(update, SimpleNamespace())
            text = query.edit_message_text.await_args.args[0]
            assert "администратором" in query.answer.await_args.args[0]
        assert "администратором" in text
        assert "Выключен" in text
        assert "Для сложных задач автоматически" in text
        assert "gpt-6.1-terra" in text
        if action == "set":
            assert "Предпочтение сохранено" in text
            save.assert_awaited_once_with(81200, "high")
            assert chat.thinking_level == "high"
        elif action in {"query", "settings"}:
            save.assert_not_awaited()
            assert chat.thinking_level == "high"
    finally:
        lifecycle._operation_snapshot.reset(token)


@pytest.mark.asyncio
async def test_baseline_reasoning_command_keeps_existing_user_preference_feedback(monkeypatch):
    chat = make_chat_state(thinking_level="high")
    monkeypatch.setattr(commands, "get_user_chat", AsyncMock(return_value=chat))
    message = SimpleNamespace(reply_text=AsyncMock())
    token = lifecycle._operation_snapshot.set(SettingsSnapshot(126, {}))
    try:
        await inspect.unwrap(commands.thinking_command)(
            SimpleNamespace(effective_user=SimpleNamespace(id=81200), message=message), SimpleNamespace(args=[])
        )
        text = message.reply_text.await_args.args[0]
        assert "Высокий" in text
        assert "администратором" not in text
    finally:
        lifecycle._operation_snapshot.reset(token)
