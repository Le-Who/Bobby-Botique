import functools
import logging
from telegram import Update
from telegram.ext import ContextTypes
from app import database as db

def authorized_only(func):
    """Decorator to check if the user is authorized."""
    @functools.wraps(func)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE, *args, **kwargs):
        user_id = update.effective_user.id
        if not await db.is_authorized(user_id):
            logging.warning(f"Unauthorized access attempt by user {user_id} to {func.__name__}")
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
        user_id = update.effective_user.id
        if not db.is_admin(user_id):
            logging.warning(f"Non-admin access attempt by user {user_id} to {func.__name__}")
            if update.message:
                await update.message.reply_text("❌ У вас нет прав администратора.")
            elif update.callback_query:
                await update.callback_query.answer("❌ У вас нет прав администратора.", show_alert=True)
            return
        return await func(update, context, *args, **kwargs)
    return wrapper
