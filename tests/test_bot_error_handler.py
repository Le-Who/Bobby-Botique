from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

import bot


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
    )
    alert.assert_awaited_once()
    call = alert.await_args
    assert call.args[0] is application
    assert call.kwargs["exc"] is error
    assert call.kwargs["error_id"] == "error-123"
    assert credential not in call.args[1]
    assert "RuntimeError" in call.args[1]
