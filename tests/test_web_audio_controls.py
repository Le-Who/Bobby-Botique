"""Offline admin boundaries for durable TTS catalogs and synthesis parameters."""

from copy import deepcopy
from unittest.mock import AsyncMock

import pytest

GEMINI = {
    "default_voice": "Kore",
    "voices": [
        {"id": "Kore", "name": "Спокойный", "prompt": "Read calmly."},
        {"id": "Puck", "name": "Весёлый", "prompt": "Read cheerfully."},
    ],
    "temperature": 0.8,
    "max_output_tokens": 4096,
}
ELEVENLABS = {
    "default_voice": "voice-two",
    "voices": [{"id": "voice-two", "name": "Второй"}, {"id": "voice-one", "name": "Первый"}],
    "voice_settings": {"stability": 0.4, "similarity_boost": 0.7, "style": 0.2, "use_speaker_boost": True},
    "apply_text_normalization": "on",
}


@pytest.fixture
def audio_backend(monkeypatch):
    from app import web_controls
    from app.runtime_settings import lifecycle, models, prompts, store
    from tests.test_runtime_settings_store import Database

    database = Database()
    backend = store.RuntimeSettingsStore()
    monkeypatch.setattr(store.db.db_manager, "pool", database)
    for name in ("get_state", "get_snapshot", "set_value", "reset_value", "update_values", "restore_revision"):
        monkeypatch.setattr(store, name, getattr(backend, name))
    monkeypatch.setattr(web_controls, "get_state", backend.get_state)
    monkeypatch.setattr(web_controls, "restore_revision", backend.restore_revision)
    monkeypatch.setattr(web_controls, "list_processes", AsyncMock(return_value=[]))
    monkeypatch.setattr(lifecycle, "load_controlled_prompts", lambda: None)
    monkeypatch.setattr(prompts, "list_prompts", AsyncMock(return_value=[]))
    monkeypatch.setattr(prompts, "refresh_prompts", AsyncMock())
    monkeypatch.setattr(models, "list_catalogs", AsyncMock(return_value=[]))
    monkeypatch.setattr(models, "list_limits", AsyncMock(return_value=[]))
    monkeypatch.setattr(models, "refresh_catalogs", AsyncMock())
    return database, backend


async def admin_client():
    from app.web import quart_app

    client = quart_app.test_client()
    async with client.session_transaction() as session:
        session.update(authenticated=True, controls_csrf="test-csrf")
    return client


@pytest.mark.asyncio
@pytest.mark.parametrize("provider,value", [("gemini", GEMINI), ("elevenlabs", ELEVENLABS)])
async def test_audio_save_get_conflict_reset_and_restore(audio_backend, provider, value):
    database, backend = audio_backend
    client = await admin_client()
    headers = {"X-CSRF-Token": "test-csrf"}
    payload = {"section": "audio", "id": provider, "value": value, "expected_revision": 0}
    response = await client.post("/api/admin/controls", json=payload, headers=headers)
    assert response.status_code == 200, await response.get_json()
    assert (await response.get_json())["revision"] == 1

    inventory = await (await client.get("/api/admin/controls")).get_json()
    assert inventory["revision"] == 1
    item = next(row for row in inventory["audio"] if row["id"] == provider)
    assert item["value"] == value
    assert item["source"] == "admin"
    assert item["apply"] == "next_request"

    conflict = await client.post("/api/admin/controls", json=payload, headers=headers)
    assert conflict.status_code == 409
    assert database.writes == 1
    reset = await client.post(
        "/api/admin/controls",
        json={"section": "audio", "id": provider, "reset": True, "expected_revision": 1},
        headers=headers,
    )
    assert reset.status_code == 200
    assert f"audio:{provider}" not in (await backend.get_snapshot()).values
    restored = await client.post(
        "/api/admin/controls/restore", json={"revision": 1, "expected_revision": 2}, headers=headers
    )
    assert restored.status_code == 200, await restored.get_json()
    final = await (await client.get("/api/admin/controls")).get_json()
    assert final["revision"] == 3
    assert next(row for row in final["audio"] if row["id"] == provider)["value"] == value


@pytest.mark.asyncio
async def test_audio_requires_admin_csrf_and_valid_catalog_before_write(audio_backend):
    from app.web import quart_app

    database, _ = audio_backend
    anonymous = quart_app.test_client()
    payload = {"section": "audio", "id": "gemini", "value": GEMINI, "expected_revision": 0}
    assert (await anonymous.get("/api/admin/controls")).status_code == 401
    assert (await anonymous.post("/api/admin/controls", json=payload)).status_code == 401
    client = await admin_client()
    assert (await client.post("/api/admin/controls", json=payload)).status_code == 403
    invalid = deepcopy(GEMINI)
    invalid["voices"] = []
    response = await client.post(
        "/api/admin/controls", json={**payload, "value": invalid}, headers={"X-CSRF-Token": "test-csrf"}
    )
    assert response.status_code == 400
    assert database.writes == 0


@pytest.mark.asyncio
async def test_restore_rejects_invalid_audio_history_before_write(audio_backend):
    database, backend = audio_backend
    invalid = deepcopy(GEMINI)
    invalid["default_voice"] = "missing"
    await backend.set_value("audio:gemini", invalid, expected_revision=0, actor="legacy")
    await backend.reset_value("audio:gemini", expected_revision=1, actor="admin")
    client = await admin_client()
    response = await client.post(
        "/api/admin/controls/restore",
        json={"revision": 1, "expected_revision": 2},
        headers={"X-CSRF-Token": "test-csrf"},
    )
    assert response.status_code == 400
    assert database.writes == 2
    assert not (await backend.get_snapshot()).values
