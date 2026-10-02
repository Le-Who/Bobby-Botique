"""A failed legacy catalog read cannot partially publish a runtime revision."""

import asyncio
import time
from types import MappingProxyType, SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.repos import models_repo
from app.runtime_settings import models, store


@pytest.fixture
def target(monkeypatch):
    monkeypatch.setattr(models, "_applied", {"openrouter"})
    monkeypatch.setattr(
        models_repo, "_catalog_sources", dict.fromkeys(models.PROVIDERS, models_repo.ModelCatalogSource.ENV)
    )
    return SimpleNamespace(
        AVAILABLE_MODELS=["gemini-old"],
        OPENCODE_AVAILABLE_MODELS=[],
        OPENROUTER_AVAILABLE_MODELS=["vendor/current"],
        FREETHEAI_AVAILABLE_MODELS=[],
        OPENROUTER_DEFAULT_MODEL="vendor/env",
    )


@pytest.mark.asyncio
async def test_failed_restore_keeps_every_confirmed_catalog(target, monkeypatch):
    snapshot = store.SettingsSnapshot(4, MappingProxyType({"catalog:gemini": ("gemini-new",)}))
    monkeypatch.setattr(models_repo, "get_global_setting", AsyncMock(side_effect=ConnectionError("unavailable")))
    with pytest.raises(ConnectionError):
        await models.refresh_catalogs(target, snapshot=snapshot)
    assert target.AVAILABLE_MODELS == ["gemini-old"]
    assert target.OPENROUTER_AVAILABLE_MODELS == ["vendor/current"]
    assert models._applied == {"openrouter"}
    assert models_repo._catalog_sources["gemini"] == models_repo.ModelCatalogSource.ENV


@pytest.mark.asyncio
async def test_legacy_restore_read_has_one_bounded_deadline(target, monkeypatch):
    monkeypatch.setattr(models, "_CATALOG_REFRESH_TIMEOUT", 0.02, raising=False)

    async def hang(*_, **__):
        await asyncio.Event().wait()

    monkeypatch.setattr(models_repo, "get_global_setting", hang)
    began = time.monotonic()
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(
            models.refresh_catalogs(target, snapshot=store.SettingsSnapshot(4, MappingProxyType({}))), 0.2
        )
    assert time.monotonic() - began < 0.15
    assert target.OPENROUTER_AVAILABLE_MODELS == ["vendor/current"]
