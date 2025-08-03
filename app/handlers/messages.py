import asyncio
import logging
from telegram import Update
from telegram.ext import ContextTypes

from . import agent
from .. import config
from .. import database as db
from bot import ACTIVE_USER_TASKS # <-- ИЗМЕНЕННЫЙ ИМПОРТ

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
    task = asyncio.create_task(agent.process_long_request(placeholder_message, update, context))
    ACTIVE_USER_TASKS[user_id] = task
