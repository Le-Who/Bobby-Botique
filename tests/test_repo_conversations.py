"""Tests for app.repos.conversations — role data lookup (system roles branch)."""

import pytest

from app.repos.conversations import get_role_data


class TestGetRoleDataSystemRoles:
    """Test the system roles branch of get_role_data (no DB needed)."""

    @pytest.mark.asyncio
    async def test_system_role_returns_data(self):
        """Known system role should return title, prompt, is_custom=False."""
        result = await get_role_data("teacher", user_id=0)
        assert result is not None
        assert result["key"] == "teacher"
        assert result["is_custom"] is False
        assert len(result["prompt"]) > 10
        assert result["title"]  # Non-empty

    @pytest.mark.asyncio
    async def test_none_key_returns_none(self):
        result = await get_role_data(None, user_id=0)
        assert result is None

    @pytest.mark.asyncio
    async def test_empty_key_returns_none(self):
        result = await get_role_data("", user_id=0)
        assert result is None

    @pytest.mark.asyncio
    async def test_unknown_role_returns_none(self):
        result = await get_role_data("nonexistent_role_xyz", user_id=0)
        assert result is None

    @pytest.mark.asyncio
    async def test_all_default_roles_loadable(self):
        """Every role in DEFAULT_ROLES should be retrievable."""
        from app.prompts import DEFAULT_ROLES

        for key in DEFAULT_ROLES:
            result = await get_role_data(key, user_id=0)
            assert result is not None, f"Role {key} not found"
            assert result["key"] == key
