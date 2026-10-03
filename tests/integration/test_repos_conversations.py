"""Integration tests for saved conversations through repository APIs."""

from datetime import UTC, datetime, timedelta

import pytest

from app.repos import chats, conversations, roles

pytestmark = [pytest.mark.integration, pytest.mark.asyncio, pytest.mark.usefixtures("force_test_db_conn")]


class TestConversationCRUD:
    async def test_save_and_list_conversations(self, db_conn_with_user, test_user_id):
        ids = [await conversations.save_conversation(test_user_id, title) for title in ("Chat A", "Chat B", "Chat C")]
        assert all(conversation_id is not None for conversation_id in ids)
        # CURRENT_TIMESTAMP is identical within the fixture transaction; give the
        # list API distinct timestamps so its ordering and pagination are observable.
        await db_conn_with_user.executemany(
            "UPDATE conversations SET created_at = $1 WHERE id = $2",
            [(datetime(2026, 1, 1, tzinfo=UTC) + timedelta(days=index), cid) for index, cid in enumerate(ids)],
        )

        rows = await conversations.get_user_conversations(test_user_id)
        assert [row["title"] for row in rows] == ["Chat C", "Chat B", "Chat A"]
        assert {row["id"] for row in rows} == set(ids)
        page = await conversations.get_user_conversations(test_user_id, limit=1, offset=1)
        assert [row["title"] for row in page] == ["Chat B"]
        assert (
            await db_conn_with_user.fetchval("SELECT COUNT(*) FROM conversations WHERE user_id = $1", test_user_id) == 3
        )

    async def test_save_conversation_with_custom_role(self, db_conn_with_user, test_user_id):
        await roles.create_custom_role(test_user_id, "Teacher", "You teach")
        role_id = await db_conn_with_user.fetchval(
            "SELECT id FROM user_roles WHERE user_id = $1 AND title = $2", test_user_id, "Teacher"
        )

        conv_id = await conversations.save_conversation(test_user_id, "Teacher Chat", "user_role", role_id)
        assert conv_id is not None

        row = await db_conn_with_user.fetchrow("SELECT role_type, role_id FROM conversations WHERE id = $1", conv_id)
        assert dict(row) == {"role_type": "user_role", "role_id": role_id}
        listed = await conversations.get_user_conversations(test_user_id)
        assert listed[0]["role_title"] == "Teacher"

    async def test_save_active_messages_and_load_saved_history(self, db_conn_with_user, test_user_id):
        state = await chats.get_user_chat(test_user_id)
        assert state is not None
        state.history = [
            {"role": "user", "parts": ["Hello"]},
            {"role": "model", "parts": ["Hi there!"]},
            {"role": "user", "parts": ["How are you?"]},
        ]
        state.token_count = 37
        assert await chats.update_user_chat(test_user_id, state)

        conv_id = await conversations.save_conversation(test_user_id, "Test Chat")
        assert conv_id is not None
        stored = await db_conn_with_user.fetch(
            "SELECT id, role, content, owner_user_id FROM conversation_messages WHERE conversation_id = $1 ORDER BY id",
            conv_id,
        )
        assert [(row["role"], row["content"], row["owner_user_id"]) for row in stored] == [
            ("user", "Hello", test_user_id),
            ("model", "Hi there!", test_user_id),
            ("user", "How are you?", test_user_id),
        ]
        assert await db_conn_with_user.fetchval("SELECT token_budget FROM conversations WHERE id = $1", conv_id) == 37
        loaded = await conversations.get_conversation_messages(conv_id, test_user_id)
        assert loaded is not None
        assert [(row["role"], row["content"]) for row in loaded] == [
            ("user", "Hello"),
            ("model", "Hi there!"),
            ("user", "How are you?"),
        ]
        assert len({row["created_at"] for row in loaded}) == 1

    async def test_empty_saved_conversation_has_empty_messages(self, db_conn_with_user, test_user_id):
        conv_id = await conversations.save_conversation(test_user_id, "Empty")
        assert conv_id is not None
        assert await conversations.get_conversation_messages(conv_id, test_user_id) == []

    async def test_rename_conversation(self, db_conn_with_user, test_user_id):
        conv_id = await conversations.save_conversation(test_user_id, "Old Title")
        assert conv_id is not None

        assert await conversations.rename_conversation(test_user_id, conv_id, "New Title")

        assert await db_conn_with_user.fetchval("SELECT title FROM conversations WHERE id = $1", conv_id) == "New Title"

    async def test_delete_conversation_cascades_messages(self, db_conn_with_user, test_user_id):
        state = await chats.get_user_chat(test_user_id)
        assert state is not None
        state.history = [{"role": "user", "parts": ["Msg"]}]
        assert await chats.update_user_chat(test_user_id, state)
        conv_id = await conversations.save_conversation(test_user_id, "To Delete")
        assert conv_id is not None
        assert (
            await db_conn_with_user.fetchval(
                "SELECT COUNT(*) FROM conversation_messages WHERE conversation_id = $1", conv_id
            )
            == 1
        )

        assert await conversations.delete_conversation(test_user_id, conv_id)

        assert await db_conn_with_user.fetchrow("SELECT id FROM conversations WHERE id = $1", conv_id) is None
        assert (
            await db_conn_with_user.fetchval(
                "SELECT COUNT(*) FROM conversation_messages WHERE conversation_id = $1", conv_id
            )
            == 0
        )
        assert await conversations.delete_conversation(test_user_id, conv_id) is False

    async def test_conversation_count(self, db_conn_with_user, test_user_id):
        assert await conversations.get_conversation_count(test_user_id) == 0
        for index in range(5):
            assert await conversations.save_conversation(test_user_id, f"Chat {index}") is not None
        assert await conversations.get_conversation_count(test_user_id) == 5

    async def test_foreign_user_cannot_read_or_change_saved_conversation(self, db_conn_with_user, test_user_id):
        other_user_id = 888888
        await db_conn_with_user.execute("INSERT INTO users (user_id) VALUES ($1)", other_user_id)
        owner = await chats.get_user_chat(test_user_id)
        other = await chats.get_user_chat(other_user_id)
        assert owner is not None and other is not None
        owner.history = [{"role": "user", "parts": ["Owner's saved message"]}]
        other.history = [{"role": "user", "parts": ["Other user's active message"]}]
        assert await chats.update_user_chat(test_user_id, owner)
        assert await chats.update_user_chat(other_user_id, other)
        conv_id = await conversations.save_conversation(test_user_id, "Owner's title")
        assert conv_id is not None

        assert await conversations.get_user_conversations(other_user_id) == []
        assert await conversations.get_conversation_count(other_user_id) == 0
        assert await conversations.get_conversation_messages(conv_id, other_user_id) is None
        assert await conversations.switch_to_conversation(other_user_id, conv_id) is False
        assert await conversations.rename_conversation(other_user_id, conv_id, "Foreign rename") is False
        assert await conversations.delete_conversation(other_user_id, conv_id) is False

        assert (
            await db_conn_with_user.fetchval("SELECT title FROM conversations WHERE id = $1", conv_id)
            == "Owner's title"
        )
        saved = await conversations.get_conversation_messages(conv_id, test_user_id)
        assert saved is not None
        assert [(row["role"], row["content"]) for row in saved] == [("user", "Owner's saved message")]
        unchanged = await chats.get_user_chat(other_user_id)
        assert unchanged is not None
        assert unchanged.history == [{"role": "user", "parts": ["Other user's active message"]}]
