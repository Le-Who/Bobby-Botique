# /app/handlers/cb_feedback.py
"""Callback handlers — RLHF feedback via inline buttons.

Two-stage UX to reduce button clutter:
    1. ``feedback:reveal``  — user taps "📝 Оценить" → row expands into 👍/👎
    2. ``feedback:up|down`` — user taps a choice → RLHF actions + confirmation

On downvote: stores negative LTM signal + penalizes recent graph edges.
On upvote: records positive signal (no reaction — avoids conflicting with
the existing 🔍→⚡ status reactions on the user's message).

Also keeps ``_noop_callback`` for decorative confirmed-feedback indicators.
"""

__all__ = ["feedback_callback", "_noop_callback"]

import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Message, Update
from telegram.ext import ContextTypes

from app.repos.users import save_feedback

logger = logging.getLogger(__name__)


async def _noop_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """No-op callback for decorative buttons (e.g. confirmed feedback indicator)."""
    await update.callback_query.answer()


async def feedback_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Route feedback callbacks: ``reveal`` | ``up`` | ``down``."""
    query = update.callback_query
    if not query:
        return

    data = query.data or ""
    action = data.split(":", 1)[1] if ":" in data else ""

    if action == "reveal":
        await _handle_reveal(query)
    elif action in ("up", "down"):
        await _handle_vote(query, action)
    else:
        await query.answer()


# ── Stage 1: Reveal 👍/👎 buttons ────────────────────────────────────────────


async def _handle_reveal(query) -> None:
    """Replace the "📝 Оценить" button with actual 👍/👎 choice pair."""
    await query.answer()

    msg = query.message
    if not isinstance(msg, Message):
        return

    try:
        old_markup = msg.reply_markup
        new_buttons = []
        if old_markup and old_markup.inline_keyboard:
            for row in old_markup.inline_keyboard:
                # Replace the reveal button row with actual 👍/👎 buttons
                if any((getattr(btn, "callback_data", "") or "") == "feedback:reveal" for btn in row):
                    new_buttons.append(
                        [
                            InlineKeyboardButton("👍", callback_data="feedback:up"),
                            InlineKeyboardButton("👎", callback_data="feedback:down"),
                        ]
                    )
                else:
                    new_buttons.append(row)

        await msg.edit_reply_markup(reply_markup=InlineKeyboardMarkup(new_buttons))
    except Exception:
        pass  # Best-effort UI update


# ── Stage 2: Process vote ────────────────────────────────────────────────────


async def _handle_vote(query, rating: str) -> None:
    """Handle 👍/👎 vote: save + RLHF actions + visual confirmation.

    On downvote: LTM negative signal + graph edge penalty (background tasks).
    On upvote: simple DB save (no reaction — status reactions 🔍→⚡ are on
    the user's message and we shouldn't add competing reactions on the bot's
    response message).
    """
    user_id = query.from_user.id

    msg = query.message
    if not isinstance(msg, Message):
        await query.answer()
        return
    message_id = msg.message_id

    # Acknowledge immediately
    emoji = "👍" if rating == "up" else "👎"
    await query.answer(f"{emoji} Отзыв принят!", show_alert=False)

    # ── 1. Save feedback to DB ────────────────────────────────────────────
    try:
        await save_feedback(user_id, message_id, rating)
    except Exception as e:
        logger.warning("Feedback save failed: %s", e)

    # ── 2. RLHF actions on downvote (fire-and-forget) ─────────────────────
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

    # ── 3. Visual confirmation: replace feedback row in keyboard ──────────
    try:
        old_markup = msg.reply_markup
        new_buttons = []
        if old_markup and old_markup.inline_keyboard:
            for row in old_markup.inline_keyboard:
                # Skip the feedback row (contains feedback: callbacks)
                if any((getattr(btn, "callback_data", "") or "").startswith("feedback:") for btn in row):
                    continue
                new_buttons.append(row)

        # Add confirmed feedback indicator as last row (where feedback was)
        confirmed_row = [InlineKeyboardButton(f"{emoji} Отзыв учтён ✓", callback_data="noop")]
        new_buttons.append(confirmed_row)

        await msg.edit_reply_markup(reply_markup=InlineKeyboardMarkup(new_buttons))
    except Exception:
        pass  # Best-effort UI update
