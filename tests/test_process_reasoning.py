"""Admin process reasoning supersedes user preferences only when explicit."""

from unittest.mock import AsyncMock

import pytest

from app.process_policies import ReasoningEffort, resolve_policy_value, validate_policy
from app.providers.stream_types import ThinkingLevel
from app.runtime_settings import store
from tests.test_process_service_tiers import LUNA
from tests.test_runtime_settings_store import Database


@pytest.fixture
def tiers(monkeypatch):
    backend = store.RuntimeSettingsStore()
    monkeypatch.setattr(store.db.db_manager, "pool", Database())
    for name in ("get_snapshot", "set_value", "reset_value", "update_values"):
        monkeypatch.setattr(store, name, getattr(backend, name))
    monkeypatch.setattr("app.process_policies.baseline_models", AsyncMock(return_value=("gemini-baseline",)))
    return backend


def test_absent_reasoning_keeps_exact_legacy_policy_shape_and_null_clears():
    legacy = {"models": [LUNA], "strategy": "sequential", "inherit_user_model": False}
    assert validate_policy("chat", legacy) == legacy
    assert resolve_policy_value("chat", legacy, ()).reasoning_effort is None
    assert resolve_policy_value("chat", None, (LUNA,)).reasoning_effort is None
    cleared = validate_policy("chat", {**legacy, "reasoning_effort": None})
    assert "reasoning_effort" in cleared and cleared["reasoning_effort"] is None


def test_sol_none_rejected_instead_of_silently_running_low():
    sol = "openai-direct/gpt-6.1-sol"
    for models in ([sol], [LUNA, sol]):
        with pytest.raises(ValueError, match="Sol не поддерживает reasoning none"):
            validate_policy("chat", {"models": models, "reasoning_effort": "none"})
        for effort in ("low", "medium", "high", None):
            assert validate_policy("chat", {"models": models, "reasoning_effort": effort})["reasoning_effort"] == effort


@pytest.mark.parametrize("value", ["off", "auto", "minimal", "HIGH", "", True, 1, [], {}])
def test_unknown_reasoning_is_rejected(value):
    with pytest.raises(ValueError, match="reasoning effort"):
        validate_policy("chat", {"models": [LUNA], "reasoning_effort": value})


@pytest.mark.parametrize(
    "models", [["gemini-test"], ["openai/gpt-6-luna"], ["openai-direct/gpt-4o-mini"], [LUNA, "gemini-test"]]
)
def test_reasoning_never_silently_applies_to_unsupported_or_mixed_plan(models):
    with pytest.raises(ValueError, match="Reasoning override"):
        validate_policy("chat", {"models": models, "reasoning_effort": "none"})
    assert validate_policy("chat", {"models": models, "reasoning_effort": None})["reasoning_effort"] is None


@pytest.mark.parametrize("effort", list(ReasoningEffort))
@pytest.mark.parametrize("process_id", ["chat", "chat.complex", "search", "inline", "crocodile.judge"])
def test_known_reasoning_is_typed_for_direct_gpt6(process_id, effort):
    policy = resolve_policy_value(process_id, {"models": [LUNA], "reasoning_effort": effort.value}, ())
    assert policy.reasoning_effort is effort


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "override,expected", [("none", "none"), ("low", "low"), ("medium", "medium"), ("high", "high"), (None, "high")]
)
async def test_actual_factory_sdk_payload_respects_admin_override_and_null(tiers, override, expected):
    from app.providers.openai import responses_payload
    from app.providers.request_factory import generation_request_from_history
    from app.runtime_settings.lifecycle import runtime_settings_scope

    snapshot = await tiers.set_value(
        "process:chat.complex",
        {"models": [LUNA], "service_tier": "flex", "reasoning_effort": override},
        expected_revision=0,
        actor="test",
    )
    async with runtime_settings_scope(snapshot):
        request = await generation_request_from_history(
            process_id="chat.complex",
            models=("gemini-stale",),
            history=[{"role": "user", "parts": ["Synthetic"]}],
            thinking_level="high",
        )
    assert request.models == (LUNA,)
    assert request.service_tier.value == "flex"
    assert request.first_text_timeout_seconds == 15
    assert request.thinking_level is (ThinkingLevel.OFF if expected == "none" else ThinkingLevel(expected))
    assert (await responses_payload(request, LUNA))["reasoning"]["effort"] == expected


@pytest.mark.asyncio
async def test_absent_override_preserves_user_high_in_baseline_and_gemini(tiers):
    from app.providers.request_factory import generation_request_from_history
    from app.runtime_settings.lifecycle import runtime_settings_scope

    for values in ({}, {"process:chat": {"models": ["gemini-test"]}}):
        async with runtime_settings_scope(store.SettingsSnapshot(1, values)):
            request = await generation_request_from_history(
                process_id="chat",
                models=("gemini-user",),
                history=[{"role": "user", "parts": ["Synthetic"]}],
                thinking_level="high",
            )
        assert request.thinking_level is ThinkingLevel.HIGH


@pytest.mark.asyncio
async def test_primary_writer_preserves_reasoning_and_requires_explicit_clear(tiers):
    from app.runtime_settings.processes import save_primary_model_at_revision

    await tiers.set_value(
        "process:chat", {"models": [LUNA], "reasoning_effort": "low"}, expected_revision=0, actor="test"
    )
    saved = await save_primary_model_at_revision(
        "chat", "openai-direct/gpt-6.1-terra", expected_revision=1, actor="test"
    )
    assert saved.values["process:chat"]["reasoning_effort"] == "low"
    with pytest.raises(ValueError, match="Reasoning override"):
        await save_primary_model_at_revision("chat", "openai-direct/gpt-4o-mini", expected_revision=2, actor="test")
    assert (await tiers.get_snapshot()).revision == 2


@pytest.mark.asyncio
async def test_reasoning_dashboard_save_preview_cas_clear_and_restore(tiers, monkeypatch):
    from app import web_controls
    from app.process_policies import list_processes
    from app.runtime_settings.lifecycle import runtime_settings_scope
    from app.web import quart_app

    monkeypatch.setattr(web_controls, "set_value", tiers.set_value)
    monkeypatch.setattr(web_controls, "get_state", tiers.get_state)
    client = quart_app.test_client()
    async with client.session_transaction() as session:
        session.update(authenticated=True, controls_csrf="reasoning-csrf")
    headers = {"X-CSRF-Token": "reasoning-csrf"}
    value = {"models": [LUNA], "service_tier": "flex", "reasoning_effort": "low"}
    payload = {"section": "process", "id": "chat.complex", "value": value, "expected_revision": 0}
    assert (await client.post("/api/admin/controls", json=payload, headers=headers)).status_code == 200
    assert (await client.post("/api/admin/controls", json=payload, headers=headers)).status_code == 409
    preview = await client.post(
        "/api/admin/controls/preview", json={**payload, "expected_revision": 1}, headers=headers
    )
    assert (await preview.get_json())["reasoning_effort"] == "low"
    async with runtime_settings_scope(await tiers.get_snapshot()):
        row = next(row for row in await list_processes() if row["id"] == "chat.complex")
    assert row["reasoning_effort"] == "low" and row["reasoning_effort_explicit"]
    assert row["service_tiers"] == ["default", "flex"]
    await tiers.set_value(
        "process:chat.complex", {**value, "reasoning_effort": None}, expected_revision=1, actor="test"
    )
    restored = await tiers.restore_revision(
        1, expected_revision=2, actor="test", validate=web_controls._validate_restored_values
    )
    assert restored.values["process:chat.complex"]["reasoning_effort"] == "low"


@pytest.mark.asyncio
async def test_reasoning_changes_cached_result_identity():
    from app.runtime_settings.cache_identity import cache_identity
    from app.runtime_settings.lifecycle import runtime_settings_scope

    identities = []
    for effort in (None, "none", "low"):
        value = {"models": [LUNA], "reasoning_effort": effort}
        async with runtime_settings_scope(store.SettingsSnapshot(1, {"process:crocodile.judge": value})):
            identities.append(await cache_identity("crocodile.judge", "same-answer"))
    assert len(set(identities)) == 3


@pytest.mark.asyncio
async def test_completed_text_adapter_applies_reasoning_override(tiers):
    from app.process_policies import execute_text_process
    from app.providers.stream_types import (
        FinishReason,
        GroundingReport,
        ProviderKind,
        RouteUsed,
        StreamCompleted,
        TextDelta,
        TokenUsage,
    )
    from app.runtime_settings.lifecycle import runtime_settings_scope

    captured = []

    class Router:
        async def stream(self, request):
            captured.append(request)
            yield TextDelta("Synthetic")
            yield StreamCompleted(
                FinishReason.from_raw("stop"),
                TokenUsage(total=3),
                GroundingReport(),
                RouteUsed(ProviderKind.OPENAI, LUNA, LUNA),
            )

    snapshot = await tiers.set_value(
        "process:chat", {"models": [LUNA], "reasoning_effort": "none"}, expected_revision=0, actor="test"
    )
    async with runtime_settings_scope(snapshot):
        await execute_text_process(
            "chat", (LUNA,), [{"role": "user", "parts": ["Synthetic"]}], router=Router(), thinking_level="high"
        )
    assert captured[0].thinking_level is ThinkingLevel.OFF
