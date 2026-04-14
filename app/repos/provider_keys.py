# /app/repos/provider_keys.py
"""Runtime provider API key management via global_settings DB table.

Each provider has a key stored in global_settings as 'provider_key:<name>'.
If no DB override exists, falls back to the env-based Settings value.
5-minute TTL cache prevents per-request DB overhead.

Providers: gemini, tavily, weather, exchange, pollinations, elevenlabs, jina
"""

import logging

from app.repos.settings_repo import get_global_setting, set_global_setting

logger = logging.getLogger(__name__)

# Map provider names to their Settings field names (env fallback)
_PROVIDER_ENV_MAP: dict[str, str] = {
    "weather": "WEATHER_API_KEY",
    "exchange": "EXCHANGE_RATE_API_KEY",
    "pollinations": "POLLINATIONS_API_KEY",
    "jina": "JINA_API_KEY",
}

# Providers with list-based keys (multiple keys)
_LIST_PROVIDERS: set[str] = {"gemini", "tavily", "openrouter", "elevenlabs"}


def _db_key(provider: str) -> str:
    """Return the global_settings key_name for a provider."""
    return f"provider_key:{provider}"


async def get_provider_key(provider: str) -> str:
    """Return the active API key for a provider.

    Priority: DB override → env Settings fallback → empty string.
    """
    db_val = await get_global_setting(_db_key(provider), default="")
    if db_val:
        return db_val

    # Fallback to env
    env_field = _PROVIDER_ENV_MAP.get(provider)
    if env_field:
        try:
            from app.config import settings

            return getattr(settings, env_field, "") or ""
        except Exception:
            return ""

    return ""


async def set_provider_key(provider: str, value: str) -> None:
    """Store a runtime override for a provider key."""
    await set_global_setting(_db_key(provider), value)
    logger.info("Provider key updated: %s", provider)


async def clear_provider_key(provider: str) -> None:
    """Remove the runtime override, falling back to env."""
    await set_global_setting(_db_key(provider), "")
    logger.info("Provider key cleared (fallback to env): %s", provider)


async def get_provider_status(provider: str) -> dict[str, str]:
    """Return status info for a provider key.

    Returns dict with 'source' ('db'|'env'|'missing') and 'preview' (masked key).
    """
    db_val = await get_global_setting(_db_key(provider), default="")
    if db_val:
        return {"source": "db", "preview": _mask_key(db_val)}

    env_field = _PROVIDER_ENV_MAP.get(provider)
    if env_field:
        try:
            from app.config import settings

            env_val = getattr(settings, env_field, "") or ""
            if env_val:
                return {"source": "env", "preview": _mask_key(env_val)}
        except Exception:
            pass

    # List-based providers (gemini, tavily, etc.)
    if provider in _LIST_PROVIDERS:
        try:
            from app.config import settings

            field_map = {
                "gemini": "GEMINI_API_KEYS",
                "tavily": "TAVILY_API_KEYS",
                "openrouter": "OPENROUTER_API_KEYS",
                "elevenlabs": "ELEVENLABS_API_KEYS",
            }
            keys = getattr(settings, field_map.get(provider, ""), []) or []
            if keys:
                return {"source": "env", "preview": f"{len(keys)} key(s)"}
        except Exception:
            pass

    return {"source": "missing", "preview": "—"}


def _mask_key(key: str) -> str:
    """Mask a key for safe display: show first 4 + last 4 chars."""
    if len(key) <= 8:
        return "****"
    return f"{key[:4]}…{key[-4:]}"
