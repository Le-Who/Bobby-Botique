import os
import time
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import telegram

from app.database import db_manager

pytestmark = pytest.mark.integration


@pytest.fixture
def mock_db_manager(db_conn):
    """Patches db_manager to yield the test transactional connection"""

    @asynccontextmanager
    async def mock_acquire():
        yield db_conn

    with (
        patch.object(db_manager, "pool", create=True) as mock_pool,
        patch.object(db_manager, "_is_pool_closed", return_value=False),
    ):
        mock_pool.acquire = mock_acquire
        # Some things might try to async close
        mock_pool.close = AsyncMock()
        yield mock_pool


@pytest.mark.asyncio
async def test_e2e_smoke_telegram_message_flow(db_conn, mock_db_manager):
    """
    E2E Smoke: Tests the entire message processing flow from the Telegram dispatch
    down to the database and external API.
    Does not use the network, but uses the real integrated Quart + Bot Handler + Database stack.
    """
    from app.handlers.agent import process_long_request

    # 1. Arrange
    user_id = 9100100
    chat_id = 9100100
    user_text = "Привет, бот!"

    # Initialize User
    await db_conn.execute("INSERT INTO users (user_id) VALUES ($1) ON CONFLICT DO NOTHING", user_id)

    # Construct a fully mocked Telegram Update
    mock_update = MagicMock(spec=telegram.Update)
    mock_message = MagicMock(spec=telegram.Message)
    mock_user = MagicMock(spec=telegram.User)
    mock_chat = MagicMock(spec=telegram.Chat)

    mock_chat.id = chat_id
    mock_chat.type = "private"
    mock_user.id = user_id
    mock_user.username = "test_e2e_user"

    mock_message.chat = mock_chat
    mock_message.from_user = mock_user
    mock_message.text = user_text

    # Crucial explicitly empty attributes so it doesn't route to photo/document handlers
    mock_message.photo = ()
    mock_message.document = None
    mock_message.voice = None
    mock_message.video = None

    # The message that says "thinking..." which is edited later
    mock_thinking_msg = MagicMock()
    mock_thinking_msg.edit_text = AsyncMock()
    mock_thinking_msg.id = 123
    mock_message.reply_text = AsyncMock(return_value=mock_thinking_msg)

    mock_update.message = mock_message
    mock_update.effective_user = mock_user

    # Context
    mock_context = MagicMock()
    mock_context.bot = MagicMock()
    mock_context.bot.send_message = AsyncMock(return_value=mock_thinking_msg)

    # We must patch the network calls to AI Provider
    with (
        patch("app.handlers.ai_chat._resolve_ai_request", new_callable=AsyncMock) as mock_resolve,
        patch("app.handlers.ai_chat._get_ai_response_with_routing", new_callable=AsyncMock) as mock_get_answer,
        patch("app.handlers.ai_chat.send_long_message", new_callable=AsyncMock) as mock_send_long,
        patch("app.handlers.ai_chat.is_openrouter_model", return_value=True),
    ):
        # Simulate successful key resolution
        mock_resolve.return_value = ({"api_key": "k", "key_hash": "h"}, "gemini-2.0-flash", "direct")

        # Simulate successful streaming response
        mock_get_answer.return_value = ("E2E answer from bot", 42)

        # 2. Act: Send the message through the actual processor
        await process_long_request(mock_thinking_msg, mock_update, mock_context)

    # 3. Assert
    # Check that messages were inserted into active_chat_messages
    rows = await db_conn.fetch(
        "SELECT role, content FROM active_chat_messages WHERE user_id = $1 ORDER BY id ASC", user_id
    )
    assert rows, "E2E: Messages should be stored in active_chat_messages"

    contents = [row["content"] for row in rows]

    user_msg_found = any("Привет, бот!" in c for c in contents)
    ai_msg_found = any("E2E answer from bot" in c for c in contents)

    assert user_msg_found, f"E2E: User msg should be persisted. Found: {contents}"
    assert ai_msg_found, f"E2E: AI response should be persisted. Found: {contents}"

    mock_send_long.assert_awaited_once()
