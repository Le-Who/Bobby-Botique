"""Mini App reasoning is admin-owned for effective OpenAI routes."""

import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from quart import Quart

from tests.factories import make_valid_init_data

LUNA = "openai-direct/gpt-6-luna"
GEMINI = "gemini-3.5-flash-lite"


@pytest.fixture
def reasoning_web(monkeypatch):
    from app import web_miniapp as web
    from app.runtime_settings import lifecycle
    from app.runtime_settings.store import SettingsSnapshot

    state = SimpleNamespace(
        model=GEMINI,
        thinking_level="high",
        system_prompt="Original",
        ltm_enabled=True,
        search_enabled=False,
        temperature=None,
        voice_id=None,
        tts_temperature=None,
        memory_epoch=1,
        live_thinking_level="low",
        live_voice_name="Kore",
        live_persona_id="friendly",
    )
    snapshot = [SettingsSnapshot(7, {})]
    writes, consent = AsyncMock(), AsyncMock(return_value=2)
    monkeypatch.setattr(web.settings, "TELEGRAM_BOT_TOKEN", "miniapp-reasoning-test")
    monkeypatch.setattr(web.settings, "OPENAI_AVAILABLE_MODELS", [LUNA])
    monkeypatch.setattr(web.settings, "AVAILABLE_MODELS", [GEMINI])
    monkeypatch.setattr("app.repos.users.is_authorized", AsyncMock(return_value=True))
    monkeypatch.setattr("app.repos.chats.get_user_chat", AsyncMock(return_value=state))
    monkeypatch.setattr("app.repos.chats.update_user_chat", writes)
    monkeypatch.setattr("app.repos.chats.set_ltm_enabled", consent)
    monkeypatch.setattr("app.repos.roles.get_user_custom_roles", AsyncMock(return_value=[]))
    monkeypatch.setattr(lifecycle, "refresh_runtime_settings", AsyncMock())

    async def current_snapshot(*args, **kwargs):
        return snapshot[0]

    monkeypatch.setattr("app.runtime_settings.store.get_snapshot", current_snapshot)
    app = Quart(__name__)
    app.register_blueprint(web.miniapp_blueprint, url_prefix="/webapp")
    headers = {"Authorization": f"tma {make_valid_init_data('miniapp-reasoning-test', user_id=777)}"}
    return SimpleNamespace(
        client=app.test_client(), headers=headers, state=state, snapshot=snapshot, writes=writes, consent=consent
    )


def select_profile(web, provider, effort="none"):
    from app.runtime_settings.store import SettingsSnapshot

    web.snapshot[0] = SettingsSnapshot(
        8,
        {
            "live_route:provider": provider,
            "process:chat": {
                "models": [LUNA if provider == "openai" else GEMINI],
                "reasoning_effort": effort if provider == "openai" else None,
            },
        },
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("effort,source", [("none", "admin"), ("low", "admin"), (None, "model_default")])
async def test_get_effective_openai_reasoning_uses_pinned_profile_not_saved_gemini(reasoning_web, effort, source):
    web = reasoning_web
    select_profile(web, "openai", effort)
    response = await web.client.get("/webapp/api/settings", headers=web.headers)
    assert response.status_code == 200
    data = await response.get_json()
    assert data["thinking_editable"] is False
    assert data["thinking_levels"] == []
    assert data["effective_reasoning"] == {"effort": effort, "source": source, "model": LUNA}
    assert data["settings"]["model"] == LUNA
    assert data["settings"]["thinking_level"] == "high"  # Old-client unchanged payload compatibility.
    assert data["available_models"] == [LUNA]
    assert data["model_editable"] is False
    assert web.state.model == GEMINI and web.state.thinking_level == "high"
    web.writes.assert_not_awaited()


@pytest.mark.asyncio
async def test_baseline_direct_openai_is_readonly_but_gemini_profile_overrides_old_openai_state(reasoning_web):
    web = reasoning_web
    web.state.model = LUNA
    response = await web.client.get("/webapp/api/settings", headers=web.headers)
    data = await response.get_json()
    assert data["thinking_editable"] is False and data["model_editable"] is True
    assert data["effective_reasoning"]["source"] == "model_default"
    select_profile(web, "gemini")
    response = await web.client.get("/webapp/api/settings", headers=web.headers)
    data = await response.get_json()
    assert data["thinking_editable"] is True
    assert data["settings"]["model"] == GEMINI
    assert data["thinking_levels"] == ["off", "low", "medium", "high"]


@pytest.mark.asyncio
@pytest.mark.parametrize("level", ["off", "low", "medium", None, False, 1, ["high"]])
async def test_forged_openai_reasoning_change_rejected_before_any_state_or_consent_mutation(reasoning_web, level):
    web = reasoning_web
    select_profile(web, "openai")
    response = await web.client.patch(
        "/webapp/api/settings",
        headers=web.headers,
        json={
            "thinking_level": level,
            "system_prompt": "Changed",
            "ltm_enabled": False,
            "search_enabled": True,
        },
    )
    assert response.status_code == 403
    assert (await response.get_json())["error"] == "thinking_readonly"
    assert web.state.system_prompt == "Original" and web.state.ltm_enabled is True
    assert web.state.search_enabled is False and web.state.thinking_level == "high"
    web.writes.assert_not_awaited()
    web.consent.assert_not_awaited()


@pytest.mark.asyncio
async def test_old_client_unchanged_saved_high_is_ignored_and_other_preferences_still_save(reasoning_web):
    web = reasoning_web
    select_profile(web, "openai")
    response = await web.client.patch(
        "/webapp/api/settings",
        headers=web.headers,
        json={
            "model": LUNA,
            "thinking_level": "high",
            "system_prompt": "Changed",
        },
    )
    assert response.status_code == 200
    web.writes.assert_awaited_once()
    assert web.state.system_prompt == "Changed"
    assert web.state.model == GEMINI and web.state.thinking_level == "high"


@pytest.mark.asyncio
async def test_atomic_model_switch_to_openai_cannot_also_set_user_reasoning(reasoning_web):
    web = reasoning_web
    web.state.thinking_level = "low"
    response = await web.client.patch(
        "/webapp/api/settings",
        headers=web.headers,
        json={
            "model": LUNA,
            "thinking_level": "high",
            "system_prompt": "Changed",
            "ltm_enabled": False,
        },
    )
    assert response.status_code == 403
    assert web.state.model == GEMINI and web.state.thinking_level == "low"
    assert web.state.system_prompt == "Original"
    web.writes.assert_not_awaited()
    web.consent.assert_not_awaited()


@pytest.mark.asyncio
async def test_baseline_model_switch_ignores_unchanged_thinking_and_gemini_can_then_change_it(reasoning_web):
    web = reasoning_web
    response = await web.client.patch(
        "/webapp/api/settings", headers=web.headers, json={"model": LUNA, "thinking_level": "high"}
    )
    assert response.status_code == 200
    assert web.state.model == LUNA and web.state.thinking_level == "high"
    response = await web.client.patch(
        "/webapp/api/settings", headers=web.headers, json={"model": GEMINI, "thinking_level": "low"}
    )
    assert response.status_code == 200
    assert web.state.model == GEMINI and web.state.thinking_level == "low"


@pytest.mark.asyncio
async def test_explicit_baseline_openai_plan_remains_readonly_despite_saved_gemini(reasoning_web):
    from app.runtime_settings.store import SettingsSnapshot

    web = reasoning_web
    web.snapshot[0] = SettingsSnapshot(9, {"process:chat": {"models": [LUNA], "reasoning_effort": None}})
    data = await (await web.client.get("/webapp/api/settings", headers=web.headers)).get_json()
    assert data["thinking_editable"] is False and data["model_editable"] is False
    assert data["settings"]["model"] == LUNA
    response = await web.client.patch(
        "/webapp/api/settings", headers=web.headers, json={"model": GEMINI, "thinking_level": "low"}
    )
    assert response.status_code == 403
    assert web.state.model == GEMINI and web.state.thinking_level == "high"
    web.writes.assert_not_awaited()


@pytest.mark.asyncio
async def test_gemini_reasoning_controls_and_gemini_live_thinking_remain_separate(reasoning_web):
    web = reasoning_web
    select_profile(web, "gemini")
    response = await web.client.patch("/webapp/api/settings", headers=web.headers, json={"thinking_level": "medium"})
    assert response.status_code == 200 and web.state.thinking_level == "medium"
    select_profile(web, "openai")
    response = await web.client.patch(
        "/webapp/api/live-settings", headers=web.headers, json={"live_thinking_level": "high"}
    )
    assert response.status_code == 200
    assert web.state.live_thinking_level == "high"
    assert web.state.thinking_level == "medium"


@pytest.mark.asyncio
async def test_settings_reasoning_still_requires_authorized_telegram_identity(reasoning_web, monkeypatch):
    web = reasoning_web
    assert (await web.client.get("/webapp/api/settings")).status_code == 401
    monkeypatch.setattr("app.repos.users.is_authorized", AsyncMock(return_value=False))
    assert (await web.client.get("/webapp/api/settings", headers=web.headers)).status_code == 403


def test_cached_thinking_is_not_editable_until_fresh_server_policy_arrives():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node unavailable")
    template = (Path(__file__).parents[1] / "app/templates/miniapp.html").read_text(encoding="utf-8")
    load = template[template.index("async function loadSettings()") : template.index("function renderRoles(")]
    script = (
        r"""
const assert=require('node:assert/strict');
const rendered=[];let release,isDirty;
const pendingSettings=new Promise(resolve=>release=resolve);
const tg={CloudStorage:{getItem:(key,cb)=>cb(null,JSON.stringify({d:{settings:{thinking_level:'high'}},r:{},v:{}})),setItem:()=>{}}};
const api=path=>path==='/settings'?pendingSettings:Promise.resolve({});
const renderSettings=d=>rendered.push(d);
const showToast=message=>{throw Error(message)};
"""
        + load
        + r"""
(async()=>{
 const done=loadSettings();
 assert.equal(rendered.length,1);
 assert.equal(rendered[0].thinking_editable,false);
 assert.equal(rendered[0].effective_reasoning.source,'loading');
 release({settings:{thinking_level:'high'},thinking_editable:false,effective_reasoning:{effort:'none',source:'admin'}});
 await done;
 assert.equal(rendered.length,2);
 assert.equal(rendered[1].effective_reasoning.effort,'none');
 assert.equal(rendered[1].thinking_editable,false);
})().catch(error=>{console.error(error);process.exitCode=1});
"""
    )
    result = subprocess.run([node, "-e", script], capture_output=True, encoding="utf-8", timeout=10)
    assert result.returncode == 0, result.stdout + result.stderr
