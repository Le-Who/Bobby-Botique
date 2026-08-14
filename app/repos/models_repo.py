"""Runtime management for user-selectable provider model catalogs.

Environment variables are the baseline. The database stores only explicit
admin overrides written by ``/models``. This distinction prevents an old,
automatically seeded DB copy from masking a newly deployed environment value.
"""

import logging
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from app.repos.settings_repo import delete_global_setting, get_global_setting, set_global_setting
from app.utils.json_compat import json

logger = logging.getLogger(__name__)

_KEY_PREFIX = "provider_models"
_OVERRIDE_VERSION = 2


class ModelCatalogSource(StrEnum):
    ENV = "env"
    ADMIN = "admin"


class ModelMutationCode(StrEnum):
    ADDED = "added"
    REMOVED = "removed"
    DUPLICATE = "duplicate"
    NOT_FOUND = "not_found"
    INVALID = "invalid"
    UNSUPPORTED = "unsupported"
    VALIDATION_UNAVAILABLE = "validation_unavailable"
    UNKNOWN_PROVIDER = "unknown_provider"


@dataclass(frozen=True)
class ModelCatalog:
    provider: str
    models: tuple[str, ...]
    source: ModelCatalogSource


@dataclass(frozen=True)
class ModelMutationResult:
    code: ModelMutationCode
    provider: str
    model: str
    catalog: ModelCatalog | None = None


@dataclass(frozen=True)
class _ProviderSpec:
    settings_attr: str
    env_var: str


_PROVIDERS: dict[str, _ProviderSpec] = {
    "gemini": _ProviderSpec("AVAILABLE_MODELS", "GEMINI_AVAILABLE_MODELS"),
    "opencode": _ProviderSpec("OPENCODE_AVAILABLE_MODELS", "OPENCODE_AVAILABLE_MODELS"),
    "openrouter": _ProviderSpec("OPENROUTER_AVAILABLE_MODELS", "OPENROUTER_AVAILABLE_MODELS"),
    "freetheai": _ProviderSpec("FREETHEAI_AVAILABLE_MODELS", "FREETHEAI_AVAILABLE_MODELS"),
}
_catalog_sources: dict[str, ModelCatalogSource] = dict.fromkeys(_PROVIDERS, ModelCatalogSource.ENV)


def _db_key(provider: str) -> str:
    return f"{_KEY_PREFIX}:{provider}"


def _provider_spec(provider: str) -> _ProviderSpec:
    try:
        return _PROVIDERS[provider]
    except KeyError as exc:
        raise ValueError(f"Unknown model provider: {provider}") from exc


def _dedupe(models: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for model in models:
        clean = str(model).strip()
        if clean and clean not in seen:
            result.append(clean)
            seen.add(clean)
    return result


def _normalize_models(provider: str, models: list[str]) -> list[str]:
    normalized = _dedupe(models)
    if provider == "gemini":
        from app.config import is_gemini_chat_model_id

        normalized = [model for model in normalized if is_gemini_chat_model_id(model)]
    elif provider == "freetheai":
        from app.config import is_freetheai_chat_model_id

        normalized = [model for model in normalized if is_freetheai_chat_model_id(model)]
    return normalized


def _encode_override(models: list[str]) -> str:
    return json.dumps(
        {"version": _OVERRIDE_VERSION, "source": ModelCatalogSource.ADMIN.value, "models": models},
        ensure_ascii=False,
    )


def _decode_record(raw: str) -> tuple[str, list[str] | None]:
    """Return ``(kind, models)`` where kind is override, legacy, or invalid."""
    try:
        data: Any = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return "invalid", None

    if isinstance(data, list):
        return "legacy", None
    if (
        isinstance(data, dict)
        and data.get("version") == _OVERRIDE_VERSION
        and data.get("source") == ModelCatalogSource.ADMIN.value
        and isinstance(data.get("models"), list)
    ):
        return "override", [str(model) for model in data["models"] if model is not None]
    return "invalid", None


def _provider_defaults(provider: str, settings_obj: Any) -> list[str]:
    if provider == "gemini":
        from app.config import DEFAULT_GEMINI_MODELS

        return DEFAULT_GEMINI_MODELS.copy()
    if provider == "openrouter":
        return [getattr(settings_obj, "OPENROUTER_DEFAULT_MODEL", "")]
    if provider == "opencode":
        return [
            getattr(settings_obj, "OPENCODE_DEFAULT_MODEL", ""),
            getattr(settings_obj, "OPENCODE_QNA_MODEL", ""),
            getattr(settings_obj, "OPENCODE_RESEARCH_MODEL", ""),
            getattr(settings_obj, "OPENCODE_VISION_MODEL", ""),
            getattr(settings_obj, "OPENCODE_INLINE_MODEL", ""),
        ]
    return [getattr(settings_obj, "FREETHEAI_DEFAULT_MODEL", "")]


def _env_baseline(provider: str, settings_obj: Any) -> list[str]:
    from app.config import _load_available_models, is_gemini_chat_model_id

    spec = _provider_spec(provider)
    validator = is_gemini_chat_model_id if provider == "gemini" else None
    models = _load_available_models(
        spec.env_var,
        _provider_defaults(provider, settings_obj),
        validator=validator,
    )
    return _normalize_models(provider, models)


async def sync_models_from_db(settings_obj: Any | None = None) -> None:
    """Apply explicit v2 admin overrides and discard ambiguous legacy copies."""
    from app.config import settings

    target_settings = settings_obj if settings_obj is not None else settings

    for provider, spec in _PROVIDERS.items():
        _catalog_sources[provider] = ModelCatalogSource.ENV
        key = _db_key(provider)
        raw = await get_global_setting(key, default="")
        if not raw:
            logger.info(
                "models_repo: using env catalog for %s (%d model(s))",
                provider,
                len(getattr(target_settings, spec.settings_attr, []) or []),
            )
            continue

        kind, decoded = _decode_record(raw)
        if kind == "legacy":
            await delete_global_setting(key)
            logger.warning("models_repo: removed legacy baseline record for %s; current env now wins", provider)
            continue
        if kind == "invalid" or decoded is None:
            logger.warning("models_repo: ignored invalid catalog record for %s; current env now wins", provider)
            continue

        models = _normalize_models(provider, decoded)
        setattr(target_settings, spec.settings_attr, models)
        _catalog_sources[provider] = ModelCatalogSource.ADMIN
        logger.info("models_repo: loaded %d %s model(s) from admin override", len(models), provider)


async def get_model_catalog(provider: str) -> ModelCatalog:
    from app.config import settings

    spec = _provider_spec(provider)
    models = tuple(getattr(settings, spec.settings_attr, []) or [])
    return ModelCatalog(provider=provider, models=models, source=_catalog_sources[provider])


async def get_models(provider: str) -> list[str]:
    """Return a copy of the active selectable list for *provider*."""
    return list((await get_model_catalog(provider)).models)


async def _persist_admin_override(provider: str, models: list[str]) -> None:
    from app.config import settings

    spec = _provider_spec(provider)
    await set_global_setting(_db_key(provider), _encode_override(models))
    setattr(settings, spec.settings_attr, models)
    _catalog_sources[provider] = ModelCatalogSource.ADMIN


def _is_valid_model_identifier(model_name: str) -> bool:
    return 3 <= len(model_name) <= 200 and not any(character.isspace() for character in model_name)


async def _validate_gemini_model(model_name: str) -> str:
    from app.providers.gemini import validate_gemini_chat_model_capability

    return (await validate_gemini_chat_model_capability(model_name)).value


async def _mutation_result(
    code: ModelMutationCode,
    provider: str,
    model: str,
) -> ModelMutationResult:
    catalog = await get_model_catalog(provider) if provider in _PROVIDERS else None
    return ModelMutationResult(code=code, provider=provider, model=model, catalog=catalog)


async def add_model(provider: str, model_name: str) -> ModelMutationResult:
    """Append a validated model to an explicit admin override."""
    clean = model_name.strip()
    if provider not in _PROVIDERS:
        return await _mutation_result(ModelMutationCode.UNKNOWN_PROVIDER, provider, clean)
    if not _is_valid_model_identifier(clean):
        return await _mutation_result(ModelMutationCode.INVALID, provider, clean)

    current = await get_models(provider)
    if provider == "gemini":
        from app.config import is_gemini_chat_model_id

        if not is_gemini_chat_model_id(clean):
            return await _mutation_result(ModelMutationCode.INVALID, provider, clean)
    elif provider == "freetheai":
        from app.config import is_freetheai_chat_model_id

        if not is_freetheai_chat_model_id(clean):
            return await _mutation_result(ModelMutationCode.INVALID, provider, clean)
    if clean in current:
        return await _mutation_result(ModelMutationCode.DUPLICATE, provider, clean)
    if provider == "gemini":
        validation = await _validate_gemini_model(clean)
        if validation == "unsupported":
            logger.warning("models_repo: rejected unsupported Gemini chat model '%s'", clean)
            return await _mutation_result(ModelMutationCode.UNSUPPORTED, provider, clean)
        if validation != "supported":
            logger.warning("models_repo: Gemini model validation unavailable for '%s'", clean)
            return await _mutation_result(ModelMutationCode.VALIDATION_UNAVAILABLE, provider, clean)

    current.append(clean)
    await _persist_admin_override(provider, current)
    logger.info("models_repo: added model '%s' to %s admin override", clean, provider)
    return await _mutation_result(ModelMutationCode.ADDED, provider, clean)


async def remove_model(provider: str, model_name: str) -> ModelMutationResult:
    """Remove a model from an explicit admin override."""
    clean = model_name.strip()
    if provider not in _PROVIDERS:
        return await _mutation_result(ModelMutationCode.UNKNOWN_PROVIDER, provider, clean)
    if not _is_valid_model_identifier(clean):
        return await _mutation_result(ModelMutationCode.INVALID, provider, clean)

    current = await get_models(provider)
    if clean not in current:
        return await _mutation_result(ModelMutationCode.NOT_FOUND, provider, clean)
    current.remove(clean)
    await _persist_admin_override(provider, current)
    logger.info("models_repo: removed model '%s' from %s admin override", clean, provider)
    return await _mutation_result(ModelMutationCode.REMOVED, provider, clean)


async def reset_models_to_env(provider: str) -> list[str]:
    """Delete the admin override and restore the current env baseline."""
    from app.config import settings

    spec = _provider_spec(provider)
    baseline = _env_baseline(provider, settings)
    await delete_global_setting(_db_key(provider))
    setattr(settings, spec.settings_attr, baseline)
    _catalog_sources[provider] = ModelCatalogSource.ENV
    logger.info("models_repo: reset %s catalog to env (%d model(s))", provider, len(baseline))
    return baseline
