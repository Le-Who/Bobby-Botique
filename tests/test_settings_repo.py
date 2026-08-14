from unittest.mock import AsyncMock

import pytest


@pytest.mark.asyncio
async def test_delete_global_setting_deletes_row_and_invalidates_cache(monkeypatch):
    from app.repos import settings_repo

    ensure_mock = AsyncMock()
    query_mock = AsyncMock()
    monkeypatch.setattr(settings_repo, "_ensure_table", ensure_mock)
    monkeypatch.setattr(settings_repo.db, "db_query", query_mock)
    settings_repo._cache["provider_models:gemini"] = "stale"

    await settings_repo.delete_global_setting("provider_models:gemini")

    ensure_mock.assert_awaited_once()
    query_mock.assert_awaited_once_with(
        "DELETE FROM global_settings WHERE key_name = $1",
        ("provider_models:gemini",),
    )
    assert "provider_models:gemini" not in settings_repo._cache
