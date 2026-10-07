import asyncio
from unittest.mock import AsyncMock, patch

import pytest


@pytest.mark.asyncio
async def test_controls_page_requires_auth_and_supplies_csrf_for_admin():
    from app.web import quart_app

    client = quart_app.test_client()
    assert (await client.get("/controls")).status_code == 302
    async with client.session_transaction() as session:
        session["authenticated"] = True
    response = await client.get("/controls")
    assert response.status_code == 200
    async with client.session_transaction() as session:
        assert session["controls_csrf"]


@pytest.mark.asyncio
async def test_controls_mutation_rejects_missing_csrf_without_writing():
    from app.web import quart_app

    client = quart_app.test_client()
    async with client.session_transaction() as session:
        session["authenticated"] = True
    response = await client.post(
        "/api/admin/controls",
        json={"section": "prompt", "id": "system_prompt_full", "value": "test", "expected_revision": 0},
    )
    assert response.status_code == 403


@pytest.mark.asyncio
async def test_controls_rejects_unknown_section_without_writing():
    from app.web import quart_app

    client = quart_app.test_client()
    async with client.session_transaction() as session:
        session.update(authenticated=True, controls_csrf="test-csrf")
    response = await client.post(
        "/api/admin/controls",
        json={"section": "secrets", "id": "api_key", "value": "test", "expected_revision": 0},
        headers={"X-CSRF-Token": "test-csrf"},
    )
    assert response.status_code == 400


@pytest.mark.asyncio
async def test_controls_conflict_does_not_claim_success():
    from app.runtime_settings.store import RevisionConflict
    from app.web import quart_app

    client = quart_app.test_client()
    async with client.session_transaction() as session:
        session.update(authenticated=True, controls_csrf="test-csrf")
    with patch("app.web_controls.set_value", new=AsyncMock(side_effect=RevisionConflict("changed"))):
        response = await client.post(
            "/api/admin/controls",
            json={
                "section": "process",
                "id": "chat",
                "value": {"models": ["gemini-future"], "strategy": "sequential", "inherit_user_model": False},
                "expected_revision": 0,
            },
            headers={"X-CSRF-Token": "test-csrf"},
        )
    assert response.status_code == 409


@pytest.mark.asyncio
async def test_chunked_oversized_mutation_is_rejected_before_write():
    from app.web import quart_app

    client = quart_app.test_client()
    async with client.session_transaction() as session:
        session.update(authenticated=True, controls_csrf="test-csrf")
    with patch("app.web_controls.set_value", new=AsyncMock()) as write:
        async with client.request(
            "/api/admin/controls",
            method="POST",
            headers={"X-CSRF-Token": "test-csrf", "Content-Type": "application/json"},
        ) as connection:
            await connection.send(b" " * 300_001)
            # Reject an oversized stream without waiting for its end.
            await asyncio.wait_for(connection.receive(), timeout=1)
            await connection.send_complete()
        response = await connection.as_response()
        assert response.status_code == 413
        write.assert_not_awaited()


@pytest.mark.asyncio
async def test_command_inventory_requires_auth_and_reads_live_handlers(monkeypatch):
    from types import SimpleNamespace

    from telegram.ext import CommandHandler

    from app import bot_instance
    from app.web import quart_app

    async def draw(update, context):
        pass

    application = SimpleNamespace(handlers={0: [CommandHandler(["draw", "img"], draw)]})
    monkeypatch.setattr(bot_instance, "get_application", lambda: application)
    client = quart_app.test_client()
    assert (await client.get("/api/admin/controls/commands")).status_code == 401
    async with client.session_transaction() as session:
        session["authenticated"] = True
    response = await client.get("/api/admin/controls/commands")
    assert response.status_code == 200
    payload = await response.get_json()
    assert payload["available"] is True
    assert payload["commands"][0]["command"] == "/draw"
    assert payload["commands"][0]["aliases"] == ["/img"]
    application.handlers.clear()
    assert (await (await client.get("/api/admin/controls/commands")).get_json())["commands"] == []


@pytest.mark.asyncio
async def test_command_aliases_save_conflict_reset_and_restore(monkeypatch):
    from types import SimpleNamespace

    from telegram.ext import CommandHandler

    from app import bot_instance, web_controls
    from app.runtime_settings import store
    from app.web import quart_app
    from tests.test_runtime_settings_store import Database

    async def draw(update, context):
        pass

    application = SimpleNamespace(handlers={0: [CommandHandler(["draw", "img"], draw)]})
    monkeypatch.setattr(bot_instance, "get_application", lambda: application)
    database = Database()
    backend = store.RuntimeSettingsStore()
    monkeypatch.setattr(store.db.db_manager, "pool", database)
    for name in ("get_state", "get_snapshot", "update_values", "restore_revision"):
        monkeypatch.setattr(store, name, getattr(backend, name))
    monkeypatch.setattr(web_controls, "get_state", backend.get_state)
    monkeypatch.setattr(web_controls, "restore_revision", backend.restore_revision)
    client = quart_app.test_client()
    async with client.session_transaction() as session:
        session.update(authenticated=True, controls_csrf="test-csrf")
    headers = {"X-CSRF-Token": "test-csrf"}

    response = await client.post(
        "/api/admin/controls",
        json={"section": "command", "id": "draw", "value": ["/paint", "/рисуй", "нарисуй"], "expected_revision": 0},
        headers=headers,
    )
    assert response.status_code == 200
    assert (await response.get_json())["revision"] == 1
    inventory = await (await client.get("/api/admin/controls/commands")).get_json()
    assert inventory["revision"] == 1
    row = inventory["commands"][0]
    assert row["editable"] is True
    assert row["aliases"] == ["/paint", "/рисуй", "нарисуй"]
    assert row["default_aliases"] == ["/img"]
    assert row["source"] == "override"

    conflict = await client.post(
        "/api/admin/controls",
        json={"section": "command", "id": "draw", "value": ["/other"], "expected_revision": 0},
        headers=headers,
    )
    assert conflict.status_code == 409
    assert database.writes == 1

    reset = await client.post(
        "/api/admin/controls",
        json={"section": "command", "id": "draw", "reset": True, "expected_revision": 1},
        headers=headers,
    )
    assert reset.status_code == 200
    row = (await (await client.get("/api/admin/controls/commands")).get_json())["commands"][0]
    assert row["aliases"] == ["/img"]
    assert row["source"] == "default"

    restored = await client.post(
        "/api/admin/controls/restore",
        json={"revision": 1, "expected_revision": 2},
        headers=headers,
    )
    assert restored.status_code == 200
    row = (await (await client.get("/api/admin/controls/commands")).get_json())["commands"][0]
    assert row["aliases"] == ["/paint", "/рисуй", "нарисуй"]
