"""Offline checks of atomic live routing and reversible provider profiles."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.runtime_settings import store
from tests.test_runtime_settings_store import Database


@pytest.fixture
def profiles(monkeypatch):
    from app.runtime_settings import live_routes

    database = Database()
    backend = store.RuntimeSettingsStore()
    monkeypatch.setattr(store.db.db_manager, "pool", database)
    monkeypatch.setattr(store, "get_snapshot", backend.get_snapshot)
    monkeypatch.setattr(store, "update_values", backend.update_values)
    monkeypatch.setattr("app.process_policies.baseline_models", AsyncMock(return_value=("gemini-baseline",)))
    return live_routes, backend, database


@pytest.mark.asyncio
async def test_provider_switch_is_one_revision_and_preserves_original_and_custom_profiles(profiles):
    routes, backend, database = profiles
    custom = {"models": ["gemini-custom", "gemini-reserve"], "strategy": "hedged", "inherit_user_model": True}
    await backend.set_value("process:chat", custom, expected_revision=0, actor="test")
    await backend.set_value("process:daily.trivia", {"models": ["gemini-offline"]}, expected_revision=1, actor="test")
    saved = await routes.switch_provider("openai", expected_revision=2, actor="test")
    assert saved.revision == 3 and database.writes == 3
    assert saved.values["process:chat"]["models"] == ("openai-direct/gpt-6.1-terra",)
    assert saved.values["process:inline"]["models"] == ("openai-direct/gpt-6-luna",)
    assert saved.values["process:crocodile.judge"]["models"] == ("openai-direct/gpt-6.1-terra",)
    assert saved.values["process:daily.trivia"]["models"] == ("gemini-offline",)
    gemini = await routes.switch_provider("gemini", expected_revision=3, actor="test")
    assert gemini.values["process:chat"]["models"] == ("gemini-custom", "gemini-reserve")
    assert gemini.values["process:chat"]["inherit_user_model"] is True
    assert all(m.startswith("gemini-") for m in gemini.values["process:inline"]["models"])
    await backend.set_value("process:inline", {"models": ["gemini-my-inline"]}, expected_revision=4, actor="test")
    await routes.switch_provider("openai", expected_revision=5, actor="test")
    restored = await routes.switch_provider("gemini", expected_revision=6, actor="test")
    assert restored.values["process:inline"]["models"] == ("gemini-my-inline",)
    original = await routes.switch_provider("baseline", expected_revision=7, actor="test")
    assert "process:inline" not in original.values
    assert original.values["process:chat"]["models"] == ("gemini-custom", "gemini-reserve")


@pytest.mark.asyncio
async def test_stale_switch_and_unavailable_store_never_write(profiles):
    routes, backend, database = profiles
    await routes.switch_provider("openai", expected_revision=0, actor="test")
    with pytest.raises(store.RevisionConflict):
        await routes.switch_provider("gemini", expected_revision=0, actor="test")
    assert database.writes == 1
    database.error = ConnectionError("offline")
    with pytest.raises(ConnectionError):
        await routes.switch_provider("gemini", expected_revision=1, actor="test")
    assert database.writes == 1


def test_readiness_reports_only_key_presence(monkeypatch):
    from app.runtime_settings import live_routes

    monkeypatch.setattr("app.config.settings", SimpleNamespace(OPENAI_API_KEYS=["private-value"]))
    status = live_routes.status(store.SettingsSnapshot(4, {}))
    assert status["provider"] == "baseline"
    assert status["readiness"]["openai"]["configured"] is True
    assert "private-value" not in str(status)


@pytest.mark.parametrize("provider,model", [("openai", "vendor/foo"), ("openrouter", "openai-direct/gpt-6-luna")])
def test_catalog_rejects_confused_direct_openai_namespace(provider, model):
    from app.runtime_settings.models import validate_catalog

    with pytest.raises(ValueError):
        validate_catalog(provider, [model])


def test_direct_openai_catalog_accepts_supported_namespace():
    from app.runtime_settings.models import validate_catalog

    assert validate_catalog("openai", ["openai-direct/gpt-6-luna"]) == ["openai-direct/gpt-6-luna"]
    assert validate_catalog("openrouter", ["openai/gpt-4"]) == ["openai/gpt-4"]
    with pytest.raises(ValueError):
        validate_catalog("openai", ["openai-direct/gpt-5.4-mini"])
    with pytest.raises(ValueError):
        validate_catalog("openai", ["openai-direct/gpt-image-2"])


@pytest.mark.asyncio
async def test_process_save_rejects_unsupported_direct_models_and_keeps_legacy_openrouter(profiles):
    from app.process_policies import validate_policy
    from app.web import quart_app

    _, _, database = profiles
    assert validate_policy("chat", {"models": ["openai/gpt-4"]})["models"] == ["openai/gpt-4"]
    client = quart_app.test_client()
    async with client.session_transaction() as session:
        session.update(authenticated=True, controls_csrf="test-profile-csrf")
    response = await client.post(
        "/api/admin/controls",
        json={
            "section": "process",
            "id": "chat",
            "value": {"models": ["openai-direct/gpt-5.4-mini"]},
            "expected_revision": 0,
        },
        headers={"X-CSRF-Token": "test-profile-csrf"},
    )
    assert response.status_code == 400
    assert database.writes == 0


@pytest.mark.asyncio
async def test_live_provider_endpoint_requires_auth_csrf_and_revision(profiles):
    from app.web import quart_app

    _, backend, database = profiles
    client = quart_app.test_client()
    payload = {"provider": "openai", "expected_revision": 0}
    assert (await client.post("/api/admin/controls/live-provider", json=payload)).status_code == 401
    async with client.session_transaction() as session:
        session.update(authenticated=True, controls_csrf="test-profile-csrf")
    assert (await client.post("/api/admin/controls/live-provider", json=payload)).status_code == 403
    headers = {"X-CSRF-Token": "test-profile-csrf"}
    response = await client.post("/api/admin/controls/live-provider", json=payload, headers=headers)
    assert response.status_code == 200
    result = await response.get_json()
    assert result["revision"] == 1
    assert result["live_routes"]["provider"] == "openai"
    payload["provider"] = "gemini"
    assert (await client.post("/api/admin/controls/live-provider", json=payload, headers=headers)).status_code == 409
    assert (await backend.get_snapshot()).values["live_route:provider"] == "openai"
    assert database.writes == 1


@pytest.mark.asyncio
async def test_unavailable_switch_returns_safe_error_without_provider_calls(profiles):
    from app.web import quart_app

    _, _, database = profiles
    database.error = ConnectionError("private-database-address")
    client = quart_app.test_client()
    async with client.session_transaction() as session:
        session.update(authenticated=True, controls_csrf="test-profile-csrf")
    response = await client.post(
        "/api/admin/controls/live-provider",
        json={"provider": "openai", "expected_revision": 0},
        headers={"X-CSRF-Token": "test-profile-csrf"},
    )
    assert response.status_code == 503
    assert "private-database-address" not in await response.get_data(as_text=True)
    assert database.writes == 0


@pytest.mark.asyncio
async def test_profile_routes_pin_exact_openai_request_without_gemini_fallback(profiles):
    from app.providers.request_factory import generation_request_from_history
    from app.runtime_settings.lifecycle import runtime_settings_scope

    routes, _, _ = profiles
    snapshot = await routes.switch_provider("openai", expected_revision=0, actor="test")
    async with runtime_settings_scope(snapshot):
        request = await generation_request_from_history(
            process_id="chat", models=("gemini-legacy",), history=[{"role": "user", "parts": ["hello"]}]
        )
    assert request.models == ("openai-direct/gpt-6.1-terra",)
    assert request.allow_model_fallback is False
    assert request.route_strategy == "sequential"
    assert request.policy_revision == snapshot.revision


@pytest.mark.asyncio
async def test_active_profile_ignores_user_model_even_when_saved_gemini_plan_inherits(profiles):
    from app.process_policies import resolve_process
    from app.runtime_settings.lifecycle import runtime_settings_scope

    routes, backend, _ = profiles
    custom = {"models": ["gemini-custom"], "strategy": "sequential", "inherit_user_model": True}
    await backend.set_value("process:chat", custom, expected_revision=0, actor="test")
    active = await routes.switch_provider("gemini", expected_revision=1, actor="test")
    async with runtime_settings_scope(active):
        assert (await resolve_process("chat", ("openai-direct/gpt-6-luna",))).models == ("gemini-custom",)
    original = await routes.switch_provider("baseline", expected_revision=2, actor="test")
    async with runtime_settings_scope(original):
        assert (await resolve_process("chat", ("openai-direct/gpt-6-luna",))).models == (
            "openai-direct/gpt-6-luna",
            "gemini-custom",
        )


@pytest.mark.asyncio
async def test_history_restore_accepts_complete_profiles_and_rejects_invalid_saved_policy(profiles):
    from app.web_controls import _validate_restored_values

    routes, backend, _ = profiles
    snapshot = await routes.switch_provider("openai", expected_revision=0, actor="test")
    _validate_restored_values(snapshot.values)
    await routes.switch_provider("gemini", expected_revision=1, actor="test")
    restored = await backend.restore_revision(1, expected_revision=2, actor="test", validate=_validate_restored_values)
    assert restored.values["live_route:provider"] == "openai"
    with pytest.raises(ValueError):
        routes.validate_value("saved:gemini", {"chat": {"models": []}})


@pytest.mark.asyncio
async def test_reset_stays_provider_pinned_in_runtime_preview_and_inventory(profiles, monkeypatch):
    from app import web_controls
    from app.process_policies import list_processes, resolve_process
    from app.runtime_settings.lifecycle import runtime_settings_scope
    from app.runtime_settings.processes import reset_process
    from app.web import quart_app

    routes, backend, _ = profiles
    monkeypatch.setattr(store, "set_value", backend.set_value)
    monkeypatch.setattr(store, "reset_value", backend.reset_value)
    monkeypatch.setattr(web_controls, "get_state", backend.get_state)
    await routes.switch_provider("openai", expected_revision=0, actor="test")
    await backend.set_value(
        "process:chat", {"models": ["openai-direct/gpt-6.1-sol"]}, expected_revision=1, actor="test"
    )
    reset = await reset_process("chat", expected_revision=2, actor="test")
    assert reset.values["process:chat"]["models"] == ("openai-direct/gpt-6.1-terra",)
    async with runtime_settings_scope(reset):
        assert (await resolve_process("chat", ("gemini-stale-user",))).models == ("openai-direct/gpt-6.1-terra",)
        row = next(row for row in await list_processes() if row["id"] == "chat")
        assert row["models"] == ["openai-direct/gpt-6.1-terra"]
        assert row["user_model_allowed"] is False
    client = quart_app.test_client()
    async with client.session_transaction() as session:
        session.update(authenticated=True, controls_csrf="test-profile-csrf")
    response = await client.post(
        "/api/admin/controls/preview",
        json={
            "section": "process",
            "id": "chat",
            "reset": True,
            "expected_revision": 3,
            "selected_model": "gemini-stale-user",
        },
        headers={"X-CSRF-Token": "test-profile-csrf"},
    )
    assert response.status_code == 200
    assert (await response.get_json())["models"] == ["openai-direct/gpt-6.1-terra"]


@pytest.mark.asyncio
async def test_active_snapshot_missing_policy_uses_default_and_invalid_policy_fails_closed():
    from app.process_policies import resolve_process
    from app.runtime_settings.cache_identity import cache_identity
    from app.runtime_settings.lifecycle import runtime_settings_scope

    snapshot = store.SettingsSnapshot(8, {"live_route:provider": "openai"})
    async with runtime_settings_scope(snapshot):
        policy = await resolve_process("chat", ("gemini-stale-user",))
        assert policy.explicit and policy.models == ("openai-direct/gpt-6.1-terra",)
        assert await cache_identity("chat", "original-cache") != "original-cache"
    invalid = store.SettingsSnapshot(9, {"live_route:provider": "openai", "process:chat": {"models": []}})
    async with runtime_settings_scope(invalid):
        with pytest.raises(ValueError):
            await resolve_process("chat", ("gemini-stale-user",))
    unknown = store.SettingsSnapshot(10, {"live_route:provider": "unknown"})
    async with runtime_settings_scope(unknown):
        with pytest.raises(ValueError):
            await resolve_process("chat", ("gemini-stale-user",))
