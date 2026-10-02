"""Versioned compatibility values for Crocodile's shared default and Auto mode."""

from app.runtime_settings import store

CROC_ROLES = ("words", "category", "hints", "judge", "image_prompt")
KEYS = {"daily_croc_text_model", *(f"daily_croc_text_model_{role}" for role in CROC_ROLES)}


def validate_value(key: str, value: object) -> str:
    from app.config import is_gemini_chat_model_id

    if key not in KEYS or not isinstance(value, str) or (value and not is_gemini_chat_model_id(value)):
        raise ValueError("Invalid Crocodile model setting")
    return value


async def read_value(key: str, baseline: str) -> str:
    from app.runtime_settings.lifecycle import operation_snapshot

    snapshot = await operation_snapshot()
    value = snapshot.values.get(f"legacy_model:{key}", baseline)
    return validate_value(key, value)


async def save_croc_model(process: str, model: str, *, actor: str) -> store.SettingsSnapshot:
    from app.runtime_settings.processes import save_primary_model

    key = f"daily_croc_text_model_{process}" if process else "daily_croc_text_model"
    validate_value(key, model)
    if process and model:
        return await save_primary_model(f"crocodile.{process}", model, actor=actor)
    snapshot = await store.get_snapshot(force=True)
    if snapshot.degraded:
        raise ConnectionError("Runtime model state is unavailable")
    # Empty is intentional: it shadows an old persisted legacy selection.
    # Resetting one process to Auto and clearing its chain is indivisible.
    return await store.update_values(
        {f"legacy_model:{key}": model},
        removals=(f"process:crocodile.{process}",) if process else (),
        expected_revision=snapshot.revision,
        actor=actor,
    )
