"""Mini App voice selection follows the administrator's audio catalog."""

import json
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from quart import Quart

from tests.factories import make_valid_init_data

_TOKEN = "test-miniapp-audio-token"


@pytest.fixture
def audio_web(monkeypatch):
    from app import web_miniapp as web
    from app.runtime_settings import lifecycle
    from app.runtime_settings.store import SettingsSnapshot

    monkeypatch.setattr(web.settings, "TELEGRAM_BOT_TOKEN", _TOKEN)
    monkeypatch.setattr("app.repos.users.is_authorized", AsyncMock(return_value=True))
    monkeypatch.setattr(lifecycle, "refresh_runtime_settings", AsyncMock())
    monkeypatch.setattr("app.runtime_settings.store.get_snapshot", AsyncMock(return_value=SettingsSnapshot(7, {})))
    app = Quart(__name__)
    app.register_blueprint(web.miniapp_blueprint, url_prefix="/webapp")
    return app.test_client(), {"Authorization": f"tma {make_valid_init_data(_TOKEN, user_id=777)}"}


@pytest.mark.asyncio
async def test_voices_api_returns_admin_catalog_order_and_default(audio_web, monkeypatch):
    from app import web_miniapp as web
    from app.runtime_settings.lifecycle import current_operation_snapshot

    async def catalog():
        assert current_operation_snapshot() is not None
        return {
            "provider": "gemini",
            "default_voice": "CustomVoice",
            "voices": [{"id": "Kore", "name": "Second"}, {"id": "CustomVoice", "name": "Preferred"}],
        }

    monkeypatch.setattr(web, "user_voice_catalog", catalog, raising=False)
    client, headers = audio_web
    response = await client.get("/webapp/api/voices", headers=headers)
    assert response.status_code == 200
    assert await response.get_json() == {
        "provider": "gemini",
        "default_voice": "CustomVoice",
        "voices": [{"id": "Kore", "name": "Second"}, {"id": "CustomVoice", "name": "Preferred"}],
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("voice_id", ["removed-voice", "", 17, False, ["Kore"], {"id": "Kore"}])
async def test_settings_rejects_invalid_voice_before_mutating_other_fields(audio_web, monkeypatch, voice_id):
    from app import web_miniapp as web

    state = SimpleNamespace(voice_id="Kore", system_prompt="original", ltm_enabled=True)
    writes = []

    async def write(*args):
        writes.append(args)

    monkeypatch.setattr(web, "valid_user_voice", AsyncMock(return_value=False), raising=False)
    monkeypatch.setattr("app.repos.chats.get_user_chat", AsyncMock(return_value=state))
    monkeypatch.setattr("app.repos.chats.update_user_chat", write)
    monkeypatch.setattr("app.repos.chats.set_ltm_enabled", write)
    client, headers = audio_web
    response = await client.patch(
        "/webapp/api/settings",
        headers=headers,
        json={"voice_id": voice_id, "system_prompt": "changed", "ltm_enabled": False},
    )
    assert response.status_code == 400
    assert await response.get_json() == {"error": "invalid_voice_id"}
    assert state.voice_id == "Kore"
    assert state.system_prompt == "original"
    assert state.ltm_enabled is True
    assert writes == []


@pytest.mark.asyncio
@pytest.mark.parametrize("voice_id", [None, "CustomVoice"])
async def test_settings_persists_valid_voice_or_default_reset(audio_web, monkeypatch, voice_id):
    from app import web_miniapp as web

    state = SimpleNamespace(voice_id="Kore")
    saved = []

    async def write(user_id, chat_state):
        saved.append((user_id, chat_state.voice_id))

    monkeypatch.setattr(web, "valid_user_voice", AsyncMock(return_value=True), raising=False)
    monkeypatch.setattr("app.repos.chats.get_user_chat", AsyncMock(return_value=state))
    monkeypatch.setattr("app.repos.chats.update_user_chat", write)
    client, headers = audio_web
    response = await client.patch("/webapp/api/settings", headers=headers, json={"voice_id": voice_id})
    assert response.status_code == 200
    assert await response.get_json() == {"ok": True}
    assert saved == [(777, voice_id)]


@pytest.mark.asyncio
async def test_voices_api_still_requires_bot_authorization(audio_web, monkeypatch):
    monkeypatch.setattr("app.repos.users.is_authorized", AsyncMock(return_value=False))
    client, headers = audio_web
    response = await client.get("/webapp/api/voices", headers=headers)
    assert response.status_code == 403


@pytest.mark.asyncio
@pytest.mark.parametrize("preference,expected_voice", [(None, "Kore"), ("removed", "Kore"), ("Aoede", "Aoede")])
async def test_read_aloud_button_uses_admin_default_without_user_preference(monkeypatch, preference, expected_voice):
    from app import voice_engine
    from app.handlers.cb_ai_actions import tts_reply_callback
    from app.runtime_settings.lifecycle import runtime_settings_scope
    from app.runtime_settings.store import SettingsSnapshot

    manager = voice_engine.VoiceReplyManager()
    monkeypatch.setattr(manager, "_set_status", AsyncMock())
    sent_voices = []
    generated = []

    async def synthesize(chunks, voice, timeout, **kwargs):
        sent_voices.append(voice)
        return [b"pcm"]

    async def enqueue(**kwargs):
        generated.append(await manager._pregenerate_audio(SimpleNamespace(**kwargs)))
        return SimpleNamespace(deduped=False, queued=True, reason_code=None)

    monkeypatch.setattr(voice_engine, "_voice_reply_manager", SimpleNamespace(enqueue=enqueue))
    monkeypatch.setattr(voice_engine, "_run_gemini_pipeline", synthesize)
    monkeypatch.setattr("app.repos.memory_consent.resolve_current_epoch", AsyncMock(return_value=0))
    monkeypatch.setattr(
        "app.repos.chats.get_user_chat",
        AsyncMock(return_value=SimpleNamespace(voice_id=preference, tts_temperature=None)),
    )
    monkeypatch.setattr("app.utils.audio.crossfade_pcm_chunks", lambda _: b"pcm")
    monkeypatch.setattr("app.utils.audio.pcm_to_ogg_opus", AsyncMock(return_value=b"ogg"))
    message = SimpleNamespace(
        text="Read this answer aloud.", reply_markup=None, chat=SimpleNamespace(id=777), chat_id=777, message_id=5
    )
    query = SimpleNamespace(
        id="read-aloud", from_user=SimpleNamespace(id=777), message=message, get_bot=lambda: None, answer=AsyncMock()
    )
    snapshot = SettingsSnapshot(
        4,
        {
            "process:tts.delivery": {"models": ["gemini"], "strategy": "sequential", "inherit_user_model": False},
            "audio:gemini": {
                "default_voice": "Kore",
                "voices": [
                    {"id": "Aoede", "name": "First", "prompt": ""},
                    {"id": "Kore", "name": "Default", "prompt": ""},
                ],
                "temperature": 0.5,
                "max_output_tokens": 4096,
            },
        },
    )
    async with runtime_settings_scope(snapshot):
        await tts_reply_callback(SimpleNamespace(callback_query=query), SimpleNamespace())
    assert generated == [b"ogg"]
    assert sent_voices == [expected_voice]


def test_voice_picker_preserves_catalog_order_default_and_literal_text():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js unavailable")
    template = (Path(__file__).parents[1] / "app/templates/miniapp.html").read_text(encoding="utf-8")
    render = template[template.index("function renderSettings(") : template.index("async function loadSettings(")]
    choose = template[template.index("function setVoice(") : template.index("// ── Temperature Controller")]
    script = r"""
const assert = require('node:assert/strict');
class Element {
  constructor() { this.children = []; this.listeners = {}; this.textContent = ''; this.dataset = {}; }
  appendChild(child) { this.children.push(child); return child; }
  append(...children) { this.children.push(...children); }
  replaceChildren(...children) { this.children = children; }
  addEventListener(name, callback) { this.listeners[name] = callback; }
  setAttribute(name, value) { this[name] = value; }
  querySelector(selector) { return this.children.find(child => '.' + child.className === selector); }
  set innerHTML(value) { throw Error('Voice labels must enter the DOM as literal text'); }
}
const elements = new Map();
const document = {
  getElementById(id) { if (!elements.has(id)) elements.set(id, new Element()); return elements.get(id); },
  createElement() { return new Element(); },
  querySelectorAll() { return elements.get('settings-voices-list').children; }
};
const tg = {};
let settingsData, availableModels, groupedModels, roleDataCache, dirty = false;
function buildModelSheet() {}
function renderRoles() {}
function setThinking() {}
function syncTempControls() {}
function syncTtsTempControls() {}
function modelDisplayName(value) { return value; }
function esc(value) { return value; }
function markDirty() { dirty = true; }
"""
    script += render + choose
    script += (
        "const catalog = "
        + json.dumps(
            {
                "default_voice": "CustomVoice",
                "voices": [
                    {"id": "Kore", "name": "<img src=x onerror=alert(1)>"},
                    {"id": "CustomVoice", "name": "Preferred"},
                ],
            }
        )
        + ";\n"
    )
    script += r"""
renderSettings({settings:{model:'test', voice_id:'retired-voice'}}, {}, catalog);
const list = document.getElementById('settings-voices-list');
assert.equal(list.children.length, 2);
assert.equal(list.children[0].querySelector('.setting-label').textContent, '<img src=x onerror=alert(1)>');
assert.equal(list.children[0].querySelector('.setting-val').textContent, '');
assert.equal(list.children[1].querySelector('.setting-val').textContent, '✓');
assert.equal(settingsData.voice_id, null);
list.children[0].listeners.click();
assert.equal(settingsData.voice_id, 'Kore');
assert.equal(list.children[0].querySelector('.setting-val').textContent, '✓');
assert.equal(list.children[1].querySelector('.setting-val').textContent, '');
assert.equal(dirty, true);
renderSettings({settings:{model:'test', voice_id:'Kore'}}, {}, catalog);
assert.equal(document.getElementById('settings-voices-list').children[0].querySelector('.setting-val').textContent, '✓');
"""
    result = subprocess.run([node, "-"], input=script, encoding="utf-8", capture_output=True)
    assert result.returncode == 0, result.stderr
