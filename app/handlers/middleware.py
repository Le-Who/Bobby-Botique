import logging
from telegram import Update
from telegram.ext import ContextTypes, ApplicationHandlerStop
from app.security import check_user_rate_limit

async def rate_limit_middleware(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Global rate limit middleware.
    Stops processing if rate limit is exceeded.
    """
    if not update.effective_user:
        return

    user_id = update.effective_user.id

    # Check rate limit
    if not await check_user_rate_limit(user_id):
        logging.warning(f"Rate limit exceeded for user {user_id}")

        # Notify user
        msg = "⏱️ Слишком много запросов. Пожалуйста, подождите."
        try:
            if update.callback_query:
                await update.callback_query.answer(msg, show_alert=True)
            elif update.message:
                await update.message.reply_text(msg)
        except Exception as e:
            logging.warning(f"Failed to send rate limit notification: {e}")

        # Stop further processing
        raise ApplicationHandlerStop
