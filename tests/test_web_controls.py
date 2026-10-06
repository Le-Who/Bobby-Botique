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
