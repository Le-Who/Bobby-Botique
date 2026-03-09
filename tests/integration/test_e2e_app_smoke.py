"""Tests integration between Agent processor and the Application Stack."""

import contextlib
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import telegram

from app.database import db_manager
from app.handlers.agent import process_long_request

pytestmark = pytest.mark.integration


@pytest.fixture
def mock_db_manager(db_conn):
    """Patches db_manager to yield the test transactional connection (Integration boundary)."""

    @contextlib.asynccontextmanager
    async def mock_acquire():
        yield db_conn

    with (
        patch.object(db_manager, "pool", create=True) as mock_pool,
        patch.object(db_manager, "_is_pool_closed", return_value=False),
    ):
        mock_pool.acquire = mock_acquire
        mock_pool.close = AsyncMock()
        yield mock_pool


@pytest.fixture
def mock_external_network():
    """Mock external APIs (AI HTTP boundaries) to prevent real billing/network calls."""
    with (
        patch("app.handlers.ai_chat._resolve_ai_request", new_callable=AsyncMock) as mock_resolve,
        patch("app.handlers.ai_chat._get_ai_response_with_routing", new_callable=AsyncMock) as mock_get_answer,
        patch("app.handlers.ai_chat.send_long_message", new_callable=AsyncMock) as mock_send_long,
        patch("app.handlers.ai_chat.is_openrouter_model", return_value=True),
    ):
        # Default mock successful resolutions
        mock_resolve.return_value = ({"api_key": "mock_key", "key_hash": "h"}, "gemini-2.5-flash", "direct")
        mock_get_answer.return_value = ("Integration answer from bot", 42)

        yield {
            "resolve": mock_resolve,
            "get_answer": mock_get_answer,
            "send_long": mock_send_long,
        }


@pytest.mark.asyncio
async def test_integration_full_message_flow(db_conn, mock_db_manager, mock_external_network):
    """
    Risk Covered: Disconnects between Telegram Dispatcher, Agent logic, and Database.
    Level: Integration.
    Tests the process_long_request flow using real DB queries but mocked external AI networks.
    """
    # ── Arrange ──
    user_id = 9100100
    chat_id = 9100100
    user_text = "Привет, бот!"

    # Seed Database state
    await db_conn.execute("INSERT INTO users (user_id) VALUES ($1) ON CONFLICT DO NOTHING", user_id)

    # Build mocked Telegram Update wrapper
    mock_update = MagicMock(spec=telegram.Update)
    mock_message = MagicMock(spec=telegram.Message)
    mock_user = MagicMock(spec=telegram.User)
    mock_chat = MagicMock(spec=telegram.Chat)

    mock_chat.id = chat_id
    mock_chat.type = "private"
    mock_user.id = user_id
    mock_user.username = "test_int_user"

    mock_message.chat = mock_chat
    mock_message.from_user = mock_user
    mock_message.text = user_text

    # Provide explicitly empty message attributes to prevent router fallbacks
    mock_message.photo = ()
    mock_message.document = None
    mock_message.voice = None
    mock_message.video = None

    # Bot thinking message
    mock_thinking_msg = MagicMock()
    mock_thinking_msg.edit_text = AsyncMock()
    mock_thinking_msg.id = 123
    mock_message.reply_text = AsyncMock(return_value=mock_thinking_msg)

    mock_update.message = mock_message
    mock_update.effective_user = mock_user

    mock_context = MagicMock()
    mock_context.bot = MagicMock()
    mock_context.bot.send_message = AsyncMock(return_value=mock_thinking_msg)

    # ── Act ──
    await process_long_request(mock_thinking_msg, mock_update, mock_context)

    # ── Assert ──
    # DB Integrity Check: Verify active_chat_messages syncs
    rows = await db_conn.fetch(
        "SELECT role, content FROM active_chat_messages WHERE user_id = $1 ORDER BY id ASC", user_id
    )
    assert rows, "Integration: Messages should be stored in active_chat_messages"

    contents = [row["content"] for row in rows]

    # We expect both the user prompt and the mocked AI response to be persisted
    assert any("Привет, бот!" in c for c in contents), f"User msg not persisted: {contents}"
    assert any("Integration answer from bot" in c for c in contents), f"AI msg not persisted: {contents}"

    # Telegram Output Check
    mock_external_network["send_long"].assert_awaited_once()
