"""Subscription writes apply preferences to both new and existing rows."""

import sqlite3

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
