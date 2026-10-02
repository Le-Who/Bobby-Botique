from types import MappingProxyType
from unittest.mock import AsyncMock

import pytest

from app.runtime_settings import processes, store


@pytest.mark.asyncio
async def test_single_model_control_preserves_reserves_and_uses_revision(monkeypatch):
    snapshot = store.SettingsSnapshot(
        8,
        MappingProxyType(
            {
                "process:inline": MappingProxyType(
                    {
                        "models": ("gemini-old", "gemini-backup"),
                        "strategy": "sequential",
                        "inherit_user_model": True,
                    }
                )
            }
        ),
    )
    monkeypatch.setattr(store, "get_snapshot", AsyncMock(return_value=snapshot))
    writer = AsyncMock(return_value=snapshot)
    monkeypatch.setattr(store, "set_value", writer)
    await processes.save_primary_model("inline", "gemini-new", actor="admin")
    writer.assert_awaited_once_with(
        "process:inline",
        {
            "models": ["gemini-new", "gemini-backup"],
            "strategy": "sequential",
            "inherit_user_model": False,
        },
        expected_revision=8,
        actor="admin",
    )


@pytest.mark.asyncio
async def test_unavailable_state_never_reports_legacy_write_as_success(monkeypatch):
    monkeypatch.setattr(store, "get_snapshot", AsyncMock(return_value=store.SettingsSnapshot(0, {}, degraded=True)))
    writer = AsyncMock()
    monkeypatch.setattr(store, "set_value", writer)
    with pytest.raises(ConnectionError):
        await processes.save_primary_model("inline", "gemini-new", actor="admin")
    writer.assert_not_awaited()


@pytest.mark.asyncio
async def test_croc_auto_clears_old_role_and_inherits_versioned_shared_default(monkeypatch):
    from app.games.daily_ai import get_daily_text_model_for
    from app.process_policies import baseline_models
    from app.repos import settings_repo
    from app.runtime_settings import legacy_models

    values = {"legacy_model:daily_croc_text_model": "gemini-new", "legacy_model:daily_croc_text_model_judge": ""}
    snapshot = store.SettingsSnapshot(9, values)
    monkeypatch.setattr(store, "get_snapshot", AsyncMock(return_value=snapshot))
    monkeypatch.setattr(settings_repo, "get_global_setting", AsyncMock(return_value="gemini-obsolete"))
    writer = AsyncMock(return_value=snapshot)
    monkeypatch.setattr(store, "update_values", writer)
    await legacy_models.save_croc_model("judge", "", actor="admin")
    writer.assert_awaited_once_with(
        {"legacy_model:daily_croc_text_model_judge": ""},
        removals=("process:crocodile.judge",),
        expected_revision=9,
        actor="admin",
    )
    assert await get_daily_text_model_for("judge") == "gemini-new"
    assert await baseline_models("crocodile.judge") == ("gemini-new",)
