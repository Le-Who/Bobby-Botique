"""Shared process writers for the dashboard and legacy single-model controls."""

from collections.abc import Mapping

from app.process_policies import baseline_models, validate_policy
from app.runtime_settings import store


async def save_primary_model(process_id: str, model: str, *, actor: str) -> store.SettingsSnapshot:
    """Replace the primary while retaining an explicitly configured reserve chain.

    The command is an immediate mutation, so it reads a fresh revision. A racing
    edit is rejected by CAS instead of silently rebasing or discarding its chain.
    """
    current = await store.get_snapshot(force=True)
    return await save_primary_model_at_revision(process_id, model, expected_revision=current.revision, actor=actor)


async def snapshot_at_revision(expected_revision: int) -> store.SettingsSnapshot:
    if type(expected_revision) is not int or expected_revision < 0:
        raise ValueError("Invalid expected revision")
    current = await store.get_snapshot(force=True)
    if current.degraded:
        raise ConnectionError("Runtime process state is unavailable")
    if current.revision != expected_revision:
        raise store.RevisionConflict("Runtime settings changed; reload before saving")
    return current


async def save_primary_model_at_revision(
    process_id: str, model: str, *, expected_revision: int, actor: str
) -> store.SettingsSnapshot:
    """Apply a browser edit to exactly the settings revision it was based on."""
    current = await snapshot_at_revision(expected_revision)
    previous = current.values.get(f"process:{process_id}")
    reserves = (
        list(previous.get("models", ()))[1:]
        if isinstance(previous, Mapping)
        else list(await baseline_models(process_id))[1:]
    )
    value = validate_policy(
        process_id,
        {
            "models": list(dict.fromkeys((model, *reserves))),
            "strategy": previous.get("strategy", "sequential") if isinstance(previous, Mapping) else "sequential",
            "inherit_user_model": False,
        },
    )
    return await store.set_value(f"process:{process_id}", value, expected_revision=expected_revision, actor=actor)


async def effective_primary_model(process_id: str, baseline: str) -> str:
    """Read effective admin state for controls, independent of an operation's pin."""
    snapshot = await store.get_snapshot()
    value = snapshot.values.get(f"process:{process_id}")
    if isinstance(value, Mapping):
        return validate_policy(process_id, value)["models"][0]
    return baseline
