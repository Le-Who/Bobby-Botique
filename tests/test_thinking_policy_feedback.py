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
@pytest.mark.parametrize(
    "mode",
    ["profile_none", "profile_null", "profile_missing", "profile_complex_null", "baseline_direct", "baseline_plan"],
)
async def test_openai_reasoning_is_readonly_even_for_forged_controls_and_stale_high(monkeypatch, action, mode):
    chat = make_chat_state(
        model="openai-direct/gpt-6-luna" if mode == "baseline_direct" else "gemini-previous-user-choice",
        thinking_level="high",
    )
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
    values = {"live_route:provider": "baseline" if mode.startswith("baseline_") else "openai"}
    if mode != "profile_missing" and mode != "baseline_direct":
        values["process:chat"] = {
            "models": ["openai-direct/gpt-6-luna"],
            "strategy": "sequential",
            "inherit_user_model": False,
            "reasoning_effort": None if mode == "profile_null" else "none",
        }
    if mode == "profile_complex_null":
        values["process:chat.complex"] = {
            "models": ["openai-direct/gpt-6.1-terra"],
            "strategy": "sequential",
            "inherit_user_model": False,
            "reasoning_effort": None,
        }
    snapshot = SettingsSnapshot(125, values)
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
        assert "Высокий" not in text
        assert "Сохранить предпочтение" not in text
        assert "когда администратор не задаёт" not in text
        assert "Предпочтение сохранено" not in text
        if mode.startswith("profile_"):
            assert "Для сложных задач автоматически" in text
            assert "gpt-6.1-terra" in text
        if action in {"settings", "callback"}:
            keyboard = (message.reply_text if action == "settings" else query.edit_message_text).await_args.kwargs[
                "reply_markup"
            ]
            assert all(
                button.callback_data != "settings_thinking" for row in keyboard.inline_keyboard for button in row
            )
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


@pytest.mark.asyncio
@pytest.mark.parametrize("profile", ["baseline", "gemini"])
@pytest.mark.parametrize("action", ["set", "callback"])
async def test_gemini_keeps_user_reasoning_mutable(monkeypatch, profile, action):
    chat = make_chat_state(
        model="openai-direct/gpt-6-luna" if profile == "gemini" else "gemini-3.5-flash-lite", thinking_level="high"
    )
    monkeypatch.setattr(commands, "get_user_chat", AsyncMock(return_value=chat))
    monkeypatch.setattr(cb_navigation, "get_user_chat", AsyncMock(return_value=chat))
    monkeypatch.setattr(cb_navigation, "_is_user_busy", lambda _: False)
    save = AsyncMock()
    monkeypatch.setattr("app.repos.chats.update_thinking_level", save)
    query = SimpleNamespace(from_user=SimpleNamespace(id=81200), answer=AsyncMock(), edit_message_text=AsyncMock())
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=81200, language_code="ru"),
        message=SimpleNamespace(reply_text=AsyncMock()),
        callback_query=query,
    )
    token = lifecycle._operation_snapshot.set(SettingsSnapshot(126, {"live_route:provider": profile}))
    try:
        if action == "set":
            await inspect.unwrap(commands.thinking_command)(update, SimpleNamespace(args=["low"]))
            save.assert_awaited_once_with(81200, "low")
            assert chat.thinking_level == "low"
        else:
            await cb_navigation.settings_thinking_callback(update, SimpleNamespace())
            save.assert_awaited_once_with(81200, None)
            assert chat.thinking_level is None
    finally:
        lifecycle._operation_snapshot.reset(token)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "model,override,expected",
    [
        ("openai-direct/gpt-6.1-sol", None, "Низкий"),
        ("openai-direct/gpt-6-luna", "high", "Высокий"),
    ],
)
async def test_readonly_openai_feedback_keeps_model_default_and_admin_override_distinct(
    monkeypatch, model, override, expected
):
    chat = make_chat_state(model=model, thinking_level="medium")
    monkeypatch.setattr(commands, "get_user_chat", AsyncMock(return_value=chat))
    save = AsyncMock()
    monkeypatch.setattr("app.repos.chats.update_thinking_level", save)
    message = SimpleNamespace(reply_text=AsyncMock())
    token = lifecycle._operation_snapshot.set(
        SettingsSnapshot(
            127,
            {
                "process:chat": {
                    "models": [model],
                    "strategy": "sequential",
                    "inherit_user_model": False,
                    "reasoning_effort": override,
                },
            },
        )
    )
    try:
        await inspect.unwrap(commands.thinking_command)(
            SimpleNamespace(effective_user=SimpleNamespace(id=81200), message=message), SimpleNamespace(args=["auto"])
        )
        text = message.reply_text.await_args.args[0]
        assert expected in text
        assert "Средний" not in text
        assert "администратором" in text
        assert "Сохранить предпочтение" not in text
        assert chat.thinking_level == "medium"
        save.assert_not_awaited()
    finally:
        lifecycle._operation_snapshot.reset(token)
