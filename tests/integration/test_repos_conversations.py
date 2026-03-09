"""Integration tests for conversations — mirrors repos/conversations.py SQL.

Tests save_conversation, get_user_conversations, add/get messages, delete.
"""

import pytest

pytestmark = pytest.mark.integration


class TestConversationCRUD:
    """Test conversation lifecycle mirroring repos/conversations.py."""

    @pytest.mark.asyncio
    async def test_save_and_list_conversations(self, db_conn_with_user):
        """Mirrors save_conversation + get_user_conversations."""
        conn = db_conn_with_user
        user_id = 999999

        # Insert 3 conversations
        for title in ["Chat A", "Chat B", "Chat C"]:
            await conn.execute(
                "INSERT INTO conversations (user_id, title) VALUES ($1, $2)",
                user_id,
                title,
            )

        rows = await conn.fetch(
            "SELECT title FROM conversations WHERE user_id = $1 ORDER BY created_at DESC",
            user_id,
        )
        assert len(rows) == 3

    @pytest.mark.asyncio
    async def test_save_conversation_with_role(self, db_conn_with_user):
        """Mirrors save_conversation with role_type and role_id."""
        conn = db_conn_with_user
        user_id = 999999

        conv_id = await conn.fetchval(
            """INSERT INTO conversations (user_id, title, role_type, role_id)
               VALUES ($1, $2, $3, $4) RETURNING id""",
            user_id,
            "Teacher Chat",
            "system",
            5,
        )
        row = await conn.fetchrow("SELECT * FROM conversations WHERE id = $1", conv_id)
        assert row["role_type"] == "system"
        assert row["role_id"] == 5

    @pytest.mark.asyncio
    async def test_conversation_messages_lifecycle(self, db_conn_with_user):
        """Mirrors add_message + get_conversation_messages."""
        conn = db_conn_with_user
        user_id = 999999

        conv_id = await conn.fetchval(
            "INSERT INTO conversations (user_id, title) VALUES ($1, $2) RETURNING id",
            user_id,
            "Test Chat",
        )

        # Add messages
        messages = [
            (conv_id, "user", "Hello", 10, user_id),
            (conv_id, "model", "Hi there!", 15, user_id),
            (conv_id, "user", "How are you?", 12, user_id),
        ]
        await conn.executemany(
            """INSERT INTO conversation_messages
               (conversation_id, role, content, token_estimate, owner_user_id)
               VALUES ($1, $2, $3, $4, $5)""",
            messages,
        )

        # Get messages — mirrors get_conversation_messages
        rows = await conn.fetch(
            """SELECT role, content, token_estimate
               FROM conversation_messages
               WHERE conversation_id = $1
               ORDER BY created_at ASC""",
            conv_id,
        )
        assert len(rows) == 3
        assert rows[0]["role"] == "user"
        assert rows[1]["content"] == "Hi there!"

    @pytest.mark.asyncio
    async def test_rename_conversation(self, db_conn_with_user):
        """Mirrors rename_conversation."""
        conn = db_conn_with_user
        user_id = 999999

        conv_id = await conn.fetchval(
            "INSERT INTO conversations (user_id, title) VALUES ($1, $2) RETURNING id",
            user_id,
            "Old Title",
        )
        await conn.execute(
            "UPDATE conversations SET title = $1 WHERE id = $2 AND user_id = $3",
            "New Title",
            conv_id,
            user_id,
        )
        row = await conn.fetchrow("SELECT title FROM conversations WHERE id = $1", conv_id)
        assert row["title"] == "New Title"

    @pytest.mark.asyncio
    async def test_delete_conversation_cascades_messages(self, db_conn_with_user):
        """Mirrors delete_conversation — deletes messages first, then conv."""
        conn = db_conn_with_user
        user_id = 999999

        conv_id = await conn.fetchval(
            "INSERT INTO conversations (user_id, title) VALUES ($1, $2) RETURNING id",
            user_id,
            "To Delete",
        )
        await conn.execute(
            """INSERT INTO conversation_messages
               (conversation_id, role, content, owner_user_id)
               VALUES ($1, $2, $3, $4)""",
            conv_id,
            "user",
            "Msg",
            user_id,
        )

        # Delete messages then conversation (mirrors delete_conversation)
        await conn.execute("DELETE FROM conversation_messages WHERE conversation_id = $1", conv_id)
        await conn.execute("DELETE FROM conversations WHERE id = $1 AND user_id = $2", conv_id, user_id)

        row = await conn.fetchrow("SELECT * FROM conversations WHERE id = $1", conv_id)
        assert row is None
        msg_count = await conn.fetchval(
            "SELECT COUNT(*) FROM conversation_messages WHERE conversation_id = $1", conv_id
        )
        assert msg_count == 0

    @pytest.mark.asyncio
    async def test_conversation_count(self, db_conn_with_user):
        """Mirrors get_conversation_count."""
        conn = db_conn_with_user
        user_id = 999999

        for i in range(5):
            await conn.execute(
                "INSERT INTO conversations (user_id, title) VALUES ($1, $2)",
                user_id,
                f"Chat {i}",
            )

        count = await conn.fetchval(
            "SELECT COUNT(*) FROM conversations WHERE user_id = $1 AND archived = false",
            user_id,
        )
        assert count == 5
