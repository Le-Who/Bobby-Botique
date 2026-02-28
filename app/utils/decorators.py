import functools
import logging

from telegram import Update
from telegram.ext import ContextTypes

from app.repos.users import is_admin, is_authorized


def authorized_only(func):
    """Decorator to check if the user is authorized."""

    @functools.wraps(func)
    async def wrapper(
        update: Update, context: ContextTypes.DEFAULT_TYPE, *args, **kwargs
    ):
        user_id = update.effective_user.id
        if not await is_authorized(user_id):
            logging.warning(
                f"Unauthorized access attempt by user {user_id} to {func.__name__}"
            )
            if update.message:
                await update.message.reply_text("❌ У вас нет доступа к этому боту.")
            elif update.callback_query:
                await update.callback_query.answer(
                    "❌ У вас нет доступа к этому боту.", show_alert=True
                )
            return
        return await func(update, context, *args, **kwargs)

    return wrapper


def admin_only(func):
    """Decorator to check if the user is an admin."""

    @functools.wraps(func)
    async def wrapper(
        update: Update, context: ContextTypes.DEFAULT_TYPE, *args, **kwargs
    ):
        user_id = update.effective_user.id
        if not is_admin(user_id):
            logging.warning(
                f"Non-admin access attempt by user {user_id} to {func.__name__}"
            )
            if update.message:
                await update.message.reply_text("❌ У вас нет прав администратора.")
            elif update.callback_query:
                await update.callback_query.answer(
                    "❌ У вас нет прав администратора.", show_alert=True
                )
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
        async def wrapper(
            update: Update, context: ContextTypes.DEFAULT_TYPE, *args, **kwargs
        ):
            try:
                return await func(update, context, *args, **kwargs)
            except Exception as e:
                user_id = getattr(update.effective_user, "id", "?")
                logging.error(
                    "Unhandled error in %s for user %s: %s",
                    func.__name__, user_id, e, exc_info=True,
                )
                try:
                    if update.message:
                        await update.message.reply_text(error_message)
                    elif update.callback_query:
                        await update.callback_query.answer(
                            error_message, show_alert=True
                        )
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
        async def wrapper(
            update: Update, context: ContextTypes.DEFAULT_TYPE, *args, **kwargs
        ):
            try:
                return await func(update, context, *args, **kwargs)
            except Exception as e:
                user_id = getattr(update.effective_user, "id", "?")
                logging.error(
                    "Unhandled error in %s for user %s: %s",
                    func.__name__, user_id, e, exc_info=True,
                )
                try:
                    if update.callback_query:
                        await update.callback_query.answer(
                            error_message, show_alert=True
                        )
                except Exception:
                    pass
        return wrapper
    return decorator
