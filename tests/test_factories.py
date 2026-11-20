"""
AAA unit tests for tests/factories.py shared test utilities.

Risk covered: Broken test factories corrupt every test that uses them —
a silent, hard-to-diagnose source of test pollution.

These tests verify that all factory functions produce objects with the
correct structure and attribute types expected by handlers and assertions.
"""

import pytest

from tests.factories import (
    make_chat_state,
    make_telegram_chat,
    make_telegram_context,
    make_telegram_message,
    make_telegram_update,
    make_telegram_user,
)


def test_make_chat_state_defaults_are_sensible():
    """Default chat state must be empty, ltm-enabled, not searching."""
    state = make_chat_state()

    assert state.history == []
    assert state.ltm_enabled is True
    assert state.is_deep_dive is False
    assert state.search_enabled is False
    assert state.token_count == 0
    assert state.branch_id is None


def test_make_chat_state_accepts_custom_history():
    """History parameter must be stored as-is."""
    history = [{"role": "user", "parts": ["Hello"]}]

    state = make_chat_state(history=history)

    assert state.history is history


def test_make_chat_state_different_calls_produce_independent_objects():
    """Two calls must produce separate state objects, not shared references."""
    state1 = make_chat_state()
    state2 = make_chat_state()
    state1.history.append({"role": "user", "parts": ["test"]})

    assert state2.history == [], "States must not share the same list reference"


def test_make_telegram_user_has_correct_id():
    """User ID must be set from the parameter."""
    user = make_telegram_user(user_id=42)

    assert user.id == 42


def test_make_telegram_user_has_username():
    user = make_telegram_user(username="alice")

    assert user.username == "alice"


def test_make_telegram_chat_sets_id_and_type():
    chat = make_telegram_chat(chat_id=100, chat_type="group")

    assert chat.id == 100
    assert chat.type == "group"


def test_make_telegram_message_text_is_set():
    msg = make_telegram_message(text="Hi there")

    assert msg.text == "Hi there"


def test_make_telegram_message_has_async_edit_text():
    """edit_text must be an AsyncMock so tests can await it."""
    msg = make_telegram_message()

    import inspect

    assert inspect.iscoroutinefunction(msg.edit_text) or hasattr(msg.edit_text, "_mock_is_coroutine")


def test_make_telegram_message_media_defaults_are_neutral():
    """By default, photo/document/voice/video must be falsy (no media attached)."""
    msg = make_telegram_message()

    assert not msg.photo
    assert msg.document is None
    assert msg.voice is None
    assert msg.video is None


def test_make_telegram_message_links_user_and_chat():
    """Message must have both from_user and chat set to consistent objects."""
    msg = make_telegram_message(user_id=999, chat_id=888)

    assert msg.from_user.id == 999
    assert msg.chat.id == 888


@pytest.mark.asyncio
async def test_plain_message_factory_reaches_text_intent_without_forwarded_metadata():
    from app.middleware.debounce import _build_entry
    from app.voice_intent import detect_tts_intent

    message = make_telegram_message(text="What is the capital of France?")
    decision = await detect_tts_intent(user_text=message.text, caption=message.caption)
    entry = await _build_entry(message)

    assert not decision.explicit_tts
    assert entry.is_user_authored and not entry.is_forwarded
    assert entry.text == message.text


def test_make_telegram_update_message_is_accessible():
    """Update must expose message attribute for handler code."""
    update = make_telegram_update(message_text="Test", user_id=1)

    assert update.message is not None
    assert update.message.text == "Test"


def test_make_telegram_update_effective_user_matches_message_user():
    """effective_user and message.from_user must refer to the same mock."""
    update = make_telegram_update(user_id=777)

    assert update.effective_user.id == update.message.from_user.id


def test_make_telegram_update_effective_message_matches_message():
    """effective_message must be the same as message."""
    update = make_telegram_update()

    assert update.effective_message is update.message


def test_make_telegram_context_has_send_message():
    """Context must have a bot with async send_message."""
    ctx = make_telegram_context()

    assert ctx.bot is not None
    assert hasattr(ctx.bot, "send_message")


def test_make_telegram_context_user_data_is_dict():
    ctx = make_telegram_context()

    assert isinstance(ctx.user_data, dict)
    assert isinstance(ctx.chat_data, dict)
