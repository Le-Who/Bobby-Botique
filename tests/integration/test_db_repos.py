import pytest

pytestmark = pytest.mark.integration
"""Integration tests for repos — real database queries against test Supabase project.

All tests use transactional rollback — NO data persists after tests complete.
"""

import pytest

pytestmark = pytest.mark.integration


class TestUsersTable:
    """Test basic user CRUD operations against real DB."""

    @pytest.mark.asyncio
    async def test_insert_and_select_user(self, db_conn):
        await db_conn.execute(
            "INSERT INTO users (user_id, is_authorized) VALUES ($1, $2)",
            100001,
            1,
        )
        row = await db_conn.fetchrow(
            "SELECT user_id, is_authorized FROM users WHERE user_id = $1",
            100001,
        )
        assert row is not None
        assert row["user_id"] == 100001
        assert row["is_authorized"] == 1

    @pytest.mark.asyncio
    async def test_user_defaults(self, db_conn):
        await db_conn.execute(
            "INSERT INTO users (user_id) VALUES ($1)",
            100002,
        )
        row = await db_conn.fetchrow("SELECT * FROM users WHERE user_id = $1", 100002)
        assert row["is_authorized"] == 0
        assert row["is_deep_dive"] is False
        assert row["deep_dive_thread_id"] is None


class TestChatsTable:
    """Test chat state CRUD."""

    @pytest.mark.asyncio
    async def test_insert_chat_with_defaults(self, db_conn_with_user):
        conn = db_conn_with_user
        user_id = 999999
        await conn.execute(
            "INSERT INTO chats (user_id, model) VALUES ($1, $2)",
            user_id,
            "gemini-2.5-flash",
        )
        row = await conn.fetchrow("SELECT * FROM chats WHERE user_id = $1", user_id)
        assert row["model"] == "gemini-2.5-flash"
        assert row["token_count"] == 0
        assert row["search_enabled"] is False
        assert row["history"] == "[]"
        assert row["context_summary"] is None

    @pytest.mark.asyncio
    async def test_update_chat_model(self, db_conn_with_user):
        conn = db_conn_with_user
        user_id = 999999
        await conn.execute(
            "INSERT INTO chats (user_id, model) VALUES ($1, $2)",
            user_id,
            "gemini-2.5-flash",
        )
        await conn.execute(
            "UPDATE chats SET model = $1 WHERE user_id = $2",
            "gemini-2.5-flash-lite",
            user_id,
        )
        row = await conn.fetchrow("SELECT model FROM chats WHERE user_id = $1", user_id)
        assert row["model"] == "gemini-2.5-flash-lite"


class TestActiveChatMessages:
    """Test message storage."""

    @pytest.mark.asyncio
    async def test_insert_and_order_messages(self, db_conn_with_user):
        conn = db_conn_with_user
        user_id = 999999
        await conn.execute(
            "INSERT INTO active_chat_messages (user_id, role, content) VALUES ($1, $2, $3)",
            user_id,
            "user",
            "Hello",
        )
        await conn.execute(
            "INSERT INTO active_chat_messages (user_id, role, content) VALUES ($1, $2, $3)",
            user_id,
            "model",
            "Hi there!",
        )
        rows = await conn.fetch(
            "SELECT role, content FROM active_chat_messages WHERE user_id = $1 ORDER BY id ASC",
            user_id,
        )
        assert len(rows) == 2
        assert rows[0]["role"] == "user"
        assert rows[1]["role"] == "model"


class TestConversations:
    """Test conversation CRUD."""

    @pytest.mark.asyncio
    async def test_create_and_list_conversations(self, db_conn_with_user):
        conn = db_conn_with_user
        user_id = 999999
        await conn.execute(
            "INSERT INTO conversations (user_id, title) VALUES ($1, $2)",
            user_id,
            "Test Chat 1",
        )
        await conn.execute(
            "INSERT INTO conversations (user_id, title) VALUES ($1, $2)",
            user_id,
            "Test Chat 2",
        )
        rows = await conn.fetch(
            "SELECT title FROM conversations WHERE user_id = $1 ORDER BY created_at DESC",
            user_id,
        )
        assert len(rows) == 2

    @pytest.mark.asyncio
    async def test_conversation_messages_fk(self, db_conn_with_user):
        conn = db_conn_with_user
        user_id = 999999
        conv_id = await conn.fetchval(
            "INSERT INTO conversations (user_id, title) VALUES ($1, $2) RETURNING id",
            user_id,
            "Test",
        )
        await conn.execute(
            "INSERT INTO conversation_messages (conversation_id, role, content, owner_user_id) VALUES ($1, $2, $3, $4)",
            conv_id,
            "user",
            "Message 1",
            user_id,
        )
        count = await conn.fetchval(
            "SELECT COUNT(*) FROM conversation_messages WHERE conversation_id = $1",
            conv_id,
        )
        assert count == 1


class TestUserState:
    """Test user state persistence."""

    @pytest.mark.asyncio
    async def test_user_state_defaults(self, db_conn_with_user):
        conn = db_conn_with_user
        user_id = 999999
        await conn.execute("INSERT INTO user_state (user_id) VALUES ($1)", user_id)
        row = await conn.fetchrow("SELECT * FROM user_state WHERE user_id = $1", user_id)
        assert row["document_mode"] is False
        assert row["awaiting_custom_role_input"] is False
        assert row["generating_custom_role"] is False
        assert row["manual_role_title"] == ""

    @pytest.mark.asyncio
    async def test_user_state_update(self, db_conn_with_user):
        conn = db_conn_with_user
        user_id = 999999
        await conn.execute("INSERT INTO user_state (user_id) VALUES ($1)", user_id)
        await conn.execute(
            "UPDATE user_state SET document_mode = true, selected_document_id = 42 WHERE user_id = $1",
            user_id,
        )
        row = await conn.fetchrow("SELECT * FROM user_state WHERE user_id = $1", user_id)
        assert row["document_mode"] is True
        assert row["selected_document_id"] == 42


class TestUserRoles:
    """Test user role CRUD."""

    @pytest.mark.asyncio
    async def test_create_and_fetch_role(self, db_conn_with_user):
        conn = db_conn_with_user
        user_id = 999999
        role_id = await conn.fetchval(
            "INSERT INTO user_roles (user_id, title, prompt) VALUES ($1, $2, $3) RETURNING id",
            user_id,
            "My Custom Role",
            "You are a helpful teacher",
        )
        row = await conn.fetchrow(
            "SELECT * FROM user_roles WHERE id = $1 AND user_id = $2",
            role_id,
            user_id,
        )
        assert row["title"] == "My Custom Role"
        assert row["prompt"] == "You are a helpful teacher"

    @pytest.mark.asyncio
    async def test_delete_role(self, db_conn_with_user):
        conn = db_conn_with_user
        user_id = 999999
        role_id = await conn.fetchval(
            "INSERT INTO user_roles (user_id, title, prompt) VALUES ($1, $2, $3) RETURNING id",
            user_id,
            "Temp",
            "temp",
        )
        await conn.execute("DELETE FROM user_roles WHERE id = $1", role_id)
        row = await conn.fetchrow("SELECT * FROM user_roles WHERE id = $1", role_id)
        assert row is None


class TestFeedback:
    """Test feedback table constraints."""

    @pytest.mark.asyncio
    async def test_valid_rating(self, db_conn_with_user):
        conn = db_conn_with_user
        user_id = 999999
        await conn.execute(
            "INSERT INTO feedback (user_id, rating) VALUES ($1, $2)",
            user_id,
            "up",
        )
        row = await conn.fetchrow("SELECT rating FROM feedback WHERE user_id = $1", user_id)
        assert row["rating"] == "up"

    @pytest.mark.asyncio
    async def test_invalid_rating_rejected(self, db_conn_with_user):
        conn = db_conn_with_user
        user_id = 999999
        with pytest.raises(Exception):  # CHECK constraint violation
            await conn.execute(
                "INSERT INTO feedback (user_id, rating) VALUES ($1, $2)",
                user_id,
                "invalid",
            )


class TestModelConfiguration:
    """Test model config table."""

    @pytest.mark.asyncio
    async def test_insert_model_config(self, db_conn):
        await db_conn.execute(
            "INSERT INTO model_configuration (model_name, daily_limit, provider) VALUES ($1, $2, $3)",
            "test-model",
            100,
            "gemini",
        )
        row = await db_conn.fetchrow(
            "SELECT * FROM model_configuration WHERE model_name = $1",
            "test-model",
        )
        assert row["daily_limit"] == 100
        assert row["provider"] == "gemini"
