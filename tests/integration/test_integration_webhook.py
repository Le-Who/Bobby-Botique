"""
Integration tests for the Quart webhook endpoint.
Coverage for Quart webhook routing and Telegram Update processing.
"""

import json
from unittest.mock import AsyncMock, patch

import pytest
from telegram import Update

from app.config import settings
from app.web import quart_app


@pytest.fixture
def app_client():
    """Arrange: setup Quart test client."""
    quart_app.testing = True
    return quart_app.test_client()


@pytest.fixture
def mock_application():
    """Arrange: mock the Telegram Application object."""
    from datetime import timezone
    mock_app = AsyncMock()
    mock_app.bot = AsyncMock()
    # Explicitly set tzinfo to utc to avoid AsyncMock errors deep in PTB
    mock_app.bot.defaults.tzinfo = timezone.utc
    return mock_app


@pytest.mark.asyncio
async def test_webhook_receives_update_and_processes_it(app_client, mock_application):
    """
    Risk Covered: The webhook route fails to parse or pass the Telegram Update.
    Level: Integration.
    """
    # ── Arrange ──
    webhook_path = f"/webhook/{settings.TELEGRAM_BOT_TOKEN}"

    # We dynamically register the route just like bot.py does when WEBHOOK_URL is set
    @quart_app.route(webhook_path, methods=["POST"])
    async def webhook_handler():
        from quart import request

        json_data = await request.get_json()
        update_obj = Update.de_json(json_data, mock_application.bot)
        await mock_application.process_update(update_obj)
        return "", 200

    payload = {
        "update_id": 123456789,
        "message": {"message_id": 1, "date": 1614556800, "chat": {"id": 111, "type": "private"}, "text": "Hello bot"},
    }

    # ── Act ──
    response = await app_client.post(webhook_path, json=payload)

    # ── Assert ──
    assert response.status_code == 200, "Webhook must return HTTP 200 OK"

    # Verify the application received the processed update
    mock_application.process_update.assert_awaited_once()
    passed_update = mock_application.process_update.call_args[0][0]

    assert passed_update.update_id == 123456789, "Update ID must match payload"
    assert passed_update.message.text == "Hello bot", "Update text must match payload"
