from unittest.mock import AsyncMock

import pytest

from app.runtime_settings import cache_identity, store


@pytest.mark.asyncio
async def test_only_relevant_route_and_prompt_changes_invalidate_cached_outputs(monkeypatch):
    snapshot = store.SettingsSnapshot(1, {})
    monkeypatch.setattr(cache_identity, "operation_snapshot", AsyncMock(side_effect=lambda: snapshot))
    assert await cache_identity.cache_identity("crocodile.hints", "topic", "crocodile.hints.classic") == "topic"
    snapshot = store.SettingsSnapshot(2, {"model_limit:gemini-test": 100})
    assert await cache_identity.cache_identity("crocodile.hints", "topic", "crocodile.hints.classic") == "topic"
    snapshot = store.SettingsSnapshot(3, {"prompt:crocodile.hints.classic": "New instructions"})
    first = await cache_identity.cache_identity("crocodile.hints", "topic", "crocodile.hints.classic")
    assert first != "topic"
    snapshot = store.SettingsSnapshot(
        4, {"prompt:crocodile.hints.classic": "New instructions", "model_limit:gemini-test": 20}
    )
    assert await cache_identity.cache_identity("crocodile.hints", "topic", "crocodile.hints.classic") == first
    snapshot = store.SettingsSnapshot(5, {"prompt:crocodile.hints.classic": "Other instructions"})
    assert await cache_identity.cache_identity("crocodile.hints", "topic", "crocodile.hints.classic") != first
