"""Integration tests for actual key quota, rotation and health APIs."""

import hashlib
from datetime import date, datetime, timedelta
from types import SimpleNamespace

import pytest

from app.database import db_manager
from app.repos import keys

pytestmark = [pytest.mark.integration, pytest.mark.asyncio, pytest.mark.usefixtures("force_test_db_conn")]

MODEL = "gemini-3.1-flash-lite"
USAGE_DATE = date(2026, 1, 5)


@pytest.fixture
async def key_clock(db_conn, monkeypatch):
    frozen_now = await db_conn.fetchval("SELECT CURRENT_TIMESTAMP")

    class DatabaseClock(datetime):
        @classmethod
        def now(cls, tz=None):
            return frozen_now.astimezone(tz) if tz is not None else frozen_now.replace(tzinfo=None)

    monkeypatch.setattr(keys, "datetime", DatabaseClock)
    return frozen_now


@pytest.fixture
def daily_manager(monkeypatch, key_clock):
    manager = keys.DailyKeyManager("api_keys", "key_usage")
    monkeypatch.setattr(manager, "_today", lambda: USAGE_DATE)
    monkeypatch.setattr(keys, "settings", SimpleNamespace(LIMIT_THRESHOLD_PERCENT=1.0))
    monkeypatch.setattr(db_manager, "_active_keys_cache", {})
    return manager


@pytest.fixture
async def selection_keys(db_conn_with_user):
    conn = db_conn_with_user
    # Exclude pre-existing keys so this test does not depend on seed data.
    excluded = {row["key_hash"] for row in await conn.fetch("SELECT key_hash FROM api_keys")}
    key_a = "integration-gemini-key-a"
    key_b = "integration-gemini-key-b"
    hash_a = hashlib.sha256(key_a.encode("utf-8")).hexdigest()
    hash_b = hashlib.sha256(key_b.encode("utf-8")).hexdigest()
    await conn.executemany(
        "INSERT INTO api_keys (api_key, key_hash) VALUES ($1, $2)", [(key_a, hash_a), (key_b, hash_b)]
    )
    return conn, hash_a, hash_b, excluded


class TestKeyUsageTracking:
    async def test_insert_usage_counter(self, db_conn_with_key, daily_manager):
        conn, key_hash = db_conn_with_key

        assert await daily_manager.increment_usage(key_hash, MODEL) == [{"request_count": 1}]

        assert (
            await conn.fetchval(
                "SELECT request_count FROM key_usage WHERE key_hash = $1 AND model_name = $2 AND usage_date = $3",
                key_hash,
                MODEL,
                USAGE_DATE,
            )
            == 1
        )

    async def test_increment_usage_counter(self, db_conn_with_key, daily_manager):
        conn, key_hash = db_conn_with_key
        for expected in (1, 2, 3):
            assert await daily_manager.increment_usage(key_hash, MODEL) == [{"request_count": expected}]

        assert (
            await conn.fetchval(
                "SELECT request_count FROM key_usage WHERE key_hash = $1 AND model_name = $2 AND usage_date = $3",
                key_hash,
                MODEL,
                USAGE_DATE,
            )
            == 3
        )

    async def test_usage_counter_per_model(self, db_conn_with_key, daily_manager):
        conn, key_hash = db_conn_with_key
        await daily_manager.increment_usage(key_hash, MODEL)
        await daily_manager.increment_usage(key_hash, MODEL)
        await daily_manager.increment_usage(key_hash, "gemini-3.1-pro-preview")

        rows = await conn.fetch(
            "SELECT model_name, request_count FROM key_usage WHERE key_hash = $1 AND usage_date = $2",
            key_hash,
            USAGE_DATE,
        )
        assert {row["model_name"]: row["request_count"] for row in rows} == {MODEL: 2, "gemini-3.1-pro-preview": 1}

    @pytest.mark.parametrize("daily_limit,percent,slots", [(2, 1.0, 2), (10, 0.9, 9)])
    async def test_reservation_stops_at_threshold(
        self, db_conn_with_key, daily_manager, monkeypatch, daily_limit, percent, slots
    ):
        conn, key_hash = db_conn_with_key
        monkeypatch.setattr(keys, "settings", SimpleNamespace(LIMIT_THRESHOLD_PERCENT=percent))
        assert await daily_manager.is_key_available(key_hash, MODEL, daily_limit) is True
        for count in range(1, slots + 1):
            assert await daily_manager.reserve_usage(key_hash, MODEL, daily_limit) == count

        assert await daily_manager.reserve_usage(key_hash, MODEL, daily_limit) is None
        assert await daily_manager.is_key_available(key_hash, MODEL, daily_limit) is False
        assert await daily_manager.is_key_available(key_hash, "other-model", daily_limit) is True
        assert (
            await conn.fetchval(
                "SELECT request_count FROM key_usage WHERE key_hash = $1 AND model_name = $2 AND usage_date = $3",
                key_hash,
                MODEL,
                USAGE_DATE,
            )
            == slots
        )

    async def test_unlimited_reservations_still_record_usage(self, db_conn_with_key, daily_manager):
        conn, key_hash = db_conn_with_key
        for count in (1, 2, 3):
            assert await daily_manager.reserve_usage(key_hash, MODEL, daily_limit=None) == count
        assert await daily_manager.is_key_available(key_hash, MODEL, daily_limit=None) is True
        assert (
            await conn.fetchval(
                "SELECT request_count FROM key_usage WHERE key_hash = $1 AND model_name = $2 AND usage_date = $3",
                key_hash,
                MODEL,
                USAGE_DATE,
            )
            == 3
        )


class TestKeySelection:
    async def test_select_least_used_key(self, selection_keys, daily_manager):
        conn, hash_a, hash_b, excluded = selection_keys
        await conn.executemany(
            "INSERT INTO key_usage (key_hash, model_name, usage_date, request_count) VALUES ($1, $2, $3, $4)",
            [(hash_a, MODEL, USAGE_DATE, 5), (hash_b, MODEL, USAGE_DATE, 2)],
        )

        selected = await daily_manager.get_fresh_available_key(MODEL, daily_limit=10, excluded_hashes=excluded)
        assert selected == {"key_hash": hash_b, "api_key": "integration-gemini-key-b"}
        assert await daily_manager.get_fresh_available_key(
            MODEL, daily_limit=10, excluded_hashes=excluded | {hash_b}
        ) == {"key_hash": hash_a, "api_key": "integration-gemini-key-a"}

    async def test_key_with_no_usage_preferred(self, selection_keys, daily_manager):
        conn, hash_a, hash_b, excluded = selection_keys
        await conn.execute(
            "INSERT INTO key_usage (key_hash, model_name, usage_date, request_count) VALUES ($1, $2, $3, $4)",
            hash_a,
            MODEL,
            USAGE_DATE,
            5,
        )

        assert await daily_manager.get_fresh_available_key(MODEL, daily_limit=10, excluded_hashes=excluded) == {
            "key_hash": hash_b,
            "api_key": "integration-gemini-key-b",
        }

    async def test_exhausted_or_suspended_keys_are_not_selected(self, selection_keys, daily_manager):
        conn, hash_a, hash_b, excluded = selection_keys
        await conn.execute(
            "INSERT INTO key_usage (key_hash, model_name, usage_date, request_count) VALUES ($1, $2, $3, $4)",
            hash_a,
            MODEL,
            USAGE_DATE,
            10,
        )
        manager = keys.KeyStatusManager()
        await manager.suspend_key(hash_b, MODEL, "permanent", "Invalid credential")

        assert await daily_manager.get_fresh_available_key(MODEL, daily_limit=10, excluded_hashes=excluded) is None

        await manager.record_success(hash_b, MODEL)
        assert await daily_manager.get_fresh_available_key(MODEL, daily_limit=10, excluded_hashes=excluded) == {
            "key_hash": hash_b,
            "api_key": "integration-gemini-key-b",
        }


class TestKeyModelStatus:
    async def test_suspend_key(self, db_conn_with_key, daily_manager, key_clock):
        conn, key_hash = db_conn_with_key
        manager = keys.KeyStatusManager()

        await manager.suspend_key(key_hash, MODEL, "permanent", "Rate limit details")
        row = await conn.fetchrow(
            "SELECT status, failure_count, last_error, suspended_until FROM key_model_status WHERE key_hash = $1 AND model_name = $2",
            key_hash,
            MODEL,
        )
        assert row["status"] == "suspended"
        assert row["failure_count"] == 1
        assert row["last_error"] == "Rate limit details"
        assert row["suspended_until"] == key_clock + timedelta(hours=24)
        # An immediate duplicate suspension must not increment the DB counter.
        await manager.suspend_key(key_hash, MODEL, "permanent", "Duplicate")
        assert (
            await conn.fetchval(
                "SELECT failure_count FROM key_model_status WHERE key_hash = $1 AND model_name = $2", key_hash, MODEL
            )
            == 1
        )
        assert (
            await conn.fetchval(
                "SELECT COUNT(*) FROM key_model_status WHERE key_hash = $1 AND model_name = $2", key_hash, "other-model"
            )
            == 0
        )

    async def test_reactivate_key(self, db_conn_with_key, daily_manager):
        conn, key_hash = db_conn_with_key
        manager = keys.KeyStatusManager()
        await manager.suspend_key(key_hash, MODEL, "permanent", "Invalid credential")

        await manager.record_success(key_hash, MODEL)

        row = await conn.fetchrow(
            "SELECT status, failure_count, suspended_until, last_error FROM key_model_status WHERE key_hash = $1 AND model_name = $2",
            key_hash,
            MODEL,
        )
        assert dict(row) == {"status": "active", "failure_count": 0, "suspended_until": None, "last_error": None}
        statuses = await manager.get_all_statuses()
        own_statuses = [status for status in statuses if status["key_hash"] == key_hash]
        assert len(own_statuses) == 1
        assert own_statuses[0]["status"] == "active"
