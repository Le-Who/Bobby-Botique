import pytest

pytestmark = pytest.mark.integration
"""Integration tests for chat state — mirrors repos/chats.py SQL.

Tests get_user_chat / update_user_chat / update_thinking_level logic.
"""

import json

import pytest

pytestmark = pytest.mark.integration


class TestChatStateLifecycle:
    """Test chat state CRUD mirroring repos/chats.py SQL."""

    @pytest.mark.asyncio
    async def test_get_user_chat_creates_defaults(self, db_conn_with_user):
        """Mirrors the INSERT ... ON CONFLICT in get_user_chat."""
        conn = db_conn_with_user
        user_id = 999999

        # Ensure user in users table but no chat yet
        await conn.execute(
            """INSERT INTO chats (user_id, model, history, token_count, search_enabled)
               VALUES ($1, $2, '[]'::jsonb, 0, false)
               ON CONFLICT (user_id) DO NOTHING""",
            user_id,
            "gemini-2.5-flash",
        )

        row = await conn.fetchrow("SELECT * FROM chats WHERE user_id = $1", user_id)
        assert row["model"] == "gemini-2.5-flash"
        assert row["token_count"] == 0
        assert row["search_enabled"] is False
        assert row["history"] == []

    @pytest.mark.asyncio
    async def test_update_chat_model_and_history(self, db_conn_with_user):
        """Mirrors update_user_chat model/history update."""
        conn = db_conn_with_user
        user_id = 999999

        await conn.execute(
            "INSERT INTO chats (user_id, model) VALUES ($1, $2)",
            user_id,
            "gemini-2.5-flash",
        )

        history = [
            {"role": "user", "content": "Hello"},
            {"role": "model", "content": "Hi!"},
        ]
        await conn.execute(
            "UPDATE chats SET model = $1, history = $2::jsonb, token_count = $3 WHERE user_id = $4",
            "gemini-2.5-flash-lite",
            json.dumps(history),
            150,
            user_id,
        )

        row = await conn.fetchrow("SELECT model, history, token_count FROM chats WHERE user_id = $1", user_id)
        assert row["model"] == "gemini-2.5-flash-lite"
        assert row["token_count"] == 150
        loaded_history = json.loads(row["history"]) if isinstance(row["history"], str) else row["history"]
        assert len(loaded_history) == 2
        assert loaded_history[0]["role"] == "user"

    @pytest.mark.asyncio
    async def test_clear_chat_resets_state(self, db_conn_with_user):
        """Mirrors clearing chat state."""
        conn = db_conn_with_user
        user_id = 999999

        await conn.execute(
            "INSERT INTO chats (user_id, model, token_count, context_summary) VALUES ($1, $2, $3, $4)",
            user_id,
            "gemini-2.5-flash",
            500,
            "Previous summary",
        )

        await conn.execute(
            "UPDATE chats SET history = '[]'::jsonb, token_count = 0, context_summary = NULL WHERE user_id = $1",
            user_id,
        )

        row = await conn.fetchrow("SELECT token_count, context_summary FROM chats WHERE user_id = $1", user_id)
        assert row["token_count"] == 0
        assert row["context_summary"] is None

    @pytest.mark.asyncio
    async def test_update_thinking_level(self, db_conn_with_user):
        """Mirrors update_thinking_level."""
        conn = db_conn_with_user
        user_id = 999999

        await conn.execute(
            "INSERT INTO chats (user_id, model) VALUES ($1, $2)",
            user_id,
            "gemini-2.5-flash",
        )

        await conn.execute("UPDATE chats SET thinking_level = $1 WHERE user_id = $2", "high", user_id)
        row = await conn.fetchrow("SELECT thinking_level FROM chats WHERE user_id = $1", user_id)
        assert row["thinking_level"] == "high"

        # Reset to None
        await conn.execute("UPDATE chats SET thinking_level = NULL WHERE user_id = $1", user_id)
        row = await conn.fetchrow("SELECT thinking_level FROM chats WHERE user_id = $1", user_id)
        assert row["thinking_level"] is None


class TestActiveChatMessages:
    """Test active_chat_messages table (mirrors message sync in update_user_chat)."""

    @pytest.mark.asyncio
    async def test_bulk_insert_and_query(self, db_conn_with_user):
        """Mirrors db_execute_many for message batch insert."""
        conn = db_conn_with_user
        user_id = 999999

        messages = [
            (user_id, "user", "What is Python?"),
            (user_id, "model", "Python is a programming language."),
            (user_id, "user", "Tell me more"),
        ]
        await conn.executemany(
            "INSERT INTO active_chat_messages (user_id, role, content) VALUES ($1, $2, $3)",
            messages,
        )

        rows = await conn.fetch(
            "SELECT role, content FROM active_chat_messages WHERE user_id = $1 ORDER BY id ASC",
            user_id,
        )
        assert len(rows) == 3
        assert rows[0]["content"] == "What is Python?"
        assert rows[2]["role"] == "user"

    @pytest.mark.asyncio
    async def test_delete_messages_on_clear(self, db_conn_with_user):
        """Mirrors clearing active messages when starting new topic."""
        conn = db_conn_with_user
        user_id = 999999

        await conn.execute(
            "INSERT INTO active_chat_messages (user_id, role, content) VALUES ($1, $2, $3)",
            user_id,
            "user",
            "Hello",
        )
        await conn.execute("DELETE FROM active_chat_messages WHERE user_id = $1", user_id)
        count = await conn.fetchval("SELECT COUNT(*) FROM active_chat_messages WHERE user_id = $1", user_id)
        assert count == 0
