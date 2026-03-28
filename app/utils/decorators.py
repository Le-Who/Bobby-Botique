import functools
import logging

from telegram import Update
from telegram.ext import ContextTypes

from app.repos.users import is_admin, is_authorized
from app.request_context import set_request_id, set_user_context


def authorized_only(func):
    """Decorator to check if the user is authorized."""

    @functools.wraps(func)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE, *args, **kwargs):
        if not update.effective_user:
            return
        user_id = update.effective_user.id
        chat_id = getattr(update.effective_chat, "id", None)
        set_user_context(user_id, chat_id)
        # Generate request_id: callbacks use query.id, messages use update_id
        if update.callback_query:
            set_request_id(f"tgcb-{user_id}-{update.callback_query.id}")
        else:
            set_request_id(f"tgcmd-{chat_id}-{getattr(update, 'update_id', 'na')}")
            # Protect against state leakage: clear volatile UI mode flags on any new command
            user_data = context.user_data
            if update.message and update.message.text and update.message.text.startswith("/") and user_data is not None:
                volatile_keys = [
                    "rename_role_id",
                    "rename_role_key",
                    "edit_prompt_role_id",
                    "edit_prompt_role_key",
                    "edit_prompt_ai_role_id",
                    "edit_prompt_ai_role_key",
                    "edit_prompt_ai_current",
                    "edit_prompt_ai_save_role_id",
                    "edit_prompt_ai_save_role_key",
                    "rename_conv_id",
                    "edit_prompt_ai_preview",
                ]
                for key in volatile_keys:
                    user_data.pop(key, None)
        if not await is_authorized(user_id):
            logging.warning("Unauthorized access attempt by user %s to %s", user_id, func.__name__)
            if update.message:
                await update.message.reply_text("❌ У вас нет доступа к этому боту.")
            elif update.callback_query:
                await update.callback_query.answer("❌ У вас нет доступа к этому боту.", show_alert=True)
            return
        return await func(update, context, *args, **kwargs)

    return wrapper


def admin_only(func):
    """Decorator to check if the user is an admin."""

    @functools.wraps(func)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE, *args, **kwargs):
        if not update.effective_user:
            return
        user_id = update.effective_user.id
        chat_id = getattr(update.effective_chat, "id", None)
        set_user_context(user_id, chat_id)
        if update.callback_query:
            set_request_id(f"tgcb-{user_id}-{update.callback_query.id}")
        else:
            set_request_id(f"tgcmd-{chat_id}-{getattr(update, 'update_id', 'na')}")
        if not is_admin(user_id):
            logging.warning("Non-admin access attempt by user %s to %s", user_id, func.__name__)
            if update.message:
                await update.message.reply_text("❌ У вас нет прав администратора.")
            elif update.callback_query:
                await update.callback_query.answer("❌ У вас нет прав администратора.", show_alert=True)
            return
        return await func(update, context, *args, **kwargs)

    return wrapper


def safe_handler(error_message: str = "❌ Произошла ошибка. Попробуйте позже."):
    """Decorator that wraps a Telegram command handler with top-level error handling.

    Catches any unhandled exception, logs it, and sends a generic error reply
    to the user. Reduces boilerplate try/except in every handler.

    Usage::

        @authorized_only
        @safe_handler()
        async def my_command(update, context):
            ...  # no try/except needed for the generic case
    """

    def decorator(func):
        @functools.wraps(func)
        async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE, *args, **kwargs):
            try:
                return await func(update, context, *args, **kwargs)
            except Exception as e:
                user_id = getattr(update.effective_user, "id", "?")
                logging.error(
                    "Unhandled error in %s for user %s: %s",
                    func.__name__,
                    user_id,
                    e,
                    exc_info=True,
                )
                try:
                    if update.message:
                        await update.message.reply_text(error_message)
                    elif update.callback_query:
                        await update.callback_query.answer(error_message, show_alert=True)
                except Exception:
                    pass  # Best-effort error notification

        return wrapper

    return decorator


def safe_callback(error_message: str = "❌ Ошибка. Попробуйте ещё раз."):
    """Like safe_handler but tuned for callback query handlers.

    Answers the callback query to dismiss the spinner, then sends error.
    """

    def decorator(func):
        @functools.wraps(func)
        async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE, *args, **kwargs):
            try:
                return await func(update, context, *args, **kwargs)
            except Exception as e:
                user_id = getattr(update.effective_user, "id", "?")
                logging.error(
                    "Unhandled error in %s for user %s: %s",
                    func.__name__,
                    user_id,
                    e,
                    exc_info=True,
                )
                try:
                    if update.callback_query:
                        await update.callback_query.answer(error_message, show_alert=True)
                except Exception:
                    pass

        return wrapper

    return decorator
