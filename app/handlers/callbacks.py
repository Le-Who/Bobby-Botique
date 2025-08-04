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

async def complex_search_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    action = query.data.split(':')[1]
    placeholder_message = query.message

    if action == "cancel":
        await placeholder_message.delete()
        return

    original_message = query.message.reply_to_message
    if not original_message:
        await placeholder_message.edit_text("Не удалось найти оригинальное сообщение.")
        return

    user_id = original_message.from_user.id
    user_lock = state.USER_LOCKS[user_id]

    if user_lock.locked():
        return

    # --- ИСПРАВЛЕНИЕ ЗДЕСЬ ---
    # 1. Определяем, какую задачу будем запускать.
    task_to_run = None
    if action == "vision_only":
        # 2. СРАЗУ даем обратную связь пользователю.
        await placeholder_message.edit_text("🖼️ Описываю изображение...")
        chat_state = await db.get_user_chat(user_id)
        task_to_run = agent._handle_photo(placeholder_message, original_message, chat_state)
    elif action == "confirm":
        # У этой функции своя обратная связь ("Анализирую..."), поэтому здесь ничего не меняем.
        search_prefix = '??' if (original_message.caption and original_message.caption.startswith('??')) else '?'
        task_to_run = agent._handle_complex_agent_search(placeholder_message, original_message, search_prefix)

    # 3. Если задача определена, запускаем ее в фоне под блокировкой.
    if task_to_run:
        async def task_wrapper():
            async with user_lock:
                await task_to_run
        
        asyncio.create_task(task_wrapper())


async def fallback_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    _, action, model_override = query.data.split(':')
    placeholder_message = query.message

    if action == "cancel":
        await placeholder_message.edit_text("Операция отменена.")
        return

    original_message = query.message.reply_to_message
    if not original_message:
        await placeholder_message.edit_text("Не удалось найти оригинальное сообщение.")
        return
    
    user_id = original_message.from_user.id
    user_lock = state.USER_LOCKS[user_id]

    if user_lock.locked():
        return

    async def task_wrapper():
        async with user_lock:
            if action == "confirm":
                chat_state = await db.get_user_chat(user_id)
                user_message = original_message.text
                await agent._handle_regular_chat(placeholder_message, user_id, user_message, chat_state, model_override=model_override)

    asyncio.create_task(task_wrapper())

async def document_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обрабатывает callback-кнопки для управления документами"""
    query = update.callback_query
    await query.answer()
    
    action = query.data.split(':')[1]
    user_id = query.from_user.id
    
    if action == "upload_new":
        await query.edit_message_text(
            "📄 **Загрузите новый документ**\n\n"
            "Отправьте PDF или DOCX файл, и я обработаю его для вас.",
            parse_mode='Markdown'
        )
        return
    
    elif action == "list":
        from ..document_processor import get_user_documents
        
        documents = await get_user_documents(user_id)
        if not documents:
            await query.edit_message_text(
                "📋 **Ваши документы**\n\n"
                "У вас пока нет загруженных документов.",
                parse_mode='Markdown'
            )
            return
        
        # Формируем список документов
        doc_list = "📋 **Ваши документы:**\n\n"
        for i, doc in enumerate(documents[:10], 1):  # Показываем только первые 10
            doc_list += f"{i}. **{doc['filename']}**\n"
            doc_list += f"   📄 Страниц: {doc['pages']}\n"
            doc_list += f"   📅 Загружен: {doc['created_at'][:10]}\n\n"
        
        if len(documents) > 10:
            doc_list += f"... и еще {len(documents) - 10} документов\n\n"
        
        doc_list += "💡 Отправьте новый документ, чтобы начать работу с ним."
        
        await query.edit_message_text(doc_list, parse_mode='Markdown')
        return
    
    elif action == "cancel":
        await query.edit_message_text(
            "✅ **Режим работы с документами отключен**\n\n"
            "Теперь ваши сообщения будут обрабатываться в обычном режиме чата.\n"
            "Чтобы снова работать с документами, загрузите новый файл.",
            parse_mode='Markdown'
        )
        return
    
    elif action == "clear_all":
        from ..document_processor import delete_user_document
        
        # Получаем все документы пользователя
        documents = await get_user_documents(user_id)
        if not documents:
            await query.edit_message_text(
                "📋 **Ваши документы**\n\n"
                "У вас нет документов для удаления.",
                parse_mode='Markdown'
            )
            return
        
        # Удаляем все документы
        deleted_count = 0
        for doc in documents:
            success = await delete_user_document(doc['id'], user_id)
            if success:
                deleted_count += 1
        
        await query.edit_message_text(
            f"🗑️ **Документы удалены**\n\n"
            f"Удалено документов: `{deleted_count}`\n\n"
            "Теперь ваши сообщения будут обрабатываться в обычном режиме чата.",
            parse_mode='Markdown'
        )
        return

def register(application: Application):
    application.add_handler(CallbackQueryHandler(model_button_callback, pattern="^model_"))
    application.add_handler(CallbackQueryHandler(complex_search_callback, pattern="^complex:"))
    application.add_handler(CallbackQueryHandler(fallback_callback, pattern="^fallback:"))
    application.add_handler(CallbackQueryHandler(document_callback, pattern="^doc:"))
