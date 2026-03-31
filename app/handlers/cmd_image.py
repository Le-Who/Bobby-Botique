"""
Image generation command handler.

Entry point: /draw <prompt>  (aliases: /img, /image, /generate)

UX flow:
    1. User sends: /draw A cyberpunk cityscape at night
    2. Bot replies immediately with a "🎨 Рисую..." placeholder.
    3. ChatAction.UPLOAD_PHOTO keeps the upload indicator alive during generation.
    4. On success: bot edits placeholder to send the photo + Interactive Canvas buttons.
    5. Inline buttons allow: 🔄 Regenerate | 📐 Aspect Ratio | 🤖 Switch Model

Provider routing:
    - Models starting with "imagen-*"  → Google ImagenProvider  (requires paid key)
    - All other models (flux, zimage…) → PollinationsProvider   (free tier, no key needed)

State:
    Prompt and last-used parameters are stored in context.user_data (PTB-scoped,
    ephemeral, no DB needed) under key "draw_state" to power the inline actions.

Config:
    IMAGE_MODELS          env var  — comma-separated list of Pollinations model IDs.
    DEFAULT_IMAGE_MODEL   env var  — default model when user has not selected one.
    POLLINATIONS_API_KEY  env var  — optional API key for higher rate limits.
"""

from __future__ import annotations

import asyncio
import logging
import math

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatAction
from telegram.ext import ContextTypes

from app.config import (
    IMAGEN_MODELS_ORDERED,
    settings,
)
from app.providers.imagen_provider import (
    ASPECT_RATIO_LABELS,
    SUPPORTED_ASPECT_RATIOS,
    ImageGenResult,
    get_imagen_provider,
)
from app.providers.imagen_provider import (
    MODEL_LABELS as IMAGEN_MODEL_LABELS,
)
from app.providers.pollinations import (
    PollinationsResult,
    get_model_label,
    get_pollinations_provider,
)
from app.utils.decorators import authorized_only, safe_handler

logger = logging.getLogger(__name__)

# ── Aspect ratios ─────────────────────────────────────────────────────────────
# Re-exported from imagen_provider for consistency.
# Pollinations also supports width/height freely, but we normalize to the
# same 5-ratio set for a uniform UI across both backends.

_SUPPORTED_AR = SUPPORTED_ASPECT_RATIOS
_AR_LABELS = ASPECT_RATIO_LABELS

# Map "W:H" ratios to pixel dimensions for Pollinations (it uses width+height, not ratio)
_AR_TO_PIXELS: dict[str, tuple[int, int]] = {
    "1:1":  (1024, 1024),
    "3:4":  (768, 1024),
    "4:3":  (1024, 768),
    "9:16": (576, 1024),
    "16:9": (1024, 576),
}

# ── State key ─────────────────────────────────────────────────────────────────

_DRAW_STATE_KEY = "draw_state"


def _get_draw_state(context: ContextTypes.DEFAULT_TYPE) -> dict:
    """Return current draw state from user_data, or empty defaults."""
    default_model = settings.POLLINATIONS_DEFAULT_IMAGE_MODEL
    return context.user_data.get(  # type: ignore[union-attr]
        _DRAW_STATE_KEY,
        {
            "prompt": "",
            "model": default_model,
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


# ── Provider routing ──────────────────────────────────────────────────────────


def _is_imagen_model(model: str) -> bool:
    """True if model should be routed to Google ImagenProvider."""
    return model.startswith("imagen-")


def _get_all_models() -> list[str]:
    """
    Returns the unified ordered list of models to show in the Canvas keyboard.

    Priority:
        1. Pollinations models (from IMAGE_MODELS env var, default: flux, zimage)
        2. Google Imagen models — only appended if keys are present and
           the model list is not overridden to exclude them.
    """
    models: list[str] = list(settings.POLLINATIONS_IMAGE_MODELS)
    return models


def _model_label(model: str) -> str:
    """Human-readable label: checks Imagen dict first, then Pollinations dict."""
    if model in IMAGEN_MODEL_LABELS:
        return IMAGEN_MODEL_LABELS[model]
    return get_model_label(model)


# ── Keyboard builders ─────────────────────────────────────────────────────────


def _chunk(lst: list, size: int) -> list[list]:
    """Split a list into chunks of at most `size`."""
    return [lst[i: i + size] for i in range(0, len(lst), size)]


def _build_canvas_keyboard(
    model: str,
    aspect_ratio: str,
) -> InlineKeyboardMarkup:
    """
    Build the Interactive Canvas keyboard shown below a generated image.

    Layout:
        Row 1 | 🔄 Сгенерировать заново
        Rows  | ◻️ 1:1  📱 3:4  🖥️ 4:3      (aspect ratios, ≤3 per row)
              | 📲 9:16  🎬 16:9
        Rows  | ✨ Flux  ⚡ Z-Image  …       (models, ≤3 per row — dynamic!)
        Last  | ✨ Начать новую тему

    The model rows are *fully dynamic*: they render whatever is in
    settings.POLLINATIONS_IMAGE_MODELS (+ optional Imagen models if present).
    This means adding IMAGE_MODELS="flux,zimage,gptimage" in env automatically
    produces a correctly wrapped 3rd row of model buttons with no code changes.
    """
    rows = []

    # Row 1 — Regenerate
    rows.append([InlineKeyboardButton("🔄 Сгенерировать заново", callback_data="draw:regen")])

    # Aspect-ratio rows (≤3 per row keeps buttons readable on narrow screens)
    ar_buttons = []
    for ar in _SUPPORTED_AR:
        label = _AR_LABELS.get(ar, ar)
        if ar == aspect_ratio:
            label = f"✅ {label}"
        ar_buttons.append(InlineKeyboardButton(label, callback_data=f"draw:ar:{ar}"))

    for chunk in _chunk(ar_buttons, 3):
        rows.append(chunk)

    # Model-selection rows (dynamic, ≤3 per row)
    all_models = _get_all_models()
    model_buttons = []
    for m in all_models:
        label = _model_label(m)
        if m == model:
            label = f"✅ {label}"
        model_buttons.append(InlineKeyboardButton(label, callback_data=f"draw:model:{m}"))

    # Smart chunking: prefer balanced rows
    # E.g. 4 models → 2+2 (not 3+1)
    cols = _ideal_columns(len(model_buttons))
    for chunk in _chunk(model_buttons, cols):
        rows.append(chunk)

    # Escape button
    rows.append([InlineKeyboardButton("✨ Начать новую тему", callback_data="new_topic")])

    return InlineKeyboardMarkup(rows)


def _ideal_columns(n: int, max_cols: int = 3) -> int:
    """
    Return the ideal number of columns for n items so rows are balanced.

    Examples:
        1 → 1      3 → 3      4 → 2 (2+2 better than 3+1)
        5 → 3      6 → 3      7 → 4? capped at 3 → 3+3+1
    """
    if n <= max_cols:
        return n
    # Find divisor that produces the most balanced split
    for cols in range(max_cols, 0, -1):
        rows_needed = math.ceil(n / cols)
        if rows_needed * cols - n < cols:  # last row is at least half-full
            return cols
    return max_cols


# ── Heartbeat ─────────────────────────────────────────────────────────────────


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


# ── Core generation logic ─────────────────────────────────────────────────────


async def _run_generation(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    prompt: str,
    model: str,
    aspect_ratio: str,
) -> None:
    """
    Execute image generation and update the UI.
    Routes to Google Imagen or Pollinations based on model ID.
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

    image_bytes: bytes | None = None
    error_message: str = ""

    try:
        if _is_imagen_model(model):
            # ── Google Imagen path ────────────────────────────────────────
            provider = get_imagen_provider()
            result: ImageGenResult = await provider.generate(
                prompt=prompt,
                model=model,
                aspect_ratio=aspect_ratio,
                number_of_images=1,
            )
            if result.success and result.images:
                image_bytes = result.images[0]
            else:
                error_message = result.error_message
        else:
            # ── Pollinations path ─────────────────────────────────────────
            width, height = _AR_TO_PIXELS.get(aspect_ratio, (1024, 1024))
            poll_provider = get_pollinations_provider()
            poll_result: PollinationsResult = await poll_provider.generate(
                prompt=prompt,
                model=model,
                width=width,
                height=height,
                seed=0,
                enhance=False,
            )
            if poll_result.success and poll_result.images:
                image_bytes = poll_result.images[0]
                if poll_result.warning:
                    logger.info(
                        "Pollinations warning for user=%s: %s",
                        update.effective_user.id if update.effective_user else "?",
                        poll_result.warning,
                    )
            else:
                error_message = poll_result.error_message
    finally:
        stop_event.set()
        heartbeat_task.cancel()

    keyboard = _build_canvas_keyboard(model, aspect_ratio)

    # ── Success branch ────────────────────────────────────────────────────────
    if image_bytes:
        try:
            await placeholder.delete()
        except Exception:
            pass

        model_label = _model_label(model)
        caption = (
            f"🎨 *{_escape_md(prompt[:80])}{'...' if len(prompt) > 80 else ''}*\n"
            f"_{model_label} · {aspect_ratio}_"
        )
        try:
            await message.reply_photo(
                photo=image_bytes,
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
            await placeholder.edit_text(
                "❌ Изображение создано, но не удалось отправить. Попробуйте снова."
            )
        return

    # ── Error branch ──────────────────────────────────────────────────────────
    err = error_message or "unknown"
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
            "Попробуйте завтра или переключитесь на другую модель."
        )
    elif err == "paid_tier_required":
        text = (
            "💳 *Эта модель требует оплаченного аккаунта.*\n\n"
            "Переключитесь на **✨ Flux** или **⚡ Z-Image** — они работают бесплатно."
        )
    elif err == "unauthorized":
        text = (
            "🔑 *Ошибка авторизации.*\n\n"
            "Проверьте корректность `POLLINATIONS_API_KEY`."
        )
    elif err == "timeout":
        text = "⏰ *Время ожидания истекло.* Серверы перегружены — попробуйте ещё раз."
    elif err == "empty_prompt":
        text = "⚠️ *Пустой запрос.* Напишите описание изображения."
    elif err == "invalid_content_type":
        text = (
            "⚠️ *Сервер вернул неожиданный ответ.*\n\n"
            "Возможно, модель временно недоступна. Попробуйте другую."
        )
    elif err.startswith(("get_http_", "http_")):
        code = err.split("_")[-1]
        text = f"❌ *Ошибка сервера HTTP {code}.* Попробуйте позже."
    else:
        text = (
            "❌ *Не удалось создать изображение.*\n\n"
            f"`{err}`\n\n"
            "Попробуйте позже или измените запрос."
        )

    retry_keyboard = InlineKeyboardMarkup(
        [[InlineKeyboardButton("🔄 Попробовать снова", callback_data="draw:regen")]]
    )
    try:
        await placeholder.edit_text(text, parse_mode="Markdown", reply_markup=retry_keyboard)
    except Exception:
        await placeholder.edit_text(
            text.replace("*", "").replace("`", "").replace("_", ""),
            reply_markup=retry_keyboard,
        )


def _escape_md(text: str) -> str:
    """Escape Markdown special chars for Telegram MarkdownV1 captions."""
    for ch in ("*", "_", "`", "["):
        text = text.replace(ch, f"\\{ch}")
    return text


# ── Command handler ───────────────────────────────────────────────────────────

def _build_help_text() -> str:
    """Build dynamic help text reflecting currently configured models."""
    models = _get_all_models()
    model_list = " · ".join(_model_label(m) for m in models)
    return (
        "🎨 *Генерация изображений*\n\n"
        "Отправьте `/draw <описание>` чтобы создать изображение.\n\n"
        "*Примеры:*\n"
        "`/draw Неоновый город ночью, киберпанк`\n"
        "`/draw Акварельный пейзаж с горами`\n"
        "`/draw Портрет кошки в стиле маслом`\n\n"
        f"*Модели:* {model_list}\n"
        f"*Форматы:* {' · '.join(_SUPPORTED_AR)}\n\n"
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
            _build_help_text() + prev_info,
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
