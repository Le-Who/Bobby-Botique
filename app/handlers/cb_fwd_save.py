# /app/handlers/cb_fwd_save.py
"""Callback handler for the "💾 Сохранить тезисы в память" button.

This button appears after the AI analyzes a forwarded-message batch
(Improvement 7).  When pressed, it extracts the last model response from
chat history, embeds it as a long-term memory entry, and confirms with an
inline toast so the user gets instant feedback without navigating away.
"""

import logging

from telegram import Update
from telegram.ext import ContextTypes

from app.repos.chats import get_user_chat


async def fwd_save_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Save the last AI response as a long-term memory entry."""
    query = update.callback_query
    if query is None:
        return
    # A Telegram callback query can only be answered once.  Acknowledge it
    # immediately so the spinner stops, then report the eventual result as a
    # normal message (embedding may take longer than the callback deadline).
    await query.answer("⏳ Сохраняю…")

    user_id = update.effective_user.id

    try:
        chat_state = await get_user_chat(user_id)
        from app.repos.memory_consent import capture_epoch

        memory_epoch = capture_epoch(chat_state)
        if memory_epoch is None:
            await _report_result(query, "❌ Долгосрочная память выключена в настройках")
            return

        # Pull the last model turn from history
        ai_response: str | None = None
        for entry in reversed(chat_state.history):
            if entry.get("role") == "model":
                parts = entry.get("parts", [])
                ai_response = parts[0] if parts else None
                break

        if not ai_response or len(ai_response) < 30:
            await _report_result(query, "❌ Ответ слишком короткий для сохранения")
            return

        # Store in long-term memory (same pipeline as _store_memory_in_background)
        from app.repos.keys import get_available_gemini_key
        from app.repos.memory import EMBEDDING_MODEL, store_memory

        key_data = await get_available_gemini_key(model_name=EMBEDDING_MODEL)
        if not key_data:
            await _report_result(query, "❌ Нет доступного ключа для сохранения")
            return

        snippet = ai_response[:800] if ai_response else ""
        memory_id = await store_memory(
            user_id,
            snippet,
            key_data["api_key"],
            source_type="forward_analysis",
            expected_epoch=memory_epoch,
        )
        if memory_id is None:
            await _report_result(query, "❌ Память выключена или запись устарела")
            return
        logging.info("fwd_save: stored %d chars for user %d", len(snippet), user_id)

        # Update button label immediately so the user sees confirmation
        try:
            current_markup = query.message.reply_markup
            if current_markup:
                # Replace the fwd_save button with a ✅ confirmation label
                new_rows = []
                for row in current_markup.inline_keyboard:
                    new_row = []
                    for btn in row:
                        if btn.callback_data == "fwd_save":
                            from telegram import InlineKeyboardButton

                            new_row.append(InlineKeyboardButton("✅ Тезисы сохранены в память", callback_data="noop"))
                        else:
                            new_row.append(btn)
                    new_rows.append(new_row)
                from telegram import InlineKeyboardMarkup

                await query.message.edit_reply_markup(reply_markup=InlineKeyboardMarkup(new_rows))
        except Exception as markup_err:
            logging.debug("fwd_save: could not update button label: %s", markup_err)

        await _report_result(query, "✅ Тезисы сохранены в долгосрочную память")

    except Exception as e:
        logging.error("fwd_save_callback error for user %d: %s", user_id, e, exc_info=True)
        await _report_result(query, "❌ Не удалось сохранить — попробуйте позже")


async def _report_result(query, text: str) -> None:
    """Report a completed save without trying to answer the callback twice."""
    try:
        await query.message.reply_text(text)
    except Exception as exc:
        logging.debug("fwd_save: could not report result: %s", exc)
