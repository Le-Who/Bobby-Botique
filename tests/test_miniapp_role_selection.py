"""Saved role choices expose the selecting user's exact persisted prompt."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest


@pytest.mark.asyncio
async def test_roles_api_returns_own_saved_prompt_for_selection(monkeypatch):
    from app import database, web_miniapp
    from app.web import quart_app

    prompt = "You're a tutor. Keep 'quoted' text intact."

    async def query(sql, params):
        assert params == (777,)
        assert "WHERE user_id = $1" in sql
        row = {"id": 23, "title": "Saved tutor"}
        if "prompt" in sql:
            row["prompt"] = prompt
        return [row]

    monkeypatch.setattr(database, "db_query", query)
    monkeypatch.setattr(web_miniapp, "_validate_init_data", lambda *args: {"user": {"id": 777}})
    monkeypatch.setattr("app.repos.users.is_authorized", AsyncMock(return_value=True))
    response = await quart_app.test_client().get("/webapp/api/roles", headers={"Authorization": "tma synthetic"})
    assert response.status_code == 200
    assert (await response.get_json())["roles"] == [{"id": 23, "title": "Saved tutor", "prompt": prompt}]


@pytest.mark.asyncio
async def test_empty_prompt_explicitly_restores_default_role(monkeypatch):
    from app import web_miniapp
    from app.runtime_settings.store import SettingsSnapshot
    from app.web import quart_app

    chat_state = SimpleNamespace(system_prompt="Previously selected role")
    saved = []

    async def update(user_id, state):
        saved.append((user_id, state.system_prompt))

    monkeypatch.setattr(web_miniapp, "_validate_init_data", lambda *args: {"user": {"id": 777}})
    monkeypatch.setattr("app.repos.users.is_authorized", AsyncMock(return_value=True))
    monkeypatch.setattr("app.repos.chats.get_user_chat", AsyncMock(return_value=chat_state))
    monkeypatch.setattr("app.repos.chats.update_user_chat", update)
    monkeypatch.setattr("app.runtime_settings.lifecycle.refresh_runtime_settings", AsyncMock())
    monkeypatch.setattr("app.runtime_settings.store.get_snapshot", AsyncMock(return_value=SettingsSnapshot(0, {})))
    response = await quart_app.test_client().patch(
        "/webapp/api/settings", headers={"Authorization": "tma synthetic"}, json={"system_prompt": ""}
    )
    assert response.status_code == 200
    assert saved == [(777, None)]
