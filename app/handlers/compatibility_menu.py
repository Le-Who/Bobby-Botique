"""Private sign picker and detailed compatibility entry points."""

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler, filters

from app.bot_commands import language_from_telegram
from app.handlers.compatibility import clear_compatibility_input, compatibility_result_keyboard, start_compatibility
from app.handlers.menu_intents import COMPATIBILITY_ENTRY_RE
from app.i18n import t
from app.natal.compatibility import (
    CompatibilityPair,
    CompatibilityPartner,
    build_compatibility_tarot_context,
    build_sign_compatibility_html,
    compatibility_start_payload,
)
from app.utils.decorators import authorized_only, safe_handler

COMPATIBILITY_CALLBACK_RE = r"^compat_(?:pick:(?:ru|en):(?:[0-9]|1[01])|pair:(?:ru|en):(?:[0-9]|1[01]):(?:[0-9]|1[01])|dates:compat_(?:e_)?n\d{1,2}_n\d{1,2}|menu)$"


def _sign_keyboard(lang: str, first: int | None = None) -> InlineKeyboardMarkup:
    buttons = [
        InlineKeyboardButton(
            t(f"compat.sign.{sign}", lang),
            callback_data=f"compat_pick:{lang}:{sign}" if first is None else f"compat_pair:{lang}:{first}:{sign}",
        )
        for sign in range(12)
    ]
    rows = [buttons[index : index + 3] for index in range(0, 12, 3)]
    if first is not None:
        rows.append([InlineKeyboardButton(t("compat.pick_back", lang), callback_data="compat_menu")])
    return InlineKeyboardMarkup(rows)


async def _clear_other_mode(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    from app.state import clear_tarot_session, ensure_state_loaded

    await ensure_state_loaded(update.effective_user.id)
    clear_tarot_session(update.effective_user.id)
    clear_compatibility_input(context.user_data)


@authorized_only
@safe_handler()
async def compatibility_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool | None:
    """Open compatibility: two sign buttons, a brief reading, then optional details."""
    lang = language_from_telegram(update.effective_user.language_code)
    if update.effective_chat.type != "private":
        await update.effective_message.reply_text(t("compat.private_only", lang))
        return False
    await _clear_other_mode(update, context)
    await update.effective_message.reply_text(
        t("compat.pick_first", lang), parse_mode="HTML", reply_markup=_sign_keyboard(lang)
    )
    return True


@authorized_only
@safe_handler()
async def compatibility_menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool | None:
    query = update.callback_query
    await query.answer()
    if update.effective_chat.type != "private":
        return False
    if query.data == "compat_menu":
        return await compatibility_command(update, context)
    await _clear_other_mode(update, context)
    action, *values = query.data.split(":")
    if action == "compat_dates":
        await start_compatibility(update, context, values[0])
        return True
    lang = values[0]
    first = int(values[1])
    if action == "compat_pick":
        await query.edit_message_text(
            t("compat.pick_second", lang, first=t(f"compat.sign.{first}", lang)),
            parse_mode="HTML",
            reply_markup=_sign_keyboard(lang, first),
        )
        return True
    second = int(values[2])
    pair = CompatibilityPair(CompatibilityPartner(first), CompatibilityPartner(second), lang)
    tarot = compatibility_result_keyboard(
        context.user_data, build_compatibility_tarot_context(pair, lang=lang), lang=lang
    )
    await query.edit_message_text(
        build_sign_compatibility_html(pair, lang=lang),
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        t("compat.dates_button", lang),
                        callback_data=f"compat_dates:{compatibility_start_payload(pair)}",
                    )
                ],
                *tarot.inline_keyboard,
                [InlineKeyboardButton(t("compat.pick_again", lang), callback_data="compat_menu")],
            ]
        ),
    )
    return True


def register_compatibility_menu(application: Application) -> None:
    application.add_handler(CommandHandler("compatibility", compatibility_command))
    application.add_handler(
        MessageHandler(
            filters.UpdateType.MESSAGE
            & filters.ChatType.PRIVATE
            & filters.TEXT
            & filters.Regex(COMPATIBILITY_ENTRY_RE),
            compatibility_command,
        )
    )
    application.add_handler(CallbackQueryHandler(compatibility_menu_callback, pattern=COMPATIBILITY_CALLBACK_RE))
