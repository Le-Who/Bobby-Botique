from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from telegram import Message, Update

import bot
from app.admin_alerts import AlertSeverity
from app.errors import (
    APIQuotaExceededError,
    AuthenticationError,
    ConfigurationError,
    DocumentProcessingError,
    ErrorCode,
    InputValidationError,
    UserLimitExceededError,
    strip_error_tag,
    user_friendly_error,
)


@pytest.mark.asyncio
async def test_global_error_handler_correlates_admin_alert_without_raw_error_message():
    credential = "unclassified-provider-credential-ABCDEFGH"
    error = RuntimeError(f"upstream rejected {credential}")
    application = object()
    context = SimpleNamespace(error=error, application=application)

    with (
        patch("app.observability.events.record_exception", return_value="error-123") as record,
        patch("app.admin_alerts.alert_admin", new_callable=AsyncMock) as alert,
    ):
        await bot.global_error_handler(object(), context)

    record.assert_called_once_with(
        "telegram.unhandled_update_failed",
        error,
        operation="telegram.update",
        level="error",
        fields={"error_code": "GENERIC"},
    )
    alert.assert_awaited_once()
    call = alert.await_args
    assert call.args[0] is application
    assert call.kwargs["exc"] is error
    assert call.kwargs["error_id"] == "error-123"
    assert credential not in call.args[1]
    assert "RuntimeError" in call.args[1]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error,code,severity",
    [
        (httpx.ReadTimeout(""), ErrorCode.TIMEOUT, None),
        (InputValidationError("synthetic private input"), ErrorCode.INVALID_REQUEST, None),
        (UserLimitExceededError("synthetic user throttle"), ErrorCode.USER_RATE_LIMIT, None),
        (DocumentProcessingError("synthetic document failure"), ErrorCode.DOCUMENT, None),
        (ConfigurationError("synthetic provider unavailable"), ErrorCode.NO_KEYS, AlertSeverity.WARNING),
        (AuthenticationError("synthetic credential rejected"), ErrorCode.INVALID_KEY, AlertSeverity.WARNING),
        (APIQuotaExceededError("synthetic provider quota"), ErrorCode.QUOTA_EXCEEDED, AlertSeverity.WARNING),
        (RuntimeError("synthetic private unexpected failure"), ErrorCode.GENERIC, AlertSeverity.CRITICAL),
    ],
)
async def test_global_error_handler_classifies_user_reply_and_actionable_admin_alert(error, code, severity):
    message = AsyncMock(spec=Message)
    update = Update(update_id=17, message=message)
    application = object()
    context = SimpleNamespace(error=error, application=application)
    with (
        patch("app.observability.events.record_exception", return_value="incident-17") as record,
        patch("app.admin_alerts.alert_admin", new_callable=AsyncMock) as alert,
    ):
        await bot.global_error_handler(update, context)

    assert record.call_args.kwargs["fields"]["error_code"] == code.value
    assert record.call_args.kwargs["level"] == ("error" if severity is AlertSeverity.CRITICAL else "warning")
    message.reply_text.assert_awaited_once_with(strip_error_tag(user_friendly_error(error)))
    if severity is None:
        alert.assert_not_awaited()
    else:
        alert.assert_awaited_once()
        assert alert.await_args.kwargs["severity"] is severity
        assert alert.await_args.kwargs["error_id"] == "incident-17"
        assert str(error) not in alert.await_args.args[1]
