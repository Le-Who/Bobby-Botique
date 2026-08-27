from datetime import datetime, time, timezone

import pytest

from app.database import db_query
from app.repos.horoscope_subscriptions import (
    get_due_horoscope_subscriptions,
    get_horoscope_subscription,
    upsert_horoscope_subscription,
)


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
    params = calls[0][1]
    assert params[3] == time(7, 0)
    assert params[4] == time(21, 0)


@pytest.mark.asyncio
async def test_upsert_and_get_subscription(postgres_container):
    # This assumes db_conn fixture clears DB and gives us a clean slate
    user_id = 9999123

    # Needs to be in DB (users table) for foreign key if RLS/FK exist.
    # Usually we can insert a dummy user or tests disable FKs. We'll try just upsert.
    await db_query("INSERT INTO users (user_id) VALUES ($1) ON CONFLICT DO NOTHING", (user_id,))

    # 1. Upsert new
    await upsert_horoscope_subscription(
        user_id=user_id, sign="aries", time_today="09:00", time_tomorrow="20:00", utc_offset=3
    )

    # 2. Get
    sub = await get_horoscope_subscription(user_id)
    assert sub is not None
    assert sub["sign"] == "aries"
    assert sub["time_today"] == "09:00"
    assert sub["time_tomorrow"] == "20:00"
    assert sub["utc_offset"] == 3
    assert sub["is_active"] is True

    # 3. Upsert update (disable today)
    await upsert_horoscope_subscription(user_id=user_id, time_today=None)
    sub = await get_horoscope_subscription(user_id)
    assert sub["time_today"] is None
    assert sub["time_tomorrow"] == "20:00"  # untouched
    assert sub["sign"] == "aries"  # untouched


@pytest.mark.asyncio
async def test_get_due_subscriptions(postgres_container):
    user_id = 9999124
    await db_query("INSERT INTO users (user_id) VALUES ($1) ON CONFLICT DO NOTHING", (user_id,))

    await upsert_horoscope_subscription(
        user_id=user_id, sign="taurus", time_today="09:00", time_tomorrow="20:00", utc_offset=3
    )

    # If UTC now is 06:00, user time is 09:00 (due for today)
    # Target UTC hour = 6, minute = 0
    subs_today = await get_due_horoscope_subscriptions(6, 0, "today")
    assert any(s["user_id"] == user_id for s in subs_today)

    subs_tomorrow = await get_due_horoscope_subscriptions(17, 0, "tomorrow")
    assert any(s["user_id"] == user_id for s in subs_tomorrow)
