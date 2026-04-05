"""
E2E testing for the Telegram Webhook lifecycle and Quart web server integrations.

Design decisions
────────────────
- webhook_client uses a *fresh* Quart() instance (not the production quart_app
  singleton). This eliminates route-accumulation across test runs: each call
  to the fixture previously added a new POST handler for the same path to the
  global url_map, which is only masked by xdist worker isolation. With a
  dedicated test app the route is registered exactly once per module.

- test_health_endpoint_success uses the production quart_app because it must
  exercise the real /health route registered at import time.
"""

import json
from unittest.mock import AsyncMock, patch

import pytest
from quart import Quart
from telegram import Bot, Update
from telegram.ext import Application

from app.config import settings
from app.web import quart_app

# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture(scope="module")
def _webhook_test_app():
    """
    One-time setup: a minimal Quart app with the webhook route registered once.

    scope="module" ensures the route is registered exactly once per test module
    regardless of how many test functions request webhook_client.

    Returns (test_app, mock_telegram_application) — the mock is shared across
    module-scoped context; tests reset its call history via mock.reset_mock().
    """
    webhook_path = f"/webhook/{settings.TELEGRAM_BOT_TOKEN}"

    # A real Bot is required because Update.de_json() calls bot.defaults.tzinfo
    # directly — an AsyncMock here raises "tzinfo argument must be None or of a
    # tzinfo subclass, not type 'AsyncMock'".
    bot_instance = Bot("123456789:ABCDefghIJKlmnOPQRstuVWXyz")
    mock_application = AsyncMock(spec=Application)
    mock_application.bot = bot_instance

    # Fresh, isolated Quart app — never pollutes production quart_app.url_map
    test_app = Quart(__name__)

    @test_app.route(webhook_path, methods=["POST"])
    async def webhook_handler():
        from quart import request  # noqa: PLC0415

        json_data = await request.get_json()
        # If the JSON is unparseable Quart returns None for get_json(silent=True).
        # Returning 400 explicitly mirrors the production error path.
        if json_data is None:
            return "Bad Request: invalid JSON", 400
        try:
            update_obj = Update.de_json(json_data, mock_application.bot)
            await mock_application.process_update(update_obj)
            return "", 200
        except Exception as exc:  # noqa: BLE001
            return str(exc), 400

    return test_app, mock_application


@pytest.fixture
def webhook_client(_webhook_test_app):
    """
    Per-test fixture: yields (QuartClient, mock_application).

    Resets mock call history before each test so assertions are independent.
    """
    test_app, mock_app = _webhook_test_app
    mock_app.reset_mock()
    yield test_app.test_client(), mock_app


# ── Webhook lifecycle tests ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_webhook_valid_payload(webhook_client):
    """
    Arrange: valid minimal Telegram Update JSON.
    Act:     POST to /webhook/<token>.
    Assert:  HTTP 200 returned; Application.process_update called with
             a correctly-deserialised Update object.
    """
    client, mock_app = webhook_client
    webhook_path = f"/webhook/{settings.TELEGRAM_BOT_TOKEN}"

    payload = {
        "update_id": 987654321,
        "message": {
            "message_id": 1111,
            "date": 1690000000,
            "chat": {"id": 123456789, "type": "private"},
            "text": "/start",
        },
    }

    response = await client.post(
        webhook_path,
        headers={"Content-Type": "application/json"},
        data=json.dumps(payload),
    )

    response_text = await response.get_data(as_text=True)
    assert response.status_code == 200, f"Expected 200 OK, got {response.status_code}: {response_text}"

    mock_app.process_update.assert_awaited_once()
    passed_update = mock_app.process_update.await_args[0][0]
    assert isinstance(passed_update, Update)
    assert passed_update.update_id == 987654321
    assert passed_update.message is not None
    assert passed_update.message.text == "/start"


@pytest.mark.asyncio
async def test_webhook_unregistered_path_returns_404(webhook_client):
    """
    Arrange: a path that was never registered as a webhook route.
    Act:     POST with a fake/wrong bot token.
    Assert:  HTTP 404 — Quart finds no matching route.
             process_update is not called.

    Note: this tests *routing* not authentication. The bot only registers one
    path containing its real token; any other path returns 404 by construction.
    """
    client, mock_app = webhook_client

    response = await client.post(
        "/webhook/invalid:fake_token_for_test",
        headers={"Content-Type": "application/json"},
        data=json.dumps({"update_id": 12345}),
    )

    assert response.status_code == 404
    mock_app.process_update.assert_not_called()


@pytest.mark.asyncio
async def test_webhook_malformed_json_returns_400(webhook_client):
    """
    Arrange: syntactically invalid JSON body.
    Act:     POST to the registered webhook path.
    Assert:  HTTP 400 — handler detects None from get_json() and rejects.
             process_update is not called (no Update constructed).
    """
    client, mock_app = webhook_client
    webhook_path = f"/webhook/{settings.TELEGRAM_BOT_TOKEN}"

    response = await client.post(
        webhook_path,
        headers={"Content-Type": "application/json"},
        data="NOT VALID JSON {",
    )

    assert response.status_code == 400
    mock_app.process_update.assert_not_called()


# ── Health endpoint smoke test ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_health_endpoint_success():
    """
    Arrange: DB and Redis both report healthy (patched).
    Act:     GET /health on the production Quart app.
    Assert:  HTTP 200 with expected JSON structure.
    """
    client = quart_app.test_client()
    with (
        patch("app.database.is_database_connected", return_value=True),
        patch("app.cache.ping_safe", new_callable=AsyncMock, return_value=True),
    ):
        response = await client.get("/health")
        assert response.status_code == 200

        json_data = await response.get_json()
        assert json_data["status"] == "healthy"
        assert json_data["services"]["database"] == "connected"
        assert json_data["services"]["bot"] == "running"
