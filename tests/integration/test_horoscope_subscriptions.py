"""Exercise the real subscription repository in the rollback-owned test DB."""

from datetime import time

import pytest

from app.repos.horoscope_subscriptions import (
    get_due_horoscope_subscriptions,
    get_horoscope_subscription,
    mark_horoscope_sent,
    upsert_horoscope_subscription,
)

pytestmark = [pytest.mark.integration, pytest.mark.asyncio, pytest.mark.usefixtures("force_test_db_conn")]


async def test_upsert_and_get_subscription(db_conn):
    user_id = 9999123
    await db_conn.execute("INSERT INTO users (user_id) VALUES ($1) ON CONFLICT DO NOTHING", user_id)
    await db_conn.execute("DELETE FROM horoscope_subscriptions WHERE user_id = $1", user_id)

    assert await upsert_horoscope_subscription(
        user_id=user_id, sign="aries", time_today="09:00", time_tomorrow="20:00", utc_offset=3
    )

    sub = await get_horoscope_subscription(user_id)
    assert sub is not None
    assert sub["sign"] == "aries"
    assert sub["time_today"] == time(9, 0)
    assert sub["time_tomorrow"] == time(20, 0)
    assert sub["utc_offset"] == 3
    assert sub["is_active"] is True

    assert await upsert_horoscope_subscription(user_id=user_id, time_today=None)
    sub = await get_horoscope_subscription(user_id)
    assert sub is not None
    assert sub["time_today"] is None
    assert sub["time_tomorrow"] == time(20, 0)
    assert sub["sign"] == "aries"
    assert sub["utc_offset"] == 3
    assert sub["is_active"] is True


async def test_get_due_subscriptions(db_conn):
    user_id = 9999124
    await db_conn.execute("INSERT INTO users (user_id) VALUES ($1) ON CONFLICT DO NOTHING", user_id)
    await db_conn.execute("DELETE FROM horoscope_subscriptions WHERE user_id = $1", user_id)
    assert await upsert_horoscope_subscription(
        user_id=user_id, sign="taurus", time_today="09:00", time_tomorrow="20:00", utc_offset=3
    )

    before_today = await get_due_horoscope_subscriptions(5, 59, "today")
    assert user_id not in {sub["user_id"] for sub in before_today}
    at_today = await get_due_horoscope_subscriptions(6, 0, "today")
    assert user_id in {sub["user_id"] for sub in at_today}
    after_today = await get_due_horoscope_subscriptions(6, 15, "today")
    assert user_id in {sub["user_id"] for sub in after_today}
    at_tomorrow = await get_due_horoscope_subscriptions(17, 0, "tomorrow")
    assert user_id in {sub["user_id"] for sub in at_tomorrow}

    await mark_horoscope_sent(user_id, "today")
    sent_today = await get_due_horoscope_subscriptions(6, 15, "today")
    assert user_id not in {sub["user_id"] for sub in sent_today}
    unsent_tomorrow = await get_due_horoscope_subscriptions(17, 0, "tomorrow")
    assert user_id in {sub["user_id"] for sub in unsent_tomorrow}

    assert await upsert_horoscope_subscription(user_id=user_id, is_active=False)
    inactive_tomorrow = await get_due_horoscope_subscriptions(17, 0, "tomorrow")
    assert user_id not in {sub["user_id"] for sub in inactive_tomorrow}
