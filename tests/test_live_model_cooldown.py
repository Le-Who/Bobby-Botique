"""Redis response compatibility for the cluster-wide Live cooldown."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("stored_reason", "expected_reason"),
    [
        (b"quota exceeded", "quota exceeded"),
        ("quota exceeded", "quota exceeded"),
        ("Лимит модели".encode(), "Лимит модели"),
        (None, ""),
        (b"", ""),
        ("", ""),
    ],
)
async def test_live_cooldown_reason_accepts_binary_and_decoded_redis_responses(
    monkeypatch, stored_reason, expected_reason
):
    from app.web_miniapp import _get_live_model_cooldown_reason

    get = AsyncMock(return_value=stored_reason)
    monkeypatch.setattr("app.cache.redis_client", SimpleNamespace(get=get))

    assert await _get_live_model_cooldown_reason() == expected_reason
    get.assert_awaited_once_with("live_model_cooldown")


@pytest.mark.asyncio
async def test_live_cooldown_reason_is_empty_without_redis(monkeypatch):
    from app.web_miniapp import _get_live_model_cooldown_reason

    monkeypatch.setattr("app.cache.redis_client", None)

    assert await _get_live_model_cooldown_reason() == ""
