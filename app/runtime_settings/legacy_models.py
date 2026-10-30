"""Versioned compatibility values for Crocodile's shared default and Auto mode."""

from app.runtime_settings import store

CROC_ROLES = ("words", "category", "hints", "judge", "image_prompt")
KEYS = {"daily_croc_text_model", *(f"daily_croc_text_model_{role}" for role in CROC_ROLES)}
_IMAGE_KEY = "legacy_model:daily_croc_image_model"


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
    snapshot = await store.get_snapshot(force=True)
    return await save_croc_model_at_revision(process, model, expected_revision=snapshot.revision, actor=actor)


async def save_croc_model_at_revision(
    process: str, model: str, *, expected_revision: int, actor: str
) -> store.SettingsSnapshot:
    from app.runtime_settings.processes import save_primary_model_at_revision, snapshot_at_revision

    key = f"daily_croc_text_model_{process}" if process else "daily_croc_text_model"
    validate_value(key, model)
    if process and model:
        return await save_primary_model_at_revision(
            f"crocodile.{process}", model, expected_revision=expected_revision, actor=actor
        )
    await snapshot_at_revision(expected_revision)
    # Empty is intentional: it shadows an old persisted legacy selection.
    # Resetting one process to Auto and clearing its chain is indivisible.
    return await store.update_values(
        {f"legacy_model:{key}": model},
        removals=(f"process:crocodile.{process}",) if process else (),
        expected_revision=expected_revision,
        actor=actor,
    )


async def read_croc_image_model(baseline: str) -> str:
    from app.runtime_settings.lifecycle import operation_snapshot

    return str((await operation_snapshot()).values.get(_IMAGE_KEY, baseline))


async def save_croc_image_model(model: str, *, actor: str) -> store.SettingsSnapshot:
    snapshot = await store.get_snapshot(force=True)
    return await save_croc_image_model_at_revision(model, expected_revision=snapshot.revision, actor=actor)


async def save_croc_image_model_at_revision(
    model: str, *, expected_revision: int, actor: str
) -> store.SettingsSnapshot:
    from app.runtime_settings.processes import snapshot_at_revision

    if not isinstance(model, str) or not model.strip() or len(model) > 200:
        raise ValueError("Invalid Crocodile image model")
    await snapshot_at_revision(expected_revision)
    return await store.set_value(_IMAGE_KEY, model, expected_revision=expected_revision, actor=actor)
