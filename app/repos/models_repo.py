# /app/repos/models_repo.py
"""Runtime model list management via global_settings DB.

Stored as JSON lists under keys:
  'provider_models:gemini'     → list[str]
  'provider_models:openrouter' → list[str]

On bot startup call sync_models_from_db() to restore the persisted list into
the in-memory settings object. Admin mutations (add/remove/reset) write to the
DB and immediately mutate the live settings list, so the change is zero-latency
for all in-flight Telegram requests.

Terminology:
  - "env baseline"  = the list baked into the process at start from .env /
                      DEFAULT_GEMINI_MODELS (before any DB overrides).
  - "db override"   = admin-written list that supersedes the env baseline.
"""

import json
import logging

from app.repos.settings_repo import get_global_setting, set_global_setting

logger = logging.getLogger(__name__)

# DB key prefix
_KEY_PREFIX = "provider_models"


def _db_key(provider: str) -> str:
    return f"{_KEY_PREFIX}:{provider}"


# ── Serialisation helpers ─────────────────────────────────────────────────────


def _encode(models: list[str]) -> str:
    return json.dumps(models, ensure_ascii=False)


def _decode(raw: str) -> list[str]:
    try:
        data = json.loads(raw)
        if isinstance(data, list):
            return [str(m).strip() for m in data if m]
    except (json.JSONDecodeError, TypeError):
        pass
    return []


# ── Startup hook ─────────────────────────────────────────────────────────────


async def sync_models_from_db() -> None:
    """Load any DB-persisted model lists and apply them to the live settings.

    Called once during bot startup and after every admin mutation so the in-
    memory settings always reflect the source of truth.

    Contract:
      * If DB key is empty/missing we seed the DB from the current .env list so
        the next restart is also consistent.
      * We never wipe an existing .env list or DEFAULT_GEMINI_MODELS cold.
    """
    from app.config import settings

    for provider, attr in (
        ("gemini", "AVAILABLE_MODELS"),
        ("openrouter", "OPENROUTER_AVAILABLE_MODELS"),
        ("opencode", "OPENCODE_AVAILABLE_MODELS"),
    ):
        raw = await get_global_setting(_db_key(provider), default="")
        if raw:
            loaded = _decode(raw)
            if loaded:
                setattr(settings, attr, loaded)
                logger.info(
                    "models_repo: loaded %d %s model(s) from DB: %s",
                    len(loaded),
                    provider,
                    loaded,
                )
                continue

        # No DB override yet — seed it from the current in-memory list so the
        # first startup produces a stable baseline without DB data loss.
        current: list[str] = getattr(settings, attr, []) or []
        if current:
            await set_global_setting(_db_key(provider), _encode(current))
            logger.info(
                "models_repo: seeded DB baseline with %d %s model(s)",
                len(current),
                provider,
            )


# ── Mutations ─────────────────────────────────────────────────────────────────


async def get_models(provider: str) -> list[str]:
    """Return the active model list for *provider* ('gemini' | 'openrouter')."""
    from app.config import settings

    if provider == "gemini":
        attr = "AVAILABLE_MODELS"
    elif provider == "openrouter":
        attr = "OPENROUTER_AVAILABLE_MODELS"
    else:
        attr = "OPENCODE_AVAILABLE_MODELS"
    return list(getattr(settings, attr, []) or [])


async def add_model(provider: str, model_name: str) -> bool:
    """Append *model_name* to the live list and persist.

    Returns False if already present, True on success.
    """
    from app.config import settings

    if provider == "gemini":
        attr = "AVAILABLE_MODELS"
    elif provider == "openrouter":
        attr = "OPENROUTER_AVAILABLE_MODELS"
    else:
        attr = "OPENCODE_AVAILABLE_MODELS"
    current: list[str] = list(getattr(settings, attr, []) or [])

    clean = model_name.strip()
    if not clean:
        return False
    if clean in current:
        return False

    current.append(clean)
    setattr(settings, attr, current)
    await set_global_setting(_db_key(provider), _encode(current))
    logger.info("models_repo: added model '%s' to %s list", clean, provider)
    return True


async def remove_model(provider: str, model_name: str) -> bool:
    """Remove *model_name* from the live list and persist.

    Returns False if not present, True on success.
    """
    from app.config import settings

    if provider == "gemini":
        attr = "AVAILABLE_MODELS"
    elif provider == "openrouter":
        attr = "OPENROUTER_AVAILABLE_MODELS"
    else:
        attr = "OPENCODE_AVAILABLE_MODELS"
    current: list[str] = list(getattr(settings, attr, []) or [])

    clean = model_name.strip()
    if clean not in current:
        return False

    current.remove(clean)
    setattr(settings, attr, current)
    await set_global_setting(_db_key(provider), _encode(current))
    logger.info("models_repo: removed model '%s' from %s list", clean, provider)
    return True


async def reset_models_to_env(provider: str) -> list[str]:
    """Drop the DB override and restore the original .env / DEFAULT list.

    The restored list is immediately applied to live settings and written
    back to the DB so the next startup is also consistent.
    """
    from app.config import DEFAULT_GEMINI_MODELS, settings

    if provider == "gemini":
        # Re-read from env; fall back to hardcoded defaults
        from app.config import _load_and_clean_keys  # type: ignore[attr-defined]

        try:
            env_list = _load_and_clean_keys("GEMINI_AVAILABLE_MODELS", required=False) or DEFAULT_GEMINI_MODELS.copy()
        except Exception:
            env_list = DEFAULT_GEMINI_MODELS.copy()
        settings.AVAILABLE_MODELS = env_list
        await set_global_setting(_db_key(provider), _encode(env_list))
        logger.info("models_repo: reset gemini list to env (%d models)", len(env_list))
        return env_list
    elif provider == "openrouter":
        try:
            from app.config import _load_and_clean_keys  # type: ignore[attr-defined]

            env_list = _load_and_clean_keys("OPENROUTER_AVAILABLE_MODELS", required=False) or []
        except Exception:
            env_list = []
        settings.OPENROUTER_AVAILABLE_MODELS = env_list
        await set_global_setting(_db_key(provider), _encode(env_list))
        logger.info("models_repo: reset openrouter list to env (%d models)", len(env_list))
        return env_list
    else:  # opencode
        try:
            from app.config import _load_and_clean_keys  # type: ignore[attr-defined]

            env_list = _load_and_clean_keys("OPENCODE_AVAILABLE_MODELS", required=False) or []
        except Exception:
            env_list = []
        settings.OPENCODE_AVAILABLE_MODELS = env_list
        await set_global_setting(_db_key(provider), _encode(env_list))
        logger.info("models_repo: reset opencode list to env (%d models)", len(env_list))
        return env_list

