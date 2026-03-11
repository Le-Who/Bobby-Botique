import pytest

pytestmark = pytest.mark.integration
"""Integration tests for user state persistence — mirrors repos/users.py SQL.

Tests save_user_state and load_user_state UPSERT logic against real DB.
"""

import json

import pytest

pytestmark = pytest.mark.integration


class TestUserStatePersistence:
    """Test save/load user_state round-trip (mirroring repos/users.py SQL)."""

    @pytest.mark.asyncio
    async def test_save_and_load_state_round_trip(self, db_conn_with_user):
        conn = db_conn_with_user
        user_id = 999999

        # Save (UPSERT) — mirrors save_user_state()
        role_json = json.dumps({"title": "Teacher", "prompt": "You teach"})
        await conn.execute(
            """
            INSERT INTO user_state (
                user_id, document_mode, selected_document_id,
                awaiting_custom_role_input, generated_role,
                last_custom_role_prompt, generating_custom_role,
                last_sent_message_text,
                awaiting_manual_role_title, awaiting_manual_role_prompt,
                manual_role_title, manual_role_prompt,
                updated_at
            ) VALUES ($1, $2, $3, $4, $5::jsonb, $6, $7, $8, $9, $10, $11, $12, CURRENT_TIMESTAMP)
            ON CONFLICT (user_id) DO UPDATE SET
                document_mode = EXCLUDED.document_mode,
                selected_document_id = EXCLUDED.selected_document_id,
                awaiting_custom_role_input = EXCLUDED.awaiting_custom_role_input,
                generated_role = EXCLUDED.generated_role,
                last_custom_role_prompt = EXCLUDED.last_custom_role_prompt,
                generating_custom_role = EXCLUDED.generating_custom_role,
                last_sent_message_text = EXCLUDED.last_sent_message_text,
                awaiting_manual_role_title = EXCLUDED.awaiting_manual_role_title,
                awaiting_manual_role_prompt = EXCLUDED.awaiting_manual_role_prompt,
                manual_role_title = EXCLUDED.manual_role_title,
                manual_role_prompt = EXCLUDED.manual_role_prompt,
                updated_at = CURRENT_TIMESTAMP
            """,
            user_id,
            True,
            42,
            True,
            role_json,
            "make me a teacher",
            True,
            "Hello bot",
            False,
            False,
            "",
            "",
        )

        # Load — mirrors load_user_state()
        row = await conn.fetchrow(
            """
            SELECT document_mode, selected_document_id,
                   awaiting_custom_role_input, generated_role,
                   last_custom_role_prompt, generating_custom_role,
                   last_sent_message_text,
                   awaiting_manual_role_title, awaiting_manual_role_prompt,
                   manual_role_title, manual_role_prompt
            FROM user_state WHERE user_id = $1
            """,
            user_id,
        )

        assert row is not None
        assert row["document_mode"] is True
        assert row["selected_document_id"] == 42
        assert row["awaiting_custom_role_input"] is True
        assert row["generating_custom_role"] is True
        assert row["last_sent_message_text"] == "Hello bot"
        assert row["last_custom_role_prompt"] == "make me a teacher"
        # JSONB round-trip
        role_data = (
            json.loads(row["generated_role"]) if isinstance(row["generated_role"], str) else row["generated_role"]
        )
        assert role_data["title"] == "Teacher"

    @pytest.mark.asyncio
    async def test_upsert_overwrites_existing(self, db_conn_with_user):
        conn = db_conn_with_user
        user_id = 999999

        # First insert
        await conn.execute(
            """INSERT INTO user_state (user_id, document_mode, manual_role_title)
               VALUES ($1, $2, $3)""",
            user_id,
            False,
            "Original",
        )
        # Upsert with new values
        await conn.execute(
            """INSERT INTO user_state (user_id, document_mode, manual_role_title)
               VALUES ($1, $2, $3)
               ON CONFLICT (user_id) DO UPDATE SET
                   document_mode = EXCLUDED.document_mode,
                   manual_role_title = EXCLUDED.manual_role_title""",
            user_id,
            True,
            "Updated",
        )

        row = await conn.fetchrow("SELECT document_mode, manual_role_title FROM user_state WHERE user_id = $1", user_id)
        assert row["document_mode"] is True
        assert row["manual_role_title"] == "Updated"


class TestFeedbackPersistence:
    """Test feedback insertion with CHECK constraint (mirroring repos/users.py SQL)."""

    @pytest.mark.asyncio
    async def test_save_feedback_up(self, db_conn_with_user):
        conn = db_conn_with_user
        await conn.execute(
            "INSERT INTO feedback (user_id, message_id, rating) VALUES ($1, $2, $3)",
            999999,
            12345,
            "up",
        )
        row = await conn.fetchrow("SELECT rating FROM feedback WHERE user_id = $1 AND message_id = $2", 999999, 12345)
        assert row["rating"] == "up"

    @pytest.mark.asyncio
    async def test_save_feedback_down(self, db_conn_with_user):
        conn = db_conn_with_user
        await conn.execute(
            "INSERT INTO feedback (user_id, message_id, rating) VALUES ($1, $2, $3)",
            999999,
            12346,
            "down",
        )
        row = await conn.fetchrow("SELECT rating FROM feedback WHERE user_id = $1", 999999)
        assert row["rating"] == "down"
