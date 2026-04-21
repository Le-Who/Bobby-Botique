from __future__ import annotations

import pytest

from app.errors import classify_key_error, extract_retry_after_seconds
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

    await record_result("ai_studio", "gemini-3-flash-preview", "rate_limit", retry_after_seconds=57, reason=message)
    cooldown = get_model_cooldown("ai_studio", "gemini-3-flash-preview")

    assert cooldown is not None
    assert cooldown.last_retry_after_seconds == 57


@pytest.mark.asyncio
async def test_background_ai_studio_slot_blocked_during_model_cooldown():
    reset_budget_state_for_tests()
    await record_result("ai_studio", "gemini-3.1-flash-lite-preview", "rate_limit", retry_after_seconds=60)

    lease = await acquire_background_slot("hint_generation", "ai_studio", "gemini-3.1-flash-lite-preview")
    assert lease is None


@pytest.mark.asyncio
async def test_foreground_ai_studio_slot_allows_budgeted_request():
    reset_budget_state_for_tests()

    lease = await acquire_foreground_slot("hint_generation", "ai_studio", "gemini-3.1-flash-lite-preview")
    assert lease is not None
    await lease.release()
