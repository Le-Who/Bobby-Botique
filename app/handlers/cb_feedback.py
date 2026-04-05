# /app/handlers/cb_feedback.py
"""Callback handlers — RLHF feedback via inline buttons.

Handles 👍/👎 feedback buttons (``feedback:up`` / ``feedback:down``).
On downvote: stores negative LTM signal + penalizes recent graph edges.
On upvote: sets ❤️ reaction as acknowledgment (1 reaction = safe for Bot API).

Also keeps ``_noop_callback`` for decorative confirmed-feedback indicators.
"""

__all__ = ["feedback_callback", "_noop_callback"]

import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from app.repos.users import save_feedback

logger = logging.getLogger(__name__)


async def _noop_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """No-op callback for decorative buttons (e.g. confirmed feedback indicator)."""
    await update.callback_query.answer()


async def feedback_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle 👍/👎 inline button feedback on AI responses.

    Flow:
        1. Save rating to feedback DB table
        2. On downvote: store LTM negative signal + penalize graph edges (RLHF)
        3. On upvote: set ❤️ reaction on the message (1 reaction = safe)
        4. Replace feedback row with "✅ Отзыв учтён" confirmation label
    """
    query = update.callback_query
    if not query:
        return

    user_id = query.from_user.id
    data = query.data or ""
    rating = data.split(":", 1)[1] if ":" in data else "up"

    from telegram import Message

    msg = query.message
    if not isinstance(msg, Message):
        return
    message_id = msg.message_id
    chat_id = msg.chat_id

    # Acknowledge the tap immediately (stop loading animation)
    emoji = "👍" if rating == "up" else "👎"
    await query.answer(f"{emoji} Отзыв принят!", show_alert=False)

    # ── 1. Save feedback to DB ────────────────────────────────────────────
    try:
        await save_feedback(user_id, message_id, rating)
    except Exception as e:
        logger.warning("Feedback save failed: %s", e)

    # ── 2. RLHF actions (fire-and-forget background tasks) ────────────────
    if rating == "down":
        from app.utils.background_tasks import submit_task

        # 2a. Store negative signal in LTM
        async def _store_negative_signal():
            try:
                from app.repos.keys import get_available_gemini_key
                from app.repos.memory import EMBEDDING_MODEL, store_memory

                key_data = await get_available_gemini_key(model_name=EMBEDDING_MODEL)
                if not key_data:
                    return
                await store_memory(
                    user_id,
                    f"[FEEDBACK] Пользователю не понравился ответ (msg_id={message_id}). "
                    "Учитывай это при формировании будущих ответов.",
                    key_data["api_key"],
                    source_type="negative_feedback",
                )
            except Exception as mem_err:
                logger.debug("LTM negative signal failed: %s", mem_err)

        submit_task(_store_negative_signal())

        # 2b. Penalize graph edges used for this response
        async def _penalize_edges():
            try:
                from app.repos.memory import penalize_graph_edges

                count = await penalize_graph_edges(user_id, penalty=0.10)
                if count > 0:
                    logger.info(
                        "RLHF: user %d downvote penalized %d graph edges",
                        user_id,
                        count,
                    )
            except Exception as pen_err:
                logger.debug("RLHF edge penalization skipped: %s", pen_err)

        submit_task(_penalize_edges())

    elif rating == "up":
        # Set ❤️ reaction as acknowledgment (safe: Bot API allows 1 reaction)
        try:
            from app.utils.ux_improvements import set_message_reaction

            await set_message_reaction(context.bot, chat_id, message_id, "❤️")
        except Exception:
            pass

    # ── 3. Visual confirmation: replace feedback row in keyboard ──────────
    try:
        old_markup = query.message.reply_markup
        new_buttons = []
        if old_markup and old_markup.inline_keyboard:
            for row in old_markup.inline_keyboard:
                # Skip the original feedback row (contains feedback: callbacks)
                if any((getattr(btn, "callback_data", "") or "").startswith("feedback:") for btn in row):
                    continue
                new_buttons.append(row)

        # Add confirmed feedback indicator as first row
        confirmed_row = [InlineKeyboardButton(f"{emoji} Отзыв учтён ✓", callback_data="noop")]
        new_buttons.insert(0, confirmed_row)

        await query.message.edit_reply_markup(reply_markup=InlineKeyboardMarkup(new_buttons))
    except Exception:
        pass  # Best-effort UI update
