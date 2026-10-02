"""Explicit model plans for specialized direct Gemini SDK workloads."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.process_policies import ResolvedPolicy


@pytest.fixture(autouse=True)
def key_health(monkeypatch):
    manager = SimpleNamespace(record_success=AsyncMock(), suspend_key=AsyncMock())
    monkeypatch.setattr("app.repos.keys.get_key_status_manager", lambda: manager)
    return manager


@pytest.mark.asyncio
async def test_explicit_plan_tries_only_configured_models_and_reserves_once_per_attempt(monkeypatch):
    from app.runtime_settings import gemini_execution

    monkeypatch.setattr(
        gemini_execution,
        "resolve_process",
        AsyncMock(return_value=ResolvedPolicy(("gemini-first", "gemini-second"), "sequential", True, 7)),
    )
    selected = AsyncMock(return_value={"api_key": "second-key", "key_hash": "second-hash"})
    reserved = AsyncMock(return_value=True)
    monkeypatch.setattr("app.repos.keys.get_available_gemini_key", selected)
    monkeypatch.setattr("app.repos.keys.reserve_gemini_key_usage", reserved)
    seen = []

    async def execute(model, api_key):
        seen.append((model, api_key))
        if model == "gemini-first":
            raise ValueError("invalid JSON")
        return {"facts": ["checked"]}

    result = await gemini_execution.run_gemini_override(
        "memory.extract", ("gemini-baseline",), execute, initial_api_key="first-key"
    )
    assert result == {"facts": ["checked"]}
    assert seen == [("gemini-first", "first-key"), ("gemini-second", "second-key")]
    assert reserved.await_count == 2
    assert selected.await_count == 1


@pytest.mark.asyncio
async def test_absent_override_leaves_legacy_execution_to_caller(monkeypatch):
    from app.runtime_settings import gemini_execution

    monkeypatch.setattr(
        gemini_execution,
        "resolve_process",
        AsyncMock(return_value=ResolvedPolicy(("gemini-baseline",), "legacy", False, 0)),
    )
    execute = AsyncMock()
    result = await gemini_execution.run_gemini_override("memory.expand", ("gemini-baseline",), execute)
    assert result is None
    execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_explicit_empty_result_does_not_signal_legacy_fallback(monkeypatch):
    from app.runtime_settings import gemini_execution

    monkeypatch.setattr(
        gemini_execution,
        "resolve_process",
        AsyncMock(return_value=ResolvedPolicy(("gemini-chosen",), "sequential", True, 1)),
    )
    monkeypatch.setattr(
        "app.repos.keys.get_available_gemini_key",
        AsyncMock(return_value={"api_key": "test-key", "key_hash": "test-hash"}),
    )
    monkeypatch.setattr("app.repos.keys.reserve_gemini_key_usage", AsyncMock(return_value=True))
    with pytest.raises(ValueError, match="empty result"):
        await gemini_execution.run_gemini_override("memory.extract", ("gemini-legacy",), AsyncMock(return_value=None))


@pytest.mark.asyncio
async def test_reservation_race_rotates_keys_before_changing_model(monkeypatch):
    from app.runtime_settings import gemini_execution

    monkeypatch.setattr(
        gemini_execution,
        "resolve_process",
        AsyncMock(return_value=ResolvedPolicy(("gemini-chosen", "gemini-second"), "sequential", True, 1)),
    )
    selections = []

    async def select(model, excluded_hashes=None):
        selections.append((model, set(excluded_hashes or ())))
        return {"api_key": "second-key", "key_hash": "second-hash"}

    reservations = []

    async def reserve(key_hash, model):
        reservations.append((key_hash, model))
        return key_hash == "second-hash"

    monkeypatch.setattr("app.repos.keys.get_available_gemini_key", select)
    monkeypatch.setattr("app.repos.keys.reserve_gemini_key_usage", reserve)
    attempts = []

    async def execute(model, key):
        attempts.append((model, key))
        return "checked"

    assert (
        await gemini_execution.run_gemini_override(
            "memory.extract", ("gemini-baseline",), execute, initial_api_key="first-key"
        )
        == "checked"
    )
    assert attempts == [("gemini-chosen", "second-key")]
    assert len(reservations) == 2
    assert selections[0][0] == "gemini-chosen"
    assert reservations[0][0] in selections[0][1]


@pytest.mark.asyncio
async def test_quota_failure_rotates_and_records_safe_health(monkeypatch, key_health):
    from app.runtime_settings import gemini_execution

    monkeypatch.setattr(
        gemini_execution,
        "resolve_process",
        AsyncMock(return_value=ResolvedPolicy(("gemini-chosen",), "sequential", True, 1)),
    )
    selected = AsyncMock(
        side_effect=[
            {"api_key": "first-key", "key_hash": "first-hash"},
            {"api_key": "second-key", "key_hash": "second-hash"},
        ]
    )
    monkeypatch.setattr("app.repos.keys.get_available_gemini_key", selected)
    reserved = AsyncMock(return_value=True)
    monkeypatch.setattr("app.repos.keys.reserve_gemini_key_usage", reserved)
    attempts = []

    async def execute(model, key):
        attempts.append((model, key))
        if key == "first-key":
            raise RuntimeError("quota exceeded; sensitive prompt body and first-key")
        return {"result": "checked"}

    assert await gemini_execution.run_gemini_override("memory.extract", (), execute) == {"result": "checked"}
    assert attempts == [("gemini-chosen", "first-key"), ("gemini-chosen", "second-key")]
    assert reserved.await_count == 2
    assert selected.await_args.kwargs["excluded_hashes"] == {"first-hash"}
    key_health.suspend_key.assert_awaited_once_with("first-hash", "gemini-chosen", "quota", "RuntimeError")
    key_health.record_success.assert_awaited_once_with("second-hash", "gemini-chosen")


@pytest.mark.asyncio
async def test_parse_failure_preserves_model_chain_without_penalizing_key(monkeypatch, key_health):
    from app.runtime_settings import gemini_execution

    monkeypatch.setattr(
        gemini_execution,
        "resolve_process",
        AsyncMock(return_value=ResolvedPolicy(("gemini-first", "gemini-second"), "sequential", True, 1)),
    )
    monkeypatch.setattr(
        "app.repos.keys.get_available_gemini_key",
        AsyncMock(return_value={"api_key": "test-key", "key_hash": "test-hash"}),
    )
    monkeypatch.setattr("app.repos.keys.reserve_gemini_key_usage", AsyncMock(return_value=True))
    attempts = []

    async def execute(model, key):
        attempts.append(model)
        if model == "gemini-first":
            raise ValueError("invalid JSON includes a quota field")
        return "checked"

    assert await gemini_execution.run_gemini_override("memory.extract", (), execute) == "checked"
    assert attempts == ["gemini-first", "gemini-second"]
    key_health.suspend_key.assert_not_awaited()


@pytest.mark.asyncio
async def test_cancellation_stops_model_chain_and_health_writes(monkeypatch, key_health):
    from app.runtime_settings import gemini_execution

    monkeypatch.setattr(
        gemini_execution,
        "resolve_process",
        AsyncMock(return_value=ResolvedPolicy(("gemini-first", "gemini-second"), "sequential", True, 1)),
    )
    monkeypatch.setattr(
        "app.repos.keys.get_available_gemini_key",
        AsyncMock(return_value={"api_key": "test-key", "key_hash": "test-hash"}),
    )
    monkeypatch.setattr("app.repos.keys.reserve_gemini_key_usage", AsyncMock(return_value=True))
    attempts = []

    async def execute(model, key):
        attempts.append(model)
        raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await gemini_execution.run_gemini_override("memory.extract", (), execute)
    assert attempts == ["gemini-first"]
    key_health.suspend_key.assert_not_awaited()
    key_health.record_success.assert_not_awaited()


@pytest.mark.asyncio
async def test_total_deadline_cancels_sdk_without_trying_reserve_or_penalizing_key(monkeypatch, key_health):
    from app.runtime_settings import gemini_execution

    monkeypatch.setattr(
        gemini_execution,
        "resolve_process",
        AsyncMock(return_value=ResolvedPolicy(("gemini-first", "gemini-second"), "sequential", True, 1)),
    )
    monkeypatch.setattr(
        "app.repos.keys.get_available_gemini_key",
        AsyncMock(return_value={"api_key": "test-key", "key_hash": "test-hash"}),
    )
    reservation = AsyncMock(return_value=True)
    monkeypatch.setattr("app.repos.keys.reserve_gemini_key_usage", reservation)
    stopped = asyncio.Event()

    async def execute(model, key):
        try:
            await asyncio.Event().wait()
        finally:
            stopped.set()

    with pytest.raises(TimeoutError):
        await gemini_execution.run_gemini_override("memory.extract", (), execute, timeout=0.01)
    assert stopped.is_set()
    reservation.assert_awaited_once()
    key_health.suspend_key.assert_not_awaited()
