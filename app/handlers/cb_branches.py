"""Callback handlers for conversation branching.

Handles the inline button flow:
  🔀 Что если… → snapshot current history, answer toast
  ↩️ К основной ветке → restore snapshot, answer toast + delete branch
"""

import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from app.repos.branches import create_branch, delete_branch, restore_branch
from app.repos.chats import get_user_chat, update_user_chat

logger = logging.getLogger(__name__)

__all__ = ["branch_create_callback", "branch_return_callback"]


async def branch_create_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """🔀 Что если… — fork current history into a branch."""
    query = update.callback_query
    await query.answer()

    user_id = update.effective_user.id
    chat_state = await get_user_chat(user_id)
    if not chat_state:
        await query.answer("⚠️ Чат не найден", show_alert=True)
        return

    if chat_state.branch_id:
        await query.answer("⚠️ Вы уже в ветке. Вернитесь в основную ветку сначала.", show_alert=True)
        return

    # Snapshot current history before the branch point
    branch_id = await create_branch(user_id, chat_state.history, label="whatif")
    if not branch_id:
        await query.answer("❌ Не удалось создать ветку", show_alert=True)
        return

    # Mark chat state as being in a branch
    chat_state.branch_id = branch_id
    await update_user_chat(user_id, chat_state)

    # Notify user with return button
    keyboard = InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("↩️ К основной ветке", callback_data="branch_return")],
        ]
    )
    await query.message.reply_text(
        "🔀 <b>Ветка создана!</b>\n\n"
        "Теперь вы можете задавать вопросы «что если» без влияния на основной разговор.\n"
        "Нажмите ↩️ чтобы вернуться к основной ветке.",
        parse_mode="HTML",
        reply_markup=keyboard,
    )


async def branch_return_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """↩️ К основной ветке — restore original history from snapshot."""
    query = update.callback_query
    await query.answer()

    user_id = update.effective_user.id
    chat_state = await get_user_chat(user_id)
    if not chat_state:
        await query.answer("⚠️ Чат не найден", show_alert=True)
        return

    branch_id = chat_state.branch_id
    if not branch_id:
        await query.answer("ℹ️ Вы уже в основной ветке", show_alert=True)
        return

    # Restore snapshotted history
    original_history = await restore_branch(user_id, branch_id)
    if original_history is None:
        await query.answer("❌ Не удалось восстановить ветку", show_alert=True)
        return

    chat_state.history = original_history
    chat_state.branch_id = None
    await update_user_chat(user_id, chat_state)

    # Clean up the branch row
    await delete_branch(branch_id, user_id)

    await query.message.reply_text(
        "↩️ <b>Вернулись в основную ветку!</b>\n\nИстория разговора восстановлена.",
        parse_mode="HTML",
    )
