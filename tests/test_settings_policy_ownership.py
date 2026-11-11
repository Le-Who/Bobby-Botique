"""Settings policy is checked against the state that will actually be saved."""

import asyncio
from unittest.mock import AsyncMock

import pytest

from app import state, web_miniapp
from app.handlers import menus
from tests.test_miniapp_mutation_ownership import settings_web as _settings_web

settings_web = _settings_web


@pytest.mark.asyncio
async def test_reasoning_patch_rechecks_model_after_waiting_for_request_lock(settings_web, monkeypatch):
    web = settings_web
    web.chat.model = "gemini-3.1-flash-lite"
    web.chat.thinking_level = "low"
    owner = state.begin_interaction(777, "role")
    checked = asyncio.Event()
    original = menus.openai_reasoning_readonly

    def check(chat):
        result = original(chat)
        checked.set()
        return result

    monkeypatch.setattr(menus, "openai_reasoning_readonly", check)
    web.write.return_value = True
    lock = state.get_user_lock(777)
    await lock.acquire()
    request = asyncio.create_task(
        web.client.patch("/webapp/api/settings", headers=web.headers, json={"thinking_level": "high"})
    )
    try:
        await asyncio.wait_for(checked.wait(), 1)
        web.chat.model = "openai-direct/gpt-6"
        lock.release()
        response = await request
    finally:
        if lock.locked():
            lock.release()
        await asyncio.gather(request, return_exceptions=True)
    assert response.status_code == 403
    assert (await response.get_json())["error"] == "thinking_readonly"
    web.write.assert_not_awaited()
    assert web.chat.thinking_level == "low"
    assert state.get_user_state(777).interaction is owner


@pytest.mark.asyncio
async def test_reasoning_policy_uses_the_same_request_model_patch(settings_web, monkeypatch):
    web = settings_web
    web.chat.model = "gemini-3.1-flash-lite"
    web.chat.thinking_level = "low"
    monkeypatch.setattr(web_miniapp, "_miniapp_chat_model_editable", lambda: True)
    monkeypatch.setattr("app.config.get_all_available_models", lambda: ["gemini-3.1-flash-lite"])
    checked = asyncio.Event()
    original = menus.openai_reasoning_readonly

    def check(chat):
        result = original(chat)
        checked.set()
        return result

    monkeypatch.setattr(menus, "openai_reasoning_readonly", check)
    web.write.return_value = True
    lock = state.get_user_lock(777)
    await lock.acquire()
    request = asyncio.create_task(
        web.client.patch(
            "/webapp/api/settings",
            headers=web.headers,
            json={"model": "gemini-3.1-flash-lite", "thinking_level": "high"},
        )
    )
    try:
        await asyncio.wait_for(checked.wait(), 1)
        web.chat.model = "openai-direct/gpt-6"
        lock.release()
        response = await request
    finally:
        if lock.locked():
            lock.release()
        await asyncio.gather(request, return_exceptions=True)
    assert response.status_code == 200
    assert web.write.await_args.args[1].model == "gemini-3.1-flash-lite"
    assert web.write.await_args.args[1].thinking_level == "high"


@pytest.mark.asyncio
async def test_settings_patch_preserves_newer_consent_while_waiting_for_lock(settings_web, monkeypatch):
    web = settings_web
    web.chat.ltm_enabled = False
    consent_written = asyncio.Event()

    async def enable(*args):
        consent_written.set()
        return 8

    monkeypatch.setattr("app.repos.chats.set_ltm_enabled", enable)
    monkeypatch.setattr("app.repos.memory_autosave.cancel_user_memory_tasks", AsyncMock())
    web.write.return_value = True
    lock = state.get_user_lock(777)
    await lock.acquire()
    request = asyncio.create_task(
        web.client.patch(
            "/webapp/api/settings",
            headers=web.headers,
            json={"ltm_enabled": True, "system_prompt": "New prompt"},
        )
    )
    try:
        await asyncio.wait_for(consent_written.wait(), 1)
        # A later dedicated opt-out does not wait on an ordinary request lock.
        web.chat.memory_epoch = 9
        web.chat.ltm_enabled = False
        lock.release()
        response = await request
    finally:
        if lock.locked():
            lock.release()
        await asyncio.gather(request, return_exceptions=True)
    assert response.status_code == 200
    saved = web.write.await_args.args[1]
    assert saved.memory_epoch == 9
    assert saved.ltm_enabled is False
    assert saved.system_prompt == "New prompt"
