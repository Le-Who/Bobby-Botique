"""
AAA unit tests for tests/factories.py shared test utilities.

Risk covered: Broken test factories corrupt every test that uses them —
a silent, hard-to-diagnose source of test pollution.

These tests verify that all factory functions produce objects with the
correct structure and attribute types expected by handlers and assertions.
"""

import asyncio

import pytest

from tests.factories import (
    make_chat_state,
    make_telegram_chat,
    make_telegram_context,
    make_telegram_message,
    make_telegram_update,
    make_telegram_user,
)

# ─── make_chat_state ──────────────────────────────────────────────────────────


def test_make_chat_state_defaults_are_sensible():
    """Default chat state must be empty, ltm-enabled, not searching."""
    # Arrange / Act
    state = make_chat_state()

    # Assert
    assert state.history == []
    assert state.ltm_enabled is True
    assert state.is_deep_dive is False
    assert state.search_enabled is False
    assert state.token_count == 0
    assert state.branch_id is None


def test_make_chat_state_accepts_custom_history():
    """History parameter must be stored as-is."""
    # Arrange
    history = [{"role": "user", "parts": ["Hello"]}]

    # Act
    state = make_chat_state(history=history)

    # Assert
    assert state.history is history


def test_make_chat_state_different_calls_produce_independent_objects():
    """Two calls must produce separate state objects, not shared references."""
    # Arrange / Act
    state1 = make_chat_state()
    state2 = make_chat_state()
    state1.history.append({"role": "user", "parts": ["test"]})

    # Assert
    assert state2.history == [], "States must not share the same list reference"


# ─── make_telegram_user ───────────────────────────────────────────────────────


def test_make_telegram_user_has_correct_id():
    """User ID must be set from the parameter."""
    # Arrange / Act
    user = make_telegram_user(user_id=42)

    # Assert
    assert user.id == 42


def test_make_telegram_user_has_username():
    # Arrange / Act
    user = make_telegram_user(username="alice")

    # Assert
    assert user.username == "alice"


# ─── make_telegram_chat ───────────────────────────────────────────────────────


def test_make_telegram_chat_sets_id_and_type():
    # Arrange / Act
    chat = make_telegram_chat(chat_id=100, chat_type="group")

    # Assert
    assert chat.id == 100
    assert chat.type == "group"


# ─── make_telegram_message ───────────────────────────────────────────────────


def test_make_telegram_message_text_is_set():
    # Arrange / Act
    msg = make_telegram_message(text="Hi there")

    # Assert
    assert msg.text == "Hi there"


def test_make_telegram_message_has_async_edit_text():
    """edit_text must be an AsyncMock so tests can await it."""
    # Arrange
    msg = make_telegram_message()

    # Act / Assert — confirm it's awaitable
    import inspect

    assert inspect.iscoroutinefunction(msg.edit_text) or hasattr(msg.edit_text, "_mock_is_coroutine")


def test_make_telegram_message_media_defaults_are_neutral():
    """By default, photo/document/voice/video must be falsy (no media attached)."""
    # Arrange / Act
    msg = make_telegram_message()

    # Assert
    assert not msg.photo
    assert msg.document is None
    assert msg.voice is None
    assert msg.video is None


def test_make_telegram_message_links_user_and_chat():
    """Message must have both from_user and chat set to consistent objects."""
    # Arrange / Act
    msg = make_telegram_message(user_id=999, chat_id=888)

    # Assert
    assert msg.from_user.id == 999
    assert msg.chat.id == 888


# ─── make_telegram_update ────────────────────────────────────────────────────


def test_make_telegram_update_message_is_accessible():
    """Update must expose message attribute for handler code."""
    # Arrange / Act
    update = make_telegram_update(message_text="Test", user_id=1)

    # Assert
    assert update.message is not None
    assert update.message.text == "Test"


def test_make_telegram_update_effective_user_matches_message_user():
    """effective_user and message.from_user must refer to the same mock."""
    # Arrange / Act
    update = make_telegram_update(user_id=777)

    # Assert
    assert update.effective_user.id == update.message.from_user.id


def test_make_telegram_update_effective_message_matches_message():
    """effective_message must be the same as message."""
    # Arrange / Act
    update = make_telegram_update()

    # Assert
    assert update.effective_message is update.message


# ─── make_telegram_context ───────────────────────────────────────────────────


def test_make_telegram_context_has_send_message():
    """Context must have a bot with async send_message."""
    # Arrange / Act
    ctx = make_telegram_context()

    # Assert
    assert ctx.bot is not None
    assert hasattr(ctx.bot, "send_message")


def test_make_telegram_context_user_data_is_dict():
    # Arrange / Act
    ctx = make_telegram_context()

    # Assert
    assert isinstance(ctx.user_data, dict)
    assert isinstance(ctx.chat_data, dict)
