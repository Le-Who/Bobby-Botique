"""Contracts for clear user errors that do not expose server internals."""

from unittest.mock import AsyncMock, patch

import pytest

from app.errors import QUOTA_ERROR, ErrorCode, strip_error_tag, user_message_for_error_code
from app.handlers.cmd_image import _error_text
from app.i18n import t
from app.search_services import tavily_search_agent


@pytest.mark.parametrize("key", ["error.no_api_keys", "role.no_api_keys"])
@pytest.mark.parametrize("lang", ["ru", "en"])
def test_localized_service_errors_hide_credential_details(key: str, lang: str) -> None:
    message = t(key, lang)

    assert "API" not in message
    assert "ключ" not in message.lower()
    assert "key" not in message.lower()


def test_image_authorization_error_hides_provider_configuration() -> None:
    message = _error_text("auth_error")

    assert "POLLINATIONS_API_KEY" not in message
    assert "API" not in message
    assert "попробуйте позже" in message.lower()


def test_unknown_image_error_does_not_echo_upstream_details() -> None:
    upstream_detail = "upstream secret: sk-private-value"

    message = _error_text(upstream_detail)

    assert upstream_detail not in message
    assert "sk-private-value" not in message


@pytest.mark.asyncio
async def test_search_capacity_error_hides_key_pool_details() -> None:
    with (
        patch("app.search_services.get_cached_search_result", new_callable=AsyncMock, return_value=None),
        patch("app.search_services.get_available_tavily_key", new_callable=AsyncMock, return_value=None),
    ):
        result = await tavily_search_agent("актуальный вопрос")

    message = result["error"]
    assert "API" not in message
    assert "ключ" not in message.lower()
    assert "месячн" in message.lower()


@pytest.mark.parametrize(
    "message",
    [
        QUOTA_ERROR,
        user_message_for_error_code(ErrorCode.INVALID_RESPONSE),
    ],
)
def test_standard_service_errors_avoid_transport_jargon(message: str) -> None:
    visible = strip_error_tag(message)

    assert "API" not in visible
    assert "HTTP" not in visible
