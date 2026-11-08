"""Atomic, reversible profiles for latency-sensitive text and image analysis.

Profiles materialize ordinary process policies so execution, previews, caches
and operation snapshots keep using the same route contract. The original
absence of a policy is retained separately from an explicit Gemini plan.
"""

from collections.abc import Mapping
from typing import Any

from app.runtime_settings import store

LIVE_PROCESS_IDS = (
    "chat",
    "inline",
    "search",
    "document",
    "vision",
    "roles.generate",
    "roles.edit",
    "trivia.explain",
    "tarot.chat",
    "tarot.inline",
    "compatibility",
    "board.synthesis",
    "horoscope",
    "vision.intent",
    "vision.search_query",
    "image.prompt_extract",
    "image.translate",
    "intent.voice",
    "media.intent",
    "crocodile.judge",
    "memory.expand",
    "memory.relevance",
)
_FAST_PROCESSES = frozenset(
    {
        "inline",
        "vision.intent",
        "image.prompt_extract",
        "image.translate",
        "intent.voice",
        "media.intent",
        "memory.expand",
        "memory.relevance",
    }
)
PROVIDERS = ("baseline", "gemini", "openai")


def active_provider(snapshot: store.SettingsSnapshot | None = None) -> str:
    """Read the pinned profile synchronously for user menus within an operation."""
    from app.runtime_settings.lifecycle import current_operation_snapshot

    snapshot = snapshot if snapshot is not None else current_operation_snapshot()
    provider = snapshot.values.get("live_route:provider", "baseline") if snapshot is not None else "baseline"
    validate_value("provider", provider)
    return str(provider)


def default_policy(process_id: str, provider: str) -> dict[str, Any]:
    """A provider-pinned reset default, independent of stored user selection."""
    from app.config import GEMINI_ECONOMY_MODEL, GEMINI_PRIMARY_MODEL, settings
    from app.process_policies import PROCESSES, validate_policy

    if process_id not in LIVE_PROCESS_IDS or provider not in {"gemini", "openai"}:
        raise ValueError("Профиль не управляет этим процессом")
    if provider == "openai":
        model = _openai_model(process_id)
    else:
        spec = PROCESSES[process_id]
        model = getattr(settings, spec.setting, spec.fallback) if spec.setting else spec.fallback
        if not model.startswith("gemini-"):
            model = GEMINI_ECONOMY_MODEL if process_id in _FAST_PROCESSES else GEMINI_PRIMARY_MODEL
    return validate_policy(process_id, {"models": [model], "strategy": "sequential", "inherit_user_model": False})


def effective_policy_value(process_id: str, snapshot: store.SettingsSnapshot) -> Any:
    """Read profile defaults even if a reset or older snapshot omitted a plan."""
    raw = snapshot.values.get(f"process:{process_id}")
    provider = active_provider(snapshot)
    if provider == "baseline" or process_id not in LIVE_PROCESS_IDS:
        return raw
    if raw is None:
        return default_policy(process_id, provider)
    if isinstance(raw, Mapping):
        return {**raw, "inherit_user_model": False}
    return raw  # The ordinary process validator rejects malformed explicit plans.


def _plain(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    return value


def validate_value(identity: str, value: Any) -> None:
    """Validate profile metadata on history restore, without provider calls."""
    from app.process_policies import validate_policy

    if identity == "provider":
        if value not in PROVIDERS:
            raise ValueError("Неизвестный профиль маршрутов")
        return
    if identity not in {f"saved:{provider}" for provider in PROVIDERS}:
        raise ValueError("Неизвестное поле профиля маршрутов")
    if not isinstance(value, Mapping) or set(value) != set(LIVE_PROCESS_IDS):
        raise ValueError("Неполный сохранённый профиль маршрутов")
    for process_id, policy in value.items():
        if policy is not None:
            validate_policy(process_id, policy)


def _openai_model(process_id: str) -> str:
    from app.config import settings

    if process_id in _FAST_PROCESSES:
        return getattr(settings, "OPENAI_INLINE_MODEL", "openai-direct/gpt-6-luna")
    if process_id == "search":
        return getattr(settings, "OPENAI_QNA_MODEL", "openai-direct/gpt-6.1-terra")
    return getattr(settings, "OPENAI_DEFAULT_MODEL", "openai-direct/gpt-6.1-terra")


async def _initial_gemini(original: Mapping[str, Any]) -> dict[str, Any]:
    from app.config import GEMINI_ECONOMY_MODEL, GEMINI_PRIMARY_MODEL
    from app.process_policies import baseline_models, validate_policy

    plans = {}
    for process_id in LIVE_PROCESS_IDS:
        previous = original.get(process_id)
        # Preserve custom Gemini chains, strategy and inheritance verbatim.
        if previous and all(model.startswith("gemini-") for model in previous["models"]):
            plans[process_id] = _plain(previous)
            continue
        baseline = tuple(model for model in await baseline_models(process_id) if model.startswith("gemini-"))
        models = list(baseline or ((GEMINI_ECONOMY_MODEL if process_id in _FAST_PROCESSES else GEMINI_PRIMARY_MODEL),))
        plans[process_id] = validate_policy(
            process_id, {"models": models, "strategy": "sequential", "inherit_user_model": False}
        )
    return plans


async def switch_provider(provider: str, *, expected_revision: int, actor: str) -> store.SettingsSnapshot:
    """Save outgoing routes and activate the requested profile in one CAS write."""
    from app.process_policies import validate_policy
    from app.runtime_settings.processes import snapshot_at_revision

    validate_value("provider", provider)
    current = await snapshot_at_revision(expected_revision)
    outgoing = current.values.get("live_route:provider", "baseline")
    validate_value("provider", outgoing)
    if outgoing == provider:
        return current
    original = {process_id: _plain(current.values.get(f"process:{process_id}")) for process_id in LIVE_PROCESS_IDS}
    saved_key = f"live_route:saved:{provider}"
    target = current.values.get(saved_key)
    if target is None:
        if provider == "openai":
            target = {process_id: default_policy(process_id, provider) for process_id in LIVE_PROCESS_IDS}
        elif provider == "gemini":
            # On first activation capture the original Gemini configuration too,
            # before replacing it with the OpenAI profile.
            baseline = current.values.get("live_route:saved:baseline", original)
            target = await _initial_gemini(baseline)
        else:
            raise ValueError("Исходный профиль ещё не сохранён")
    validate_value(f"saved:{provider}", target)
    updates = {"live_route:provider": provider, f"live_route:saved:{outgoing}": original}
    removals = []
    for process_id, policy in target.items():
        key = f"process:{process_id}"
        if policy is None:
            removals.append(key)
        else:
            updates[key] = validate_policy(process_id, policy)
    return await store.update_values(
        updates, removals=tuple(removals), expected_revision=expected_revision, actor=actor
    )


def status(snapshot: store.SettingsSnapshot) -> dict[str, Any]:
    """Safe configuration readiness; this does not probe provider availability."""
    from app.config import settings

    provider = active_provider(snapshot)
    return {
        "provider": provider,
        "process_ids": list(LIVE_PROCESS_IDS),
        "models": {
            process_id: list((effective_policy_value(process_id, snapshot) or {}).get("models", ()))
            for process_id in LIVE_PROCESS_IDS
        },
        "readiness": {
            "openai": {"configured": bool(getattr(settings, "OPENAI_API_KEYS", ()))},
            "gemini": {"configured": bool(getattr(settings, "GEMINI_API_KEYS", ()))},
        },
        "can_restore_baseline": "live_route:saved:baseline" in snapshot.values,
        "openai_defaults": {process_id: _openai_model(process_id) for process_id in LIVE_PROCESS_IDS},
        "note": "Проверено только наличие настроенных ключей; доступ к моделям не проверялся.",
    }
