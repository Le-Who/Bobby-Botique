import asyncio
from telegram import Update
from telegram.ext import ContextTypes, CallbackQueryHandler, Application

from . import agent
from .. import database as db
from .. import config
from .. import state

async def model_button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    model_name = query.data.split("_", 1)[1]
    chat_state = await db.get_user_chat(query.from_user.id)
    chat_state.model = model_name
    await db.update_user_chat(query.from_user.id, chat_state)
    await query.edit_message_text(f"Основная модель изменена на: {chat_state.model}")

async def complex_search_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    action = query.data.split(':')[1]
    placeholder_message = query.message
    original_message = query.message.reply_to_message

    if not original_message:
        await placeholder_message.edit_text("Не удалось найти оригинальное сообщение. Пожалуйста, попробуйте снова.")
        return

    user_id = original_message.from_user.id
    if user_id in state.ACTIVE_USER_TASKS:
        await context.bot.answer_callback_query(query.id, "Пожалуйста, дождитесь завершения предыдущей операции.", show_alert=True)
        return

    if action == "cancel":
        await placeholder_message.delete()
        return

    chat_state = await db.get_user_chat(user_id)
    if action == "vision_only":
        task = asyncio.create_task(agent._handle_photo(placeholder_message, original_message, chat_state))
        state.ACTIVE_USER_TASKS[user_id] = task
    
    elif action == "confirm":
        search_prefix = '??' if (original_message.caption and original_message.caption.startswith('??')) else '?'
        task = asyncio.create_task(agent._handle_complex_agent_search(placeholder_message, original_message, search_prefix))
        state.ACTIVE_USER_TASKS[user_id] = task

async def fallback_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    _, action, model_override = query.data.split(':')
    placeholder_message = query.message
    original_message = query.message.reply_to_message

    if not original_message:
        await placeholder_message.edit_text("Не удалось найти оригинальное сообщение. Пожалуйста, попробуйте снова.")
        return
        
    user_id = original_message.from_user.id
    if user_id in state.ACTIVE_USER_TASKS:
        await context.bot.answer_callback_query(query.id, "Пожалуйста, дождитесь завершения предыдущей операции.", show_alert=True)
        return

    if action == "cancel":
        await placeholder_message.edit_text("Операция отменена.")
        return
    
    if action == "confirm":
        chat_state = await db.get_user_chat(user_id)
        user_message = original_message.text
        task = asyncio.create_task(agent._handle_regular_chat(placeholder_message, user_id, user_message, chat_state, model_override=model_override))
        state.ACTIVE_USER_TASKS[user_id] = task

def register(application: Application):
    application.add_handler(CallbackQueryHandler(model_button_callback, pattern="^model_"))
    application.add_handler(CallbackQueryHandler(complex_search_callback, pattern="^complex:"))
    application.add_handler(CallbackQueryHandler(fallback_callback, pattern="^fallback:"))
