# /app/handlers/cmd_keys.py
"""Unified /keys wizard — admin-only inline keyboard for managing all API provider keys.

Flow:
  /keys → provider grid (inline keyboard)
      → tap provider → status + [✏️ Изменить] [✅ Проверить] [🗑 Очистить]
      → ✏️ → bot prompts for key → user sends key → bot stores + deletes user message
      → ✅ → lightweight health check against provider API
      → 🗑 → clears DB override, falls back to .env

Health checks run only when an administrator explicitly requests one, because
the available provider endpoints consume the same quota as user requests.
"""

import logging

import httpx
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

from app.handlers.conversation import suppress_hybrid_conversation_handler_warning
from app.repos.provider_keys import (
    clear_provider_key,
    get_provider_key,
    get_provider_status,
    set_provider_key,
)
from app.utils.decorators import admin_only
from app.utils.formatting import TelegramFormatter

logger = logging.getLogger(__name__)

# ── Provider Registry ────────────────────────────────────────────────────────

_PROVIDERS: dict[str, str] = {
    "weather": "🌤 Weather",
    "exchange": "💱 Exchange",
    "pollinations": "🎨 Pollinations",
    "jina": "📄 Jina",
}

# ConversationHandler states
AWAITING_KEY = 0

# ── Health check endpoints ───────────────────────────────────────────────────

_HEALTH_CHECKS: dict[str, str] = {
    "weather": "https://api.weatherapi.com/v1/current.json?key={key}&q=London&aqi=no",
    "exchange": "https://v6.exchangerate-api.com/v6/{key}/pair/USD/EUR",
}


# ── /keys entrypoint ─────────────────────────────────────────────────────────


@admin_only
async def keys_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show the provider key management grid."""
    keyboard = _build_provider_grid()
    text = "🔑 **Управление API-ключами**\n\nВыберите провайдера для просмотра статуса:"
    fmt, pm = TelegramFormatter.format_text(text)
    await update.message.reply_text(fmt, parse_mode=pm, reply_markup=keyboard)


def _build_provider_grid() -> InlineKeyboardMarkup:
    """Build a 2-column grid of provider buttons."""
    items = list(_PROVIDERS.items())
    rows = []
    for i in range(0, len(items), 2):
        row = [InlineKeyboardButton(label, callback_data=f"keys:show:{name}") for name, label in items[i : i + 2]]
        rows.append(row)
    rows.append([InlineKeyboardButton("🔄 Общий статус", callback_data="keys:status_all")])
    return InlineKeyboardMarkup(rows)


# ── Callback Handlers ────────────────────────────────────────────────────────


@admin_only
async def keys_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int | None:
    """Handle all keys:* callbacks."""
    query = update.callback_query
    await query.answer()
    data = query.data or ""

    if data.startswith("keys:show:"):
        provider = data.split(":", 2)[2]
        if provider not in _PROVIDERS:
            return ConversationHandler.END
        return await _show_provider_status(query, provider)

    if data.startswith("keys:check:"):
        provider = data.split(":", 2)[2]
        if provider not in _PROVIDERS:
            return ConversationHandler.END
        return await _check_provider_health_cb(query, provider)

    if data.startswith("keys:clear:"):
        provider = data.split(":", 2)[2]
        if provider not in _PROVIDERS:
            return ConversationHandler.END
        return await _clear_provider_key_cb(query, provider)

    if data.startswith("keys:edit:"):
        provider = data.split(":", 2)[2]
        if provider not in _PROVIDERS:
            return ConversationHandler.END
        if context.user_data is not None:
            context.user_data["keys_editing_provider"] = provider
        label = _PROVIDERS.get(provider, provider)
        await query.edit_message_text(
            f"✏️ Отправьте новый API-ключ для **{label}**.\n\n"
            f"_Сообщение с ключом будет автоматически удалено после сохранения._",
            parse_mode="Markdown",
        )
        return AWAITING_KEY

    if data == "keys:back":
        keyboard = _build_provider_grid()
        text = "🔑 **Управление API-ключами**\n\nВыберите провайдера:"
        fmt, pm = TelegramFormatter.format_text(text)
        await query.edit_message_text(fmt, parse_mode=pm, reply_markup=keyboard)
        return ConversationHandler.END

    if data == "keys:status_all":
        return await _show_all_statuses(query)

    return ConversationHandler.END


async def _show_provider_status(query, provider: str) -> int:
    """Show the status of a single provider with action buttons."""
    status = await get_provider_status(provider)
    label = _PROVIDERS.get(provider, provider)

    source_emoji = {"db": "💾 БД", "env": "📦 .env", "missing": "⚠️ Отсутствует"}
    source_text = source_emoji.get(status["source"], status["source"])

    text = f"🔑 **{label}**\n\n📍 Источник: {source_text}\n🔐 Ключ: `{status['preview']}`"
    fmt, pm = TelegramFormatter.format_text(text)

    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("✏️ Изменить", callback_data=f"keys:edit:{provider}"),
                InlineKeyboardButton("✅ Проверить", callback_data=f"keys:check:{provider}"),
            ],
            [
                InlineKeyboardButton("🗑 Очистить", callback_data=f"keys:clear:{provider}"),
                InlineKeyboardButton("◀️ Назад", callback_data="keys:back"),
            ],
        ]
    )
    await query.edit_message_text(fmt, parse_mode=pm, reply_markup=keyboard)
    return ConversationHandler.END


async def _check_provider_health_cb(query, provider: str) -> int:
    """Run a lightweight health check against a provider."""
    label = _PROVIDERS.get(provider, provider)
    result = await check_single_provider_health(provider)

    if result is True:
        text = f"✅ **{label}** — работает нормально"
    elif result is False:
        text = f"❌ **{label}** — не отвечает или ключ невалиден"
    else:
        text = f"⚠️ **{label}** — проверка недоступна (нет тестового эндпоинта)"

    fmt, pm = TelegramFormatter.format_text(text)
    keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("◀️ Назад", callback_data=f"keys:show:{provider}")]])
    await query.edit_message_text(fmt, parse_mode=pm, reply_markup=keyboard)
    return ConversationHandler.END


async def _clear_provider_key_cb(query, provider: str) -> int:
    """Clear the DB override for a provider."""
    label = _PROVIDERS.get(provider, provider)
    await clear_provider_key(provider)

    text = f"🗑 **{label}** — ключ из БД удалён.\nБот будет использовать значение из .env (если задано)."
    fmt, pm = TelegramFormatter.format_text(text)
    keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("◀️ Назад", callback_data=f"keys:show:{provider}")]])
    await query.edit_message_text(fmt, parse_mode=pm, reply_markup=keyboard)
    return ConversationHandler.END


async def _show_all_statuses(query) -> int:
    """Show a compact overview of all providers."""
    import asyncio

    lines = ["🔑 **Статус всех провайдеров**\n"]
    providers = list(_PROVIDERS.items())
    statuses = await asyncio.gather(*(get_provider_status(p) for p, _ in providers))

    for (_provider, label), status in zip(providers, statuses, strict=False):
        if status["source"] == "missing":
            icon = "⚠️"
        elif status["source"] == "db":
            icon = "💾"
        else:
            icon = "✅"
        lines.append(f"{icon} {label}: `{status['preview']}`")

    text = "\n".join(lines)
    fmt, pm = TelegramFormatter.format_text(text)
    keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("◀️ Назад", callback_data="keys:back")]])
    await query.edit_message_text(fmt, parse_mode=pm, reply_markup=keyboard)
    return ConversationHandler.END


# ── Key Receive Handler ──────────────────────────────────────────────────────


@admin_only
async def receive_key_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle the user's new API key message — store + delete the message."""
    provider = context.user_data.pop("keys_editing_provider", None) if context.user_data is not None else None
    if provider not in _PROVIDERS:
        return ConversationHandler.END

    new_key = (update.message.text or "").strip()
    if not new_key or len(new_key) < 8:
        await update.message.reply_text("❌ Ключ слишком короткий. Попробуйте ещё раз или /keys")
        return ConversationHandler.END

    # Store the key
    await set_provider_key(provider, new_key)

    # Delete the user's message containing the key (security)
    deleted = False
    try:
        await update.message.delete()
        deleted = True
    except Exception as del_err:
        logger.debug("Could not delete key message: %s", del_err)

    label = _PROVIDERS.get(provider, provider)
    deletion_note = (
        "_Сообщение с ключом удалено для безопасности._"
        if deleted
        else "⚠️ _Не удалось удалить сообщение с ключом — удалите его вручную._"
    )
    text = f"✅ Ключ для **{label}** обновлён.\n{deletion_note}"
    fmt, pm = TelegramFormatter.format_text(text)
    keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("◀️ К провайдерам", callback_data="keys:back")]])
    await update.message.chat.send_message(fmt, parse_mode=pm, reply_markup=keyboard)
    return ConversationHandler.END


# ── Health Check Engine ──────────────────────────────────────────────────────


async def check_single_provider_health(provider: str) -> bool | None:
    """Check if a provider's API is responding. Returns True/False/None (no check available)."""
    template = _HEALTH_CHECKS.get(provider)
    key = await get_provider_key(provider)

    if not template:
        return None  # No check available

    if not key:
        return False

    try:
        async with httpx.AsyncClient(timeout=6.0) as client:
            url = template.format(key=key)
            resp = await client.get(url)
            return resp.status_code == 200
    except Exception:
        return False


def build_keys_conversation_handler() -> ConversationHandler:
    """Build the ConversationHandler for the /keys wizard flow."""
    with suppress_hybrid_conversation_handler_warning():
        return ConversationHandler(
            entry_points=[
                CommandHandler("keys", keys_command),
                CallbackQueryHandler(keys_callback, pattern=r"^keys:"),
            ],
            states={
                AWAITING_KEY: [
                    MessageHandler(filters.TEXT & ~filters.COMMAND, receive_key_message),
                ],
            },
            fallbacks=[
                CommandHandler("keys", keys_command),
                CallbackQueryHandler(keys_callback, pattern=r"^keys:"),
            ],
            per_user=True,
            per_chat=True,
            per_message=False,
            name="keys_wizard",
        )
