"""Integration tests for API key management — mirrors repos/keys.py SQL.

Tests key rotation, usage tracking, daily limits, and key status management.
"""

import pytest

pytestmark = pytest.mark.integration


class TestKeyUsageTracking:
    """Test daily key usage counter (mirrors DailyKeyManager SQL)."""

    @pytest.mark.asyncio
    async def test_insert_usage_counter(self, db_conn_with_key):
        """UPSERT into key_usage should create a new counter."""
        conn, key_hash = db_conn_with_key

        await conn.execute(
            """INSERT INTO key_usage (key_hash, model_name, usage_date, request_count)
               VALUES ($1, $2, CURRENT_DATE, 1)
               ON CONFLICT (key_hash, model_name, usage_date) DO UPDATE
               SET request_count = key_usage.request_count + 1""",
            key_hash, "gemini-2.5-flash",
        )

        row = await conn.fetchrow(
            "SELECT request_count FROM key_usage WHERE key_hash = $1 AND model_name = $2",
            key_hash, "gemini-2.5-flash",
        )
        assert row["request_count"] == 1

    @pytest.mark.asyncio
    async def test_increment_usage_counter(self, db_conn_with_key):
        """Repeated UPSERT should increment the counter."""
        conn, key_hash = db_conn_with_key

        upsert_sql = """INSERT INTO key_usage (key_hash, model_name, usage_date, request_count)
                        VALUES ($1, $2, CURRENT_DATE, 1)
                        ON CONFLICT (key_hash, model_name, usage_date) DO UPDATE
                        SET request_count = key_usage.request_count + 1"""

        # Insert 3 times
        for _ in range(3):
            await conn.execute(upsert_sql, key_hash, "gemini-2.5-flash")

        row = await conn.fetchrow(
            "SELECT request_count FROM key_usage WHERE key_hash = $1 AND model_name = $2",
            key_hash, "gemini-2.5-flash",
        )
        assert row["request_count"] == 3

    @pytest.mark.asyncio
    async def test_usage_counter_per_model(self, db_conn_with_key):
        """Usage should be tracked separately per model."""
        conn, key_hash = db_conn_with_key

        for model in ("gemini-2.5-flash", "gemini-2.0-flash"):
            await conn.execute(
                """INSERT INTO key_usage (key_hash, model_name, usage_date, request_count)
                   VALUES ($1, $2, CURRENT_DATE, 1)""",
                key_hash, model,
            )

        rows = await conn.fetch(
            "SELECT model_name FROM key_usage WHERE key_hash = $1",
            key_hash,
        )
        models = {r["model_name"] for r in rows}
        assert models == {"gemini-2.5-flash", "gemini-2.0-flash"}


class TestKeySelection:
    """Test key selection queries (mirrors DailyKeyManager.get_available_key)."""

    @pytest.mark.asyncio
    async def test_select_least_used_key(self, db_conn_with_user):
        """Key with lowest request_count should be selected first."""
        conn = db_conn_with_user

        # Insert 2 keys
        await conn.execute(
            "INSERT INTO api_keys (api_key, key_hash) VALUES ($1, $2)",
            "key-a", "hash_a",
        )
        await conn.execute(
            "INSERT INTO api_keys (api_key, key_hash) VALUES ($1, $2)",
            "key-b", "hash_b",
        )

        # Key A: 5 requests, Key B: 2 requests
        await conn.execute(
            "INSERT INTO key_usage (key_hash, model_name, usage_date, request_count) VALUES ($1, $2, CURRENT_DATE, $3)",
            "hash_a", "gemini-2.5-flash", 5,
        )
        await conn.execute(
            "INSERT INTO key_usage (key_hash, model_name, usage_date, request_count) VALUES ($1, $2, CURRENT_DATE, $3)",
            "hash_b", "gemini-2.5-flash", 2,
        )

        # Should select hash_b (least used)
        row = await conn.fetchrow(
            """SELECT k.key_hash
               FROM api_keys k
               LEFT JOIN key_usage u ON k.key_hash = u.key_hash
                   AND u.model_name = $1 AND u.usage_date = CURRENT_DATE
               ORDER BY COALESCE(u.request_count, 0) ASC
               LIMIT 1""",
            "gemini-2.5-flash",
        )
        assert row["key_hash"] == "hash_b"

    @pytest.mark.asyncio
    async def test_key_with_no_usage_preferred(self, db_conn_with_user):
        """Keys with no usage records should be preferred (count = 0)."""
        conn = db_conn_with_user

        await conn.execute(
            "INSERT INTO api_keys (api_key, key_hash) VALUES ($1, $2)",
            "used-key", "hash_used",
        )
        await conn.execute(
            "INSERT INTO api_keys (api_key, key_hash) VALUES ($1, $2)",
            "fresh-key", "hash_fresh",
        )
        await conn.execute(
            "INSERT INTO key_usage (key_hash, model_name, usage_date, request_count) VALUES ($1, $2, CURRENT_DATE, $3)",
            "hash_used", "gemini-2.5-flash", 10,
        )

        row = await conn.fetchrow(
            """SELECT k.key_hash
               FROM api_keys k
               LEFT JOIN key_usage u ON k.key_hash = u.key_hash
                   AND u.model_name = $1 AND u.usage_date = CURRENT_DATE
               ORDER BY COALESCE(u.request_count, 0) ASC
               LIMIT 1""",
            "gemini-2.5-flash",
        )
        assert row["key_hash"] == "hash_fresh"


class TestKeyModelStatus:
    """Test key model status tracking (mirrors KeyStatusManager SQL)."""

    @pytest.mark.asyncio
    async def test_suspend_key(self, db_conn_with_key):
        """Suspending a key should create a status record in key_model_status."""
        conn, key_hash = db_conn_with_key

        await conn.execute(
            """INSERT INTO key_model_status
                   (key_hash, model_name, status, suspended_until, failure_count, last_error, updated_at)
               VALUES ($1, $2, 'suspended', NOW() + INTERVAL '60 seconds', 1, 'rate limit hit', NOW())
               ON CONFLICT (key_hash, model_name) DO UPDATE SET
                   status = 'suspended',
                   suspended_until = EXCLUDED.suspended_until,
                   failure_count = key_model_status.failure_count + 1,
                   last_error = EXCLUDED.last_error,
                   updated_at = NOW()""",
            key_hash, "gemini-2.5-flash",
        )

        row = await conn.fetchrow(
            "SELECT status, failure_count, last_error FROM key_model_status WHERE key_hash = $1 AND model_name = $2",
            key_hash, "gemini-2.5-flash",
        )
        assert row["status"] == "suspended"
        assert row["failure_count"] == 1
        assert row["last_error"] == "rate limit hit"

    @pytest.mark.asyncio
    async def test_reactivate_key(self, db_conn_with_key):
        """Recording success should reset key to active."""
        conn, key_hash = db_conn_with_key

        # First suspend
        await conn.execute(
            """INSERT INTO key_model_status (key_hash, model_name, status, failure_count, updated_at)
               VALUES ($1, $2, 'suspended', 3, NOW())""",
            key_hash, "gemini-2.5-flash",
        )

        # Then reactivate (mirrors KeyStatusManager.record_success)
        await conn.execute(
            """UPDATE key_model_status
               SET status = 'active', failure_count = 0, suspended_until = NULL, updated_at = NOW()
               WHERE key_hash = $1 AND model_name = $2""",
            key_hash, "gemini-2.5-flash",
        )

        row = await conn.fetchrow(
            "SELECT status, failure_count FROM key_model_status WHERE key_hash = $1 AND model_name = $2",
            key_hash, "gemini-2.5-flash",
        )
        assert row["status"] == "active"
        assert row["failure_count"] == 0
