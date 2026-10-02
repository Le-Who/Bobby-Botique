"""Shared durable catalogs and local Gemini requests-per-day controls.

The environment remains the catalog baseline. Runtime entries are explicit
admin overrides, including an empty catalog or a ``None`` local RPD limit.
"""

from __future__ import annotations

import asyncio
from typing import Any

from app.runtime_settings.store import (
    RevisionConflict,
    SettingsSnapshot,
    get_snapshot,
    reset_value,
    set_value,
    update_values,
)

PROVIDERS = {
    "gemini": "AVAILABLE_MODELS",
    "opencode": "OPENCODE_AVAILABLE_MODELS",
    "openrouter": "OPENROUTER_AVAILABLE_MODELS",
    "freetheai": "FREETHEAI_AVAILABLE_MODELS",
}


class UnsupportedModelError(ValueError):
    """Gemini confirms that a model cannot serve chat requests."""


class ModelValidationUnavailableError(ValueError):
    """The Gemini Models API could not confirm chat capability."""


def validate_model_id(model: Any) -> str:
    if not isinstance(model, str) or not 3 <= len(model) <= 200 or any(c.isspace() or ord(c) < 32 for c in model):
        raise ValueError("Некорректный model ID")
    return model


def validate_catalog(provider: str, models: Any) -> list[str]:
    from app.config import is_freetheai_chat_model_id, is_gemini_chat_model_id

    if provider not in PROVIDERS:
        raise ValueError(f"Unknown model provider: {provider}")
    if not isinstance(models, (list, tuple)) or len(models) > 100:
        raise ValueError("Каталог должен содержать не более 100 моделей")
    clean = [validate_model_id(model) for model in models]
    if len(set(clean)) != len(clean):
        raise ValueError("Модели не должны повторяться")
    for model in clean:
        if provider == "gemini" and not is_gemini_chat_model_id(model):
            raise ValueError("Ожидается Gemini chat model ID")
        if provider == "freetheai" and not is_freetheai_chat_model_id(model):
            raise ValueError("Ожидается FreeTheAI chat model ID")
        if provider == "opencode" and not model.startswith("opencode-go/"):
            raise ValueError("Ожидается opencode-go/model")
    return clean


def validate_limit(model: str, limit: Any) -> int | None:
    from app.config import is_gemini_chat_model_id

    validate_model_id(model)
    if not is_gemini_chat_model_id(model):
        raise ValueError("Этот счётчик поддерживает только Gemini RPD на ключ")
    if any(marker in model.lower() for marker in ("embedding", "-image", "-live")):
        raise ValueError("Этот исполнитель не использует общий Gemini RPD: image, embeddings и Live имеют другой путь")
    if limit is not None and (type(limit) is not int or not 1 <= limit <= 1_000_000):
        raise ValueError("Лимит — целое число 1…1000000 либо null (без локального ограничения)")
    return limit


_applied: set[str] = set()
_CATALOG_REFRESH_TIMEOUT = 5.0


async def refresh_catalogs(
    settings_obj: Any | None = None, *, force: bool = False, snapshot: SettingsSnapshot | None = None
) -> SettingsSnapshot:
    """Apply runtime entries to live settings, restoring env after an override is removed."""
    from app.config import settings
    from app.repos.models_repo import (
        ModelCatalogSource,
        _catalog_sources,
        _db_key,
        _decode_record,
        _env_baseline,
        _normalize_models,
        get_global_setting,
    )

    target = settings_obj if settings_obj is not None else settings
    snapshot = snapshot if snapshot is not None else await get_snapshot(force=force)
    staged = {}
    # A restore may reveal an old v2 override. Finish all bounded reads and
    # validation before publishing, so an unavailable DB preserves every
    # last-confirmed catalog instead of applying half of a revision.
    async with asyncio.timeout(_CATALOG_REFRESH_TIMEOUT):
        for provider in PROVIDERS:
            key = f"catalog:{provider}"
            if key in snapshot.values:
                staged[provider] = (validate_catalog(provider, snapshot.values[key]), ModelCatalogSource.ADMIN)
            elif snapshot.values.get(f"catalog_baseline:{provider}") is True or provider in _applied:
                baseline = _env_baseline(provider, target)
                source = ModelCatalogSource.ENV
                if snapshot.values.get(f"catalog_baseline:{provider}") is not True:
                    kind, legacy = _decode_record(await get_global_setting(_db_key(provider), default=""))
                    if kind == "override" and legacy is not None:
                        baseline = _normalize_models(provider, legacy)
                        source = ModelCatalogSource.ADMIN
                staged[provider] = (baseline, source)
    for provider, (catalog, source) in staged.items():
        setattr(target, PROVIDERS[provider], catalog)
        _catalog_sources[provider] = source
    _applied.clear()
    _applied.update(
        provider
        for provider in PROVIDERS
        if f"catalog:{provider}" in snapshot.values or snapshot.values.get(f"catalog_baseline:{provider}") is True
    )
    return snapshot


async def list_catalogs(*, snapshot: SettingsSnapshot | None = None) -> list[dict[str, Any]]:
    from app.config import settings
    from app.repos.models_repo import _catalog_sources

    await refresh_catalogs(snapshot=snapshot)
    return [
        {
            "provider": provider,
            "models": list(getattr(settings, attr, []) or []),
            "source": _catalog_sources[provider].value,
        }
        for provider, attr in PROVIDERS.items()
    ]


async def list_limits(*, snapshot: SettingsSnapshot | None = None) -> list[dict[str, Any]]:
    from app.config import settings
    from app.process_policies import PROCESSES, baseline_models, resolve_policy_value
    from app.repos.keys import get_model_daily_limit
    from app.runtime_settings.lifecycle import runtime_settings_scope

    snapshot = snapshot if snapshot is not None else await get_snapshot()
    models = set(settings.DAILY_LIMITS) | set(snapshot.values.get("catalog:gemini", settings.AVAILABLE_MODELS))
    models.update(key.removeprefix("model_limit:") for key in snapshot.values if key.startswith("model_limit:"))
    # Routes can use a model absent from both the public selector and DAILY_LIMITS.
    # Resolve against the same snapshot that supplies the displayed quota value.
    async with runtime_settings_scope(snapshot):
        for process_id, spec in PROCESSES.items():
            if set(spec.capabilities) & {"embedding", "image_output", "live", "provider_order", "music"}:
                continue
            raw = snapshot.values.get(f"process:{process_id}")
            baseline = await baseline_models(process_id) if raw is None or raw.get("inherit_user_model") else ()
            models.update(resolve_policy_value(process_id, raw, baseline, revision=snapshot.revision).models)
    supported = []
    for model in models:
        try:
            validate_limit(model, None)
        except ValueError:
            continue
        supported.append(model)
    return [
        {
            "model": model,
            "limit": await get_model_daily_limit(model, snapshot=snapshot),
            "source": "admin" if f"model_limit:{model}" in snapshot.values else "database/env",
            "scope": "gemini_key_rpd",
            "note": (
                "Общий счётчик Gemini на модель и ключ для text/ASR/TTS. "
                "Изображения используют IMAGE_GEN_RPD_PER_KEY; embeddings и Live не резервируют этот счётчик."
            ),
        }
        for model in sorted(supported)
    ]


async def save_catalog(
    provider: str,
    models: Any,
    *,
    expected_revision: int,
    actor: str,
    allow_unverified: bool = False,
) -> SettingsSnapshot:
    """Replace one catalog with CAS; verify only newly introduced Gemini IDs."""
    from app.config import settings
    from app.repos.models_repo import _validate_gemini_model

    clean = validate_catalog(provider, models)
    snapshot = await refresh_catalogs()
    if snapshot.degraded:
        raise ConnectionError("Runtime catalog state is unavailable")
    if snapshot.revision != expected_revision:
        raise RevisionConflict("Runtime settings revision changed; refresh before saving")
    if provider == "gemini" and not allow_unverified:
        existing = set(getattr(settings, PROVIDERS[provider], []) or [])
        for model in clean:
            if model in existing:
                continue
            capability = await _validate_gemini_model(model)
            if capability == "unsupported":
                raise UnsupportedModelError(f"Gemini model {model} does not support chat generation")
            if capability != "supported":
                raise ModelValidationUnavailableError(f"Gemini model {model} could not be verified")
    saved = await set_value(f"catalog:{provider}", clean, expected_revision=expected_revision, actor=actor)
    await refresh_catalogs(force=True)
    return saved


async def reset_catalog(provider: str, *, expected_revision: int, actor: str) -> SettingsSnapshot:
    from app.config import settings
    from app.repos.models_repo import ModelCatalogSource, _catalog_sources, _env_baseline

    if provider not in PROVIDERS:
        raise ValueError(f"Unknown model provider: {provider}")
    current = await get_snapshot(force=True)
    if current.degraded:
        raise ConnectionError("Runtime catalog state is unavailable")
    if current.revision != expected_revision:
        raise RevisionConflict("Runtime settings revision changed; refresh before saving")
    # The durable baseline marker shadows historical v2 records, including on
    # another replica/startup. Reset and marker publish in one CAS; a conflict
    # cannot delete a legacy catalog or resurrect it later.
    saved = await update_values(
        {f"catalog_baseline:{provider}": True},
        removals=(f"catalog:{provider}",),
        expected_revision=expected_revision,
        actor=actor,
    )
    setattr(settings, PROVIDERS[provider], _env_baseline(provider, settings))
    _catalog_sources[provider] = ModelCatalogSource.ENV
    _applied.discard(provider)
    await refresh_catalogs(force=True)
    return saved


async def save_limit(model: str, limit: Any, *, expected_revision: int, actor: str) -> SettingsSnapshot:
    from app.repos.keys import invalidate_model_limit_cache

    saved = await set_value(
        f"model_limit:{model}", validate_limit(model, limit), expected_revision=expected_revision, actor=actor
    )
    await invalidate_model_limit_cache(model)
    return saved


async def reset_limit(model: str, *, expected_revision: int, actor: str) -> SettingsSnapshot:
    from app.repos.keys import invalidate_model_limit_cache

    validate_limit(model, None)
    saved = await reset_value(f"model_limit:{model}", expected_revision=expected_revision, actor=actor)
    await invalidate_model_limit_cache(model)
    return saved
