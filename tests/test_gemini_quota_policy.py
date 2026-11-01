"""Regression coverage for internal request bursts and provider quota evidence."""

import asyncio
from collections import Counter
from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest
from google.genai.errors import APIError

from app.errors import (
    ErrorCode,
    classify_error_from_exception,
    classify_key_error,
    extract_error_code,
    user_friendly_error,
)
from app.repos import keys


@pytest.mark.parametrize(
    ("message", "category", "code"),
    [
        ("429 RESOURCE_EXHAUSTED: Quota exceeded for generate_requests_per_minute", "rate_limit", ErrorCode.RATE_LIMIT),
        ("429 Quota exceeded for GenerateContentInputTokensPerModelPerMinute", "rate_limit", ErrorCode.RATE_LIMIT),
        ("429 RESOURCE_EXHAUSTED: quota exceeded", "rate_limit", ErrorCode.RATE_LIMIT),
        ("Quota exceeded", "rate_limit", ErrorCode.RATE_LIMIT),
        ("Rate limit exceeded", "rate_limit", ErrorCode.RATE_LIMIT),
        ("Quota exceeded for GenerateRequestsPerDayPerProjectPerModel-FreeTier", "quota", ErrorCode.QUOTA_EXCEEDED),
        ("Quota exceeded for generate_requests_per_model_per_day; retryDelay: 5s", "quota", ErrorCode.QUOTA_EXCEEDED),
        ("Daily limit exceeded", "quota", ErrorCode.QUOTA_EXCEEDED),
    ],
)
def test_quota_category_requires_daily_evidence(message, category, code):
    assert classify_key_error(message) == category
    assert classify_error_from_exception(APIError(429, {"error": {"message": message}})) is code
    assert extract_error_code(user_friendly_error(message)) is code


@pytest.mark.parametrize("category", ["transient", "rate_limit"])
@pytest.mark.parametrize("failures", [1, 3, 8, 100_000])
def test_temporary_failures_never_disable_a_key_for_minutes(monkeypatch, category, failures):
    now = datetime(2026, 11, 1, 12, tzinfo=UTC)

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return now.astimezone(tz)

    monkeypatch.setattr(keys, "datetime", Clock)
    delay = (keys._compute_suspended_until(category, failures) - now).total_seconds()
    assert 1 <= delay <= 60


@pytest.mark.asyncio
@pytest.mark.parametrize("limit", [None, 1000])
async def test_concurrent_internal_selections_spread_across_healthy_keys(monkeypatch, limit):
    manager = keys.DailyKeyManager("api_keys", "key_usage")
    # All concurrent reads can see the same daily counts before reservations commit.
    rows = [{"key_hash": f"h{i}", "api_key": f"fake-{i}", "request_count": 0} for i in range(4)]
    monkeypatch.setattr(keys, "db_query", AsyncMock(return_value=rows))
    monkeypatch.setattr(keys, "safe_decrypt", lambda value: value)

    selected = await asyncio.gather(*(manager.get_fresh_available_key("gemini-test", limit) for _ in range(16)))

    assert Counter(key["key_hash"] for key in selected) == {"h0": 4, "h1": 4, "h2": 4, "h3": 4}


@pytest.mark.asyncio
async def test_daily_usage_does_not_keep_hammering_one_previously_idle_key(monkeypatch):
    manager = keys.DailyKeyManager("api_keys", "key_usage")
    rows = [
        {"key_hash": "idle", "api_key": "fake-idle", "request_count": 0},
        {"key_hash": "busy-earlier", "api_key": "fake-busy", "request_count": 500},
    ]
    monkeypatch.setattr(keys, "db_query", AsyncMock(return_value=rows))
    monkeypatch.setattr(keys, "safe_decrypt", lambda value: value)
    selected = [await manager.get_fresh_available_key("gemini-test", None) for _ in range(4)]
    assert [key["key_hash"] for key in selected] == ["idle", "busy-earlier", "idle", "busy-earlier"]


@pytest.mark.asyncio
async def test_provider_retry_delay_is_honoured(monkeypatch):
    now = datetime.now(UTC)
    query = AsyncMock(side_effect=[[{"failure_count": 0}], []])
    monkeypatch.setattr(keys, "db_query", query)
    monkeypatch.setattr(keys, "invalidate_key_cache", AsyncMock())
    await keys.KeyStatusManager().suspend_key("fake-hash", "gemini-test", "rate_limit", "retryDelay: 47s")
    until = query.await_args_list[-1].args[1][2]
    assert 47 <= (until - now).total_seconds() < 49


@pytest.mark.asyncio
async def test_confirmed_daily_exhaustion_is_not_deduplicated_as_transient(monkeypatch):
    query = AsyncMock(side_effect=[[{"failure_count": 0}], [], [{"failure_count": 1}], []])
    monkeypatch.setattr(keys, "db_query", query)
    monkeypatch.setattr(keys, "invalidate_key_cache", AsyncMock())
    manager = keys.KeyStatusManager()
    await manager.suspend_key("fake-hash", "gemini-test", "transient", "timeout")
    await manager.suspend_key("fake-hash", "gemini-test", "quota", "daily quota exhausted")
    assert query.await_count == 4
    assert query.await_args.args[1][2] > datetime.now(UTC)
    # An older in-flight failure must not shorten a stronger active cooldown.
    assert "GREATEST(key_model_status.suspended_until, $3)" in query.await_args.args[0]


@pytest.mark.asyncio
async def test_late_success_cannot_clear_a_newer_active_cooldown(monkeypatch):
    query = AsyncMock(return_value=[])
    monkeypatch.setattr(keys, "db_query", query)
    await keys.KeyStatusManager().record_success("fake-hash", "gemini-test")
    sql, params = query.await_args.args
    assert "WHERE key_model_status.status = 'active'" in sql
    assert "key_model_status.suspended_until <= NOW()" in sql
    assert params == ("fake-hash", "gemini-test")


@pytest.mark.asyncio
async def test_stronger_provider_retry_delay_is_not_lost_to_deduplication(monkeypatch):
    now = datetime.now(UTC)
    query = AsyncMock(side_effect=[[{"failure_count": 0}], [], [{"failure_count": 1}], []])
    monkeypatch.setattr(keys, "db_query", query)
    monkeypatch.setattr(keys, "invalidate_key_cache", AsyncMock())
    manager = keys.KeyStatusManager()
    await manager.suspend_key("fake-hash", "gemini-test", "rate_limit", retry_after_seconds=15)
    await manager.suspend_key("fake-hash", "gemini-test", "rate_limit", retry_after_seconds=120)
    assert query.await_count == 4
    assert (query.await_args.args[1][2] - now).total_seconds() >= 120
