"""MiniApp mutations report durable rejection and retain request ownership."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from quart import Quart

from app import state, web_miniapp
from app.runtime_settings.store import SettingsSnapshot


@pytest.fixture
async def settings_web(monkeypatch):
    monkeypatch.setattr(state, "USER_STATES", state._UserStateStore())
    monkeypatch.setattr(state, "_schedule_persist", lambda _state: None)
    monkeypatch.setattr(web_miniapp, "_validate_init_data", lambda *args: {"user": {"id": 777}})
    monkeypatch.setattr("app.repos.users.is_authorized", AsyncMock(return_value=True))
    monkeypatch.setattr("app.runtime_settings.lifecycle.refresh_runtime_settings", AsyncMock())
    monkeypatch.setattr("app.runtime_settings.store.get_snapshot", AsyncMock(return_value=SettingsSnapshot(0, {})))
    chat = SimpleNamespace(
        system_prompt="Original", history=[{"role": "user", "content": "Original"}], token_count=5, memory_epoch=7
    )
    monkeypatch.setattr("app.repos.chats.get_user_chat", AsyncMock(return_value=chat))
    write = AsyncMock(return_value=False)
    monkeypatch.setattr("app.repos.chats.update_user_chat", write)
    app = Quart(__name__)
    app.register_blueprint(web_miniapp.miniapp_blueprint, url_prefix="/webapp")
    return SimpleNamespace(client=app.test_client(), headers={"Authorization": "tma synthetic"}, chat=chat, write=write)


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["settings", "reset"])
async def test_rejected_durable_write_never_reports_success(settings_web, action):
    web = settings_web
    if action == "settings":
        response = await web.client.patch(
            "/webapp/api/settings", headers=web.headers, json={"system_prompt": "New prompt"}
        )
    else:
        response = await web.client.post("/webapp/api/context/reset", headers=web.headers)
    assert response.status_code == 409
    assert (await response.get_json())["error"] == "state_changed"


@pytest.mark.asyncio
async def test_settings_write_waits_for_admitted_history_and_preserves_it(settings_web):
    web = settings_web
    lock = state.get_user_lock(777)
    await lock.acquire()
    request = asyncio.create_task(
        web.client.patch("/webapp/api/settings", headers=web.headers, json={"system_prompt": "New prompt"})
    )
    try:
        for _ in range(20):
            await asyncio.sleep(0)
        web.write.assert_not_awaited()
        web.chat.history.append({"role": "model", "content": "Latest admitted answer"})
        web.write.return_value = True
        lock.release()
        response = await request
        assert response.status_code == 200
        assert web.write.await_args.args[1].history[-1]["content"] == "Latest admitted answer"
    finally:
        if lock.locked():
            lock.release()
        await request
