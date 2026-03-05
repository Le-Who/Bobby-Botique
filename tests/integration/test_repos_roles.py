"""Integration tests for custom roles — mirrors repos/roles.py SQL.

Tests CRUD operations for user_roles table.
"""

import pytest

pytestmark = pytest.mark.integration


class TestCustomRolesCRUD:
    """Test custom role lifecycle mirroring repos/roles.py."""

    @pytest.mark.asyncio
    async def test_create_and_list_roles(self, db_conn_with_user):
        """Mirrors create_custom_role + get_user_custom_roles."""
        conn = db_conn_with_user
        user_id = 999999

        await conn.execute(
            "INSERT INTO user_roles (user_id, title, prompt) VALUES ($1, $2, $3)",
            user_id, "Teacher", "You are a helpful teacher",
        )
        await conn.execute(
            "INSERT INTO user_roles (user_id, title, prompt) VALUES ($1, $2, $3)",
            user_id, "Developer", "You are a senior dev",
        )

        rows = await conn.fetch(
            "SELECT id, title FROM user_roles WHERE user_id = $1 ORDER BY created_at DESC",
            user_id,
        )
        assert len(rows) == 2
        titles = {r["title"] for r in rows}
        assert titles == {"Teacher", "Developer"}

    @pytest.mark.asyncio
    async def test_get_role_prompt(self, db_conn_with_user):
        """Mirrors get_custom_role_prompt."""
        conn = db_conn_with_user
        user_id = 999999

        role_id = await conn.fetchval(
            "INSERT INTO user_roles (user_id, title, prompt) VALUES ($1, $2, $3) RETURNING id",
            user_id, "Writer", "You are a creative writer",
        )

        row = await conn.fetchrow(
            "SELECT prompt FROM user_roles WHERE id = $1 AND user_id = $2",
            role_id, user_id,
        )
        assert row["prompt"] == "You are a creative writer"

    @pytest.mark.asyncio
    async def test_rename_role(self, db_conn_with_user):
        """Mirrors rename_custom_role."""
        conn = db_conn_with_user
        user_id = 999999

        role_id = await conn.fetchval(
            "INSERT INTO user_roles (user_id, title, prompt) VALUES ($1, $2, $3) RETURNING id",
            user_id, "Old Name", "prompt",
        )
        await conn.execute(
            "UPDATE user_roles SET title = $1 WHERE id = $2 AND user_id = $3",
            "New Name", role_id, user_id,
        )

        row = await conn.fetchrow("SELECT title FROM user_roles WHERE id = $1", role_id)
        assert row["title"] == "New Name"

    @pytest.mark.asyncio
    async def test_delete_role(self, db_conn_with_user):
        """Mirrors delete_custom_role."""
        conn = db_conn_with_user
        user_id = 999999

        role_id = await conn.fetchval(
            "INSERT INTO user_roles (user_id, title, prompt) VALUES ($1, $2, $3) RETURNING id",
            user_id, "Temp Role", "temp",
        )
        await conn.execute("DELETE FROM user_roles WHERE id = $1 AND user_id = $2", role_id, user_id)

        row = await conn.fetchrow("SELECT * FROM user_roles WHERE id = $1", role_id)
        assert row is None

    @pytest.mark.asyncio
    async def test_role_count(self, db_conn_with_user):
        """Mirrors get_custom_role_count."""
        conn = db_conn_with_user
        user_id = 999999

        for i in range(3):
            await conn.execute(
                "INSERT INTO user_roles (user_id, title, prompt) VALUES ($1, $2, $3)",
                user_id, f"Role {i}", f"Prompt {i}",
            )

        count = await conn.fetchval(
            "SELECT COUNT(*) FROM user_roles WHERE user_id = $1", user_id
        )
        assert count == 3

    @pytest.mark.asyncio
    async def test_role_scoped_to_user(self, db_conn_with_user):
        """Verify roles are user-scoped: can't access other user's roles."""
        conn = db_conn_with_user
        user_id = 999999
        other_user_id = 888888

        # Create other user
        await conn.execute("INSERT INTO users (user_id) VALUES ($1)", other_user_id)

        # Create role for other user
        role_id = await conn.fetchval(
            "INSERT INTO user_roles (user_id, title, prompt) VALUES ($1, $2, $3) RETURNING id",
            other_user_id, "Secret Role", "Secret prompt",
        )

        # Try to access as user_id=999999 — should return nothing
        row = await conn.fetchrow(
            "SELECT prompt FROM user_roles WHERE id = $1 AND user_id = $2",
            role_id, user_id,
        )
        assert row is None
