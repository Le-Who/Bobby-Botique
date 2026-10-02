"""Admission counts attempts, including failures, instead of counting results."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.providers import imagen_provider as images


@pytest.mark.asyncio
async def test_local_key_reservation_admits_only_remaining_slots(monkeypatch):
    monkeypatch.setattr("app.cache.redis_client", None)
    monkeypatch.setattr(images, "_KEY_DAY_BUCKET", {})
    monkeypatch.setattr(images.settings, "IMAGE_GEN_RPD_PER_KEY", 2)
    results = await asyncio.gather(*(images._reserve_key_usage("test-key") for _ in range(20)))
    assert sum(results) == 2
    assert await images._get_key_usage("test-key") == 2


@pytest.mark.asyncio
async def test_failed_sdk_attempt_consumes_reserved_slot(monkeypatch):
    monkeypatch.setattr("app.cache.redis_client", None)
    monkeypatch.setattr(images, "_KEY_DAY_BUCKET", {})
    monkeypatch.setattr(images.settings, "IMAGE_GEN_RPD_PER_KEY", 1)
    monkeypatch.setattr(images.settings, "GEMINI_API_KEYS", ["test-key"])
    monkeypatch.setattr(images.settings, "IMAGE_GEN_MAX_RETRIES", 1)
    client = SimpleNamespace(
        aio=SimpleNamespace(interactions=SimpleNamespace(create=AsyncMock(side_effect=TimeoutError())))
    )
    monkeypatch.setattr(images, "get_cached_genai_client", lambda _: client)
    provider = images.ImagenProvider()
    assert (await provider.generate("cat")).error_message == "timeout"
    assert (await provider.generate("cat")).error_message == "quota_exhausted"
    client.aio.interactions.create.assert_awaited_once()


@pytest.mark.asyncio
async def test_redis_reservation_and_expiry_use_one_atomic_script(monkeypatch):
    redis = SimpleNamespace(eval=AsyncMock(return_value=1))
    monkeypatch.setattr("app.cache.redis_client", redis)
    assert await images._reserve_key_usage("test-key")
    args = redis.eval.await_args.args
    assert args[0] == images._RESERVE_QUOTA
    assert args[1] == 1
    assert args[-1] == images._next_midnight_ts()
    assert "test-key" not in args[2]


@pytest.mark.asyncio
@pytest.mark.parametrize("user_id", [None, 123])
async def test_unavailable_quota_store_is_not_reported_as_exhausted(monkeypatch, user_id):
    monkeypatch.setattr("app.cache.redis_client", SimpleNamespace(eval=AsyncMock(side_effect=ConnectionError())))
    monkeypatch.setattr(images.settings, "GEMINI_API_KEYS", ["test-key"])
    monkeypatch.setattr(images.settings, "IMAGE_GEN_DAILY_LIMIT", 3)
    client = AsyncMock()
    monkeypatch.setattr(images, "get_cached_genai_client", client)
    result = await images.ImagenProvider().generate("cat", user_id=user_id)
    assert result.error_message == "quota_unavailable"
    client.assert_not_called()
