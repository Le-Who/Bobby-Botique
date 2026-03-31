"""
Callback handlers for the Image Generation Interactive Canvas.

Handles draw:* callback_data patterns:
    draw:regen          — Regenerate with current settings
    draw:ar:<ratio>     — Change aspect ratio and regenerate
    draw:model:<name>   — Change model and regenerate

Model validation is performed against the *dynamic* list of all configured
models (Pollinations IMAGE_MODELS + Imagen models if keys present), not a
hardcoded constant, so that new models added via env vars work immediately.
"""

from __future__ import annotations

import logging

from telegram import Update
from telegram.ext import ContextTypes

from app.handlers.callbacks import _BUSY_TOAST, _is_user_busy
from app.providers.imagen_provider import SUPPORTED_ASPECT_RATIOS

logger = logging.getLogger(__name__)

_DRAW_STATE_KEY = "draw_state"


def _get_draw_state(context: ContextTypes.DEFAULT_TYPE) -> dict:
    from app.config import settings
    default_model = settings.POLLINATIONS_DEFAULT_IMAGE_MODEL
    return context.user_data.get(  # type: ignore[union-attr]
        _DRAW_STATE_KEY,
        {"prompt": "", "model": default_model, "aspect_ratio": "1:1"},
    )


def _all_valid_models() -> list[str]:
    """Return the unified list of valid model IDs (Pollinations + Imagen)."""
    from app.config import IMAGEN_MODELS_ORDERED, settings
    models: list[str] = list(settings.POLLINATIONS_IMAGE_MODELS)
    # Append Google Imagen models so that users who previously selected one
    # can still regenerate without an error.
    for m in IMAGEN_MODELS_ORDERED:
        if m not in models:
            models.append(m)
    return models


async def draw_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Central dispatcher for all draw:* callback queries.

    Parses the action from callback_data, updates draw state,
    and delegates to the generation flow in cmd_image._run_generation.
    """
    from app.handlers.cmd_image import _model_label, _run_generation

    query = update.callback_query
    await query.answer()

    user_id = query.from_user.id if query.from_user else 0

    if _is_user_busy(user_id):
        await query.answer(_BUSY_TOAST, show_alert=True)
        return

    data: str = query.data or ""
    parts = data.split(":")  # ["draw", "ar", "16", "9"] or ["draw", "regen"]
    action = parts[1] if len(parts) > 1 else ""

    state = _get_draw_state(context)
    current_prompt = state.get("prompt", "")
    current_model = state.get("model", "flux")
    current_ar = state.get("aspect_ratio", "1:1")

    if not current_prompt:
        await query.answer("⚠️ Сначала создайте изображение командой /draw.", show_alert=True)
        return

    new_model = current_model
    new_ar = current_ar

    if action == "regen":
        # Regenerate with exactly the same settings — no changes needed
        pass

    elif action == "ar":
        # draw:ar:16:9 → parts = ["draw", "ar", "16", "9"]
        # Rejoin from index 2 to reconstruct the colon-separated ratio
        new_ar = ":".join(parts[2:]) if len(parts) > 2 else current_ar
        if new_ar not in SUPPORTED_ASPECT_RATIOS:
            await query.answer("⚠️ Неподдерживаемый формат.", show_alert=True)
            return
        if new_ar == current_ar:
            await query.answer(f"✅ Уже используется {new_ar}")
            return

    elif action == "model":
        # draw:model:flux  or  draw:model:zimage  or  draw:model:gptimage-large
        # Parts: ["draw", "model", "flux"] or ["draw", "model", "gptimage-large"]
        # We need everything from index 2 joined back (model ids don't contain ":")
        new_model = ":".join(parts[2:]) if len(parts) > 2 else current_model
        all_models = _all_valid_models()
        if new_model not in all_models:
            await query.answer("⚠️ Модель недоступна.", show_alert=True)
            return
        if new_model == current_model:
            label = _model_label(current_model)
            await query.answer(f"✅ Уже используется {label}")
            return

    else:
        logger.warning("draw_callback: unknown action=%r data=%r", action, data)
        return

    await _run_generation(
        update=update,
        context=context,
        prompt=current_prompt,
        model=new_model,
        aspect_ratio=new_ar,
    )
