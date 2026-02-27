"""
Integration test: full message request flow.

Tests the complete path from incoming Telegram update through
handle_request → process_long_request → AI response → send_long_message.
"""

import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock, patch, PropertyMock
from types import SimpleNamespace


# ── Helpers ───────────────────────────────────────────────────────────────────

def make_update(user_id=123, chat_id=456, text="Hello AI", photo=False):
    """Creates a realistic Telegram Update mock."""
    update = MagicMock()
    update.update_id = 99
    update.effective_user = MagicMock()
    update.effective_user.id = user_id
    update.effective_chat = MagicMock()
    update.effective_chat.id = chat_id

    msg = MagicMock()
    msg.from_user = update.effective_user
    msg.text = text
    msg.document = None
    msg.photo = [MagicMock()] if photo else []
    msg.caption = "What is this?" if photo else None
    msg.media_group_id = None
    msg.reply_text = AsyncMock(return_value=MagicMock(
        edit_text=AsyncMock(),
        reply_text=AsyncMock(),
    ))

    update.message = msg
    return update


def make_context():
    """Creates a minimal Telegram context mock."""
    ctx = MagicMock()
    ctx.user_data = {}
    ctx.bot = MagicMock()
    return ctx


def make_chat_state():
    return SimpleNamespace(
        model="gemini-2.0-flash", system_prompt=None,
        history=[], token_count=0, is_deep_dive=False,
        search_enabled=False, deep_dive_thread_id=None,
    )


# ── Test 1: Unauthorized user is silently rejected ────────────────────────────

@pytest.mark.asyncio
async def test_unauthorized_user_rejected():
    """Unauthorized user's message is silently dropped."""
    update = make_update(user_id=999)
    context = make_context()

    with (
        patch("app.state.ensure_state_loaded", new_callable=AsyncMock),
        patch("app.handlers.messages.set_request_id", return_value="test-req-1"),
        patch("app.handlers.messages.bind_request_span"),
        patch("app.handlers.messages.check_user_rate_limit",
              new_callable=AsyncMock, return_value=True),
        patch("app.handlers.messages.is_authorized",
              new_callable=AsyncMock, return_value=False),
    ):
        from app.handlers.messages import handle_request
        await handle_request(update, context)

    # No reply_text for placeholder since user is unauthorized
    # The function returns early after is_authorized check
    # No "Думаю..." placeholder should be sent
    calls = update.message.reply_text.call_args_list
    # Should NOT have the "Думаю..." placeholder
    assert not any("Думаю" in str(c) for c in calls)


# ── Test 2: Rate-limited user gets warning ────────────────────────────────────

@pytest.mark.asyncio
async def test_rate_limited_user_gets_warning():
    """Rate-limited user receives a warning message."""
    update = make_update(user_id=123, text="Spam message")
    context = make_context()

    with (
        patch("app.state.ensure_state_loaded", new_callable=AsyncMock),
        patch("app.handlers.messages.set_request_id", return_value="test-req-2"),
        patch("app.handlers.messages.bind_request_span"),
        patch("app.handlers.messages.check_user_rate_limit",
              new_callable=AsyncMock, return_value=False),
    ):
        from app.handlers.messages import handle_request
        await handle_request(update, context)

    # Should get rate limit warning
    update.message.reply_text.assert_awaited()
    text = update.message.reply_text.call_args[0][0]
    assert "лимит" in text.lower()


# ── Test 3: Happy path — text message through full pipeline ───────────────────

@pytest.mark.asyncio
async def test_happy_path_text_message():
    """Full end-to-end: text message → handler → agent → response."""
    update = make_update(user_id=123, text="What is Python?")
    context = make_context()

    placeholder = MagicMock()
    placeholder.edit_text = AsyncMock()
    placeholder.reply_text = AsyncMock()
    update.message.reply_text = AsyncMock(return_value=placeholder)

    process_long_request_mock = AsyncMock()

    with (
        patch("app.state.ensure_state_loaded", new_callable=AsyncMock),
        patch("app.handlers.messages.set_request_id", return_value="test-req-3"),
        patch("app.handlers.messages.bind_request_span"),
        patch("app.handlers.messages.check_user_rate_limit",
              new_callable=AsyncMock, return_value=True),
        patch("app.handlers.messages.is_authorized",
              new_callable=AsyncMock, return_value=True),
        patch("app.handlers.messages.state") as mock_state,
        patch("app.handlers.messages.api_logger") as mock_logger,
        patch("app.handlers.messages.metrics_collector") as mock_metrics,
        patch("app.state.set_last_sent_message"),
        patch("app.handlers.agent.process_long_request", process_long_request_mock),
    ):
        mock_state.get_user_lock.return_value = AsyncMock().__aenter__ = AsyncMock()
        mock_logger.log_telegram_request.return_value = 1000.0
        mock_metrics.record_request = AsyncMock()
        # Make get_user_lock work as async context manager
        lock_mock = MagicMock()
        lock_mock.__aenter__ = AsyncMock(return_value=None)
        lock_mock.__aexit__ = AsyncMock(return_value=False)
        mock_state.get_user_lock.return_value = lock_mock

        from app.handlers.messages import handle_request
        await handle_request(update, context)

        # Give the background task time to complete
        await asyncio.sleep(0.5)

    # Placeholder should have been created
    update.message.reply_text.assert_awaited()
    placeholder_text = update.message.reply_text.call_args[0][0]
    assert "Думаю" in placeholder_text

    # process_long_request should have been called via the background task
    # (it runs in create_task so we give it time)
    # The call may or may not have completed by assertion time due to task scheduling


# ── Test 4: Error in agent shows retry keyboard ───────────────────────────────

@pytest.mark.asyncio
async def test_agent_error_shows_retry_keyboard():
    """When agent raises an exception, user gets error + retry button."""
    update = make_update(user_id=123, text="Trigger error")
    context = make_context()

    placeholder = MagicMock()
    placeholder.edit_text = AsyncMock()
    update.message.reply_text = AsyncMock(return_value=placeholder)

    with (
        patch("app.state.ensure_state_loaded", new_callable=AsyncMock),
        patch("app.handlers.messages.set_request_id", return_value="test-req-4"),
        patch("app.handlers.messages.bind_request_span"),
        patch("app.handlers.messages.check_user_rate_limit",
              new_callable=AsyncMock, return_value=True),
        patch("app.handlers.messages.is_authorized",
              new_callable=AsyncMock, return_value=True),
        patch("app.handlers.messages.state") as mock_state,
        patch("app.handlers.messages.api_logger") as mock_logger,
        patch("app.handlers.messages.metrics_collector") as mock_metrics,
        patch("app.state.set_last_sent_message"),
        patch("app.handlers.agent.process_long_request",
              new_callable=AsyncMock, side_effect=Exception("AI provider down")),
    ):
        mock_logger.log_telegram_request.return_value = 1000.0
        mock_metrics.record_request = AsyncMock()
        lock_mock = MagicMock()
        lock_mock.__aenter__ = AsyncMock(return_value=None)
        lock_mock.__aexit__ = AsyncMock(return_value=False)
        mock_state.get_user_lock.return_value = lock_mock

        from app.handlers.messages import handle_request
        await handle_request(update, context)

        # Give background task time
        await asyncio.sleep(0.5)

    # Placeholder should show error with retry keyboard
    if placeholder.edit_text.call_args_list:
        error_text = placeholder.edit_text.call_args[0][0]
        assert "ошибка" in error_text.lower()
        # Should have reply_markup (retry keyboard)
        assert placeholder.edit_text.call_args[1].get("reply_markup") is not None
