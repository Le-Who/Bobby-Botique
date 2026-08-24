# /app/repos/provider_keys.py
"""Runtime provider API key management via global_settings DB table.

Each provider has a key stored in global_settings as 'provider_key:<name>'.
If no DB override exists, falls back to the env-based Settings value.
5-minute TTL cache prevents per-request DB overhead.

Providers: weather, exchange, pollinations, jina. Other providers use their
own pooled key repositories and are intentionally not mirrored here.
"""

import logging

from app.crypto import encrypt_api_key, is_encrypted, safe_decrypt
from app.repos.settings_repo import delete_global_setting, get_global_setting, set_global_setting

logger = logging.getLogger(__name__)

# Map provider names to their Settings field names (env fallback)
_PROVIDER_ENV_MAP: dict[str, str] = {
    "weather": "WEATHER_API_KEY",
    "exchange": "EXCHANGE_RATE_API_KEY",
    "pollinations": "POLLINATIONS_API_KEY",
    "jina": "JINA_API_KEY",
}


def _db_key(provider: str) -> str:
    """Return the global_settings key_name for a provider."""
    return f"provider_key:{provider}"


async def get_provider_key(provider: str) -> str:
    """Return the active API key for a provider.

    Priority: DB override → env Settings fallback → empty string.
    """
    if provider not in _PROVIDER_ENV_MAP:
        return ""

    db_val = await get_global_setting(_db_key(provider), default="")
    if db_val:
        try:
            if is_encrypted(db_val):
                return safe_decrypt(db_val)

            # Migrate values written before encrypted provider storage existed.
            await set_global_setting(_db_key(provider), encrypt_api_key(db_val))
            logger.info("Legacy provider key encrypted at rest: %s", provider)
            return db_val
        except Exception as exc:
            logger.error("Provider key unavailable for %s (%s)", provider, type(exc).__name__)
            return ""

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
    if provider not in _PROVIDER_ENV_MAP:
        raise ValueError(f"Unsupported runtime key provider: {provider}")
    await set_global_setting(_db_key(provider), encrypt_api_key(value))
    logger.info("Provider key updated: %s", provider)


async def clear_provider_key(provider: str) -> None:
    """Remove the runtime override, falling back to env."""
    if provider not in _PROVIDER_ENV_MAP:
        raise ValueError(f"Unsupported runtime key provider: {provider}")
    await delete_global_setting(_db_key(provider))
    logger.info("Provider key cleared (fallback to env): %s", provider)


async def get_provider_status(provider: str) -> dict[str, str]:
    """Return status info for a provider key.

    Returns dict with 'source' ('db'|'env'|'missing') and 'preview' (masked key).
    """
    if provider not in _PROVIDER_ENV_MAP:
        return {"source": "missing", "preview": "—"}

    db_val = await get_global_setting(_db_key(provider), default="")
    if db_val:
        value = await get_provider_key(provider)
        if value:
            return {"source": "db", "preview": _mask_key(value)}
        return {"source": "missing", "preview": "ошибка расшифровки"}

    env_field = _PROVIDER_ENV_MAP.get(provider)
    if env_field:
        try:
            from app.config import settings

            env_val = getattr(settings, env_field, "") or ""
            if env_val:
                return {"source": "env", "preview": _mask_key(env_val)}
        except Exception:
            pass

    return {"source": "missing", "preview": "—"}


def _mask_key(key: str) -> str:
    """Mask a key for safe display: show first 4 + last 4 chars."""
    if len(key) <= 8:
        return "****"
    return f"{key[:4]}…{key[-4:]}"
