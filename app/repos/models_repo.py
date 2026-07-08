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

import logging

from app.repos.settings_repo import get_global_setting, set_global_setting
from app.utils.json_compat import json

logger = logging.getLogger(__name__)

# DB key prefix
_KEY_PREFIX = "provider_models"


def _db_key(provider: str) -> str:
    return f"{_KEY_PREFIX}:{provider}"


def _provider_attr(provider: str) -> str:
    if provider == "gemini":
        return "AVAILABLE_MODELS"
    if provider == "openrouter":
        return "OPENROUTER_AVAILABLE_MODELS"
    return "OPENCODE_AVAILABLE_MODELS"


def _sanitize_persisted_gemini_models(models: list[str]) -> list[str]:
    """Normalize DB-backed Gemini chat models to the current supported set.

    The DB may contain older admin overrides from before a model migration.
    A legacy Gemini chat model in position 0 meant "primary Gemini", so map it
    to the current primary model instead of simply dropping it and changing the
    fallback order.
    """
    from app.config import (
        CURRENT_GEMINI_MODELS,
        DEFAULT_GEMINI_MODELS,
        GEMINI_PRIMARY_MODEL,
    )

    normalized: list[str] = []
    current = set(CURRENT_GEMINI_MODELS)
    for model in models:
        clean = model.strip()
        if not clean:
            continue
        if clean in current:
            normalized.append(clean)
        elif clean.startswith("gemini-"):
            normalized.append(GEMINI_PRIMARY_MODEL)
    sanitized = _dedupe_current_gemini_models(normalized)
    return sanitized or DEFAULT_GEMINI_MODELS.copy()


def _dedupe_current_gemini_models(models: list[str]) -> list[str]:
    from app.config import CURRENT_GEMINI_MODELS

    seen: set[str] = set()
    result: list[str] = []
    allowed = set(CURRENT_GEMINI_MODELS)
    for model in models:
        clean = model.strip()
        if clean in allowed and clean not in seen:
            result.append(clean)
            seen.add(clean)
    return result


def _sanitize_env_gemini_models(models: list[str], *, include_defaults: bool = False) -> list[str]:
    from app.config import DEFAULT_GEMINI_MODELS

    sanitized = _dedupe_current_gemini_models(models)
    if sanitized or not include_defaults:
        return sanitized
    return DEFAULT_GEMINI_MODELS.copy()


def _is_current_gemini_model(model_name: str) -> bool:
    from app.config import CURRENT_GEMINI_MODELS

    return model_name in CURRENT_GEMINI_MODELS


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
        key = _db_key(provider)
        raw = await get_global_setting(key, default="")
        if raw:
            loaded = _decode(raw)
            if provider == "gemini":
                sanitized = _sanitize_persisted_gemini_models(loaded)
                if sanitized:
                    setattr(settings, attr, sanitized)
                    if sanitized != loaded:
                        await set_global_setting(key, _encode(sanitized))
                        logger.warning(
                            "models_repo: sanitized DB Gemini model list from %s to %s",
                            loaded,
                            sanitized,
                        )
                    logger.info(
                        "models_repo: loaded %d %s model(s) from DB: %s",
                        len(sanitized),
                        provider,
                        sanitized,
                    )
                    continue
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
        if provider == "gemini":
            current = _sanitize_env_gemini_models(current, include_defaults=True)
            setattr(settings, attr, current)
        if current:
            await set_global_setting(key, _encode(current))
            logger.info(
                "models_repo: seeded DB baseline with %d %s model(s)",
                len(current),
                provider,
            )


# ── Mutations ─────────────────────────────────────────────────────────────────


async def get_models(provider: str) -> list[str]:
    """Return the active model list for *provider* ('gemini' | 'openrouter')."""
    from app.config import settings

    attr = _provider_attr(provider)
    models = list(getattr(settings, attr, []) or [])
    if provider == "gemini":
        sanitized = _sanitize_env_gemini_models(models)
        if sanitized != models:
            setattr(settings, attr, sanitized)
        return sanitized
    return models


async def add_model(provider: str, model_name: str) -> bool:
    """Append *model_name* to the live list and persist.

    Returns False if already present, True on success.
    """
    from app.config import settings

    attr = _provider_attr(provider)
    current: list[str] = list(getattr(settings, attr, []) or [])
    if provider == "gemini":
        current = _sanitize_env_gemini_models(current)
        setattr(settings, attr, current)

    clean = model_name.strip()
    if not clean:
        return False
    if provider == "gemini" and not _is_current_gemini_model(clean):
        logger.warning("models_repo: rejected unsupported Gemini chat model '%s'", clean)
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

    attr = _provider_attr(provider)
    current: list[str] = list(getattr(settings, attr, []) or [])
    if provider == "gemini":
        current = _sanitize_env_gemini_models(current)
        setattr(settings, attr, current)

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
        from app.config import (
            GEMINI_ECONOMY_MODEL,
            GEMINI_PRIMARY_MODEL,
            _filter_current_gemini_models,
            _load_and_clean_keys,
            normalize_gemini_chat_model,
        )

        try:
            env_list = _load_and_clean_keys("GEMINI_AVAILABLE_MODELS", required=False) or DEFAULT_GEMINI_MODELS.copy()
        except Exception:
            env_list = DEFAULT_GEMINI_MODELS.copy()

        env_list = _filter_current_gemini_models(env_list, include_defaults=False)
        role_models = [
            normalize_gemini_chat_model(getattr(settings, "DEFAULT_MODEL", None), fallback=GEMINI_PRIMARY_MODEL),
            normalize_gemini_chat_model(getattr(settings, "QNA_MODEL", None), fallback=GEMINI_ECONOMY_MODEL),
            normalize_gemini_chat_model(getattr(settings, "INLINE_MODEL", None), fallback=GEMINI_ECONOMY_MODEL),
            normalize_gemini_chat_model(getattr(settings, "RESEARCH_MODEL", None), fallback=GEMINI_PRIMARY_MODEL),
            normalize_gemini_chat_model(getattr(settings, "URL_SELECTION_MODEL", None), fallback=GEMINI_ECONOMY_MODEL),
            normalize_gemini_chat_model(getattr(settings, "TAXONOMY_MODEL", None), fallback=GEMINI_ECONOMY_MODEL),
        ]
        env_list = _filter_current_gemini_models(env_list + role_models)

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
            
        if settings.OPENROUTER_DEFAULT_MODEL and settings.OPENROUTER_DEFAULT_MODEL not in env_list:
            env_list.append(settings.OPENROUTER_DEFAULT_MODEL)
            
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
            
        for role_model in (
            settings.OPENCODE_DEFAULT_MODEL,
            settings.OPENCODE_QNA_MODEL,
            settings.OPENCODE_RESEARCH_MODEL,
            settings.OPENCODE_VISION_MODEL,
            settings.OPENCODE_INLINE_MODEL,
        ):
            if role_model and role_model not in env_list:
                env_list.append(role_model)
                
        settings.OPENCODE_AVAILABLE_MODELS = env_list
        await set_global_setting(_db_key(provider), _encode(env_list))
        logger.info("models_repo: reset opencode list to env (%d models)", len(env_list))
        return env_list
