"""Integration tests for custom-role APIs and user scoping."""

import pytest

from app.repos import conversations, roles

pytestmark = [pytest.mark.integration, pytest.mark.asyncio, pytest.mark.usefixtures("force_test_db_conn")]


async def _create_role(conn, user_id, title="Writer", prompt="You are a creative writer"):
    await roles.create_custom_role(user_id, title, prompt)
    role_id = await conn.fetchval("SELECT id FROM user_roles WHERE user_id = $1 AND title = $2", user_id, title)
    assert role_id is not None
    return role_id


class TestCustomRolesCRUD:
    async def test_create_and_list_roles(self, db_conn_with_user, test_user_id):
        await roles.create_custom_role(test_user_id, "Teacher", "You are a helpful teacher")
        await roles.create_custom_role(test_user_id, "Developer", "You are a senior dev")

        rows = await roles.get_user_custom_roles(test_user_id)
        assert len(rows) == 2
        assert {row["title"] for row in rows} == {"Teacher", "Developer"}
        full = await roles.get_user_custom_roles_full(test_user_id)
        assert {row["title"]: row["prompt"] for row in full} == {
            "Teacher": "You are a helpful teacher",
            "Developer": "You are a senior dev",
        }
        assert await db_conn_with_user.fetchval("SELECT COUNT(*) FROM user_roles WHERE user_id = $1", test_user_id) == 2

    async def test_get_role_prompt(self, db_conn_with_user, test_user_id):
        role_id = await _create_role(db_conn_with_user, test_user_id)

        assert await roles.get_custom_role_prompt(role_id, test_user_id) == "You are a creative writer"

    async def test_rename_role(self, db_conn_with_user, test_user_id):
        role_id = await _create_role(db_conn_with_user, test_user_id, title="Old Name")

        await roles.rename_custom_role(role_id, test_user_id, "New Name")

        assert await db_conn_with_user.fetchval("SELECT title FROM user_roles WHERE id = $1", role_id) == "New Name"

    async def test_update_role_prompt(self, db_conn_with_user, test_user_id):
        role_id = await _create_role(db_conn_with_user, test_user_id)

        assert await roles.update_custom_role_prompt(role_id, test_user_id, "Updated prompt") is True

        assert (
            await db_conn_with_user.fetchval("SELECT prompt FROM user_roles WHERE id = $1", role_id) == "Updated prompt"
        )

    async def test_delete_role(self, db_conn_with_user, test_user_id):
        role_id = await _create_role(db_conn_with_user, test_user_id)

        await roles.delete_custom_role(role_id, test_user_id)

        assert await db_conn_with_user.fetchrow("SELECT id FROM user_roles WHERE id = $1", role_id) is None
        assert await roles.get_custom_role_prompt(role_id, test_user_id) is None

    async def test_role_count(self, db_conn_with_user, test_user_id):
        assert await roles.get_custom_role_count(test_user_id) == 0
        for index in range(3):
            await roles.create_custom_role(test_user_id, f"Role {index}", f"Prompt {index}")
        assert await roles.get_custom_role_count(test_user_id) == 3

    async def test_role_scoped_to_user(self, db_conn_with_user, test_user_id):
        other_user_id = 888888
        await db_conn_with_user.execute("INSERT INTO users (user_id) VALUES ($1)", other_user_id)
        role_id = await _create_role(db_conn_with_user, other_user_id, "Secret Role", "Secret prompt")

        assert await roles.get_custom_role_prompt(role_id, test_user_id) is None
        assert await roles.get_user_custom_roles(test_user_id) == []
        assert await roles.get_user_custom_roles_full(test_user_id) == []
        assert await roles.get_custom_role_count(test_user_id) == 0
        assert await conversations.get_role_data(f"user_role:{role_id}", test_user_id) is None

        await roles.rename_custom_role(role_id, test_user_id, "Foreign rename")
        assert await roles.update_custom_role_prompt(role_id, test_user_id, "Foreign prompt") is False
        await roles.delete_custom_role(role_id, test_user_id)

        row = await db_conn_with_user.fetchrow("SELECT title, prompt FROM user_roles WHERE id = $1", role_id)
        assert dict(row) == {"title": "Secret Role", "prompt": "Secret prompt"}
        assert await roles.get_custom_role_prompt(role_id, other_user_id) == "Secret prompt"
