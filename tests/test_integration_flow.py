"""
Integration test: full message request flow.

Tests the complete path from incoming Telegram update through
handle_request → process_long_request → AI response → database.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from telegram import InlineKeyboardMarkup, Message, Update

# ── Helpers ───────────────────────────────────────────────────────────────────


def make_update(user_id=123, chat_id=456, text="Hello AI", photo=False):
    """Creates a realistic Telegram Update mock."""
    update = MagicMock(spec=Update)
    update.update_id = 99
    update.effective_user = MagicMock()
    update.effective_user.id = user_id
    update.effective_chat = MagicMock()
    update.effective_chat.id = chat_id

    msg = MagicMock(spec=Message)
    msg.from_user = update.effective_user
    msg.text = text
    msg.document = None
    msg.voice = None
    msg.photo = [MagicMock()] if photo else []
    msg.caption = "What is this?" if photo else None
    msg.media_group_id = None
    msg.message_id = 1001

    placeholder_msg = MagicMock(spec=Message)
    placeholder_msg.message_id = 1002
    placeholder_msg.edit_text = AsyncMock()
    placeholder_msg.reply_text = AsyncMock()

    msg.reply_text = AsyncMock(return_value=placeholder_msg)

    update.message = msg
    update.effective_message = msg
    return update


def make_context():
    """Creates a minimal Telegram context mock."""
    ctx = MagicMock()
    ctx.user_data = {}
    ctx.bot = MagicMock()
    ctx.bot.send_message = AsyncMock()
    return ctx


@pytest.fixture
def run_background_sync():
    """Fixture to capture background tasks submitted by messages.py."""
    with patch("app.utils.background_tasks.submit_task", new_callable=MagicMock) as mock_submit:
        # Instead of scheduling it randomly, we just capture it in the mock
        mock_submit.side_effect = lambda coro, retry=0: None
        yield mock_submit


# ── Tests ─────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_unauthorized_user_rejected():
    """Unauthorized user's message is silently dropped."""
    update = make_update(user_id=999)
    context = make_context()

    with (
        patch("app.state.ensure_state_loaded", new_callable=AsyncMock),
        patch("app.handlers.messages.settings") as mock_settings,
        patch(
            "app.handlers.messages.is_authorized",
            new_callable=AsyncMock,
            return_value=False,
        ),
        patch(
            "app.handlers.messages.check_user_rate_limit",
            new_callable=AsyncMock,
            return_value=True,
        ),
        patch(
            "app.admin_alerts.alert_admin_unauthorized_user",
            new_callable=AsyncMock,
        ),
    ):
        mock_settings.TELEGRAM_MESSAGE_LIMIT = 4096
        from app.handlers.messages import handle_request

        await handle_request(update, context)

    # No reply should happen
    update.message.reply_text.assert_not_called()


@pytest.mark.asyncio
async def test_rate_limited_user_gets_warning():
    """Rate-limited user receives a warning message."""
    update = make_update(user_id=123)
    context = make_context()

    with (
        patch("app.state.ensure_state_loaded", new_callable=AsyncMock),
        patch(
            "app.handlers.messages.is_authorized",
            new_callable=AsyncMock,
            return_value=True,
        ),
        patch(
            "app.handlers.messages.check_user_rate_limit",
            new_callable=AsyncMock,
            return_value=False,
        ),
    ):
        from app.handlers.messages import handle_request

        await handle_request(update, context)

    update.message.reply_text.assert_awaited_once()
    assert "лимит" in update.message.reply_text.call_args[0][0].lower()


@pytest.mark.asyncio
async def test_happy_path_text_message(run_background_sync):
    """
    E2E Integration Flow: Message -> RateLimit -> background_task -> AI Handler -> DB Save.
    Validates that the entire orchestrator works without mocking process_long_request itself.
    """
    # ── Arrange ──
    update = make_update(user_id=123, text="Tell me a joke")
    context = make_context()

    # Preset chat state in the mock memory block
    fake_chat_state = SimpleNamespace(
        model="gemini-3.1-flash-lite",
        system_prompt=None,
        history=[],
        token_count=0,
        is_deep_dive=False,
        search_enabled=False,
        deep_dive_thread_id=None,
        context_summary=None,
        thinking_level=0,
        ltm_enabled=True,
        branch_id=None,
    )

    with (
        patch("app.state.ensure_state_loaded", new_callable=AsyncMock),
        patch(
            "app.handlers.messages.is_authorized",
            new_callable=AsyncMock,
            return_value=True,
        ),
        patch(
            "app.handlers.messages.check_user_rate_limit",
            new_callable=AsyncMock,
            return_value=True,
        ),
        patch("app.handlers.messages.state.get_user_lock", return_value=AsyncMock()),
        patch(
            "app.repos.chats.get_user_chat",
            new_callable=AsyncMock,
            return_value=fake_chat_state,
        ),
        patch(
            "app.handlers.agent.get_user_chat",
            new_callable=AsyncMock,
            return_value=fake_chat_state,
        ),
        patch("app.handlers.ai_chat.update_user_chat", new_callable=AsyncMock) as m_update_chat,
        # Patch the absolute bottom of the AI layer to avoid real network
        patch(
            "app.handlers.ai_chat._resolve_ai_request",
            new_callable=AsyncMock,
            return_value=({"api_key": "k"}, "gemini-3.1-flash-lite", "direct"),
        ),
        patch(
            "app.streaming.stream_and_display",
            new_callable=AsyncMock,
            return_value=("Mocked joke!", True, None, 0, False, False),
        ),
        patch("app.repos.memory.search_memories", new_callable=AsyncMock, return_value=[]),
        patch("app.repos.memory.store_memory", new_callable=AsyncMock),
        patch("app.metrics.role_conv_metrics.record_summarization", new_callable=AsyncMock),
        patch("app.metrics.metrics_collector.record_request", new_callable=AsyncMock),
        patch("app.state.set_last_sent_message", new_callable=MagicMock),
    ):
        mock_lock = MagicMock()
        mock_lock.__aenter__ = AsyncMock(return_value=None)
        mock_lock.__aexit__ = AsyncMock(return_value=None)
        mock_lock.locked = MagicMock(return_value=False)

        with patch("app.state.get_user_lock", return_value=mock_lock):
            from app.handlers.messages import handle_request

            # ── Act ──
            await handle_request(update, context)

            # Ensure the background task was submitted
            assert run_background_sync.call_count >= 1
            captured_coro = None
            for call in run_background_sync.call_args_list:
                coro = call[0][0]
                if "task_wrapper" in str(coro):
                    captured_coro = coro
                    break
            assert captured_coro is not None

            # Explicitly execute the task synchronously to completion
            await captured_coro

    # ── Assert ──
    # Placeholder was initially sent
    update.message.reply_text.assert_awaited_once()
    assert "Думаю" in update.message.reply_text.call_args[0][0]

    # The DB orchestrator must have been hit at the end to save the history
    m_update_chat.assert_awaited()
    saved_state = m_update_chat.call_args[0][1]

    assert len(saved_state.history) == 2, "Expected 1 user message + 1 mock response"
    assert saved_state.history[-1]["role"] == "model"
    assert "Mocked joke!" in str(saved_state.history[-1]["parts"])


@pytest.mark.asyncio
async def test_agent_error_shows_retry_keyboard(run_background_sync):
    """When the deeper handler throws an unhandled error, the keyboard should be updated."""
    # ── Arrange ──
    update = make_update(user_id=123, text="Trigger error")
    context = make_context()

    fake_chat_state = SimpleNamespace(
        model="gemini-3.1-flash-lite",
        system_prompt=None,
        history=[],
        token_count=0,
        is_deep_dive=False,
        search_enabled=False,
        deep_dive_thread_id=None,
        context_summary=None,
        thinking_level=0,
        ltm_enabled=True,
        branch_id=None,
    )

    with (
        patch("app.state.ensure_state_loaded", new_callable=AsyncMock),
        patch(
            "app.handlers.messages.is_authorized",
            new_callable=AsyncMock,
            return_value=True,
        ),
        patch(
            "app.handlers.messages.check_user_rate_limit",
            new_callable=AsyncMock,
            return_value=True,
        ),
        patch(
            "app.repos.chats.get_user_chat",
            new_callable=AsyncMock,
            return_value=fake_chat_state,
        ),
        patch(
            "app.handlers.agent.get_user_chat",
            new_callable=AsyncMock,
            return_value=fake_chat_state,
        ),
        patch("app.metrics.metrics_collector.record_request", new_callable=AsyncMock),
        patch("app.state.set_last_sent_message", new_callable=MagicMock),
        # Cause an unexpected network failure DEEP at the API router to organically bubble up
        patch(
            "app.handlers.ai_chat._resolve_ai_request",
            new_callable=AsyncMock,
            side_effect=Exception("Test crash!"),
        ),
    ):
        mock_lock = MagicMock()
        mock_lock.__aenter__ = AsyncMock(return_value=None)
        mock_lock.__aexit__ = AsyncMock(return_value=None)
        mock_lock.locked = MagicMock(return_value=False)

        with (
            patch("app.state.get_user_lock", return_value=mock_lock),
        ):
            from app.handlers.messages import handle_request

            # ── Act ──
            await handle_request(update, context)

        assert run_background_sync.call_count >= 1
        captured_coro = None
        for call in run_background_sync.call_args_list:
            coro = call[0][0]
            if "task_wrapper" in str(coro):
                captured_coro = coro
                break
        assert captured_coro is not None

        # Explicitly wait for the task to run and handle its own simulated crash
        await captured_coro

    # ── Assert ──
    # The error handler in agent.process_long_request should catch this and update placeholder
    placeholder_msg = update.message.reply_text.return_value
    placeholder_msg.edit_text.assert_awaited_once()

    error_text = placeholder_msg.edit_text.call_args[0][0]
    error_kwargs = placeholder_msg.edit_text.call_args[1]

    assert "ошибка" in error_text.lower()
    assert "reply_markup" in error_kwargs
    assert isinstance(error_kwargs["reply_markup"], InlineKeyboardMarkup)
