"""Live minute capacity rotates configured project keys before connection."""

from unittest.mock import AsyncMock

import pytest


@pytest.mark.asyncio
async def test_live_admission_rotates_without_connecting_exhausted_key(monkeypatch):
    import hashlib

    from app.providers import gemini
    from app.providers.gemini_quota import GeminiMinuteQuota
    from app.repos import keys

    monkeypatch.setattr("app.cache.redis_client", None)
    monkeypatch.setattr(keys, "gemini_minute_quota", GeminiMinuteQuota())
    monkeypatch.setattr(keys, "get_model_minute_limit", AsyncMock(return_value=1))
    monkeypatch.setattr(gemini.settings, "GEMINI_API_KEYS", ["full", "free"])
    monkeypatch.setattr(gemini, "get_cached_genai_client", lambda key: ("client", key))
    await keys.reserve_gemini_minute_usage(hashlib.sha256(b"full").hexdigest(), "gemini-live-test")
    assert await gemini.get_admitted_live_api_client("gemini-live-test") == (("client", "free"), "free")
    assert await gemini.get_admitted_live_api_client("gemini-live-test") == (None, None)
