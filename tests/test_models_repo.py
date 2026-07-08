from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.config import GEMINI_ECONOMY_MODEL, GEMINI_PRIMARY_MODEL
from app.utils.json_compat import json


def _patch_repo_settings(monkeypatch, fake_settings):
    import app.config as config

    monkeypatch.setattr(config, "settings", fake_settings)


@pytest.mark.asyncio
async def test_sync_models_from_db_sanitizes_legacy_gemini_models(monkeypatch):
    from app.repos import models_repo

    fake_settings = SimpleNamespace(
        AVAILABLE_MODELS=[],
        OPENROUTER_AVAILABLE_MODELS=[],
        OPENCODE_AVAILABLE_MODELS=[],
    )
    _patch_repo_settings(monkeypatch, fake_settings)

    db_models = ["gemini-3-flash-preview", GEMINI_ECONOMY_MODEL, "gemini-2.5-flash"]
    get_mock = AsyncMock(
        side_effect=lambda key, default="": json.dumps(db_models, ensure_ascii=False)
        if key == "provider_models:gemini"
        else ""
    )
    set_mock = AsyncMock()
    monkeypatch.setattr(models_repo, "get_global_setting", get_mock)
    monkeypatch.setattr(models_repo, "set_global_setting", set_mock)

    await models_repo.sync_models_from_db()

    expected = [GEMINI_PRIMARY_MODEL, GEMINI_ECONOMY_MODEL]
    assert fake_settings.AVAILABLE_MODELS == expected
    set_mock.assert_awaited_once_with("provider_models:gemini", json.dumps(expected, ensure_ascii=False))


@pytest.mark.asyncio
async def test_add_model_rejects_legacy_gemini_model(monkeypatch):
    from app.repos import models_repo

    current = [GEMINI_PRIMARY_MODEL, GEMINI_ECONOMY_MODEL]
    fake_settings = SimpleNamespace(AVAILABLE_MODELS=current.copy())
    _patch_repo_settings(monkeypatch, fake_settings)
    set_mock = AsyncMock()
    monkeypatch.setattr(models_repo, "set_global_setting", set_mock)

    added = await models_repo.add_model("gemini", "gemini-3-flash-preview")

    assert added is False
    assert fake_settings.AVAILABLE_MODELS == current
    set_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_sync_models_from_db_preserves_current_gemini_subset(monkeypatch):
    from app.repos import models_repo

    fake_settings = SimpleNamespace(
        AVAILABLE_MODELS=[],
        OPENROUTER_AVAILABLE_MODELS=[],
        OPENCODE_AVAILABLE_MODELS=[],
    )
    _patch_repo_settings(monkeypatch, fake_settings)

    get_mock = AsyncMock(
        side_effect=lambda key, default="": json.dumps([GEMINI_ECONOMY_MODEL], ensure_ascii=False)
        if key == "provider_models:gemini"
        else ""
    )
    set_mock = AsyncMock()
    monkeypatch.setattr(models_repo, "get_global_setting", get_mock)
    monkeypatch.setattr(models_repo, "set_global_setting", set_mock)

    await models_repo.sync_models_from_db()

    assert fake_settings.AVAILABLE_MODELS == [GEMINI_ECONOMY_MODEL]
    set_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_reset_gemini_models_filters_env_and_role_models(monkeypatch):
    from app.repos import models_repo

    monkeypatch.setenv("GEMINI_AVAILABLE_MODELS", f"gemini-3-flash-preview,{GEMINI_ECONOMY_MODEL}")

    fake_settings = SimpleNamespace(
        AVAILABLE_MODELS=[],
        DEFAULT_MODEL="gemini-3-flash-preview",
        QNA_MODEL=GEMINI_ECONOMY_MODEL,
        RESEARCH_MODEL="gemini-2.5-flash",
    )
    _patch_repo_settings(monkeypatch, fake_settings)
    set_mock = AsyncMock()
    monkeypatch.setattr(models_repo, "set_global_setting", set_mock)

    restored = await models_repo.reset_models_to_env("gemini")

    expected = [GEMINI_ECONOMY_MODEL, GEMINI_PRIMARY_MODEL]
    assert restored == expected
    assert fake_settings.AVAILABLE_MODELS == expected
    set_mock.assert_awaited_once_with("provider_models:gemini", json.dumps(expected, ensure_ascii=False))
