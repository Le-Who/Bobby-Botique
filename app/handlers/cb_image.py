"""
Callback handlers for the Image Generation Interactive Canvas.

Handles draw:* callback_data patterns:
    draw:regen          — Regenerate with current settings
    draw:ar:<ratio>     — Change aspect ratio and regenerate
    draw:model:<name>   — Change model and regenerate
"""

from __future__ import annotations

import logging

from telegram import Update
from telegram.ext import ContextTypes

from app.config import IMAGEN_MODEL_BASE, IMAGEN_MODELS_ORDERED
from app.handlers.callbacks import _BUSY_TOAST, _is_user_busy
from app.providers.imagen_provider import SUPPORTED_ASPECT_RATIOS

logger = logging.getLogger(__name__)

_DRAW_STATE_KEY = "draw_state"


def _get_draw_state(context: ContextTypes.DEFAULT_TYPE) -> dict:
    return context.user_data.get(  # type: ignore[union-attr]
        _DRAW_STATE_KEY,
        {"prompt": "", "model": IMAGEN_MODEL_BASE, "aspect_ratio": "1:1"},
    )


async def draw_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Central dispatcher for all draw:* callback queries.

    Parses the action from callback_data, updates draw state,
    and delegates to the generation flow.
    """
    from app.handlers.cmd_image import _run_generation

    query = update.callback_query
    await query.answer()

    user_id = query.from_user.id if query.from_user else 0

    if _is_user_busy(user_id):
        await query.answer(_BUSY_TOAST, show_alert=True)
        return

    data: str = query.data or ""
    parts = data.split(":")  # e.g. ["draw", "ar", "16:9"] or ["draw", "regen"]
    action = parts[1] if len(parts) > 1 else ""

    state = _get_draw_state(context)
    current_prompt = state.get("prompt", "")
    current_model = state.get("model", IMAGEN_MODEL_BASE)
    current_ar = state.get("aspect_ratio", "1:1")

    if not current_prompt:
        # No previous generation — nudge the user
        await query.answer("⚠️ Сначала создайте изображение командой /draw.", show_alert=True)
        return

    new_model = current_model
    new_ar = current_ar

    if action == "regen":
        # Regenerate with exactly the same settings
        pass

    elif action == "ar":
        # draw:ar:16:9  → parts = ["draw", "ar", "16", "9"]
        # Rejoin from index 2 to handle colons in the ratio itself
        new_ar = ":".join(parts[2:]) if len(parts) > 2 else current_ar
        if new_ar not in SUPPORTED_ASPECT_RATIOS:
            await query.answer("⚠️ Неподдерживаемый формат.", show_alert=True)
            return
        if new_ar == current_ar:
            await query.answer(f"✅ Уже используется {new_ar}")
            return

    elif action == "model":
        new_model = parts[2] if len(parts) > 2 else current_model
        if new_model not in IMAGEN_MODELS_ORDERED:
            await query.answer("⚠️ Неизвестная модель.", show_alert=True)
            return
        if new_model == current_model:
            from app.providers.imagen_provider import MODEL_LABELS

            await query.answer(f"✅ Уже используется {MODEL_LABELS.get(current_model, current_model)}")
            return

    else:
        logger.warning("draw_callback: unknown action=%r data=%r", action, data)
        return

    # --- Synthesize a fake Update pointing to the *original* message context ---
    # We need to call _run_generation which expects update.effective_message.reply_text.
    # For callback-triggered generation we reply directly to the message that
    # contains the button (the photo message), which is query.message.
    # _run_generation uses update.effective_message internally.
    # We pass the real update here — effective_message will be query.message.
    await _run_generation(
        update=update,
        context=context,
        prompt=current_prompt,
        model=new_model,
        aspect_ratio=new_ar,
    )
