"""
Callback handlers for the Image Generation Interactive Canvas 2.0.

Handles draw:* callback_data patterns:

Navigation (no generation):
    draw:nav:main        — return to main canvas menu
    draw:nav:models      — open model selection sub-menu
    draw:nav:formats     — open format/aspect-ratio sub-menu

State mutation (no generation):
    draw:set:model:<id>  — select a model, return to main menu
    draw:set:ar:<ratio>  — select an aspect ratio, return to main menu
    draw:toggle:enhance  — toggle "Улучшить промпт" flag

Prompt editing:
    draw:edit:prompt     — enter "awaiting_prompt" mode → user types next message
    draw:cancel:prompt   — cancel prompt editing

Generation trigger:
    draw:execute         — run generation with current state

All nav / mutation actions only edit the keyboard markup - they do NOT call the
provider API.  Generation happens *only* on draw:execute.
"""

from __future__ import annotations

import asyncio
import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import BadRequest
from telegram.ext import ContextTypes

from app.handlers.callbacks import _BUSY_TOAST, _is_user_busy
from app.providers.imagen_provider import SUPPORTED_ASPECT_RATIOS
from app.utils.metrics_middleware import MetricsMiddleware

logger = logging.getLogger(__name__)

_DRAW_STATE_KEY = "draw_state"

# ── Local state helpers (mirror cmd_image to avoid circular import) ────────────


def _get_draw_state(context: ContextTypes.DEFAULT_TYPE) -> dict:
    from app.config import settings

    default_model = settings.POLLINATIONS_DEFAULT_IMAGE_MODEL
    return context.user_data.get(  # type: ignore[union-attr]
        _DRAW_STATE_KEY,
        {
            "prompt": "",
            "model": default_model,
            "aspect_ratio": "1:1",
            "enhance_prompt": True,
            "awaiting_prompt": False,
            "last_photo_msg": None,
        },
    )


def _patch_draw_state(context: ContextTypes.DEFAULT_TYPE, **kwargs) -> dict:
    """Apply kwargs as key-value patches to draw_state and return updated dict."""
    state = _get_draw_state(context)
    state.update(kwargs)
    context.user_data[_DRAW_STATE_KEY] = state  # type: ignore[index]
    return state


def _all_valid_models() -> list[str]:
    from app.handlers.cmd_image import _get_all_models

    return _get_all_models()


# ── Keyboard factory (re-imported from cmd_image lazily) ──────────────────────


def _main_keyboard(state: dict) -> InlineKeyboardMarkup:
    from app.handlers.cmd_image import _build_main_menu

    return _build_main_menu(state)


def _models_keyboard(state: dict) -> InlineKeyboardMarkup:
    from app.handlers.cmd_image import _build_models_menu

    return _build_models_menu(state)


def _formats_keyboard(state: dict) -> InlineKeyboardMarkup:
    from app.handlers.cmd_image import _build_formats_menu

    return _build_formats_menu(state)


def _awaiting_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("❌ Отмена", callback_data="draw:cancel:prompt")]])


# ── Helpers ────────────────────────────────────────────────────────────────────


async def _edit_markup(query, markup: InlineKeyboardMarkup) -> None:
    """Edit only the reply_markup of current message (no-op on BadRequest)."""
    try:
        await query.edit_message_reply_markup(reply_markup=markup)
    except BadRequest as e:
        if "not modified" not in str(e).lower():
            logger.debug("edit_markup BadRequest: %s", e)
    except Exception as exc:
        logger.debug("edit_markup error: %s", exc)


async def _edit_caption_and_markup(query, caption: str, markup: InlineKeyboardMarkup) -> None:
    """Edit caption + markup (used for photo messages)."""
    try:
        await query.edit_message_caption(
            caption=caption,
            parse_mode="Markdown",
            reply_markup=markup,
        )
    except BadRequest as e:
        if "not modified" not in str(e).lower():
            logger.debug("edit_caption_and_markup BadRequest: %s", e)
        # Fallback: edit only markup
        try:
            await query.edit_message_reply_markup(reply_markup=markup)
        except Exception:
            pass
    except Exception as exc:
        logger.debug("edit_caption_and_markup error: %s", exc)


# ── Central dispatcher ─────────────────────────────────────────────────────────


async def draw_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Central dispatcher for all draw:* callback queries.

    Parses action from callback_data and routes to the right sub-handler.
    """
    query = update.callback_query
    from app.handlers.interaction_work import begin_text_input_intent

    input_intent = None
    if query.data == "draw:edit:prompt" and _get_draw_state(context).get("prompt"):
        input_intent = begin_text_input_intent(query.from_user.id)
    await query.answer()

    user_id = query.from_user.id if query.from_user else 0

    if _is_user_busy(user_id):
        await query.answer(_BUSY_TOAST, show_alert=True)
        return

    data: str = query.data or ""
    # data format: draw:<action>[:<sub>...]
    parts = data.split(":")  # e.g. ["draw", "set", "model", "flux"]
    if len(parts) < 2:
        return
    action = parts[1]  # "nav", "set", "toggle", "execute", "edit", "cancel"

    state = _get_draw_state(context)

    # ── Navigation (just swap keyboard) ───────────────────────────────────
    if action == "nav":
        sub = parts[2] if len(parts) > 2 else "main"
        if sub == "models":
            await _edit_markup(query, _models_keyboard(state))
        elif sub == "formats":
            await _edit_markup(query, _formats_keyboard(state))
        else:  # "main" or anything else
            await _edit_markup(query, _main_keyboard(state))
        return

    # ── Set model ─────────────────────────────────────────────────────────
    if action == "set" and len(parts) > 3 and parts[2] == "model":
        new_model = ":".join(parts[3:])  # handles model ids without colons
        valid = _all_valid_models()
        if new_model not in valid:
            await query.answer("⚠️ Модель недоступна.", show_alert=True)
            return
        if new_model == state.get("model"):
            from app.handlers.cmd_image import _model_label

            await query.answer(f"✅ Уже используется {_model_label(new_model)}")
            return
        state = _patch_draw_state(context, model=new_model)
        await _edit_markup(query, _main_keyboard(state))
        return

    # ── Set aspect ratio ──────────────────────────────────────────────────
    if action == "set" and len(parts) > 3 and parts[2] == "ar":
        new_ar = ":".join(parts[3:])
        if new_ar not in SUPPORTED_ASPECT_RATIOS:
            await query.answer("⚠️ Неподдерживаемый формат.", show_alert=True)
            return
        if new_ar == state.get("aspect_ratio"):
            await query.answer(f"✅ Уже используется {new_ar}")
            return
        state = _patch_draw_state(context, aspect_ratio=new_ar)
        await _edit_markup(query, _main_keyboard(state))
        return

    # ── Toggle enhance ────────────────────────────────────────────────────
    if action == "toggle" and len(parts) > 2 and parts[2] == "enhance":
        current = state.get("enhance_prompt", False)
        state = _patch_draw_state(context, enhance_prompt=not current)
        label = "✅ Улучшение промпта включено" if not current else "✨ Улучшение промпта выключено"
        await query.answer(label)
        await _edit_markup(query, _main_keyboard(state))
        return

    # ── Edit prompt ───────────────────────────────────────────────────────
    if action == "edit" and len(parts) > 2 and parts[2] == "prompt":
        current_prompt = state.get("prompt", "")
        if not current_prompt:
            await query.answer("⚠️ Сначала создайте изображение командой /draw.", show_alert=True)
            return

        from app.handlers.interaction_work import bind_context_text_input, prepare_text_input
        from app.state import begin_interaction

        if not await prepare_text_input(
            user_id, query.message, query.answer, context.user_data, input_intent=input_intent
        ):
            return
        owner = begin_interaction(user_id, "draw")
        bind_context_text_input(owner, context.user_data)
        _patch_draw_state(context, awaiting_prompt=True)
        safe_limit = 400
        short = current_prompt[:safe_limit].strip() + ("..." if len(current_prompt) > safe_limit else "")
        try:
            await query.edit_message_caption(
                caption=f"✍️ *Отправьте новый текст для генерации.*\n\nТекущий промпт:\n`{short}`",
                parse_mode="Markdown",
                reply_markup=_awaiting_keyboard(),
            )
        except Exception:
            # If it's not a photo message, edit text instead
            try:
                await query.edit_message_text(
                    f"✍️ Отправьте новый текст для генерации.\n\nТекущий промпт: `{short}`",
                    parse_mode="Markdown",
                    reply_markup=_awaiting_keyboard(),
                )
            except Exception as exc:
                logger.debug("Could not edit message for prompt editing: %s", exc)
        return

    if action == "cancel" and len(parts) > 2 and parts[2] == "prompt":
        state = _patch_draw_state(context, awaiting_prompt=False)
        current_prompt = state.get("prompt", "")
        from app.handlers.cmd_image import _escape_md, _model_label

        safe_limit = 400
        short = current_prompt[:safe_limit].strip() + ("..." if len(current_prompt) > safe_limit else "")
        model_str = _model_label(state.get("model", ""))
        ar_str = state.get("aspect_ratio", "1:1")
        caption = f"🎨 *{_escape_md(short)}*\n_{model_str} · {ar_str}_"
        await _edit_caption_and_markup(query, caption, _main_keyboard(state))
        return

    # Current and legacy buttons share admission and owner-aware cleanup.
    if action in {"execute", "regen"}:
        current_prompt = state.get("prompt", "")
        if not current_prompt:
            await query.answer("⚠️ Сначала создайте изображение командой /draw.", show_alert=True)
            return

        from app import state as request_state
        from app.utils.background_tasks import try_submit_task

        user_state = request_state.get_user_state(user_id)
        if request_state.is_ordinary_request_busy(user_id):
            await query.answer(_BUSY_TOAST, show_alert=True)
            return
        user_state.is_processing = True
        snapshot = dict(state)

        def release(owner: asyncio.Task) -> None:
            if request_state.get_active_task(user_id) is owner:
                user_state.is_processing = False
                request_state.clear_active_task(user_id)

        async def generate() -> None:
            try:
                async with request_state.get_user_lock(user_id):
                    await request_state.cancel_interaction(user_id)
                    from app.handlers.cmd_image import _run_generation

                    async with MetricsMiddleware(f"draw_{action}", user_id=user_id) as operation:
                        operation.success = await _run_generation(
                            update=update,
                            context=context,
                            prompt=current_prompt,
                            model=snapshot.get(
                                "model", "flux" if action == "regen" else settings.POLLINATIONS_DEFAULT_IMAGE_MODEL
                            ),
                            aspect_ratio=snapshot.get("aspect_ratio", "1:1"),
                            enhance=snapshot.get("enhance_prompt", True),
                        )
            finally:
                owner = asyncio.current_task()
                if owner is not None:
                    release(owner)

        task = try_submit_task(generate(), operation=f"draw.{action}")
        if task is None:
            user_state.is_processing = False
            await query.answer("⏳ Сейчас не удалось запустить генерацию. Попробуйте ещё раз.", show_alert=True)
            return
        request_state.register_active_task(user_id, task)
        # A worker cancelled before its first step never enters its finally.
        task.add_done_callback(release)
        return

    logger.warning("draw_callback: unknown action=%r data=%r", action, data)


# ── Import settings (needed for default model in _patch_draw_state) ────────────
try:
    from app.config import settings  # noqa: E402 — late import OK
except ImportError:
    pass
