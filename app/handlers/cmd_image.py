"""
Image generation command handler.

Entry point: /draw <prompt>  (aliases: /img, /image, /generate)

UX flow:
    1. User sends: /draw A cyberpunk cityscape at night
    2. Bot replies immediately with a "🎨 Рисую..." placeholder.
    3. ChatAction.UPLOAD_PHOTO keeps the upload indicator alive during generation.
    4. On success: bot edits placeholder to send the photo + Interactive Canvas buttons.
    5. Inline buttons allow: 🔄 Regenerate | 📐 Aspect Ratio | 🤖 Switch Model

State:
    Prompt and last-used parameters are stored in context.user_data (PTB-scoped,
    ephemeral, no DB needed) under key "draw_state" to power the inline actions.
"""

from __future__ import annotations

import asyncio
import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatAction
from telegram.ext import ContextTypes

from app.config import (
    IMAGEN_MODEL_BASE,
    IMAGEN_MODEL_FAST,
    IMAGEN_MODEL_ULTRA,
    IMAGEN_MODELS_ORDERED,
    settings,
)
from app.providers.imagen_provider import (
    ASPECT_RATIO_LABELS,
    MODEL_LABELS,
    SUPPORTED_ASPECT_RATIOS,
    ImageGenResult,
    get_imagen_provider,
)
from app.utils.decorators import authorized_only, safe_handler

logger = logging.getLogger(__name__)

# ── State key ────────────────────────────────────────────────────────────────

_DRAW_STATE_KEY = "draw_state"


def _get_draw_state(context: ContextTypes.DEFAULT_TYPE) -> dict:
    """Return current draw state from user_data, or empty defaults."""
    return context.user_data.get(  # type: ignore[union-attr]
        _DRAW_STATE_KEY,
        {
            "prompt": "",
            "model": IMAGEN_MODEL_BASE,
            "aspect_ratio": "1:1",
        },
    )


def _set_draw_state(
    context: ContextTypes.DEFAULT_TYPE,
    prompt: str,
    model: str,
    aspect_ratio: str,
) -> None:
    context.user_data[_DRAW_STATE_KEY] = {  # type: ignore[index]
        "prompt": prompt,
        "model": model,
        "aspect_ratio": aspect_ratio,
    }


# ── Keyboard builders ─────────────────────────────────────────────────────────


def _build_canvas_keyboard(
    model: str,
    aspect_ratio: str,
    show_ar_menu: bool = False,
    show_model_menu: bool = False,
) -> InlineKeyboardMarkup:
    """
    Build the Interactive Canvas keyboard shown below a generated image.

    Layout (compact 3-row design):
        Row 1 | 🔄 Сгенерировать заново
        Row 2 | 📐 1:1 | 3:4 | 4:3 | 9:16 | 16:9    (aspect ratio selection)
        Row 3 | ⚡ Fast | ✨ Base | 💎 Ultra          (model selection)
        Row 4 | ✨ Начать новый чат
    """
    rows = []

    # Row 1 — Regenerate
    rows.append([InlineKeyboardButton("🔄 Сгенерировать заново", callback_data="draw:regen")])

    # Row 2 — Aspect ratios (show all; highlight current)
    ar_buttons = []
    for ar in SUPPORTED_ASPECT_RATIOS:
        label = ASPECT_RATIO_LABELS.get(ar, ar)
        if ar == aspect_ratio:
            label = f"✅ {label}"
        ar_buttons.append(InlineKeyboardButton(label, callback_data=f"draw:ar:{ar}"))
    # Split into two rows of 3+2 to avoid overflow on narrow screens
    rows.append(ar_buttons[:3])
    rows.append(ar_buttons[3:])

    # Row 4 — Model selection
    model_buttons = []
    for m in IMAGEN_MODELS_ORDERED:
        label = MODEL_LABELS.get(m, m)
        if m == model:
            label = f"✅ {label}"
        model_buttons.append(InlineKeyboardButton(label, callback_data=f"draw:model:{m}"))
    rows.append(model_buttons)

    # Row 5 — Escape
    rows.append([InlineKeyboardButton("✨ Начать новую тему", callback_data="new_topic")])

    return InlineKeyboardMarkup(rows)


# ── Heartbeat helper ─────────────────────────────────────────────────────────


async def _send_typing_heartbeat(chat_id: int, bot, stop_event: asyncio.Event) -> None:
    """Periodically refresh the 'uploading photo' indicator until stop_event is set."""
    while not stop_event.is_set():
        try:
            await bot.send_chat_action(chat_id=chat_id, action=ChatAction.UPLOAD_PHOTO)
        except Exception:
            pass  # Non-critical
        try:
            await asyncio.wait_for(asyncio.shield(stop_event.wait()), timeout=4.5)
        except TimeoutError:
            pass


# ── Core generation logic ────────────────────────────────────────────────────


async def _run_generation(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    prompt: str,
    model: str,
    aspect_ratio: str,
) -> None:
    """
    Execute image generation and update the UI.
    Handles heartbeat, API call, result display, and error messages.
    """
    message = update.effective_message
    bot = update.effective_message.get_bot()
    chat_id = message.chat_id if message.chat else 0

    # Store state for subsequent inline actions
    _set_draw_state(context, prompt, model, aspect_ratio)

    # Send placeholder
    placeholder = await message.reply_text("🎨 Рисую... это займёт несколько секунд.")

    # Start heartbeat
    stop_event = asyncio.Event()
    heartbeat_task = asyncio.create_task(_send_typing_heartbeat(chat_id, bot, stop_event))

    try:
        provider = get_imagen_provider()
        result: ImageGenResult = await provider.generate(
            prompt=prompt,
            model=model,
            aspect_ratio=aspect_ratio,
            number_of_images=1,
        )
    finally:
        stop_event.set()
        heartbeat_task.cancel()

    keyboard = _build_canvas_keyboard(model, aspect_ratio)

    if result.success and result.images:
        try:
            await placeholder.delete()
        except Exception:
            pass

        model_label = MODEL_LABELS.get(model, model)
        caption = (
            f"🎨 *{_escape_md(prompt[:80])}{'...' if len(prompt) > 80 else ''}*\n"
            f"_{model_label} · {aspect_ratio}_"
        )
        try:
            await message.reply_photo(
                photo=result.images[0],
                caption=caption,
                parse_mode="Markdown",
                reply_markup=keyboard,
            )
            logger.info(
                "Image generated for user=%s model=%s ratio=%s",
                update.effective_user.id if update.effective_user else "?",
                model,
                aspect_ratio,
            )
        except Exception as send_err:
            logger.error("Failed to send generated image: %s", send_err)
            await placeholder.edit_text("❌ Изображение создано, но не удалось отправить. Попробуйте снова.")
        return

    # --- Error branch ---
    err = result.error_message
    if err == "safety_blocked":
        text = (
            "🚫 *Запрос заблокирован фильтром безопасности.*\n\n"
            "Попробуйте переформулировать описание — избегайте упоминания реальных людей, "
            "насилия или контента 18+.\n\n"
            "Можно попробовать: `Пейзаж с горами на закате`"
        )
    elif err == "quota_exhausted":
        text = (
            "⏳ *Дневной лимит генерации изображений исчерпан.*\n\n"
            f"Ежедневный лимит — {settings.IMAGE_GEN_RPD_PER_KEY} изображений на ключ "
            "для бесплатного уровня AI Studio. Попробуйте завтра."
        )
    elif err == "timeout":
        text = "⏰ *Время ожидания истекло.* Серверы Imagen перегружены — попробуйте ещё раз."
    elif err == "overloaded":
        text = "⚡ *Серверы перегружены.* Подождите несколько секунд и попробуйте снова."
    else:
        text = (
            "❌ *Не удалось создать изображение.*\n\n"
            f"`{err or 'unknown error'}`\n\n"
            "Попробуйте позже или измените запрос."
        )

    retry_keyboard = InlineKeyboardMarkup(
        [[InlineKeyboardButton("🔄 Попробовать снова", callback_data="draw:regen")]]
    )
    try:
        await placeholder.edit_text(text, parse_mode="Markdown", reply_markup=retry_keyboard)
    except Exception:
        await placeholder.edit_text(text.replace("*", "").replace("`", ""), reply_markup=retry_keyboard)


def _escape_md(text: str) -> str:
    """Escape Markdown special chars for Telegram MarkdownV1 captions."""
    for ch in ("*", "_", "`", "["):
        text = text.replace(ch, f"\\{ch}")
    return text


# ── Command handler ───────────────────────────────────────────────────────────

_DRAW_HELP = (
    "🎨 *Генерация изображений*\n\n"
    "Отправьте `/draw <описание>` чтобы создать изображение.\n\n"
    "*Примеры:*\n"
    "`/draw Неоновый город ночью, киберпанк`\n"
    "`/draw Акварельный пейзаж с горами`\n"
    "`/draw Портрет кошки в стиле маслом`\n\n"
    f"*Модели:* ⚡ Fast · ✨ Base · 💎 Ultra\n"
    f"*Форматы:* {' · '.join(SUPPORTED_ASPECT_RATIOS)}\n\n"
    "После генерации используйте кнопки под изображением для быстрой перегенерации, "
    "смены пропорций и модели."
)


@authorized_only
@safe_handler("❌ Ошибка при генерации изображения.")
async def draw_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /draw, /img, /image, /generate commands."""
    if not context.args:
        state = _get_draw_state(context)
        prev_prompt = state.get("prompt", "")
        prev_info = (
            f"\n\n_Последний запрос: `{prev_prompt[:60]}{'...' if len(prev_prompt) > 60 else ''}`_"
            if prev_prompt
            else ""
        )
        await update.message.reply_text(
            _DRAW_HELP + prev_info,
            parse_mode="Markdown",
        )
        return

    prompt = " ".join(context.args).strip()
    if len(prompt) < 3:
        await update.message.reply_text(
            "⚠️ Слишком короткое описание. Попробуйте написать хотя бы несколько слов.",
        )
        return

    state = _get_draw_state(context)
    await _run_generation(
        update,
        context,
        prompt=prompt,
        model=state["model"],
        aspect_ratio=state["aspect_ratio"],
    )
