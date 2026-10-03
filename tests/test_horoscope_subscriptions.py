from datetime import time
from unittest.mock import AsyncMock

import pytest

from app.repos.horoscope_subscriptions import upsert_horoscope_subscription


@pytest.mark.asyncio
async def test_upsert_converts_hhmm_strings_to_time_params(monkeypatch):
    calls = []

    async def fake_db_query(query, params=(), *args, **kwargs):
        calls.append((query, params))
        return []

    monkeypatch.setattr("app.repos.horoscope_subscriptions.db_query", fake_db_query)

    ok = await upsert_horoscope_subscription(
        user_id=6913772015,
        sign="scorpio",
        time_today="07:00",
        time_tomorrow="21:00",
        utc_offset=3,
        is_active=True,
    )

    assert ok is True
    query = calls[0][0]
    params = calls[0][1]
    assert "(user_id, sign, time_today, time_tomorrow, utc_offset, is_active)" in " ".join(query.split())
    assert params == (6913772015, "scorpio", time(7, 0), time(21, 0), 3, True)


@pytest.mark.asyncio
async def test_partial_upsert_keeps_missing_fields_out_of_conflict_update(monkeypatch):
    calls = []

    async def fake_db_query(query, params=(), *args, **kwargs):
        calls.append((query, params))
        return []

    monkeypatch.setattr("app.repos.horoscope_subscriptions.db_query", fake_db_query)

    ok = await upsert_horoscope_subscription(user_id=42, time_today=None)

    assert ok is True
    query, params = calls[0]
    compact = " ".join(query.split())
    assert "INSERT INTO horoscope_subscriptions (user_id, sign, time_today)" in compact
    assert "time_today = EXCLUDED.time_today" in compact
    assert "time_tomorrow = EXCLUDED.time_tomorrow" not in compact
    assert params == (42, "aries", None)


@pytest.mark.asyncio
@pytest.mark.parametrize("delivery_time", ["25:00", "not-a-time"])
async def test_upsert_rejects_invalid_delivery_time_before_query(monkeypatch, delivery_time):
    query = AsyncMock()
    monkeypatch.setattr("app.repos.horoscope_subscriptions.db_query", query)

    with pytest.raises(ValueError):
        await upsert_horoscope_subscription(user_id=42, time_today=delivery_time)

    query.assert_not_awaited()
