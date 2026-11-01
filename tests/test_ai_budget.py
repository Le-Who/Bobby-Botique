from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.errors import classify_key_error, extract_retry_after_seconds
from app.games import ai_budget
from app.games.ai_budget import (
    acquire_background_slot,
    acquire_foreground_slot,
    get_model_cooldown,
    record_result,
    reset_budget_state_for_tests,
)


@pytest.mark.asyncio
async def test_retry_after_quota_text_maps_to_rate_limit_cooldown():
    reset_budget_state_for_tests()
    message = (
        "429 RESOURCE_EXHAUSTED. Quota exceeded for metric generate_content_free_tier_requests. "
        "Please retry in 56.308516905s."
    )

    assert extract_retry_after_seconds(message) == 57
    assert classify_key_error(message) == "rate_limit"

    await record_result("ai_studio", "gemini-3.5-flash", "rate_limit", retry_after_seconds=57, reason=message)
    cooldown = get_model_cooldown("ai_studio", "gemini-3.5-flash")

    assert cooldown is not None
    assert cooldown.last_retry_after_seconds == 57


@pytest.mark.asyncio
async def test_background_ai_studio_slot_blocked_during_model_cooldown():
    reset_budget_state_for_tests()
    await record_result("ai_studio", "gemini-3.1-flash-lite", "rate_limit", retry_after_seconds=60)

    lease = await acquire_background_slot("hint_generation", "ai_studio", "gemini-3.1-flash-lite")
    assert lease is None


@pytest.mark.asyncio
async def test_foreground_ai_studio_slot_allows_budgeted_request():
    reset_budget_state_for_tests()

    lease = await acquire_foreground_slot("hint_generation", "ai_studio", "gemini-3.1-flash-lite")
    assert lease is not None
    await lease.release()


@pytest.mark.asyncio
async def test_redis_failure_does_not_admit_from_a_second_local_budget(monkeypatch, caplog):
    fail = AsyncMock(side_effect=ConnectionError("quota reply lost"))
    monkeypatch.setattr(
        ai_budget, "redis_client", SimpleNamespace(zremrangebyscore=fail, register_script=lambda _: fail)
    )
    manager = ai_budget._BudgetManager()

    lease = await manager.acquire("foreground", "judge", "ai_studio", "gemini-3.5-flash")

    assert lease is None
    assert not any(manager._windows.values())
    assert "unavailable" in caplog.text.lower()


@pytest.mark.asyncio
async def test_local_rpm_admission_remains_bounded_under_concurrency(monkeypatch):
    monkeypatch.setattr(ai_budget, "redis_client", None)
    manager = ai_budget._BudgetManager()
    admitted = await asyncio.gather(
        *(manager._try_consume_window("foreground", "judge", "ai_studio", "model", 1) for _ in range(20))
    )
    assert sum(admitted) == 1
