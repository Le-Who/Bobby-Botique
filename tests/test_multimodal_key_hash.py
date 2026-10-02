from __future__ import annotations

import hashlib
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from app.utils import multimodal_processor


@pytest.mark.asyncio
async def test_explicit_media_key_uses_full_database_hash_for_health_updates() -> None:
    status = SimpleNamespace(record_success=AsyncMock(), suspend_key=AsyncMock())
    with (
        patch("app.repos.keys.get_key_status_manager", return_value=status),
        patch("app.utils.multimodal_processor.get_cached_genai_client", return_value=object()),
        patch("app.utils.multimodal_processor.run_with_resilience", new=AsyncMock(return_value=("done", 1))),
    ):
        result = await multimodal_processor._generate_with_resilience(
            parts=[],
            model="gemini-3.6-flash",
            system_prompt="Describe",
            thinking_config=None,
            api_key="explicit-key",
        )

    assert result == "done"
    status.record_success.assert_awaited_once_with(hashlib.sha256(b"explicit-key").hexdigest(), "gemini-3.6-flash")


@pytest.mark.asyncio
async def test_media_does_not_call_sdk_without_quota_admission(monkeypatch):
    status = SimpleNamespace(record_success=AsyncMock(), suspend_key=AsyncMock())
    generate = AsyncMock(return_value=SimpleNamespace(text="result"))
    client = SimpleNamespace(aio=SimpleNamespace(models=SimpleNamespace(generate_content=generate)))
    monkeypatch.setattr("app.repos.keys.get_key_status_manager", lambda: status)
    monkeypatch.setattr("app.repos.keys.reserve_gemini_key_usage", AsyncMock(return_value=False))
    monkeypatch.setattr(multimodal_processor, "get_cached_genai_client", lambda _: client)
    monkeypatch.setattr(multimodal_processor, "_get_api_key_for_media", AsyncMock(return_value=(None, None)))
    assert (
        await multimodal_processor._generate_with_resilience(
            parts=[], model="gemini-3.6-flash", system_prompt="Describe", thinking_config=None, api_key="test-key"
        )
        is None
    )
    generate.assert_not_awaited()
    status.suspend_key.assert_not_awaited()


@pytest.mark.asyncio
async def test_each_sdk_retry_reserves_a_new_slot(monkeypatch):
    from app.resilience_policy import ResiliencePolicy

    status = SimpleNamespace(record_success=AsyncMock(), suspend_key=AsyncMock())
    generate = AsyncMock(side_effect=[ConnectionError("503"), SimpleNamespace(text="result")])
    client = SimpleNamespace(aio=SimpleNamespace(models=SimpleNamespace(generate_content=generate)))
    reservation = AsyncMock(return_value=True)
    monkeypatch.setattr("app.repos.keys.get_key_status_manager", lambda: status)
    monkeypatch.setattr("app.repos.keys.reserve_gemini_key_usage", reservation)
    monkeypatch.setattr(multimodal_processor, "get_cached_genai_client", lambda _: client)
    monkeypatch.setattr(
        multimodal_processor, "_MEDIA_RESILIENCE", ResiliencePolicy(max_retries=2, base_delay_s=0, jitter_s=0)
    )
    assert (
        await multimodal_processor._generate_with_resilience(
            parts=[],
            model="gemini-quota-retry-test",
            system_prompt="Describe",
            thinking_config=None,
            api_key="test-key",
        )
        == "result"
    )
    assert reservation.await_count == generate.await_count == 2


@pytest.mark.asyncio
async def test_direct_media_key_selection_does_not_expand_model_fallback(monkeypatch):
    select = AsyncMock(return_value=None)
    legacy = AsyncMock(return_value=({"api_key": "wrong-model-key", "key_hash": "hash"}, "gemini-other", None))
    monkeypatch.setattr("app.repos.keys.get_available_gemini_key", select)
    monkeypatch.setattr("app.handlers.ai_core._resolve_ai_request", legacy)
    assert await multimodal_processor._get_api_key_for_media("gemini-chosen", {"excluded"}) == (None, None)
    legacy.assert_not_awaited()
    select.assert_awaited_once_with("gemini-chosen", excluded_hashes={"excluded"})
