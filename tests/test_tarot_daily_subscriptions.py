"""Subscription writes apply preferences to both new and existing rows."""

import sqlite3
from datetime import UTC, date, datetime
from unittest.mock import AsyncMock

import pytest

from app.repos import tarot_daily_subscriptions as repo


@pytest.fixture
def subscriptions(monkeypatch):
    connection = sqlite3.connect(":memory:")
    connection.execute("ATTACH DATABASE ':memory:' AS public")
    connection.create_function("NOW", 0, lambda: "2026-10-23T00:00:00Z")
    connection.execute(
        """
        CREATE TABLE public.tarot_daily_subscriptions (
            user_id INTEGER PRIMARY KEY,
            is_subscribed BOOLEAN NOT NULL DEFAULT FALSE,
            timezone TEXT NOT NULL DEFAULT 'Europe/Kyiv',
            preferred_local_hour INTEGER NOT NULL DEFAULT 10,
            updated_at TEXT
        )
        """
    )

    async def query(sql, params=()):
        connection.execute(sql, {str(index): value for index, value in enumerate(params, start=1)})
        return []

    monkeypatch.setattr(repo, "db_query", query)
    yield connection
    connection.close()


@pytest.mark.asyncio
async def test_first_subscription_persists_enabled_delivery(subscriptions):
    assert await repo.upsert_tarot_subscription(42, is_subscribed=True)

    row = subscriptions.execute(
        "SELECT is_subscribed, timezone, preferred_local_hour FROM public.tarot_daily_subscriptions WHERE user_id = 42"
    ).fetchone()
    assert row == (1, "Europe/Kyiv", 10)


@pytest.mark.asyncio
async def test_new_subscription_persists_custom_preferences(subscriptions):
    assert await repo.upsert_tarot_subscription(42, timezone="Asia/Kolkata", preferred_local_hour=0)

    row = subscriptions.execute(
        "SELECT is_subscribed, timezone, preferred_local_hour FROM public.tarot_daily_subscriptions WHERE user_id = 42"
    ).fetchone()
    assert row == (0, "Asia/Kolkata", 0)


@pytest.mark.asyncio
async def test_partial_subscription_update_keeps_existing_preferences(subscriptions):
    subscriptions.execute(
        """
        INSERT INTO public.tarot_daily_subscriptions (user_id, is_subscribed, timezone, preferred_local_hour)
        VALUES (42, TRUE, 'Asia/Kolkata', 0)
        """
    )

    assert await repo.upsert_tarot_subscription(42, is_subscribed=False)

    row = subscriptions.execute(
        "SELECT is_subscribed, timezone, preferred_local_hour FROM public.tarot_daily_subscriptions WHERE user_id = 42"
    ).fetchone()
    assert row == (0, "Asia/Kolkata", 0)


@pytest.mark.asyncio
async def test_ensure_subscription_preserves_existing_preferences(subscriptions):
    assert await repo.upsert_tarot_subscription(42)
    subscriptions.execute("UPDATE public.tarot_daily_subscriptions SET is_subscribed = TRUE WHERE user_id = 42")

    assert await repo.upsert_tarot_subscription(42)

    row = subscriptions.execute(
        "SELECT is_subscribed FROM public.tarot_daily_subscriptions WHERE user_id = 42"
    ).fetchone()
    assert row == (1,)


@pytest.mark.parametrize(
    ("now", "timezone", "hour", "last_sent", "slot_date", "reason"),
    [
        (datetime(2026, 11, 9, 7, 59, tzinfo=UTC), "Europe/Kyiv", 10, None, date(2026, 11, 9), "slot_not_due"),
        (datetime(2026, 11, 9, 8, tzinfo=UTC), "Europe/Kyiv", 10, None, date(2026, 11, 9), None),
        (datetime(2026, 11, 9, 20, tzinfo=UTC), "Europe/Kyiv", 10, None, date(2026, 11, 9), None),
        (
            datetime(2026, 11, 9, 8, tzinfo=UTC),
            "Europe/Kyiv",
            10,
            date(2026, 11, 9),
            date(2026, 11, 9),
            "already_delivered",
        ),
        (
            datetime(2026, 11, 9, 8, tzinfo=UTC),
            "Europe/Kyiv",
            10,
            date(2026, 11, 10),
            date(2026, 11, 9),
            "already_delivered",
        ),
        (datetime(2026, 11, 9, 12, tzinfo=UTC), "Pacific/Kiritimati", 0, date(2026, 11, 9), date(2026, 11, 10), None),
        (
            datetime(2026, 11, 9, 1, tzinfo=UTC),
            "America/Los_Angeles",
            10,
            date(2026, 11, 8),
            date(2026, 11, 8),
            "already_delivered",
        ),
        (datetime(2026, 3, 29, 7, tzinfo=UTC), "Europe/Kyiv", 10, None, date(2026, 3, 29), None),
        (datetime(2026, 10, 25, 7, tzinfo=UTC), "Europe/Kyiv", 10, None, date(2026, 10, 25), "slot_not_due"),
        (datetime(2026, 11, 9, 8), "Europe/Kyiv", 10, date(2026, 11, 8), date(2026, 11, 9), None),
    ],
)
def test_delivery_slot_uses_local_calendar_and_catches_up(now, timezone, hour, last_sent, slot_date, reason):
    slot = repo.resolve_delivery_slot(
        {"timezone": timezone, "preferred_local_hour": hour, "last_sent_date": last_sent}, now
    )

    assert slot.slot_date == slot_date
    assert slot.reason_code == reason


@pytest.mark.parametrize(
    "schedule",
    [
        {"timezone": "Invalid/Timezone", "preferred_local_hour": 10},
        {"timezone": "", "preferred_local_hour": 10},
        {"timezone": None, "preferred_local_hour": 10},
        {"timezone": "Europe/Kyiv", "preferred_local_hour": -1},
        {"timezone": "Europe/Kyiv", "preferred_local_hour": 24},
        {"timezone": "Europe/Kyiv", "preferred_local_hour": "10"},
        {"timezone": "Europe/Kyiv", "preferred_local_hour": True},
        {"timezone": "Europe/Kyiv", "preferred_local_hour": 10, "last_sent_date": "2026-11-09"},
    ],
)
def test_delivery_slot_rejects_invalid_schedule(schedule):
    with pytest.raises(ValueError):
        repo.resolve_delivery_slot(schedule, datetime(2026, 11, 9, 12, tzinfo=UTC))


def test_delivery_fingerprint_tracks_preferences_without_changing_after_receipt():
    now = datetime(2026, 11, 9, 12, tzinfo=UTC)
    schedule = {"timezone": "Europe/Kyiv", "preferred_local_hour": 10}
    original = repo.resolve_delivery_slot(schedule, now)
    delivered = repo.resolve_delivery_slot({**schedule, "last_sent_date": original.slot_date}, now)
    moved_hour = repo.resolve_delivery_slot({**schedule, "preferred_local_hour": 11}, now)
    moved_timezone = repo.resolve_delivery_slot({**schedule, "timezone": "UTC"}, now)

    assert delivered.subscription_fingerprint == original.subscription_fingerprint
    assert moved_hour.subscription_fingerprint != original.subscription_fingerprint
    assert moved_timezone.subscription_fingerprint != original.subscription_fingerprint


@pytest.mark.asyncio
async def test_due_subscriptions_include_next_local_day_and_skip_invalid_rows(monkeypatch, caplog):
    now = datetime(2026, 11, 9, 12, tzinfo=UTC)
    rows = [
        {
            "user_id": 1,
            "timezone": "Pacific/Kiritimati",
            "preferred_local_hour": 0,
            "last_sent_date": date(2026, 11, 9),
        },
        {"user_id": 2, "timezone": "UTC", "preferred_local_hour": 13, "last_sent_date": None},
        {"user_id": 3, "timezone": "UTC", "preferred_local_hour": 10, "last_sent_date": date(2026, 11, 9)},
        {"user_id": 4, "timezone": "UNTRUSTED_PRIVATE_TIMEZONE", "preferred_local_hour": 10, "last_sent_date": None},
    ]

    async def query(sql, params):
        return [row for row in rows if row["last_sent_date"] is None or row["last_sent_date"] < params[0]]

    db_query = AsyncMock(side_effect=query)
    monkeypatch.setattr(repo, "db_query", db_query)
    due = await repo.get_due_for_current_hour(now)

    assert due == [{**rows[0], "slot_date": date(2026, 11, 10)}]
    assert rows[0].get("slot_date") is None
    assert "UNTRUSTED_PRIVATE_TIMEZONE" not in caplog.text
    assert "invalid" in caplog.text.lower()
