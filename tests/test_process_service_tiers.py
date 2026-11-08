"""Service tiers are explicit process policy, separate from model identity."""

from unittest.mock import AsyncMock

import pytest

from app.process_policies import resolve_policy_value, validate_policy
from app.runtime_settings import store
from tests.test_runtime_settings_store import Database

LUNA = "openai-direct/gpt-6-luna"


def test_legacy_policy_shape_and_default_tier_are_preserved():
    value = {"models": [LUNA], "strategy": "sequential", "inherit_user_model": False}
    assert validate_policy("chat", value) == value
    assert resolve_policy_value("chat", value, ()).service_tier.value == "default"
    assert resolve_policy_value("chat", None, ("gemini-test",)).service_tier.value == "default"


@pytest.mark.parametrize("process", ["chat", "chat.complex", "search"])
def test_supported_live_process_can_choose_flex_independently_of_model(process):
    value = validate_policy(process, {"models": [LUNA], "service_tier": "flex"})
    assert value["models"] == [LUNA]
    assert value["service_tier"] == "flex"
    assert resolve_policy_value(process, value, ()).service_tier.value == "flex"


@pytest.mark.parametrize(
    "process,models,tier",
    [
        ("inline", [LUNA], "flex"),
        ("crocodile.judge", [LUNA], "flex"),
        ("chat", ["gemini-test"], "flex"),
        ("chat", ["openai-direct/gpt-4.1-mini"], "flex"),
        ("search", [LUNA, "gemini-test"], "flex"),
        ("chat", [LUNA], "priority"),
        ("chat", [LUNA], None),
    ],
)
def test_unsupported_tier_combination_is_rejected_without_silent_downgrade(process, models, tier):
    with pytest.raises(ValueError):
        validate_policy(process, {"models": models, "service_tier": tier})


@pytest.fixture
def tiers(monkeypatch):
    database = Database()
    backend = store.RuntimeSettingsStore()
    monkeypatch.setattr(store.db.db_manager, "pool", database)
    for name in ("get_snapshot", "set_value", "reset_value", "update_values"):
        monkeypatch.setattr(store, name, getattr(backend, name))
    monkeypatch.setattr("app.process_policies.baseline_models", AsyncMock(return_value=("gemini-baseline",)))
    return backend


@pytest.mark.asyncio
async def test_profile_defaults_and_roundtrip_keep_live_answer_tiers(tiers):
    from app.runtime_settings import live_routes

    openai = await live_routes.switch_provider("openai", expected_revision=0, actor="test")
    for process in live_routes.LIVE_PROCESS_IDS:
        raw = openai.values[f"process:{process}"]
        policy = resolve_policy_value(process, raw, ())
        if process in {"chat", "search"}:
            assert policy.models == (LUNA,)
            assert policy.service_tier.value == "flex"
        elif process == "chat.complex":
            assert policy.models == ("openai-direct/gpt-6.1-terra",)
            assert policy.service_tier.value == "flex"
            assert policy.reasoning_effort.value == "low"
        else:
            assert policy.service_tier.value == "default"
    await live_routes.switch_provider("gemini", expected_revision=1, actor="test")
    restored = await live_routes.switch_provider("openai", expected_revision=2, actor="test")
    assert restored.values["process:chat"]["service_tier"] == "flex"


@pytest.mark.asyncio
async def test_primary_writer_preserves_flex_and_rejects_incompatible_model(tiers):
    from app.runtime_settings.processes import save_primary_model_at_revision

    await tiers.set_value("process:chat", {"models": [LUNA], "service_tier": "flex"}, expected_revision=0, actor="test")
    saved = await save_primary_model_at_revision(
        "chat", "openai-direct/gpt-6.1-terra", expected_revision=1, actor="test"
    )
    assert saved.values["process:chat"]["service_tier"] == "flex"
    with pytest.raises(ValueError):
        await save_primary_model_at_revision("chat", "openai-direct/gpt-4o-mini", expected_revision=2, actor="test")
    assert (await tiers.get_snapshot()).revision == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("process_id", ["chat", "search"])
async def test_dashboard_save_preview_conflict_and_restore_include_tier(tiers, monkeypatch, process_id):
    from app import web_controls
    from app.web import quart_app

    monkeypatch.setattr(web_controls, "set_value", tiers.set_value)
    monkeypatch.setattr(web_controls, "get_state", tiers.get_state)
    client = quart_app.test_client()
    async with client.session_transaction() as session:
        session.update(authenticated=True, controls_csrf="tier-csrf")
    headers = {"X-CSRF-Token": "tier-csrf"}
    value = {"models": [LUNA], "service_tier": "flex", "inherit_user_model": False}
    payload = {"section": "process", "id": process_id, "value": value, "expected_revision": 0}
    saved = await client.post("/api/admin/controls", json=payload, headers=headers)
    assert saved.status_code == 200
    assert (await tiers.get_snapshot()).values[f"process:{process_id}"]["service_tier"] == "flex"
    assert (await client.post("/api/admin/controls", json=payload, headers=headers)).status_code == 409
    preview = await client.post(
        "/api/admin/controls/preview", json={**payload, "expected_revision": 1}, headers=headers
    )
    assert preview.status_code == 200
    assert (await preview.get_json())["service_tier"] == "flex"
    invalid = {**payload, "expected_revision": 1, "value": {**value, "models": ["openai-direct/gpt-4o-mini"]}}
    assert (await client.post("/api/admin/controls", json=invalid, headers=headers)).status_code == 400
    assert (await tiers.get_snapshot()).revision == 1
    await tiers.set_value(
        f"process:{process_id}", {**value, "service_tier": "default"}, expected_revision=1, actor="test"
    )
    restored = await tiers.restore_revision(
        1, expected_revision=2, actor="test", validate=web_controls._validate_restored_values
    )
    assert restored.values[f"process:{process_id}"]["service_tier"] == "flex"


@pytest.mark.asyncio
async def test_tier_change_separates_cached_results():
    from app.runtime_settings.cache_identity import cache_identity
    from app.runtime_settings.lifecycle import runtime_settings_scope

    value = {"models": [LUNA], "strategy": "sequential", "inherit_user_model": False}
    async with runtime_settings_scope(
        store.SettingsSnapshot(1, {"process:chat": {**value, "service_tier": "default"}})
    ):
        standard = await cache_identity("chat", "same-user-query")
    async with runtime_settings_scope(store.SettingsSnapshot(2, {"process:chat": {**value, "service_tier": "flex"}})):
        flex = await cache_identity("chat", "same-user-query")
    assert flex != standard


@pytest.mark.asyncio
@pytest.mark.parametrize("process_id", ["chat", "search"])
@pytest.mark.parametrize("thinking", [None, "low"])
async def test_actual_factory_and_sdk_payload_keep_tier_without_changing_thinking(tiers, process_id, thinking):
    from app.providers.openai import responses_payload
    from app.providers.request_factory import generation_request_from_history
    from app.runtime_settings.lifecycle import runtime_settings_scope

    snapshot = await tiers.set_value(
        f"process:{process_id}", {"models": [LUNA], "service_tier": "flex"}, expected_revision=0, actor="test"
    )
    async with runtime_settings_scope(snapshot):
        request = await generation_request_from_history(
            process_id=process_id,
            models=("gemini-stale-user-choice",),
            history=[{"role": "user", "parts": ["Synthetic check"]}],
            thinking_level=thinking,
        )
    assert request.models == (LUNA,)
    assert request.service_tier.value == "flex"
    assert request.first_text_timeout_seconds == 15.0
    assert request.allow_model_fallback is False
    payload = await responses_payload(request, LUNA)
    assert payload["service_tier"] == "flex"
    assert payload["reasoning"]["effort"] == (thinking or "none")


@pytest.mark.asyncio
async def test_completed_text_adapter_keeps_explicit_tier(tiers):
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
            yield TextDelta("Synthetic answer")
            yield StreamCompleted(
                FinishReason.from_raw("stop"),
                TokenUsage(total=3),
                GroundingReport(),
                RouteUsed(ProviderKind.OPENAI, LUNA, LUNA),
            )

    snapshot = await tiers.set_value(
        "process:chat", {"models": [LUNA], "service_tier": "flex"}, expected_revision=0, actor="test"
    )
    async with runtime_settings_scope(snapshot):
        text, tokens = await execute_text_process(
            "chat", ("gemini-stale",), [{"role": "user", "parts": ["Synthetic"]}], router=Router()
        )
    assert (text, tokens) == ("Synthetic answer", 3)
    assert captured[0].service_tier.value == "flex"
