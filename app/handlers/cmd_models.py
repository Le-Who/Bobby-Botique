# /app/handlers/cmd_models.py
"""Admin /models wizard — zero-downtime model list management.

Flow:
  /models → provider selector  (Gemini | Opencode | OpenRouter | FreeTheAI)
      → provider view: numbered model list + action buttons
          [➕ Добавить] [🔃 Сбросить к .env] [◀️ Назад]
          + individual [❌] buttons per model for removal
      → ➕ → bot prompts for model name → user sends it → added instantly
"""

import contextlib
import functools
import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

from app.config import get_model_hash
from app.handlers.conversation import suppress_hybrid_conversation_handler_warning
from app.repos.models_repo import (
    ModelCatalogSource,
    ModelMutationCode,
    add_model,
    get_model_catalog,
    remove_model,
    reset_models_to_env,
)
from app.utils.decorators import admin_only
from app.utils.formatting import TelegramFormatter

logger = logging.getLogger(__name__)

# ConversationHandler state
AWAITING_MODEL_NAME = 100  # distinct from AWAITING_KEY = 0 in cmd_keys

_PROVIDERS = {
    "gemini": "🤖 Gemini",
    "opencode": "⚡ Opencode Go",
    "openrouter": "🌐 OpenRouter",
    "freetheai": "🚀 FreeTheAI",
}


def _models_admin_step(func):
    """Apply admin auth and terminate stale wizard state when access is denied."""
    protected = admin_only(func)

    @functools.wraps(func)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE, *args, **kwargs):
        result = await protected(update, context, *args, **kwargs)
        if result is None:
            if context.user_data is not None:
                context.user_data.pop("models_editing_provider", None)
            return ConversationHandler.END
        return result

    return wrapper


# ── /models entry point ───────────────────────────────────────────────────────


@_models_admin_step
async def models_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Show the provider selector for model management."""
    keyboard = _build_provider_selector()
    text = "🧠 **Управление моделями**\n\nВыберите провайдера:"
    fmt, pm = TelegramFormatter.format_text(text)
    await update.message.reply_text(fmt, parse_mode=pm, reply_markup=keyboard)
    return ConversationHandler.END


def _build_provider_selector() -> InlineKeyboardMarkup:
    buttons = [
        InlineKeyboardButton(label, callback_data=f"models:show:{name}")
        for name, label in _PROVIDERS.items()
    ]
    return InlineKeyboardMarkup(
        [buttons[index : index + 2] for index in range(0, len(buttons), 2)]
    )


# ── Callback router ───────────────────────────────────────────────────────────


@_models_admin_step
async def models_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Route all models:* callback_data payloads."""
    query = update.callback_query
    await query.answer()
    data = query.data or ""

    if data.startswith("models:show:"):
        provider = data.split(":", 2)[2]
        return await _show_provider_view(query, provider)

    if data.startswith("models:remove:"):
        # payload: models:remove:<provider>:<short_model_token>
        parts = data.split(":", 3)
        provider, model_token = parts[2], parts[3]
        return await _handle_remove(query, provider, model_token)

    if data.startswith("models:add:"):
        provider = data.split(":", 2)[2]
        if context.user_data is not None:
            context.user_data["models_editing_provider"] = provider
        label = _PROVIDERS.get(provider, provider)
        await query.edit_message_text(
            f"➕ Введите точное название модели для **{label}**.\n\n"
            "_Например:_ `gemini-3.5-flash`\n"
            "_Сообщение будет удалено после сохранения._",
            parse_mode="Markdown",
        )
        return AWAITING_MODEL_NAME

    if data.startswith("models:reset:"):
        provider = data.split(":", 2)[2]
        return await _handle_reset(query, provider)

    if data == "models:back":
        keyboard = _build_provider_selector()
        text = "🧠 **Управление моделями**\n\nВыберите провайдера:"
        fmt, pm = TelegramFormatter.format_text(text)
        await query.edit_message_text(fmt, parse_mode=pm, reply_markup=keyboard)
        return ConversationHandler.END

    return ConversationHandler.END


# ── Provider view ─────────────────────────────────────────────────────────────


async def _show_provider_view(query, provider: str, *, notice: str | None = None) -> int:
    """Render the model list plus action buttons for a provider."""
    label = _PROVIDERS.get(provider, provider)
    catalog = await get_model_catalog(provider)
    models = list(catalog.models)
    source = ".env / Secret" if catalog.source is ModelCatalogSource.ENV else "/models override"

    if models:
        numbered = "\n".join(f"`{i + 1}.` `{m}`" for i, m in enumerate(models))
        text = f"🧠 **{label}**\nИсточник: `{source}`\n\n{numbered}"
    else:
        text = f"🧠 **{label}**\nИсточник: `{source}`\n\n_Список пуст_"
    if notice:
        text = f"{notice}\n\n{text}"

    fmt, pm = TelegramFormatter.format_text(text)

    remove_rows = [
        [
            InlineKeyboardButton(
                f"❌ {model}",
                callback_data=f"models:remove:{provider}:{get_model_hash(model)}",
            )
        ]
        for model in models
    ]

    action_row = [
        InlineKeyboardButton("➕ Добавить", callback_data=f"models:add:{provider}"),
        InlineKeyboardButton("🔃 Сбросить к .env", callback_data=f"models:reset:{provider}"),
    ]
    back_row = [InlineKeyboardButton("◀️ Назад", callback_data="models:back")]

    keyboard = InlineKeyboardMarkup(remove_rows + [action_row, back_row])
    await query.edit_message_text(text=fmt, parse_mode=pm, reply_markup=keyboard)
    return ConversationHandler.END


# ── Remove handler ────────────────────────────────────────────────────────────


def _resolve_model_token(models: tuple[str, ...], token: str) -> str | None:
    matches = [model for model in models if get_model_hash(model) == token]
    return matches[0] if len(matches) == 1 else None


async def _handle_remove(query, provider: str, model_token: str) -> int:
    label = _PROVIDERS.get(provider, provider)
    try:
        catalog = await get_model_catalog(provider)
    except ValueError:
        return await _show_provider_view(query, "gemini", notice="⚠️ Неизвестный провайдер моделей.")

    model_name = _resolve_model_token(catalog.models, model_token)
    if model_name is None:
        return await _show_provider_view(
            query,
            provider,
            notice="⚠️ Список моделей уже изменился. Показана актуальная версия.",
        )

    result = await remove_model(provider, model_name)
    if result.code is ModelMutationCode.REMOVED:
        notice = f"❌ **{label}**: модель `{model_name}` удалена из ротации."
    elif result.code is ModelMutationCode.NOT_FOUND:
        notice = f"⚠️ **{label}**: модель `{model_name}` уже отсутствует."
    else:
        notice = f"⚠️ **{label}**: не удалось удалить модель `{model_name}`."
    return await _show_provider_view(query, provider, notice=notice)


# ── Reset handler ─────────────────────────────────────────────────────────────


async def _handle_reset(query, provider: str) -> int:
    label = _PROVIDERS.get(provider, provider)
    restored = await reset_models_to_env(provider)

    if restored:
        names = "\n".join(f"`{m}`" for m in restored)
        text = f"🔃 **{label}** — сброс к .env:\n\n{names}"
    else:
        text = f"🔃 **{label}** — сброс к .env выполнен. Пользовательский список пуст."

    fmt, pm = TelegramFormatter.format_text(text)
    keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("◀️ К списку", callback_data=f"models:show:{provider}")]])
    await query.edit_message_text(fmt, parse_mode=pm, reply_markup=keyboard)
    return ConversationHandler.END


# ── Add: receive message ──────────────────────────────────────────────────────


@_models_admin_step
async def receive_model_name(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Store the new model name sent by the admin."""
    provider = context.user_data.pop("models_editing_provider", None) if context.user_data is not None else None
    if not provider:
        return ConversationHandler.END

    model_name = (update.message.text or "").strip()

    # Basic sanity: must look like a model identifier
    if not model_name or len(model_name) < 3 or " " in model_name:
        await update.message.reply_text(
            "❌ Некорректное название модели. Должно быть без пробелов, минимум 3 символа.\n"
            "Попробуйте ещё раз или /models чтобы выйти."
        )
        return ConversationHandler.END

    added = await add_model(provider, model_name)
    label = _PROVIDERS.get(provider, provider)

    # Delete the admin's message for a clean UI
    with contextlib.suppress(Exception):
        await update.message.delete()

    if added.code is ModelMutationCode.ADDED:
        text = f"✅ Модель `{model_name}` добавлена в **{label}**.\nПользователи увидят её немедленно."
    elif added.code is ModelMutationCode.DUPLICATE:
        text = f"⚠️ Модель `{model_name}` уже есть в списке **{label}**."
    elif added.code is ModelMutationCode.UNSUPPORTED:
        text = f"❌ Модель `{model_name}` не поддерживает generateContent в Gemini API."
    elif added.code is ModelMutationCode.VALIDATION_UNAVAILABLE:
        text = (
            f"⚠️ Не удалось проверить модель `{model_name}` через Gemini API. "
            "Список не изменён; повторите позже."
        )
    elif added.code is ModelMutationCode.UNKNOWN_PROVIDER:
        text = "❌ Неизвестный провайдер моделей."
    else:
        text = f"❌ Некорректное название модели `{model_name}` для **{label}**."

    fmt, pm = TelegramFormatter.format_text(text)
    keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("◀️ К списку", callback_data=f"models:show:{provider}")]])
    await update.message.chat.send_message(fmt, parse_mode=pm, reply_markup=keyboard)
    return ConversationHandler.END


# ── ConversationHandler builder ───────────────────────────────────────────────


def build_models_conversation_handler() -> ConversationHandler:
    """Build the ConversationHandler for the /models admin wizard."""
    with suppress_hybrid_conversation_handler_warning():
        return ConversationHandler(
            entry_points=[
                CommandHandler("models", models_command),
                CallbackQueryHandler(models_callback, pattern=r"^models:"),
            ],
            states={
                AWAITING_MODEL_NAME: [
                    MessageHandler(filters.TEXT & ~filters.COMMAND, receive_model_name),
                ],
            },
            fallbacks=[
                CommandHandler("models", models_command),
                CallbackQueryHandler(models_callback, pattern=r"^models:"),
            ],
            per_user=True,
            per_chat=True,
            per_message=False,
            name="models_wizard",
        )
