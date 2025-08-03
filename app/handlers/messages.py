import asyncio
import logging
from telegram import Update
from telegram.ext import ContextTypes

from . import agent
from .. import config
from .. import database as db
from ..database import ACTIVE_USER_TASKS

async def handle_request(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not db.is_authorized(user_id): return
    
    if user_id in ACTIVE_USER_TASKS:
        await update.message.reply_text("Пожалуйста, подождите, я еще обрабатываю ваш предыдущий запрос.")
        return

    if update.message.text:
        chat_state = db.get_user_chat(user_id)
        if chat_state.token_count >= config.CHAT_TOKEN_LIMIT:
            await update.message.reply_text("Достигнут лимит токенов. Начните новый /newchat")
            return
            
    placeholder_message = await update.message.reply_text("⏳ Принято в обработку...")
    task = asyncio.create_task(process_long_request(placeholder_message, update, context))
    ACTIVE_USER_TASKS[user_id] = task

async def process_long_request(placeholder_message, update, context):
    user_id = update.effective_user.id
    try:
        is_photo = bool(update.message.photo)
        text = update.message.text or update.message.caption or ""
        chat_state = db.get_user_chat(user_id)

        if is_photo and (text.startswith('?') or text.startswith('??')):
            keyboard = [
                [InlineKeyboardButton("🖼️ Только описать фото", callback_data="complex:vision_only")],
                [InlineKeyboardButton("🔎 Выполнить сложный поиск", callback_data="complex:confirm")],
                [InlineKeyboardButton("❌ Отмена", callback_data="complex:cancel")]
            ]
            await placeholder_message.edit_text(
                "Обнаружен сложный запрос (изображение + поиск). Это потребует нескольких шагов и потратит больше времени. Что вы хотите сделать?",
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
            return
        
        if is_photo:
            await agent._handle_photo(placeholder_message, update, chat_state)
            return

        if text.startswith('??'):
            await agent._handle_research_agent(placeholder_message, user_id, text[2:].strip(), chat_state)
        elif text.startswith('?'):
            await agent._handle_qna_search(placeholder_message, text[1:].strip(), chat_state)
        elif chat_state.search_enabled:
            await agent._handle_research_agent(placeholder_message, user_id, text, chat_state)
        else:
            await agent._handle_regular_chat(placeholder_message, user_id, text, chat_state)

    except Exception as e:
        logging.error(f"Error in background task dispatcher: {e}", exc_info=True)
        try:
            await placeholder_message.edit_text(f"Произошла критическая ошибка: {e}")
        except Exception as inner_e:
            logging.error(f"Could not edit placeholder message: {inner_e}")
    finally:
        if user_id in ACTIVE_USER_TASKS:
            del ACTIVE_USER_TASKS[user_id]
