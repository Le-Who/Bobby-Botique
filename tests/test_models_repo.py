from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.utils.json_compat import json


def _settings(**overrides):
    values = {
        "AVAILABLE_MODELS": ["gemini-3.7-flash", "gemini-3.5-flash-lite"],
        "OPENROUTER_AVAILABLE_MODELS": ["vendor/selectable"],
        "OPENCODE_AVAILABLE_MODELS": ["opencode-go/selectable"],
        "FREETHEAI_AVAILABLE_MODELS": ["cat/selectable"],
        "OPENROUTER_DEFAULT_MODEL": "vendor/internal",
        "OPENCODE_DEFAULT_MODEL": "opencode-go/internal",
        "OPENCODE_QNA_MODEL": "opencode-go/qna",
        "OPENCODE_RESEARCH_MODEL": "opencode-go/research",
        "OPENCODE_VISION_MODEL": "opencode-go/vision",
        "OPENCODE_INLINE_MODEL": "opencode-go/inline",
        "FREETHEAI_DEFAULT_MODEL": "cat/internal",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _patch_repo(monkeypatch, fake_settings, *, db_values=None):
    import app.config as config
    from app.repos import models_repo

    values = db_values or {}
    monkeypatch.setattr(config, "settings", fake_settings)
    monkeypatch.setattr(
        models_repo,
        "get_global_setting",
        AsyncMock(side_effect=lambda key, default="": values.get(key, default)),
    )
    set_mock = AsyncMock()
    delete_mock = AsyncMock()
    monkeypatch.setattr(models_repo, "set_global_setting", set_mock)
    monkeypatch.setattr(models_repo, "delete_global_setting", delete_mock, raising=False)
    return models_repo, set_mock, delete_mock


@pytest.mark.asyncio
async def test_sync_legacy_db_list_is_removed_and_current_env_catalog_wins(monkeypatch):
    fake_settings = _settings()
    legacy = json.dumps(["gemini-3.6-flash", "gemini-3.5-flash-lite"])
    models_repo, set_mock, delete_mock = _patch_repo(
        monkeypatch,
        fake_settings,
        db_values={"provider_models:gemini": legacy},
    )

    await models_repo.sync_models_from_db()

    assert fake_settings.AVAILABLE_MODELS == ["gemini-3.7-flash", "gemini-3.5-flash-lite"]
    delete_mock.assert_awaited_once_with("provider_models:gemini")
    set_mock.assert_not_awaited()
    catalog = await models_repo.get_model_catalog("gemini")
    assert catalog.source.value == "env"


@pytest.mark.asyncio
async def test_sync_v2_admin_override_wins_even_when_list_is_empty(monkeypatch):
    fake_settings = _settings()
    override = json.dumps({"version": 2, "source": "admin", "models": []})
    models_repo, set_mock, delete_mock = _patch_repo(
        monkeypatch,
        fake_settings,
        db_values={"provider_models:gemini": override},
    )

    await models_repo.sync_models_from_db()

    assert fake_settings.AVAILABLE_MODELS == []
    set_mock.assert_not_awaited()
    delete_mock.assert_not_awaited()
    catalog = await models_repo.get_model_catalog("gemini")
    assert catalog.models == ()
    assert catalog.source.value == "admin"


@pytest.mark.asyncio
async def test_sync_can_apply_admin_override_to_reloaded_settings_object(monkeypatch):
    live_settings = _settings(AVAILABLE_MODELS=["gemini-env"])
    reloaded_settings = _settings(AVAILABLE_MODELS=["gemini-new-env"])
    override = json.dumps({"version": 2, "source": "admin", "models": ["gemini-3.7-flash"]})
    models_repo, _, _ = _patch_repo(
        monkeypatch,
        live_settings,
        db_values={"provider_models:gemini": override},
    )

    await models_repo.sync_models_from_db(reloaded_settings)

    assert reloaded_settings.AVAILABLE_MODELS == ["gemini-3.7-flash"]
    assert live_settings.AVAILABLE_MODELS == ["gemini-env"]


@pytest.mark.asyncio
async def test_sync_missing_db_override_does_not_seed_env_copy(monkeypatch):
    fake_settings = _settings()
    models_repo, set_mock, delete_mock = _patch_repo(monkeypatch, fake_settings)

    await models_repo.sync_models_from_db()

    assert fake_settings.AVAILABLE_MODELS == ["gemini-3.7-flash", "gemini-3.5-flash-lite"]
    set_mock.assert_not_awaited()
    delete_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_reset_deletes_override_and_restores_exact_current_env(monkeypatch):
    monkeypatch.setenv("GEMINI_AVAILABLE_MODELS", "gemini-3.7-flash,gemini-3.5-flash-lite")
    fake_settings = _settings(AVAILABLE_MODELS=["gemini-3.6-flash"])
    models_repo, set_mock, delete_mock = _patch_repo(monkeypatch, fake_settings)

    restored = await models_repo.reset_models_to_env("gemini")

    assert restored == ["gemini-3.7-flash", "gemini-3.5-flash-lite"]
    assert fake_settings.AVAILABLE_MODELS == restored
    delete_mock.assert_awaited_once_with("provider_models:gemini")
    set_mock.assert_not_awaited()
    catalog = await models_repo.get_model_catalog("gemini")
    assert catalog.source.value == "env"


@pytest.mark.asyncio
async def test_unknown_provider_is_rejected_instead_of_mutating_opencode(monkeypatch):
    fake_settings = _settings()
    models_repo, _, _ = _patch_repo(monkeypatch, fake_settings)

    with pytest.raises(ValueError, match="Unknown model provider"):
        await models_repo.get_models("typo")

    assert fake_settings.OPENCODE_AVAILABLE_MODELS == ["opencode-go/selectable"]


@pytest.mark.asyncio
async def test_add_model_returns_added_and_persists_v2_override(monkeypatch):
    fake_settings = _settings(AVAILABLE_MODELS=["gemini-3.5-flash-lite"])
    models_repo, set_mock, _ = _patch_repo(monkeypatch, fake_settings)
    validator = AsyncMock(return_value="supported")
    monkeypatch.setattr(models_repo, "_validate_gemini_model", validator, raising=False)

    result = await models_repo.add_model("gemini", "gemini-3.7-flash")

    assert result.code is models_repo.ModelMutationCode.ADDED
    assert fake_settings.AVAILABLE_MODELS == ["gemini-3.5-flash-lite", "gemini-3.7-flash"]
    validator.assert_awaited_once_with("gemini-3.7-flash")
    key, raw = set_mock.await_args.args
    assert key == "provider_models:gemini"
    assert json.loads(raw) == {
        "version": 2,
        "source": "admin",
        "models": ["gemini-3.5-flash-lite", "gemini-3.7-flash"],
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("validation", "expected_code"),
    [
        ("unsupported", "UNSUPPORTED"),
        ("unavailable", "VALIDATION_UNAVAILABLE"),
    ],
)
async def test_add_gemini_model_reports_validation_failure_without_mutation(monkeypatch, validation, expected_code):
    fake_settings = _settings(AVAILABLE_MODELS=["gemini-3.5-flash-lite"])
    models_repo, set_mock, _ = _patch_repo(monkeypatch, fake_settings)
    monkeypatch.setattr(models_repo, "_validate_gemini_model", AsyncMock(return_value=validation), raising=False)

    result = await models_repo.add_model("gemini", "gemini-3.7-flash")

    assert result.code is getattr(models_repo.ModelMutationCode, expected_code)
    assert fake_settings.AVAILABLE_MODELS == ["gemini-3.5-flash-lite"]
    set_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_add_model_distinguishes_duplicate_invalid_and_unknown_provider(monkeypatch):
    fake_settings = _settings(AVAILABLE_MODELS=["gemini-3.7-flash"])
    models_repo, set_mock, _ = _patch_repo(monkeypatch, fake_settings)
    validator = AsyncMock(return_value="supported")
    monkeypatch.setattr(models_repo, "_validate_gemini_model", validator, raising=False)

    duplicate = await models_repo.add_model("gemini", "gemini-3.7-flash")
    invalid = await models_repo.add_model("gemini", "not a model")
    unknown = await models_repo.add_model("typo", "vendor/model")

    assert duplicate.code is models_repo.ModelMutationCode.DUPLICATE
    assert invalid.code is models_repo.ModelMutationCode.INVALID
    assert unknown.code is models_repo.ModelMutationCode.UNKNOWN_PROVIDER
    validator.assert_not_awaited()
    set_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_remove_model_returns_typed_results_and_can_persist_empty_override(monkeypatch):
    fake_settings = _settings(AVAILABLE_MODELS=["gemini-3.7-flash"])
    models_repo, set_mock, _ = _patch_repo(monkeypatch, fake_settings)

    missing = await models_repo.remove_model("gemini", "gemini-missing")
    removed = await models_repo.remove_model("gemini", "gemini-3.7-flash")

    assert missing.code is models_repo.ModelMutationCode.NOT_FOUND
    assert removed.code is models_repo.ModelMutationCode.REMOVED
    assert fake_settings.AVAILABLE_MODELS == []
    assert json.loads(set_mock.await_args.args[1])["models"] == []


@pytest.mark.asyncio
async def test_freetheai_add_rejects_non_chat_model(monkeypatch):
    fake_settings = _settings(FREETHEAI_AVAILABLE_MODELS=["cat/chat-model"])
    models_repo, set_mock, _ = _patch_repo(monkeypatch, fake_settings)

    result = await models_repo.add_model("freetheai", "or/google/lyria-3-pro-preview")

    assert result.code is models_repo.ModelMutationCode.INVALID
    set_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_failed_override_write_does_not_mutate_live_settings(monkeypatch):
    fake_settings = _settings(OPENROUTER_AVAILABLE_MODELS=["vendor/old"])
    models_repo, set_mock, _ = _patch_repo(monkeypatch, fake_settings)
    set_mock.side_effect = RuntimeError("db unavailable")

    with pytest.raises(RuntimeError, match="db unavailable"):
        await models_repo.add_model("openrouter", "vendor/new")

    assert fake_settings.OPENROUTER_AVAILABLE_MODELS == ["vendor/old"]
