"""
Callback handlers — feedback.

Handles 👍/👎 feedback buttons and noop decorative callbacks.
"""

import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from app.repos.users import save_feedback


async def _noop_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """No-op callback for decorative buttons (e.g. confirmed feedback indicator)."""
    await update.callback_query.answer()


async def feedback_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle 👍/👎 feedback on AI responses."""
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    data = query.data or ""
    rating = data.split(":", 1)[1] if ":" in data else "up"
    message_id = query.message.message_id if query.message else 0

    # Save to DB
    try:
        await save_feedback(user_id, message_id, rating)
    except Exception as e:
        logging.warning("Feedback save failed: %s", e)

    # Visual confirmation: replace the feedback row with a "thanks" indicator
    emoji = "👍" if rating == "up" else "👎"
    try:
        # Preserve existing keyboard but replace feedback row
        old_markup = query.message.reply_markup
        new_buttons = []
        if old_markup and old_markup.inline_keyboard:
            for row in old_markup.inline_keyboard:
                # Skip the original feedback row (contains feedback: callbacks)
                if any((getattr(btn, "callback_data", "") or "").startswith("feedback:") for btn in row):
                    continue
                new_buttons.append(row)

        # Add confirmed feedback indicator
        confirmed_row = [InlineKeyboardButton(f"{emoji} Спасибо! Отзыв учтён", callback_data="noop")]
        new_buttons.insert(0, confirmed_row)

        await query.message.edit_reply_markup(reply_markup=InlineKeyboardMarkup(new_buttons))
    except Exception:
        pass  # Best-effort UI update
