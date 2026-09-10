"""Tests for app.admin_alerts — rate limiter and alert formatting."""

import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.admin_alerts import (
    AlertSeverity,
    _alert_timestamps,
    _can_alert_unauthorized,
    _is_rate_limited,
    _record_alert,
    _unauthorized_last_alerted,
    alert_admin,
    alert_admin_unauthorized_user,
)


@pytest.fixture(autouse=True)
def _clean_alert_state():
    """Reset rate limiter state for each test."""
    _alert_timestamps.clear()
    _unauthorized_last_alerted.clear()
    yield
    _alert_timestamps.clear()
    _unauthorized_last_alerted.clear()


# ── Rate limiter ─────────────────────────────────────────────────────────────


class TestRateLimiter:
    """Alert rate limiter should cap alerts per window."""

    def test_not_rate_limited_initially(self):
        assert _is_rate_limited() is False

    def test_rate_limited_after_max_alerts(self):
        now = time.monotonic()
        for _ in range(5):  # _MAX_ALERTS = 5
            _alert_timestamps.append(now)
        assert _is_rate_limited() is True

    def test_old_entries_purged(self):
        old = time.monotonic() - 400  # _WINDOW_SECONDS = 300
        for _ in range(5):
            _alert_timestamps.append(old)
        assert _is_rate_limited() is False

    def test_record_alert_adds_timestamp(self):
        _record_alert()
        assert len(_alert_timestamps) == 1

    def test_first_unauthorized_alert_is_allowed_during_process_startup(self):
        with patch("app.admin_alerts.time.monotonic", return_value=1.0):
            assert _can_alert_unauthorized(998877) is True


# ── alert_admin ──────────────────────────────────────────────────────────────


class TestAlertAdmin:
    """alert_admin sends messages to admin with rate limiting."""

    @pytest.mark.asyncio
    async def test_sends_message(self):
        mock_app = MagicMock()
        mock_app.bot = AsyncMock()

        with patch("app.config.settings", MagicMock(ADMIN_ID=12345)):
            await alert_admin(mock_app, "Test alert", severity=AlertSeverity.WARNING)

        mock_app.bot.send_message.assert_called_once()
        call_kwargs = mock_app.bot.send_message.call_args[1]
        assert call_kwargs["chat_id"] == 12345

    @pytest.mark.asyncio
    async def test_rate_limited_does_not_send(self):
        # Fill up rate limiter
        now = time.monotonic()
        for _ in range(5):
            _alert_timestamps.append(now)

        mock_app = MagicMock()
        mock_app.bot = AsyncMock()

        with patch("app.config.settings", MagicMock(ADMIN_ID=12345)):
            await alert_admin(mock_app, "Dropped alert")

        mock_app.bot.send_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_no_admin_id_skips(self):
        mock_app = MagicMock()
        mock_app.bot = AsyncMock()

        with patch("app.config.settings", MagicMock(ADMIN_ID=0)):
            await alert_admin(mock_app, "Should be skipped")

        mock_app.bot.send_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_exception_in_send_does_not_raise(self):
        mock_app = MagicMock()
        mock_app.bot = AsyncMock()
        mock_app.bot.send_message.side_effect = Exception("Network error")

        with patch("app.config.settings", MagicMock(ADMIN_ID=12345)):
            # Should not raise
            await alert_admin(mock_app, "Alert with exception")

    @pytest.mark.asyncio
    async def test_exception_alert_uses_safe_incident_summary(self):
        token = "987654321:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghi_12"
        mock_app = MagicMock()
        mock_app.bot = AsyncMock()

        try:
            raise ValueError(f"provider echoed {token}")
        except ValueError as error:
            with patch("app.config.settings", MagicMock(ADMIN_ID=12345)):
                await alert_admin(
                    mock_app,
                    f"Provider request failed: {token}",
                    severity=AlertSeverity.CRITICAL,
                    exc=error,
                    error_id="e" * 32,
                )

        text = mock_app.bot.send_message.await_args.kwargs["text"]
        assert token not in text
        assert "ValueError" in text
        assert "e" * 32 in text
        assert "fingerprint" in text.casefold()

    @pytest.mark.asyncio
    async def test_unauthorized_alert_uses_message_fingerprint_not_message_content(self):
        mock_app = MagicMock()
        mock_app.bot = AsyncMock()
        message = "real unauthorized message must stay in operational logs only"

        with patch("app.config.settings", MagicMock(ADMIN_ID=12345)):
            await alert_admin_unauthorized_user(
                mock_app,
                user_id=998877,
                username="outsider",
                first_name="Out",
                language_code="ru",
                chat_type="private",
                message_text=message,
            )

        text = mock_app.bot.send_message.await_args.kwargs["text"]
        assert message not in text
        assert "fingerprint" in text.casefold()
        assert str(len(message)) in text


# ── AlertSeverity ────────────────────────────────────────────────────────────


class TestAlertSeverity:
    """AlertSeverity enum should have emoji values."""

    def test_info_emoji(self):
        assert AlertSeverity.INFO.value is not None

    def test_warning_emoji(self):
        assert AlertSeverity.WARNING.value is not None

    def test_critical_emoji(self):
        assert AlertSeverity.CRITICAL.value is not None
