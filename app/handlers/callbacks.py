import asyncio
from telegram import Update
from telegram.ext import ContextTypes, CallbackQueryHandler, Application

from . import agent
from .. import database as db
from ..config import settings
from .. import state

async def model_button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    model_name = query.data.split("_", 1)[1]
    chat_state = await db.get_user_chat(query.from_user.id)
    chat_state.model = model_name
    await db.update_user_chat(query.from_user.id, chat_state)
    await query.edit_message_text(f"Основная модель изменена на: {chat_state.model}")

async def _execute_locked_task(query: Update.callback_query, task_coroutine):
    """Helper to run a task with user lock from a callback."""
    user_id = query.from_user.id
    user_lock = state.USER_LOCKS[user_id]
    
    if user_lock.locked():
        await query.answer("Пожалуйста, дождитесь завершения предыдущей операции.", show_alert=True)
        return
        
    async def task_wrapper():
        async with user_lock:
            await task_coroutine
            
    asyncio.create_task(task_wrapper())

async def complex_search_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    action = query.data.split(':')[1]
    placeholder_message = query.message

    # --- ИСПРАВЛЕНИЕ ЗДЕСЬ ---
    # 1. Сначала обрабатываем "Отмену", так как ей не нужно исходное сообщение.
    if action == "cancel":
        await placeholder_message.delete()
        return

    # 2. Теперь, для всех остальных действий, мы знаем, что нам нужно исходное сообщение.
    # Проверяем его наличие.
    original_message = query.message.reply_to_message
    if not original_message:
        await placeholder_message.edit_text("Не удалось найти оригинальное сообщение.")
        return

    # 3. Продолжаем с остальной логикой.
    chat_state = await db.get_user_chat(original_message.from_user.id)
    
    if action == "vision_only":
        task = agent._handle_photo(placeholder_message, original_message, chat_state)
    elif action == "confirm":
        search_prefix = '??' if (original_message.caption and original_message.caption.startswith('??')) else '?'
        task = agent._handle_complex_agent_search(placeholder_message, original_message, search_prefix)
    else:
        return

    await _execute_locked_task(query, task)

async def fallback_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    _, action, model_override = query.data.split(':')
    placeholder_message = query.message

    # Применяем ту же логику исправления, что и выше.
    if action == "cancel":
        await placeholder_message.edit_text("Операция отменена.")
        return

    original_message = query.message.reply_to_message
    if not original_message:
        await placeholder_message.edit_text("Не удалось найти оригинальное сообщение.")
        return
    
    if action == "confirm":
        user_id = original_message.from_user.id
        chat_state = await db.get_user_chat(user_id)
        user_message = original_message.text
        task = agent._handle_regular_chat(placeholder_message, user_id, user_message, chat_state, model_override=model_override)
        await _execute_locked_task(query, task)

def register(application: Application):
    application.add_handler(CallbackQueryHandler(model_button_callback, pattern="^model_"))
    application.add_handler(CallbackQueryHandler(complex_search_callback, pattern="^complex:"))
    application.add_handler(CallbackQueryHandler(fallback_callback, pattern="^fallback:"))
