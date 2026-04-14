# /app/handlers/cmd_models.py
"""Admin /models wizard — zero-downtime model list management.

Flow:
  /models → provider selector  (Gemini | OpenRouter)
      → provider view: numbered model list + action buttons
          [➕ Добавить] [🔃 Сбросить к .env] [◀️ Назад]
          + individual [❌] buttons per model for removal
      → ➕ → bot prompts for model name → user sends it → added instantly
"""

import contextlib
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

from app.repos.models_repo import add_model, get_models, remove_model, reset_models_to_env
from app.utils.decorators import admin_only
from app.utils.formatting import TelegramFormatter

logger = logging.getLogger(__name__)

# ConversationHandler state
AWAITING_MODEL_NAME = 100  # distinct from AWAITING_KEY = 0 in cmd_keys

_PROVIDERS = {
    "gemini": "🤖 Gemini",
    "openrouter": "⚡ OpenRouter",
}


# ── /models entry point ───────────────────────────────────────────────────────


@admin_only
async def models_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Show the provider selector for model management."""
    keyboard = _build_provider_selector()
    text = "🧠 **Управление моделями**\n\nВыберите провайдера:"
    fmt, pm = TelegramFormatter.format_text(text)
    await update.message.reply_text(fmt, parse_mode=pm, reply_markup=keyboard)
    return ConversationHandler.END


def _build_provider_selector() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton(label, callback_data=f"models:show:{name}") for name, label in _PROVIDERS.items()],
        ]
    )


# ── Callback router ───────────────────────────────────────────────────────────


async def models_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Route all models:* callback_data payloads."""
    query = update.callback_query
    await query.answer()
    data = query.data or ""

    if data.startswith("models:show:"):
        provider = data.split(":", 2)[2]
        return await _show_provider_view(query, provider)

    if data.startswith("models:remove:"):
        # payload: models:remove:<provider>:<model_name>
        parts = data.split(":", 3)
        provider, model_name = parts[2], parts[3]
        return await _handle_remove(query, provider, model_name)

    if data.startswith("models:add:"):
        provider = data.split(":", 2)[2]
        context.user_data["models_editing_provider"] = provider
        label = _PROVIDERS.get(provider, provider)
        await query.edit_message_text(
            f"➕ Введите точное название модели для **{label}**.\n\n"
            "_Например:_ `gemini-2.5-pro-preview`\n"
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


async def _show_provider_view(query, provider: str) -> int:
    """Render the model list plus action buttons for a provider."""
    label = _PROVIDERS.get(provider, provider)
    models = await get_models(provider)

    if models:
        numbered = "\n".join(f"`{i + 1}.` `{m}`" for i, m in enumerate(models))
        text = f"🧠 **{label}**\n\n{numbered}"
    else:
        text = f"🧠 **{label}**\n\n_Список пуст_"

    fmt, pm = TelegramFormatter.format_text(text)

    # Build per-model ❌ remove buttons
    remove_rows = [
        [InlineKeyboardButton(f"❌ {m}", callback_data=f"models:remove:{provider}:{m}")]
        for m in models
    ]

    action_row = [
        InlineKeyboardButton("➕ Добавить", callback_data=f"models:add:{provider}"),
        InlineKeyboardButton("🔃 Сбросить к .env", callback_data=f"models:reset:{provider}"),
    ]
    back_row = [InlineKeyboardButton("◀️ Назад", callback_data="models:back")]

    keyboard = InlineKeyboardMarkup(remove_rows + [action_row, back_row])
    await query.edit_message_text(fmt, parse_mode=pm, reply_markup=keyboard)
    return ConversationHandler.END


# ── Remove handler ────────────────────────────────────────────────────────────


async def _handle_remove(query, provider: str, model_name: str) -> int:
    removed = await remove_model(provider, model_name)
    label = _PROVIDERS.get(provider, provider)

    if removed:
        text = f"❌ **{label}**: модель `{model_name}` удалена из ротации."
    else:
        text = f"⚠️ **{label}**: модель `{model_name}` не найдена."

    fmt, pm = TelegramFormatter.format_text(text)
    keyboard = InlineKeyboardMarkup(
        [[InlineKeyboardButton("◀️ К списку", callback_data=f"models:show:{provider}")]]
    )
    await query.edit_message_text(fmt, parse_mode=pm, reply_markup=keyboard)
    return ConversationHandler.END


# ── Reset handler ─────────────────────────────────────────────────────────────


async def _handle_reset(query, provider: str) -> int:
    label = _PROVIDERS.get(provider, provider)
    restored = await reset_models_to_env(provider)

    if restored:
        names = "\n".join(f"`{m}`" for m in restored)
        text = f"🔃 **{label}** — сброс к .env:\n\n{names}"
    else:
        text = f"🔃 **{label}** — .env список пустой, изменений нет."

    fmt, pm = TelegramFormatter.format_text(text)
    keyboard = InlineKeyboardMarkup(
        [[InlineKeyboardButton("◀️ К списку", callback_data=f"models:show:{provider}")]]
    )
    await query.edit_message_text(fmt, parse_mode=pm, reply_markup=keyboard)
    return ConversationHandler.END


# ── Add: receive message ──────────────────────────────────────────────────────


async def receive_model_name(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Store the new model name sent by the admin."""
    provider = context.user_data.pop("models_editing_provider", None)
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

    if added:
        text = f"✅ Модель `{model_name}` добавлена в **{label}**.\nПользователи увидят её немедленно."
    else:
        text = f"⚠️ Модель `{model_name}` уже есть в списке **{label}**."

    fmt, pm = TelegramFormatter.format_text(text)
    keyboard = InlineKeyboardMarkup(
        [[InlineKeyboardButton("◀️ К списку", callback_data=f"models:show:{provider}")]]
    )
    await update.message.chat.send_message(fmt, parse_mode=pm, reply_markup=keyboard)
    return ConversationHandler.END


# ── ConversationHandler builder ───────────────────────────────────────────────


def build_models_conversation_handler() -> ConversationHandler:
    """Build the ConversationHandler for the /models admin wizard."""
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
        name="models_wizard",
    )
