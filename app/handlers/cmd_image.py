"""
Image generation command handler — Canvas 2.0.

Entry point: /draw <prompt>  (aliases: /img, /image, /generate)

UX flow:
    1. User sends: /draw Акварельный пейзаж с горами
    2. Bot immediately generates with current saved settings.
    3. After generation: photo + Interactive Canvas menu underneath.
    4. Menu allows changing Model, Format, toggling "Enhance Prompt",
       editing the prompt — all WITHOUT re-generating until the user
       explicitly presses [▶️ Сгенерировать].

Provider routing:
    - Models starting with "imagen-*"  → Google ImagenProvider  (requires paid key)
    - All other models (flux, zimage…) → PollinationsProvider   (free tier, no key needed)

Prompt translation:
    - If the selected model does not natively support Cyrillic (currently every
      non-zimage Pollinations model), and the prompt contains Cyrillic text,
      we auto-translate it to English using gemini-3.1-flash-lite BEFORE sending
      to the provider.  The translated text is shown in the caption so the user
      can see what was passed; the original Russian prompt is preserved in state.
    - The translation API call is tracked in metrics_collector under the key
      "gemini_img_translate" / model "gemini-3.1-flash-lite-preview-04-17".

State (draw_state in context.user_data):
    prompt          str   — original user prompt (always Russian / original language)
    model           str   — last used model id
    aspect_ratio    str   — last used aspect ratio (e.g. "1:1")
    enhance_prompt  bool  — whether to pass enhance=True / use enhanced LLM prompt
    awaiting_prompt bool  — True when bot is waiting for the user to type a new prompt
    last_photo_msg  int | None  — message_id of the last sent photo, so we can
                                  update its reply_markup without sending a new photo.

Config:
    IMAGE_MODELS          env var  — comma-separated list of Pollinations model IDs.
    DEFAULT_IMAGE_MODEL   env var  — default model when user has not selected one.
    POLLINATIONS_API_KEY  env var  — optional API key for higher rate limits.
"""

from __future__ import annotations

import asyncio
import logging
import math
import re

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Message, Update
from telegram.constants import ChatAction
from telegram.error import BadRequest
from telegram.ext import ContextTypes

from app.config import (
    IMAGEN_MODELS_ORDERED,
    settings,
)
from app.metrics import metrics_collector
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

# ── Constants ──────────────────────────────────────────────────────────────────

_SUPPORTED_AR = SUPPORTED_ASPECT_RATIOS
_AR_LABELS = ASPECT_RATIO_LABELS

_AR_TO_PIXELS: dict[str, tuple[int, int]] = {
    "1:1":  (1024, 1024),
    "3:4":  (768, 1024),
    "4:3":  (1024, 768),
    "9:16": (576, 1024),
    "16:9": (1024, 576),
}

# Models that natively understand non-English prompts well enough that
# we skip the auto-translation step.
_CYRILLIC_NATIVE_MODELS: frozenset[str] = frozenset({"zimage"})

_CYRILLIC_RE = re.compile(r"[а-яёА-ЯЁ]")

# Translation model — cheapest Gemini variant; fast and cost-effective.
_TRANSLATE_MODEL = "gemini-3.1-flash-lite-preview"

_DRAW_STATE_KEY = "draw_state"

# ── State helpers ──────────────────────────────────────────────────────────────


def _get_draw_state(context: ContextTypes.DEFAULT_TYPE) -> dict:
    """Return current draw state, creating defaults if missing."""
    default_model = settings.POLLINATIONS_DEFAULT_IMAGE_MODEL
    return context.user_data.get(  # type: ignore[union-attr]
        _DRAW_STATE_KEY,
        {
            "prompt": "",
            "model": default_model,
            "aspect_ratio": "1:1",
            "enhance_prompt": False,
            "awaiting_prompt": False,
            "last_photo_msg": None,
        },
    )


def _set_draw_state(
    context: ContextTypes.DEFAULT_TYPE,
    *,
    prompt: str | None = None,
    model: str | None = None,
    aspect_ratio: str | None = None,
    enhance_prompt: bool | None = None,
    awaiting_prompt: bool | None = None,
    last_photo_msg: int | None | str = "KEEP",  # sentinel: "KEEP" = don't change
) -> dict:
    """Merge partial updates into draw_state and return the updated dict."""
    state = _get_draw_state(context)
    if prompt is not None:
        state["prompt"] = prompt
    if model is not None:
        state["model"] = model
    if aspect_ratio is not None:
        state["aspect_ratio"] = aspect_ratio
    if enhance_prompt is not None:
        state["enhance_prompt"] = enhance_prompt
    if awaiting_prompt is not None:
        state["awaiting_prompt"] = awaiting_prompt
    if last_photo_msg != "KEEP":
        state["last_photo_msg"] = last_photo_msg
    context.user_data[_DRAW_STATE_KEY] = state  # type: ignore[index]
    return state


# ── Provider routing ───────────────────────────────────────────────────────────


def _is_imagen_model(model: str) -> bool:
    return model.startswith("imagen-")


def _needs_translation(prompt: str, model: str) -> bool:
    """True if prompt contains Cyrillic and model doesn't natively handle it."""
    if _is_imagen_model(model):
        return False  # Imagen understands many languages
    if model in _CYRILLIC_NATIVE_MODELS:
        return False
    return bool(_CYRILLIC_RE.search(prompt))


def _get_all_models() -> list[str]:
    return list(settings.POLLINATIONS_IMAGE_MODELS)


def _model_label(model: str) -> str:
    if model in IMAGEN_MODEL_LABELS:
        return IMAGEN_MODEL_LABELS[model]
    return get_model_label(model)


# ── Prompt translation ─────────────────────────────────────────────────────────


async def _translate_to_english(
    prompt: str,
    user_id: int | None = None,
) -> str:
    """
    Translate a prompt to English using gemini-3.1-flash-lite.

    Returns the English translation on success, original prompt on failure.
    The API call is tracked in metrics_collector.
    """
    try:
        from google import genai as _genai  # noqa: F811
        from google.genai import types as _types

        from app.providers.gemini import get_cached_genai_client

        # Pick the first available key (same pool as LLM chat)
        api_keys = settings.GEMINI_API_KEYS
        if not api_keys:
            logger.warning("No Gemini API keys available for prompt translation")
            return prompt

        api_key = api_keys[0]
        await metrics_collector.record_api_call(
            "gemini_img_translate",
            model=_TRANSLATE_MODEL,
            user_id=user_id,
        )

        client = get_cached_genai_client(api_key)
        system = (
            "You are a professional image-generation prompt translator. "
            "Translate the user's prompt into clear, descriptive English "
            "suitable for an image generation model such as FLUX or Stable Diffusion. "
            "Keep the meaning intact. Respond ONLY with the translated prompt and "
            "nothing else — no explanations, no quotes."
        )
        response = await asyncio.wait_for(
            client.aio.models.generate_content(
                model=_TRANSLATE_MODEL,
                contents=prompt,  # plain str — SDK accepts str directly
                config=_types.GenerateContentConfig(
                    system_instruction=system,
                    temperature=0.2,
                    max_output_tokens=300,
                ),
            ),
            timeout=10.0,
        )
        translated = (response.text or "").strip()
        if translated:
            logger.info(
                "Prompt translated for img gen: %r → %r (user=%s)",
                prompt[:60],
                translated[:60],
                user_id,
            )
            return translated
    except Exception as exc:
        logger.warning("Prompt translation failed (%s), using original", exc)
    return prompt


# ── Keyboard builders ──────────────────────────────────────────────────────────


def _chunk(lst: list, size: int) -> list[list]:
    return [lst[i: i + size] for i in range(0, len(lst), size)]


def _ideal_columns(n: int, max_cols: int = 3) -> int:
    if n <= max_cols:
        return n
    for cols in range(max_cols, 0, -1):
        rows_needed = math.ceil(n / cols)
        if rows_needed * cols - n < cols:
            return cols
    return max_cols


def _build_main_menu(state: dict) -> InlineKeyboardMarkup:
    """
    Main canvas menu shown under the generated photo.

    Layout:
        [🖼 Модель: Flux]   [📐 Формат: 1:1]
        [✨ Улучшить: Выкл]  [✏️ Изменить промпт]
        [▶️ СГЕНЕРИРОВАТЬ]
        [✖️ Новая тема]
    """
    model = state.get("model", settings.POLLINATIONS_DEFAULT_IMAGE_MODEL)
    ar = state.get("aspect_ratio", "1:1")
    enhance = state.get("enhance_prompt", False)

    model_btn = InlineKeyboardButton(
        f"🖼 Модель: {_model_label(model)}",
        callback_data="draw:nav:models",
    )
    format_btn = InlineKeyboardButton(
        f"📐 Формат: {ar}",
        callback_data="draw:nav:formats",
    )
    enhance_label = "✅ Улучшить: Вкл" if enhance else "✨ Улучшить: Выкл"
    enhance_btn = InlineKeyboardButton(enhance_label, callback_data="draw:toggle:enhance")
    edit_btn = InlineKeyboardButton("✏️ Изменить промпт", callback_data="draw:edit:prompt")
    generate_btn = InlineKeyboardButton("▶️ СГЕНЕРИРОВАТЬ", callback_data="draw:execute")
    close_btn = InlineKeyboardButton("✖️ Новая тема", callback_data="new_topic")

    rows = [
        [model_btn, format_btn],
        [enhance_btn, edit_btn],
        [generate_btn],
        [close_btn],
    ]
    return InlineKeyboardMarkup(rows)


def _build_models_menu(state: dict) -> InlineKeyboardMarkup:
    """Sub-menu: choose a model."""
    current = state.get("model", settings.POLLINATIONS_DEFAULT_IMAGE_MODEL)
    all_models = _get_all_models()
    # Also expose Imagen models so legacy users can still select them
    for m in IMAGEN_MODELS_ORDERED:
        if m not in all_models:
            all_models.append(m)

    model_buttons = []
    for m in all_models:
        label = _model_label(m)
        if m == current:
            label = f"✅ {label}"
        model_buttons.append(InlineKeyboardButton(label, callback_data=f"draw:set:model:{m}"))

    cols = _ideal_columns(len(model_buttons))
    rows: list[list[InlineKeyboardButton]] = []
    for chunk in _chunk(model_buttons, cols):
        rows.append(chunk)

    rows.append([InlineKeyboardButton("🔙 Назад", callback_data="draw:nav:main")])
    return InlineKeyboardMarkup(rows)


def _build_formats_menu(state: dict) -> InlineKeyboardMarkup:
    """Sub-menu: choose an aspect ratio."""
    current_ar = state.get("aspect_ratio", "1:1")
    ar_buttons = []
    for ar in _SUPPORTED_AR:
        label = _AR_LABELS.get(ar, ar)
        if ar == current_ar:
            label = f"✅ {label}"
        ar_buttons.append(InlineKeyboardButton(label, callback_data=f"draw:set:ar:{ar}"))

    rows: list[list[InlineKeyboardButton]] = []
    for chunk in _chunk(ar_buttons, 3):
        rows.append(chunk)
    rows.append([InlineKeyboardButton("🔙 Назад", callback_data="draw:nav:main")])
    return InlineKeyboardMarkup(rows)


def _build_awaiting_keyboard() -> InlineKeyboardMarkup:
    """Keyboard shown while the bot is waiting for a new prompt from the user."""
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("❌ Отмена", callback_data="draw:cancel:prompt")]]
    )


# ── Heartbeat ──────────────────────────────────────────────────────────────────


async def _send_typing_heartbeat(chat_id: int, bot, stop_event: asyncio.Event) -> None:
    while not stop_event.is_set():
        try:
            await bot.send_chat_action(chat_id=chat_id, action=ChatAction.UPLOAD_PHOTO)
        except Exception:
            pass
        try:
            await asyncio.wait_for(asyncio.shield(stop_event.wait()), timeout=4.5)
        except TimeoutError:
            pass


# ── Core generation ────────────────────────────────────────────────────────────


async def _run_generation(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    prompt: str,
    model: str,
    aspect_ratio: str,
    enhance: bool = False,
) -> None:
    """
    Execute image generation and post the result.

    - Translates Cyrillic prompts for models that need English (metrics tracked).
    - Shows a heartbeat while waiting.
    - On success: sends photo + main menu keyboard.
    - On error: shows retry prompt.
    """
    message = update.effective_message
    bot = update.effective_message.get_bot()
    chat_id = message.chat_id if message.chat else 0
    user_id = update.effective_user.id if update.effective_user else None

    # Persist state (prompt stays as original Russian for the UI)
    _set_draw_state(
        context,
        prompt=prompt,
        model=model,
        aspect_ratio=aspect_ratio,
        enhance_prompt=enhance,
        awaiting_prompt=False,
    )

    # Translate if needed
    api_prompt = prompt
    translated = False
    if _needs_translation(prompt, model):
        api_prompt = await _translate_to_english(prompt, user_id=user_id)
        translated = api_prompt != prompt

    # Placeholder
    placeholder = await message.reply_text("🎨 Рисую... это займёт несколько секунд.")

    stop_event = asyncio.Event()
    heartbeat_task = asyncio.create_task(_send_typing_heartbeat(chat_id, bot, stop_event))

    image_bytes: bytes | None = None
    error_message: str = ""

    try:
        if _is_imagen_model(model):
            provider = get_imagen_provider()
            result: ImageGenResult = await provider.generate(
                prompt=api_prompt,
                model=model,
                aspect_ratio=aspect_ratio,
                number_of_images=1,
            )
            if result.success and result.images:
                image_bytes = result.images[0]
            else:
                error_message = result.error_message
        else:
            width, height = _AR_TO_PIXELS.get(aspect_ratio, (1024, 1024))
            poll_provider = get_pollinations_provider()
            poll_result: PollinationsResult = await poll_provider.generate(
                prompt=api_prompt,
                model=model,
                width=width,
                height=height,
                seed=0,
                enhance=enhance,
            )
            if poll_result.success and poll_result.images:
                image_bytes = poll_result.images[0]
                if poll_result.warning:
                    logger.info("Pollinations warning user=%s: %s", user_id, poll_result.warning)
            else:
                error_message = poll_result.error_message
    finally:
        stop_event.set()
        heartbeat_task.cancel()

    state = _get_draw_state(context)
    keyboard = _build_main_menu(state)

    # ── Success ───────────────────────────────────────────────────────────
    if image_bytes:
        try:
            await placeholder.delete()
        except Exception:
            pass

        model_label = _model_label(model)
        display_prompt = api_prompt if translated else prompt
        caption = (
            f"🎨 *{_escape_md(display_prompt[:80])}{'...' if len(display_prompt) > 80 else ''}*\n"
            f"_{model_label} · {aspect_ratio}_"
        )
        if translated:
            original_short = prompt[:60] + ("…" if len(prompt) > 60 else "")
            caption += f"\n_🌐 Переведено: {_escape_md(original_short)}_"

        try:
            sent = await message.reply_photo(
                photo=image_bytes,
                caption=caption,
                parse_mode="Markdown",
                reply_markup=keyboard,
            )
            _set_draw_state(context, last_photo_msg=sent.message_id)
            logger.info(
                "Image generated: user=%s model=%s ar=%s translate=%s",
                user_id, model, aspect_ratio, translated,
            )
        except Exception as send_err:
            logger.error("Failed to send generated image: %s", send_err)
            await placeholder.edit_text(
                "❌ Изображение создано, но не удалось отправить. Попробуйте снова."
            )
        return

    # ── Error ─────────────────────────────────────────────────────────────
    err = error_message or "unknown"
    text = _error_text(err)

    retry_keyboard = InlineKeyboardMarkup(
        [[InlineKeyboardButton("🔄 Попробовать снова", callback_data="draw:execute")]]
    )
    try:
        await placeholder.edit_text(text, parse_mode="Markdown", reply_markup=retry_keyboard)
    except Exception:
        await placeholder.edit_text(
            text.replace("*", "").replace("`", "").replace("_", ""),
            reply_markup=retry_keyboard,
        )


def _error_text(err: str) -> str:
    if err == "safety_blocked":
        return (
            "🚫 *Запрос заблокирован фильтром безопасности.*\n\n"
            "Попробуйте переформулировать описание — избегайте упоминания реальных людей, "
            "насилия или контента 18+."
        )
    if err == "quota_exhausted":
        return (
            "⏳ *Дневной лимит генерации изображений исчерпан.*\n\n"
            "Попробуйте завтра или переключитесь на другую модель."
        )
    if err == "paid_tier_required":
        return (
            "💳 *Эта модель требует оплаченного аккаунта.*\n\n"
            "Переключитесь на **✨ Flux** или **⚡ Z-Image** — они работают бесплатно."
        )
    if err == "unauthorized":
        return "🔑 *Ошибка авторизации.* Проверьте корректность `POLLINATIONS_API_KEY`."
    if err == "timeout":
        return "⏰ *Время ожидания истекло.* Серверы перегружены — попробуйте ещё раз."
    if err == "empty_prompt":
        return "⚠️ *Пустой запрос.* Напишите описание изображения."
    if err == "invalid_content_type":
        return (
            "⚠️ *Сервер вернул неожиданный ответ.*\n\n"
            "Возможно, модель временно недоступна. Попробуйте другую."
        )
    if err.startswith(("get_http_", "http_")):
        code = err.split("_")[-1]
        return f"❌ *Ошибка сервера HTTP {code}.* Попробуйте позже."
    return (
        "❌ *Не удалось создать изображение.*\n\n"
        f"`{err}`\n\n"
        "Попробуйте позже или измените запрос."
    )


def _escape_md(text: str) -> str:
    for ch in ("*", "_", "`", "["):
        text = text.replace(ch, f"\\{ch}")
    return text


# ── "Awaiting prompt" flag: called from messages.py ──────────────────────────


async def handle_draw_prompt_input(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> bool:
    """
    Check if the user is in 'awaiting_prompt' mode for the draw command.
    If yes: capture the text as the new prompt, update the Canvas keyboard,
    and return True (consumed).  Otherwise return False.

    Called early in messages.py before the normal AI pipeline.
    """
    if not context.user_data:
        return False
    state: dict = context.user_data.get(_DRAW_STATE_KEY, {})
    if not state.get("awaiting_prompt"):
        return False

    message = update.effective_message
    if not message or not message.text:
        return False

    new_prompt = message.text.strip()
    if not new_prompt:
        return False

    # Update the prompt, clear the waiting flag
    _set_draw_state(context, prompt=new_prompt, awaiting_prompt=False)

    state = _get_draw_state(context)
    keyboard = _build_main_menu(state)

    # Try to update the existing photo caption + keyboard
    last_photo_id = state.get("last_photo_msg")
    chat_id = message.chat_id if message.chat else 0

    try:
        await message.delete()  # Remove raw text to keep chat clean
    except BadRequest:
        pass

    if last_photo_id:
        short = new_prompt[:80] + ("..." if len(new_prompt) > 80 else "")
        caption = (
            f"🎨 *{_escape_md(short)}*\n"
            f"_{_model_label(state['model'])} · {state['aspect_ratio']}_"
        )
        try:
            bot = message.get_bot()
            await bot.edit_message_caption(
                chat_id=chat_id,
                message_id=last_photo_id,
                caption=caption,
                parse_mode="Markdown",
                reply_markup=keyboard,
            )
            return True
        except Exception:
            pass

    # Fallback: send a new confirmation message
    await message.reply_text(
        f"✅ Промпт обновлён: *{_escape_md(new_prompt[:80])}*\n\nНажмите *▶️ СГЕНЕРИРОВАТЬ*.",
        parse_mode="Markdown",
        reply_markup=keyboard,
    )
    return True


# ── Help text ──────────────────────────────────────────────────────────────────


def _build_help_text() -> str:
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
        "После генерации используйте кнопки под изображением:\n"
        "• Сменить модель или формат без немедленной перегенерации\n"
        "• Нажать *▶️ СГЕНЕРИРОВАТЬ* когда параметры выбраны\n"
        "• *✏️ Изменить промпт* — отправьте новый текст следующим сообщением"
    )


# ── Command handler ────────────────────────────────────────────────────────────


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
        enhance=state.get("enhance_prompt", False),
    )
