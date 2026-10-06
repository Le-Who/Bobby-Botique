"""Private birth date input and contextual tarot entry from inline compatibility."""

from __future__ import annotations

import asyncio
import html
import os
import secrets
import time
from urllib.parse import urlencode, urlsplit

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardRemove, Update, WebAppInfo
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

from app.i18n import t
from app.natal.compatibility import (
    CompatibilityPair,
    CompatibilityQueryError,
    build_birth_date_compatibility,
    build_compatibility_tarot_context,
    compatibility_start_payload,
    looks_like_birth_date_input,
    parse_compatibility_birth_date,
    parse_compatibility_start_payload,
    partner_label,
)
from app.utils.decorators import safe_handler

_FLOW_KEY = "compatibility_flow"
_TAROT_KEY = "compatibility_tarot_context"
_BIRTH_MESSAGES_KEY = "compatibility_birth_messages"
_EXPIRY_TIMER_KEY = "compatibility_expiry_timer"
_FLOW_TTL = 30 * 60
_BIRTH_MESSAGE_LIMIT = 64


def clear_compatibility_input(user_data: dict) -> None:
    """Erase raw input and cancel its owned timer when switching private modes."""
    handle = user_data.pop(_EXPIRY_TIMER_KEY, None)
    if handle is not None:
        handle.cancel()
    flow = user_data.pop(_FLOW_KEY, None)
    if isinstance(flow, dict):
        flow.pop("first_date", None)


def _expire_input(user_data: dict, flow_id: str) -> None:
    flow = user_data.get(_FLOW_KEY)
    if isinstance(flow, dict) and flow.get("flow_id") == flow_id:
        flow.pop("first_date", None)
        flow["expired"] = True
        user_data.pop(_EXPIRY_TIMER_KEY, None)
        # Keep public routing metadata so a late date never falls into AI chat.


def _remember_birth_message(user_data: dict, message_id: int) -> None:
    message_ids = user_data.setdefault(_BIRTH_MESSAGES_KEY, {})
    message_ids[message_id] = None
    while len(message_ids) > _BIRTH_MESSAGE_LIMIT:
        message_ids.pop(next(iter(message_ids)))


def compatibility_keyboard(pair: CompatibilityPair, *, bot_username: str | None, lang: str) -> InlineKeyboardMarkup:
    if not bot_username:
        return InlineKeyboardMarkup([])
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    t("compat.dates_button", lang),
                    url=f"https://t.me/{bot_username}?start={compatibility_start_payload(pair)}",
                ),
                InlineKeyboardButton(
                    t("compat.tarot_button", lang),
                    url=f"https://t.me/{bot_username}?start={compatibility_start_payload(pair, tarot=True)}",
                ),
            ]
        ]
    )


async def start_compatibility(update: Update, context: ContextTypes.DEFAULT_TYPE, payload: str) -> None:
    message = update.effective_message
    if message is None:
        return
    lang = "ru"
    if update.effective_chat is None or update.effective_chat.type != "private":
        await message.reply_text(t("compat.private_only", lang))
        return
    try:
        pair, tarot = parse_compatibility_start_payload(payload)
    except CompatibilityQueryError:
        await message.reply_text(t("compat.invalid_link", lang))
        return
    lang = pair.language
    if tarot:
        from app.handlers.cmd_tarot import tarot_command

        await tarot_command(update, context, question_context=build_compatibility_tarot_context(pair, lang=lang))
        return
    clear_compatibility_input(context.user_data)
    from app.state import clear_tarot_session, ensure_state_loaded

    await ensure_state_loaded(update.effective_user.id)
    clear_tarot_session(update.effective_user.id)
    form_url = compatibility_form_url(pair)
    if form_url:
        labels = " + ".join(partner_label(partner, lang=lang) for partner in (pair.first, pair.second))
        await message.reply_text(
            t("compat.form_intro", lang, pair=html.escape(labels)),
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton(t("compat.form_button", lang), web_app=WebAppInfo(url=form_url))]]
            ),
        )
        return
    flow_id = secrets.token_hex(8)
    context.user_data[_FLOW_KEY] = {
        "pair": pair,
        "lang": lang,
        "started_at": time.monotonic(),
        "flow_id": flow_id,
        "message_ids": set(),
    }
    context.user_data[_EXPIRY_TIMER_KEY] = asyncio.get_running_loop().call_later(
        _FLOW_TTL,
        _expire_input,
        context.user_data,
        flow_id,
    )
    labels = " + ".join(partner_label(partner, lang=lang) for partner in (pair.first, pair.second))
    await message.reply_text(
        t(
            "compat.first_date",
            lang,
            pair=html.escape(labels),
            partner=html.escape(partner_label(pair.first, lang=lang)),
        ),
        parse_mode="HTML",
        reply_markup=ReplyKeyboardRemove(),
    )


class PendingCompatibilityFilter(filters.MessageFilter):
    """Read the same PTB user_data used by the input handler, without a second state store."""

    def __init__(self, application: Application) -> None:
        super().__init__(name="PendingCompatibility")
        self._application = application

    def filter(self, message) -> bool:
        from app.handlers.menu_intents import is_standalone_menu_request

        if not message.from_user or message.chat.type != "private":
            return False
        if is_standalone_menu_request(message.text):
            return False
        user_data = self._application.user_data.get(message.from_user.id)
        return isinstance(user_data, dict) and isinstance(user_data.get(_FLOW_KEY), dict)


class CompatibilityEditedFilter(PendingCompatibilityFilter):
    """Keep edits to recent date messages private even after the flow is closed."""

    def filter(self, message) -> bool:
        if not message.from_user or message.chat.type != "private":
            return False
        user_data = self._application.user_data.get(message.from_user.id)
        return isinstance(user_data, dict) and (
            isinstance(user_data.get(_FLOW_KEY), dict) or message.message_id in user_data.get(_BIRTH_MESSAGES_KEY, {})
        )


class OrphanBirthDateFilter(filters.MessageFilter):
    """Protect isolated private dates even when process-local routing metadata is gone."""

    def filter(self, message) -> bool:
        return bool(
            message.from_user and message.chat.type == "private" and looks_like_birth_date_input(message.text or "")
        )


@safe_handler()
async def handle_orphan_birth_date(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    # No generic auth decorator: it can forward denied message text to admins.
    # The owning form has already had a chance to handle this input.
    await update.effective_message.reply_text(t("compat.orphan_date", "ru"))


async def clear_compatibility_on_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if message and (message.text or "").split(maxsplit=1)[0].split("@", 1)[0].casefold() != "/cancel":
        clear_compatibility_input(context.user_data)


@safe_handler()
async def cancel_compatibility(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    lang = context.user_data.get(_FLOW_KEY, {}).get("lang", "ru")
    clear_compatibility_input(context.user_data)
    await update.effective_message.reply_text(t("compat.cancelled", lang))


@safe_handler()
async def handle_compatibility_date(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    flow = context.user_data.get(_FLOW_KEY)
    if update.effective_chat.type != "private":
        return
    message = update.effective_message
    edited = update.edited_message is not None
    lang = flow.get("lang", "ru") if isinstance(flow, dict) else "ru"
    if edited and (
        not isinstance(flow, dict)
        or (
            message.message_id in context.user_data.get(_BIRTH_MESSAGES_KEY, {})
            and message.message_id not in flow["message_ids"]
        )
    ):
        await message.reply_text(t("compat.edited_restart", lang))
        return
    if not isinstance(flow, dict):
        return
    _remember_birth_message(context.user_data, message.message_id)
    flow["message_ids"].add(message.message_id)
    from app.repos.users import is_authorized
    from app.request_context import ensure_request_id, set_user_context

    set_user_context(update.effective_user.id, update.effective_chat.id)
    ensure_request_id(f"tgcompat-{update.effective_chat.id}-{update.update_id}")
    if not await is_authorized(update.effective_user.id):
        # The generic auth decorator forwards denied message text to admins;
        # a birth date must never take that path.
        clear_compatibility_input(context.user_data)
        await message.reply_text(t("compat.access_required", lang))
        return
    if flow.get("expired") or time.monotonic() - flow["started_at"] > _FLOW_TTL:
        clear_compatibility_input(context.user_data)
        await message.reply_text(t("compat.expired", lang))
        return
    try:
        birth_date = parse_compatibility_birth_date(message.text or "")
    except ValueError:
        if edited and message.message_id == flow.get("first_message_id"):
            flow.pop("first_date", None)
        await message.reply_text(t("compat.bad_date", lang), parse_mode="HTML")
        return
    pair = flow["pair"]
    if "first_date" not in flow or (edited and message.message_id == flow.get("first_message_id")):
        flow["first_date"] = birth_date
        flow["first_message_id"] = message.message_id
        await message.reply_text(
            t("compat.second_date", lang, partner=html.escape(partner_label(pair.second, lang=lang))), parse_mode="HTML"
        )
        return
    first_date = flow.pop("first_date")
    clear_compatibility_input(context.user_data)
    try:
        reading = await asyncio.to_thread(build_birth_date_compatibility, pair, first_date, birth_date, lang=lang)
        from app.natal.compatibility_interpretation import interpret_compatibility

        await message.reply_text(t("compat.interpreting", lang))
        if not await is_authorized(update.effective_user.id):
            return
        reading = await interpret_compatibility(reading, user_id=update.effective_user.id, lang=lang)
    except Exception:
        await message.reply_text(t("compat.failed", lang))
        return
    if not await is_authorized(update.effective_user.id):
        return
    keyboard = compatibility_result_keyboard(context.user_data, reading.tarot_context, lang=lang)
    from app.utils.text_format import split_text_safe

    chunks = split_text_safe(reading.html)
    for index, chunk in enumerate(chunks):
        await message.reply_text(chunk, parse_mode="HTML", reply_markup=keyboard if index == len(chunks) - 1 else None)


def compatibility_result_keyboard(user_data: dict, question: str, *, lang: str) -> InlineKeyboardMarkup:
    """Store only derived pair context for both chat and Mini App results."""
    contexts = user_data.setdefault(_TAROT_KEY, {})
    now = time.monotonic()
    for token in list(contexts):
        if now - contexts[token]["created_at"] > _FLOW_TTL:
            contexts.pop(token, None)
    while len(contexts) >= 5:
        contexts.pop(next(iter(contexts)))
    token = secrets.token_hex(8)
    contexts[token] = {"question": question, "created_at": now, "lang": lang}
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton(t("compat.tarot_button", lang), callback_data=f"compat_tarot:{token}")]]
    )


def compatibility_form_url(pair: CompatibilityPair) -> str:
    from app.config import settings

    base = (getattr(settings, "WEBAPP_BASE_URL", "") or "").strip().rstrip("/")
    if not base:
        base = (
            (getattr(settings, "WEBHOOK_URL", "") or os.environ.get("WEBHOOK_URL", ""))
            .split("/webhook", 1)[0]
            .rstrip("/")
        )
    parsed = urlsplit(base)
    if (
        parsed.scheme != "https"
        or not parsed.netloc
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        return ""
    return f"{base}/webapp/compatibility-form?{urlencode({'pair': compatibility_start_payload(pair)})}"


async def clear_compatibility_on_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    clear_compatibility_input(context.user_data)


@safe_handler()
async def compatibility_tarot_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool | None:
    query = update.callback_query
    await query.answer()
    if update.effective_chat is None or update.effective_chat.type != "private":
        return False
    from app.repos.users import is_authorized
    from app.request_context import ensure_request_id, set_user_context

    set_user_context(update.effective_user.id, update.effective_chat.id)
    ensure_request_id(f"tgcb-{update.effective_user.id}-{query.id}")
    if not await is_authorized(update.effective_user.id):
        lang = context.user_data.get(_FLOW_KEY, {}).get("lang", "ru")
        clear_compatibility_input(context.user_data)
        await query.message.reply_text(t("compat.access_required", lang))
        return False
    token = query.data.rsplit(":", 1)[-1]
    contexts = context.user_data.get(_TAROT_KEY, {})
    stored = contexts.get(token)
    if not isinstance(stored, dict) or time.monotonic() - stored["created_at"] > _FLOW_TTL:
        lang = stored.get("lang", "ru") if isinstance(stored, dict) else "ru"
        contexts.pop(token, None)
        await query.message.reply_text(t("compat.tarot_expired", lang))
        return False
    from app.handlers.cmd_tarot import tarot_command

    if await tarot_command(update, context, question_context=stored["question"]):
        contexts.pop(token, None)
        return True
    return False


def register_compatibility_handlers(application: Application) -> None:
    from app.handlers.menu_intents import is_standalone_menu_request

    class MenuRequestFilter(filters.MessageFilter):
        def filter(self, message) -> bool:
            return message.chat.type == "private" and is_standalone_menu_request(message.text)

    pending = PendingCompatibilityFilter(application)
    application.add_handler(
        MessageHandler(filters.UpdateType.MESSAGE & filters.COMMAND, clear_compatibility_on_command), group=-90
    )
    application.add_handler(
        MessageHandler(
            filters.UpdateType.MESSAGE & filters.TEXT & ~filters.COMMAND & MenuRequestFilter(),
            clear_compatibility_on_menu,
        ),
        group=-89,
    )
    # Dates precede both natal conversations and ordinary chat/memory routing.
    private_dates = (filters.UpdateType.MESSAGE & pending) | (
        filters.UpdateType.EDITED_MESSAGE & CompatibilityEditedFilter(application)
    )
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND & private_dates, handle_compatibility_date))
    application.add_handler(CommandHandler("cancel", cancel_compatibility, filters=pending))


def register_birth_date_privacy_guard(application: Application) -> None:
    """Register after active date forms and before general AI/logging handlers."""
    updates = filters.UpdateType.MESSAGE | filters.UpdateType.EDITED_MESSAGE
    application.add_handler(
        MessageHandler(updates & filters.TEXT & ~filters.COMMAND & OrphanBirthDateFilter(), handle_orphan_birth_date)
    )
