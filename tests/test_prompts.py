"""Tests for app.prompts — system instruction composition, custom role cache, JSON extraction."""

import pytest

from app.prompts import (
    DEFAULT_ROLES,
    cache_custom_role,
    clear_prompt_cache,
    compose_system_instruction,
    extract_json_object,
    get_cached_custom_role,
)

# ═══════════════════════════════════════════════════════════════════════════════
# compose_system_instruction
# ═══════════════════════════════════════════════════════════════════════════════


class TestComposeSystemInstruction:
    """Test the system instruction composition pipeline."""

    def test_no_role_returns_base_prompt(self):
        result = compose_system_instruction(None)
        assert isinstance(result, str)
        assert len(result) > 50  # Base prompt has substantial content

    def test_with_role_appends_role_prompt(self):
        role = "Ты полезный ассистент."
        result = compose_system_instruction(role)
        assert role in result

    def test_compact_mode_produces_shorter_prompt(self):
        role = "Test role"
        compact = compose_system_instruction(role, use_compact=True)
        full = compose_system_instruction(role, use_compact=False)
        # Compact should be shorter or equal (may be same if registry has no compact variant)
        assert len(compact) <= len(full)

    def test_clear_cache_does_not_crash(self):
        """clear_prompt_cache should be safe to call anytime."""
        clear_prompt_cache()
        # After clearing, next compose should still work
        result = compose_system_instruction(None)
        assert isinstance(result, str)

    def test_same_input_returns_consistent_output(self):
        """Caching should return same result for same input."""
        a = compose_system_instruction("role A")
        b = compose_system_instruction("role A")
        assert a == b


# ═══════════════════════════════════════════════════════════════════════════════
# Custom role cache
# ═══════════════════════════════════════════════════════════════════════════════


class TestCustomRoleCache:
    """Test the bounded TTL cache for custom roles."""

    def test_cache_miss_returns_none(self):
        result = get_cached_custom_role("nonexistent-prompt-" + str(id(self)))
        assert result is None

    def test_cache_stores_and_retrieves(self):
        key = f"test-prompt-{id(self)}"
        role = {"title": "Test", "prompt": "Be helpful"}
        cache_custom_role(key, role)
        assert get_cached_custom_role(key) == role

    def test_cache_overwrites_existing(self):
        key = f"overwrite-{id(self)}"
        cache_custom_role(key, {"v": 1})
        cache_custom_role(key, {"v": 2})
        assert get_cached_custom_role(key) == {"v": 2}


# ═══════════════════════════════════════════════════════════════════════════════
# extract_json_object
# ═══════════════════════════════════════════════════════════════════════════════


class TestExtractJsonObject:
    """Test JSON extraction from messy AI model responses."""

    def test_returns_none_for_empty_input(self):
        assert extract_json_object("") is None
        assert extract_json_object(None) is None

    def test_extracts_clean_json(self):
        text = '{"title": "Test", "purpose": "Testing", "prompt": "Be helpful"}'
        result = extract_json_object(text)
        assert result is not None
        assert result["title"] == "Test"

    def test_extracts_from_code_fence(self):
        text = '```json\n{"title": "T", "purpose": "P", "prompt": "X"}\n```'
        result = extract_json_object(text)
        assert result is not None
        assert result["title"] == "T"

    def test_ignores_text_before_json(self):
        text = 'Here is the role:\n{"title": "T", "purpose": "P", "prompt": "X"}'
        result = extract_json_object(text)
        assert result is not None
        assert result["title"] == "T"

    def test_converts_system_prompt_to_prompt(self):
        """If AI returns 'system_prompt' instead of 'prompt', it should convert."""
        text = '{"title": "T", "purpose": "P", "system_prompt": "Be helpful"}'
        result = extract_json_object(text)
        assert result is not None
        assert "prompt" in result

    def test_requires_title_purpose_prompt(self):
        """JSON without required fields should be rejected."""
        text = '{"name": "Missing required fields"}'
        result = extract_json_object(text)
        assert result is None

    def test_returns_none_for_invalid_json(self):
        text = '{"title": "broken'
        result = extract_json_object(text)
        assert result is None


# ═══════════════════════════════════════════════════════════════════════════════
# DEFAULT_ROLES
# ═══════════════════════════════════════════════════════════════════════════════


class TestDefaultRoles:
    """Verify structure of built-in role definitions."""

    def test_default_roles_is_non_empty_dict(self):
        assert isinstance(DEFAULT_ROLES, dict)
        assert len(DEFAULT_ROLES) >= 3

    def test_each_role_has_title_and_prompt(self):
        for key, role in DEFAULT_ROLES.items():
            assert "title" in role, f"Role {key} missing title"
            assert "prompt" in role, f"Role {key} missing prompt"
            assert len(role["prompt"]) > 10, f"Role {key} has suspiciously short prompt"
