"""The start menu displays the effective route without rewriting user state."""

from types import SimpleNamespace

import pytest

from app.handlers.menus import get_start_menu_content
from app.runtime_settings.lifecycle import runtime_settings_scope
from app.runtime_settings.store import SettingsSnapshot


@pytest.mark.asyncio
@pytest.mark.parametrize("missing_policy", [False, True])
async def test_start_menu_displays_openai_profile_over_persisted_gemini(missing_policy):
    saved_model = "gemini-3.6-flash"
    state = SimpleNamespace(model=saved_model, search_enabled=False, system_prompt=None)
    values = {"live_route:provider": "openai"}
    if not missing_policy:
        values["process:chat"] = {"models": ["openai-direct/gpt-6.1-terra"], "inherit_user_model": False}
    async with runtime_settings_scope(SettingsSnapshot(8, values)):
        text, _, _ = await get_start_menu_content(state)
    assert "openai-direct/gpt-6.1-terra" in text
    assert saved_model not in text
    assert state.model == saved_model


@pytest.mark.asyncio
async def test_start_menu_preserves_user_model_display_without_active_profile():
    state = SimpleNamespace(model="gemini-3.6-flash", search_enabled=False, system_prompt=None)
    async with runtime_settings_scope(SettingsSnapshot(0, {})):
        text, _, _ = await get_start_menu_content(state)
    assert state.model in text
