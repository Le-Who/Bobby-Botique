"""Integration tests for user-state persistence and feedback repository APIs."""

import pytest

from app.repos import users

pytestmark = [pytest.mark.integration, pytest.mark.asyncio, pytest.mark.usefixtures("force_test_db_conn")]


class TestUserStatePersistence:
    async def test_save_and_load_state_round_trip(self, db_conn_with_user, test_user_id):
        saved = {
            "document_mode": True,
            "selected_document_id": 42,
            "awaiting_custom_role_input": True,
            "generated_role": {"title": "Учитель", "prompt": "Ты учишь"},
            "last_custom_role_prompt": "make me a teacher",
            "generating_custom_role": True,
            "last_sent_message_text": "Hello bot",
            "awaiting_manual_role_title": True,
            "awaiting_manual_role_prompt": True,
            "manual_role_title": "Manual title",
            "manual_role_prompt": "Manual prompt",
            "role_diaries": {"user_role:42": ["Первая запись", "Вторая запись"]},
            "tarot_mode": True,
            "tarot_session": {"step": "reading", "cards": ["star"]},
        }

        await users.save_user_state(test_user_id, **saved)
        loaded = await users.load_user_state(test_user_id)

        assert loaded is not None
        assert {name: loaded[name] for name in saved} == saved
        row = await db_conn_with_user.fetchrow(
            "SELECT generated_role, role_diaries, tarot_session, document_mode FROM user_state WHERE user_id = $1",
            test_user_id,
        )
        assert row["generated_role"] == saved["generated_role"]
        assert row["role_diaries"] == saved["role_diaries"]
        assert row["tarot_session"] == saved["tarot_session"]
        assert row["document_mode"] is True

    async def test_upsert_overwrites_existing(self, db_conn_with_user, test_user_id):
        await users.save_user_state(
            test_user_id,
            document_mode=False,
            manual_role_title="Original",
            generated_role={"title": "Original"},
            role_diaries={"role:1": ["Original diary"]},
            tarot_mode=True,
            tarot_session={"step": "reading"},
        )

        await users.save_user_state(test_user_id, document_mode=True, manual_role_title="Updated")
        loaded = await users.load_user_state(test_user_id)

        assert loaded is not None
        assert loaded["document_mode"] is True
        assert loaded["manual_role_title"] == "Updated"
        assert loaded["generated_role"] is None
        assert loaded["role_diaries"] == {}
        assert loaded["tarot_mode"] is False
        assert loaded["tarot_session"] is None
        assert await db_conn_with_user.fetchval("SELECT COUNT(*) FROM user_state WHERE user_id = $1", test_user_id) == 1

    async def test_unsaved_user_returns_none(self, db_conn_with_user, test_user_id):
        assert await users.load_user_state(test_user_id) is None

    async def test_state_load_and_save_are_scoped_to_user(self, db_conn_with_user, test_user_id):
        other_user_id = 888888
        await db_conn_with_user.execute("INSERT INTO users (user_id) VALUES ($1)", other_user_id)
        await users.save_user_state(
            test_user_id, last_sent_message_text="Owner message", generated_role={"title": "Owner role"}
        )
        assert await users.load_user_state(other_user_id) is None

        await users.save_user_state(other_user_id, last_sent_message_text="Other message")
        await users.save_user_state(other_user_id, last_sent_message_text="Other updated")
        owner = await users.load_user_state(test_user_id)
        other = await users.load_user_state(other_user_id)

        assert owner is not None and other is not None
        assert owner["last_sent_message_text"] == "Owner message"
        assert owner["generated_role"] == {"title": "Owner role"}
        assert other["last_sent_message_text"] == "Other updated"
        assert other["generated_role"] is None
        rows = await db_conn_with_user.fetch(
            "SELECT user_id, last_sent_message_text FROM user_state WHERE user_id = ANY($1)",
            [test_user_id, other_user_id],
        )
        assert {row["user_id"]: row["last_sent_message_text"] for row in rows} == {
            test_user_id: "Owner message",
            other_user_id: "Other updated",
        }


class TestFeedbackPersistence:
    @pytest.mark.parametrize("rating,message_id", [("up", 12345), ("down", 12346)])
    async def test_save_feedback(self, db_conn_with_user, test_user_id, rating, message_id):
        await users.save_feedback(test_user_id, message_id, rating)

        assert (
            await db_conn_with_user.fetchval(
                "SELECT rating FROM feedback WHERE user_id = $1 AND message_id = $2", test_user_id, message_id
            )
            == rating
        )
