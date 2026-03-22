"""
Repository for custom user roles CRUD operations.
"""

from typing import Any

from app import database as db


async def get_user_custom_roles(user_id: int) -> list[dict[str, Any]]:
    """Returns all custom roles for a user, newest first."""
    return await db.db_query(
        "SELECT id, title FROM user_roles WHERE user_id = $1 ORDER BY created_at DESC",
        (user_id,),
    )


async def get_user_custom_roles_full(user_id: int) -> list[dict[str, Any]]:
    """Returns all custom roles with prompts for a user."""
    return await db.db_query(
        "SELECT id, title, prompt FROM user_roles WHERE user_id = $1",
        (user_id,),
    )


async def get_custom_role_count(user_id: int) -> int:
    """Returns number of custom roles for a user."""
    result = await db.db_query("SELECT COUNT(*) as count FROM user_roles WHERE user_id = $1", (user_id,))
    return result[0]["count"] if result else 0


async def get_custom_role_prompt(role_id: int, user_id: int) -> str | None:
    """Returns the prompt for a specific custom role, or None if not found."""
    result = await db.db_query(
        "SELECT prompt FROM user_roles WHERE id = $1 AND user_id = $2",
        (role_id, user_id),
    )
    return result[0]["prompt"] if result else None


async def create_custom_role(user_id: int, title: str, prompt: str) -> None:
    """Creates a new custom role for a user."""
    await db.db_query(
        "INSERT INTO user_roles (user_id, title, prompt) VALUES ($1, $2, $3)",
        (user_id, title, prompt),
    )


async def delete_custom_role(role_id: int, user_id: int) -> None:
    """Deletes a custom role by ID (scoped to user_id for security)."""
    await db.db_query("DELETE FROM user_roles WHERE id = $1 AND user_id = $2", (role_id, user_id))


async def rename_custom_role(role_id: int, user_id: int, new_title: str) -> None:
    """Renames a custom role."""
    await db.db_query(
        "UPDATE user_roles SET title = $1 WHERE id = $2 AND user_id = $3",
        (new_title, role_id, user_id),
    )


async def update_custom_role_prompt(role_id: int, user_id: int, new_prompt: str) -> bool:
    """Updates the prompt of a custom role. Returns True if updated."""
    result = await db.db_query(
        "UPDATE user_roles SET prompt = $1 WHERE id = $2 AND user_id = $3 RETURNING id",
        (new_prompt, role_id, user_id),
    )
    return bool(result)
