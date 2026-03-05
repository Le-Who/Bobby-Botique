# /app/prompts.py
# Backward-compat re-exports — all code moved to proper homes.
# New code should import from the canonical locations directly.

from app.prompt_registry import DEFAULT_ROLES, get_registry  # noqa: F401
from app.utils.json_utils import extract_json_object  # noqa: F401

# ============================================================================
# COMPOSE HELPERS — delegates to PromptRegistry
# ============================================================================


def compose_system_instruction(role_prompt: str | None, use_compact: bool = True) -> str:
    """Compose system instruction. Prefer get_registry().compose_system_prompt()."""
    registry = get_registry()
    return registry.compose_system_prompt(role_prompt=role_prompt, use_compact=use_compact)


def clear_prompt_cache():
    """Clear composed prompt caches."""
    registry = get_registry()
    registry.compose_system_prompt.cache_clear()


# ============================================================================
# CUSTOM ROLE CACHE — bounded with TTL
# ============================================================================
from cachetools import TTLCache  # noqa: E402

_custom_role_cache: TTLCache = TTLCache(maxsize=256, ttl=3600)


def get_cached_custom_role(prompt: str) -> dict | None:
    """Get cached custom role by prompt."""
    return _custom_role_cache.get(prompt)


def cache_custom_role(prompt: str, role: dict):
    """Cache a custom role (auto-evicts oldest on overflow)."""
    _custom_role_cache[prompt] = role
