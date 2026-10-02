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
    if current.degraded:
        raise ConnectionError("Runtime process state is unavailable")
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
    return await store.set_value(f"process:{process_id}", value, expected_revision=current.revision, actor=actor)


async def effective_primary_model(process_id: str, baseline: str) -> str:
    """Read effective admin state for controls, independent of an operation's pin."""
    snapshot = await store.get_snapshot()
    value = snapshot.values.get(f"process:{process_id}")
    if isinstance(value, Mapping):
        return validate_policy(process_id, value)["models"][0]
    return baseline
