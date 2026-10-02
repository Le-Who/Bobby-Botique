"""Behavioral checks for shared model catalogs and local RPD controls."""

from types import MappingProxyType, SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.runtime_settings import models, store


class SettingsBackend:
    """Only the persistent store boundary is replaced; consumers stay real."""

    def __init__(self):
        self.revision = 0
        self.values = {}

    async def get_snapshot(self, *, force=False):
        return store.SettingsSnapshot(self.revision, MappingProxyType(dict(self.values)))

    async def set_value(self, key, value, *, expected_revision, actor):
        if expected_revision != self.revision:
            raise store.RevisionConflict("stale revision")
        self.values[key] = value
        self.revision += 1
        return await self.get_snapshot()

    async def reset_value(self, key, *, expected_revision, actor):
        if expected_revision != self.revision:
            raise store.RevisionConflict("stale revision")
        self.values.pop(key, None)
        self.revision += 1
        return await self.get_snapshot()

    async def update_values(self, updates, *, removals=(), expected_revision, actor):
        if expected_revision != self.revision:
            raise store.RevisionConflict("stale revision")
        self.values.update(updates)
        for key in removals:
            self.values.pop(key, None)
        self.revision += 1
        return await self.get_snapshot()


@pytest.fixture
def controls(monkeypatch):
    import app.config as config
    from app.repos import keys, models_repo

    backend = SettingsBackend()
    settings = SimpleNamespace(
        AVAILABLE_MODELS=["gemini-3.7-flash"],
        OPENCODE_AVAILABLE_MODELS=["opencode-go/qwen3.5-plus"],
        OPENROUTER_AVAILABLE_MODELS=["vendor/old"],
        FREETHEAI_AVAILABLE_MODELS=[],
        DAILY_LIMITS={"gemini-3.7-flash": 100},
        LIMIT_THRESHOLD_PERCENT=0.95,
        OPENROUTER_DEFAULT_MODEL="vendor/old",
        OPENCODE_DEFAULT_MODEL="opencode-go/qwen3.5-plus",
        FREETHEAI_DEFAULT_MODEL="",
    )
    monkeypatch.setattr(config, "settings", settings)
    monkeypatch.setattr(keys, "settings", settings)
    monkeypatch.setattr(models, "get_snapshot", backend.get_snapshot)
    monkeypatch.setattr(models, "set_value", backend.set_value)
    monkeypatch.setattr(models, "reset_value", backend.reset_value)
    monkeypatch.setattr(models, "update_values", backend.update_values)
    monkeypatch.setattr(models_repo, "get_global_setting", AsyncMock(return_value=""))
    monkeypatch.setattr(models_repo, "delete_global_setting", AsyncMock())
    monkeypatch.setattr("app.process_policies.baseline_models", AsyncMock(return_value=("gemini-3.7-flash",)))
    models._applied.clear()
    models_repo._catalog_sources.update(dict.fromkeys(models_repo._PROVIDERS, models_repo.ModelCatalogSource.ENV))
    return backend, settings


@pytest.mark.asyncio
async def test_catalog_save_reset_and_stale_revision_preserve_env_baseline(controls, monkeypatch):
    backend, settings = controls
    monkeypatch.setenv("GEMINI_AVAILABLE_MODELS", "none")
    await models.save_catalog("gemini", [], expected_revision=0, actor="admin")
    assert settings.AVAILABLE_MODELS == []
    assert backend.values["catalog:gemini"] == []
    assert (await models.list_catalogs())[0] == {"provider": "gemini", "models": [], "source": "admin"}

    with pytest.raises(store.RevisionConflict):
        await models.save_catalog("gemini", ["gemini-3.7-flash"], expected_revision=0, actor="admin")
    assert settings.AVAILABLE_MODELS == []

    await models.reset_catalog("gemini", expected_revision=1, actor="admin")
    assert settings.AVAILABLE_MODELS == []
    assert "catalog:gemini" not in backend.values
    assert (await models.list_catalogs())[0]["source"] == "env"


@pytest.mark.asyncio
async def test_catalog_reset_shadows_legacy_record_on_restart_without_deleting_it(controls, monkeypatch):
    from app.repos import models_repo

    backend, settings = controls
    legacy = '{"version":2,"source":"admin","models":["vendor/legacy"]}'
    monkeypatch.setattr(models_repo, "get_global_setting", AsyncMock(return_value=legacy))
    await models.save_catalog("openrouter", ["vendor/new"], expected_revision=0, actor="admin")
    await models.reset_catalog("openrouter", expected_revision=1, actor="admin")
    assert backend.values["catalog_baseline:openrouter"] is True
    models_repo.delete_global_setting.assert_not_awaited()
    models._applied.clear()
    settings.OPENROUTER_AVAILABLE_MODELS = ["vendor/stale-replica"]
    await models_repo.sync_models_from_db()
    assert settings.OPENROUTER_AVAILABLE_MODELS == ["vendor/old"]


@pytest.mark.asyncio
async def test_legacy_v2_catalog_is_fallback_but_runtime_override_wins(controls, monkeypatch):
    from app.repos import models_repo

    backend, settings = controls
    legacy = '{"version":2,"source":"admin","models":["vendor/legacy"]}'
    monkeypatch.setattr(
        models_repo,
        "get_global_setting",
        AsyncMock(side_effect=lambda key, default="": legacy if key.endswith("openrouter") else ""),
    )
    await models_repo.sync_models_from_db()
    assert settings.OPENROUTER_AVAILABLE_MODELS == ["vendor/legacy"]

    await models.save_catalog("openrouter", ["vendor/new"], expected_revision=0, actor="admin")
    await models_repo.sync_models_from_db()
    assert settings.OPENROUTER_AVAILABLE_MODELS == ["vendor/new"]
    assert backend.values["catalog:openrouter"] == ["vendor/new"]


@pytest.mark.asyncio
async def test_limit_override_none_survives_cache_and_reset_restores_prior_limit(controls, monkeypatch):
    from app.repos import keys

    backend, _ = controls
    monkeypatch.setattr(keys.db_manager, "_model_config_cache", {"gemini-3.7-flash": 200})
    monkeypatch.setattr(keys, "db_query", AsyncMock(return_value=[{"daily_limit": 300}]))
    assert await keys.get_model_daily_limit("gemini-3.7-flash") == 200

    await models.save_limit("gemini-3.7-flash", None, expected_revision=0, actor="admin")
    assert backend.values["model_limit:gemini-3.7-flash"] is None
    assert await keys.get_model_daily_limit("gemini-3.7-flash") is None
    assert any(
        {key: row[key] for key in ("model", "limit", "source")}
        == {"model": "gemini-3.7-flash", "limit": None, "source": "admin"}
        for row in await models.list_limits()
    )

    await models.reset_limit("gemini-3.7-flash", expected_revision=1, actor="admin")
    assert "model_limit:gemini-3.7-flash" not in backend.values
    assert await keys.get_model_daily_limit("gemini-3.7-flash") == 300


@pytest.mark.asyncio
async def test_telegram_add_uses_same_catalog_key(controls, monkeypatch):
    from app.repos import models_repo

    backend, settings = controls
    monkeypatch.setattr(models_repo, "_validate_gemini_model", AsyncMock(return_value="supported"))
    result = await models_repo.add_model("gemini", "gemini-3.5-flash-lite")
    assert result.code is models_repo.ModelMutationCode.ADDED
    assert backend.values["catalog:gemini"] == ["gemini-3.7-flash", "gemini-3.5-flash-lite"]
    assert settings.AVAILABLE_MODELS == backend.values["catalog:gemini"]


@pytest.mark.asyncio
async def test_telegram_add_rebases_once_after_revision_conflict(controls, monkeypatch):
    from app.repos import models_repo

    backend, settings = controls
    original_write = backend.set_value
    attempts = [0]

    async def competing_write(key, value, *, expected_revision, actor):
        attempts[0] += 1
        if attempts[0] == 1:
            backend.values["catalog:openrouter"] = ["vendor/concurrent"]
            backend.revision += 1
            raise store.RevisionConflict("another admin saved")
        return await original_write(key, value, expected_revision=expected_revision, actor=actor)

    monkeypatch.setattr(models, "set_value", competing_write)
    result = await models_repo.add_model("opencode", "opencode-go/new")
    assert result.code is models_repo.ModelMutationCode.ADDED
    assert attempts[0] == 2
    assert settings.OPENROUTER_AVAILABLE_MODELS == ["vendor/concurrent"]
    assert backend.values["catalog:opencode"] == ["opencode-go/qwen3.5-plus", "opencode-go/new"]


@pytest.mark.asyncio
async def test_telegram_invalid_opencode_id_returns_typed_result(controls):
    from app.repos import models_repo

    backend, _ = controls
    result = await models_repo.add_model("opencode", "vendor/wrong-provider")
    assert result.code is models_repo.ModelMutationCode.INVALID
    assert backend.values == {}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("capability", "error"),
    [
        ("unsupported", models.UnsupportedModelError),
        ("unavailable", models.ModelValidationUnavailableError),
    ],
)
async def test_new_gemini_catalog_entry_requires_confirmed_capability(controls, monkeypatch, capability, error):
    from app.repos import models_repo

    backend, settings = controls
    monkeypatch.setattr(models_repo, "_validate_gemini_model", AsyncMock(return_value=capability))
    with pytest.raises(error):
        await models.save_catalog(
            "gemini", ["gemini-3.7-flash", "gemini-3.8-flash"], expected_revision=0, actor="admin"
        )
    assert backend.values == {}
    assert settings.AVAILABLE_MODELS == ["gemini-3.7-flash"]


@pytest.mark.asyncio
async def test_limit_rejects_non_gemini_and_non_positive_values(controls):
    backend, _ = controls
    for model, limit in [("vendor/chat", 100), ("gemini-3.7-flash", 0), ("gemini-3.7-flash", True)]:
        with pytest.raises(ValueError):
            await models.save_limit(model, limit, expected_revision=0, actor="admin")
    assert backend.values == {}


@pytest.mark.asyncio
async def test_key_selection_uses_same_integer_threshold_as_reservation(controls, monkeypatch):
    from app.repos import keys

    manager = keys.DailyKeyManager("api_keys", "key_usage")
    query = AsyncMock(return_value=[{"key_hash": "hash", "api_key": "encrypted", "request_count": 1}])
    monkeypatch.setattr(keys, "db_query", query)
    assert await manager.get_fresh_available_key("gemini-3.7-flash", 2) is None
    assert await manager.is_key_available("hash", "gemini-3.7-flash", 2) is False
    query.return_value = []
    assert await manager.reserve_usage("hash", "gemini-3.7-flash", 2) is None
    assert query.await_args.args[1][-1] == 1


@pytest.mark.asyncio
async def test_reset_shadows_legacy_v2_so_it_cannot_reappear_on_reload(controls, monkeypatch):
    from app.repos import models_repo

    backend, settings = controls
    legacy = {"provider_models:openrouter": '{"version":2,"source":"admin","models":["vendor/legacy"]}'}

    async def read_legacy(key, default=""):
        return legacy.get(key, default)

    async def delete_legacy(key):
        legacy.pop(key, None)

    monkeypatch.setattr(models_repo, "get_global_setting", read_legacy)
    monkeypatch.setattr(models_repo, "delete_global_setting", delete_legacy)
    await models_repo.sync_models_from_db()
    assert settings.OPENROUTER_AVAILABLE_MODELS == ["vendor/legacy"]

    await models.reset_catalog("openrouter", expected_revision=0, actor="admin")
    assert "provider_models:openrouter" in legacy
    assert backend.values["catalog_baseline:openrouter"] is True
    assert "catalog:openrouter" not in backend.values
    await models_repo.sync_models_from_db()
    assert settings.OPENROUTER_AVAILABLE_MODELS == ["vendor/old"]


@pytest.mark.asyncio
async def test_stale_reset_does_not_delete_legacy_override(controls, monkeypatch):
    from app.repos import models_repo

    backend, _ = controls
    backend.revision = 1
    delete = AsyncMock()
    monkeypatch.setattr(models_repo, "delete_global_setting", delete)
    with pytest.raises(store.RevisionConflict):
        await models.reset_catalog("gemini", expected_revision=0, actor="admin")
    delete.assert_not_awaited()
