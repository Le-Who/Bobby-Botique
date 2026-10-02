"""Runtime prompt controls backed by versioned global settings."""

from __future__ import annotations

from collections.abc import Mapping

from app.prompt_registry import get_global_registry as get_registry
from app.prompt_registry import validate_prompt_text
from app.runtime_settings import store
from app.runtime_settings.prompt_usage import ensure_prompt_editable
from app.runtime_settings.store import SettingsSnapshot

_PREFIX = "prompt:"


def _overrides(values: Mapping[str, object]) -> dict[str, str]:
    """Validate a complete persisted prompt set before applying any of it."""
    return {
        key.removeprefix(_PREFIX): validate_prompt_text(key.removeprefix(_PREFIX), value)
        for key, value in values.items()
        if key.startswith(_PREFIX)
    }


def _apply(snapshot: SettingsSnapshot) -> SettingsSnapshot:
    get_registry().apply_overrides(_overrides(snapshot.values), revision=snapshot.revision)
    return snapshot


async def refresh_prompts(*, force: bool = False) -> SettingsSnapshot:
    """Refresh active prompts when the store observes a newer revision."""
    return _apply(await store.get_snapshot(force=force))


async def list_prompts() -> list[dict[str, object]]:
    from app.prompt_registry import get_registry as get_operation_registry
    from app.prompt_registry import prompt_scope
    from app.runtime_settings.lifecycle import operation_snapshot

    snapshot = await operation_snapshot()
    with prompt_scope(overrides=_overrides(snapshot.values), revision=snapshot.revision):
        return get_operation_registry().prompt_catalog()


async def save_prompt(
    name: str,
    text: str,
    *,
    expected_revision: int,
    actor: str,
) -> SettingsSnapshot:
    """Validate before writing, then activate the committed snapshot immediately."""
    validate_prompt_text(name, text)
    ensure_prompt_editable(name)
    current = await store.get_snapshot()
    _overrides(current.values)
    saved = await store.set_value(
        f"{_PREFIX}{name}",
        text,
        expected_revision=expected_revision,
        actor=actor,
    )
    return _apply(saved)


async def reset_prompt(name: str, *, expected_revision: int, actor: str) -> SettingsSnapshot:
    """Remove one override and activate the resulting committed snapshot."""
    if name not in {entry["id"] for entry in get_registry().prompt_catalog()}:
        raise KeyError(name)
    ensure_prompt_editable(name)
    current = await store.get_snapshot()
    _overrides(current.values)
    saved = await store.reset_value(
        f"{_PREFIX}{name}",
        expected_revision=expected_revision,
        actor=actor,
    )
    return _apply(saved)
