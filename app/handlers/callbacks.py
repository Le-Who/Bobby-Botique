import asyncio
import logging
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes, CallbackQueryHandler, Application
from app import prompts

from app.handlers import agent
from app import database as db
from app.config import settings, get_model_hash, get_openrouter_keys
from app import state
from app.utils.formatting import TelegramFormatter
from app.state import begin_custom_role_creation
from app.state import get_generated_role, clear_custom_role_state
from app import prompts
from app.metrics import role_conv_metrics
from app.state import get_last_custom_role_prompt, set_generating_custom_role, set_last_custom_role_prompt
from app.errors import build_roles_keyboard

class DummyUpdate:
    """Helper class to mock an Update object for calling commands from callbacks."""
    def __init__(self, msg, user):
        self.message = msg
        self.effective_user = user

async def model_button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    # Игнорируем клики на разделитель
    if query.data == "model_none":
        return
    
    user_id = query.from_user.id
    
    # Получаем модель из индекса (новый формат с хэшем) или из полного имени (старый формат для совместимости)
    if query.data.startswith("model:"):
        # Новый формат: model:index:hash или model:index (старый формат без хэша)
        try:
            parts = query.data.split(":")
            model_index = int(parts[1])
            expected_hash = parts[2] if len(parts) > 2 else None
            
            # Получаем актуальный список моделей из настроек
            all_models = []
            if settings.AVAILABLE_MODELS:
                all_models.extend(settings.AVAILABLE_MODELS)
            openrouter_available = bool(get_openrouter_keys())
            if openrouter_available and settings.OPENROUTER_AVAILABLE_MODELS:
                all_models.extend(settings.OPENROUTER_AVAILABLE_MODELS)
            
            if 0 <= model_index < len(all_models):
                model_name = all_models[model_index]
                
                # Если есть хэш, проверяем валидность
                if expected_hash:
                    actual_hash = get_model_hash(model_name)
                    if actual_hash != expected_hash:
                        # Модель изменилась (удалена/добавлена), просим выбрать заново
                        await query.edit_message_text(
                            "⚠️ Список моделей обновился. Пожалуйста, выберите модель заново через /model"
                        )
                        return
            else:
                await query.edit_message_text("❌ Ошибка: неверный индекс модели.")
                return
        except (ValueError, IndexError) as e:
            await query.edit_message_text("❌ Ошибка: неверный формат callback_data.")
            logging.error(f"Error parsing model callback: {e}, data: {query.data}")
            return
    else:
        # Старый формат для совместимости: model_gemini-2.5-pro
        model_name = query.data.split("_", 1)[1] if "_" in query.data else None
        if not model_name:
            await query.edit_message_text("❌ Ошибка: неверный формат callback_data.")
            return
    chat_state = await db.get_user_chat(user_id)
    chat_state.model = model_name
    await db.update_user_chat(user_id, chat_state)
    
    # Определяем провайдер
    is_openrouter = "/" in model_name
    provider_name = "OpenRouter" if is_openrouter else "Google Gemini"
    display_name = model_name.split("/")[-1] if is_openrouter else model_name
    
    text = f"✅ *Модель изменена*\n\n"
    text += f"*Модель:* `{display_name}`\n"
    text += f"*Провайдер:* {provider_name}\n"
    text += f"*Полное имя:* `{model_name}`"
    
    formatted_text, parse_mode = TelegramFormatter.format_text(text)
    await query.edit_message_text(formatted_text, parse_mode=parse_mode)

async def complex_search_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    action = query.data.split(':')[1]
    placeholder_message = query.message

    if action == "cancel":
        await placeholder_message.delete()
        return

    # Получаем оригинальное сообщение из контекста или из reply_to_message
    original_message = None
    if hasattr(context, 'user_data') and 'original_message' in context.user_data:
        original_message = context.user_data['original_message']
    else:
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

    # Получаем оригинальное сообщение из контекста или из reply_to_message
    original_message = None
    if hasattr(context, 'user_data') and 'original_message' in context.user_data:
        original_message = context.user_data['original_message']
    else:
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
    
    # Импортируем функции здесь, чтобы избежать проблем с областью видимости
    from ..document_processor import get_user_documents, delete_user_document, get_document_by_id

    if action == "upload_new":
        keyboard = [
            [InlineKeyboardButton("❌ Отмена", callback_data="doc:cancel")]
        ]
        
        text = "📄 *Загрузите новый документ*\n\nОтправьте PDF или DOCX файл, и я обработаю его для вас."
        formatted_text, parse_mode = TelegramFormatter.format_text(text)
        await query.edit_message_text(
            formatted_text,
            parse_mode=parse_mode,
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        return
    
    elif action == "list":
        
        documents = await get_user_documents(user_id)
        if not documents:
            text = "📋 *Ваши документы*\n\nУ вас пока нет загруженных документов."
            formatted_text, parse_mode = TelegramFormatter.format_text(text)
            await query.edit_message_text(
                formatted_text,
                parse_mode=parse_mode
            )
            return
        
        # Формируем список документов
        doc_list = "📋 *Ваши документы:*\n\n"
        for i, doc in enumerate(documents[:10], 1):  # Показываем только первые 10
            doc_list += f"{i}. *{doc['filename']}*\n"
            doc_list += f"   📄 Страниц: {doc['pages']}\n"
            doc_list += f"   📅 Загружен: {doc['created_at'][:10]}\n\n"
        
        if len(documents) > 10:
            doc_list += f"... и еще {len(documents) - 10} документов\n\n"
        
        doc_list += "💡 Отправьте новый документ, чтобы начать работу с ним."
        
        # Создаем кнопки для управления
        keyboard = [
            [InlineKeyboardButton("📄 Загрузить новый документ", callback_data="doc:upload_new")],
            [InlineKeyboardButton("📋 Выбрать документ", callback_data="doc:select_document")],
            [InlineKeyboardButton("🗑️ Очистить все документы", callback_data="doc:clear_all")]
        ]
        
        formatted_text, parse_mode = TelegramFormatter.format_text(doc_list)
        await query.edit_message_text(formatted_text, parse_mode=parse_mode, reply_markup=InlineKeyboardMarkup(keyboard))
        return
    
    elif action == "cancel":
        from ..state import clear_document_state
        clear_document_state(user_id)
        
        text = "✅ *Режим работы с документами отключен*\n\nТеперь ваши сообщения будут обрабатываться в обычном режиме чата.\nЧтобы снова работать с документами, загрузите новый файл или используйте команду /documents."
        formatted_text, parse_mode = TelegramFormatter.format_text(text)
        await query.edit_message_text(
            formatted_text,
            parse_mode=parse_mode
        )
        return
    
    elif action == "clear_all":
        
        # Получаем все документы пользователя
        documents = await get_user_documents(user_id)
        if not documents:
            text = "📋 *Ваши документы*\n\nУ вас нет документов для удаления."
            formatted_text, parse_mode = TelegramFormatter.format_text(text)
            await query.edit_message_text(
                formatted_text,
                parse_mode=parse_mode
            )
            return
        
        # Удаляем все документы
        deleted_count = 0
        for doc in documents:
            success = await delete_user_document(doc['id'], user_id)
            if success:
                deleted_count += 1
        
        # Очищаем состояние работы с документами
        from ..state import clear_document_state
        clear_document_state(user_id)
        
        text = f"🗑️ *Документы удалены*\n\nУдалено документов: `{deleted_count}`\n\nТеперь ваши сообщения будут обрабатываться в обычном режиме чата."
        formatted_text, parse_mode = TelegramFormatter.format_text(text)
        await query.edit_message_text(
            formatted_text,
            parse_mode=parse_mode
        )
        return
    
    elif action == "use_existing":
        # Используем существующий документ
        document_id = int(query.data.split(':')[2])
        
        document = await get_document_by_id(document_id, user_id)
        if not document:
            await query.edit_message_text("❌ Документ не найден.")
            return
        
        # Устанавливаем состояние работы с документами
        from ..state import set_document_mode
        set_document_mode(user_id, True, document_id)
        
        text = f"✅ *Используется существующий документ*\n\n📄 *{document['filename']}*\n📊 Страниц: {document['pages']}\n📅 Загружен: {document['created_at'][:10]}\n\nТеперь вы можете задавать вопросы по этому документу.\n\n💡 *Просто напишите ваш вопрос* - система автоматически найдет ответ в документе.\n\n🔄 *Для выхода из режима документов:*\n• Нажмите кнопку '❌ Отмена' ниже\n• Или отправьте команду /documents"
        formatted_text, parse_mode = TelegramFormatter.format_text(text)
        await query.edit_message_text(
            formatted_text,
            parse_mode=parse_mode,
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("❌ Отмена", callback_data="doc:cancel")]
            ])
        )
        return
    
    elif action == "force_upload":
        text = "📄 *Загрузите файл как новый документ*\n\nОтправьте файл еще раз, и он будет сохранен как новый документ."
        formatted_text, parse_mode = TelegramFormatter.format_text(text)
        await query.edit_message_text(
            formatted_text,
            parse_mode=parse_mode,
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("⬅️ Назад", callback_data="doc:list")],
                [InlineKeyboardButton("❌ Отмена", callback_data="doc:cancel")]
            ])
        )
        return
    
    elif action == "select_document":
        # Показываем меню выбора документа
        documents = await get_user_documents(user_id)
        if not documents:
            text = "📋 *Ваши документы*\n\nУ вас пока нет загруженных документов."
            formatted_text, parse_mode = TelegramFormatter.format_text(text)
            await query.edit_message_text(
                formatted_text,
                parse_mode=parse_mode
            )
            return
        
        # Создаем кнопки для каждого документа
        keyboard = []
        for doc in documents[:10]:  # Максимум 10 документов
            keyboard.append([
                InlineKeyboardButton(
                    f"📄 {doc['filename'][:30]}...", 
                    callback_data=f"doc:select:{doc['id']}"
                ),
                InlineKeyboardButton(
                    "🗑️", 
                    callback_data=f"doc:delete_document:{doc['id']}"
                )
            ])
        
        keyboard.append([InlineKeyboardButton("❌ Отмена", callback_data="doc:cancel")])
        
        text = "📋 *Выберите документ для работы:*\n\nНажмите на документ, чтобы начать работу с ним."
        formatted_text, parse_mode = TelegramFormatter.format_text(text)
        await query.edit_message_text(
            formatted_text,
            parse_mode=parse_mode,
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        return
    
    elif action == "select":
        # Выбираем конкретный документ
        document_id = int(query.data.split(':')[2])
        
        document = await get_document_by_id(document_id, user_id)
        if not document:
            await query.edit_message_text("❌ Документ не найден.")
            return
        
        # Устанавливаем состояние работы с документами
        from ..state import set_document_mode
        set_document_mode(user_id, True, document_id)
        
        text = f"✅ *Выбран документ*\n\n📄 *{document['filename']}*\n📊 Страниц: {document['pages']}\n📅 Загружен: {document['created_at'][:10]}\n\nТеперь вы можете задавать вопросы по этому документу.\n\n💡 *Просто напишите ваш вопрос* - система автоматически найдет ответ в документе.\n\n🔄 *Для выхода из режима документов:*\n• Нажмите кнопку '❌ Отмена' ниже\n• Или отправьте команду /documents"
        formatted_text, parse_mode = TelegramFormatter.format_text(text)
        await query.edit_message_text(
            formatted_text,
            parse_mode=parse_mode,
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("⬅️ Назад к списку", callback_data="doc:select_document")],
                [InlineKeyboardButton("❌ Отмена", callback_data="doc:cancel")]
            ])
        )
        return
    
    elif action == "delete_document":
        # Удаляем конкретный документ
        document_id = int(query.data.split(':')[2])
        
        document = await get_document_by_id(document_id, user_id)
        if not document:
            await query.edit_message_text("❌ Документ не найден.")
            return
        
        success = await delete_user_document(document_id, user_id)
        if success:
            # Проверяем, был ли это выбранный документ
            from ..state import get_selected_document_id, clear_document_state
            selected_doc_id = get_selected_document_id(user_id)
            if selected_doc_id == document_id:
                # Если удалили выбранный документ, очищаем состояние
                clear_document_state(user_id)
            
            text = f"🗑️ *Документ удален*\n\nДокумент `{document['filename']}` был успешно удален."
            formatted_text, parse_mode = TelegramFormatter.format_text(text)
            await query.edit_message_text(
                formatted_text,
                parse_mode=parse_mode
            )
        else:
            await query.edit_message_text("❌ Ошибка при удалении документа.")
        return

async def deep_dive_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
   """Handles callbacks from deep dive mode buttons."""
   query = update.callback_query
   await query.answer()

   action = query.data.split(':')[1]
   user_id = query.from_user.id

   if action == "new_topic":
       chat_state = await db.get_user_chat(user_id)
       chat_state.history = []
       chat_state.token_count = 0
       chat_state.system_prompt = None
       chat_state.is_deep_dive = False
       await db.update_user_chat(user_id, chat_state)
       await query.message.reply_text("✅ Новый чат создан. История и системная инструкция сброшены.")
       await query.edit_message_reply_markup(reply_markup=None)

   elif action == "deeper_dive":
       await query.edit_message_reply_markup(reply_markup=None)
       text = "Супер! Мы готовы *копнуть глубже*! 😉 \nЧто еще вы хотели бы узнать по этой теме?"
       formatted_text, parse_mode = TelegramFormatter.format_text(text)
       await query.message.reply_text(
           formatted_text,
           parse_mode=parse_mode
       )

async def new_topic_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles the 'new_topic' button press, clearing the chat context."""
    query = update.callback_query
    await query.answer("Начинаем новую тему...")
    
    user_id = query.from_user.id
    
    # Clear chat history and system prompt, similar to /newchat command
    chat_state = await db.get_user_chat(user_id)
    chat_state.history = []
    chat_state.token_count = 0
    chat_state.system_prompt = None
    await db.update_user_chat(user_id, chat_state)
    
    # Remove the old inline keyboard
    await query.edit_message_reply_markup(reply_markup=None)
    
    # Send confirmation message
    await query.message.reply_text("✅ Новый чат создан. История и системная инструкция сброшены.")

async def retry_last_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Повтор последнего пользовательского запроса по кнопке."""
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    chat_state = await db.get_user_chat(user_id)
    last_text = None
    try:
        from app.state import get_user_state
        last_text = get_user_state(user_id).last_sent_message_text
    except Exception:
        last_text = None
    if not last_text:
        await query.edit_message_text("Нет запроса для повтора.")
        return
    # Создаём плейсхолдер и запускаем обычную обработку как при новом сообщении
    placeholder_message = await query.message.reply_text("🔁 Повторяю предыдущий запрос…")
    from app.handlers.agent import _handle_regular_chat
    await _handle_regular_chat(placeholder_message, user_id, last_text, chat_state)

async def role_rename_menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    if not await db.is_authorized(user_id):
        return
    roles = await db.db_query("SELECT id, title FROM user_roles WHERE user_id = $1 ORDER BY created_at DESC", (user_id,))
    if not roles:
        await query.edit_message_text("У вас пока нет кастомных ролей.")
        return
    buttons = []
    for r in roles:
        buttons.append([InlineKeyboardButton(f"✏️ {r['title']}", callback_data=f"role_rename_pick:{r['id']}")])
    await query.message.reply_text("Выберите роль для переименования:", reply_markup=InlineKeyboardMarkup(buttons))

async def role_rename_pick_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    if not await db.is_authorized(user_id):
        return
    role_id = int(query.data.split(":")[1])
    context.user_data["rename_role_id"] = role_id
    await query.message.reply_text("Введите новое название роли одной строкой:")

async def new_chat_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    from app.handlers.commands import new_chat_command
    await new_chat_command(DummyUpdate(query.message, query.from_user), context)

async def model_menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    from app.handlers.commands import model_command
    await model_command(DummyUpdate(query.message, query.from_user), context)

async def help_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    from app.handlers.commands import help_command
    await help_command(DummyUpdate(query.message, query.from_user), context)

def register(application: Application):
   # Новые кнопки меню
   application.add_handler(CallbackQueryHandler(new_chat_callback, pattern="^new_chat$"))
   application.add_handler(CallbackQueryHandler(model_menu_callback, pattern="^model_menu$"))
   application.add_handler(CallbackQueryHandler(help_callback, pattern="^help$"))

   # Обрабатываем оба формата: model:0 (новый) и model_none (разделитель)
   application.add_handler(CallbackQueryHandler(model_button_callback, pattern="^model"))
   application.add_handler(CallbackQueryHandler(complex_search_callback, pattern="^complex:"))
   application.add_handler(CallbackQueryHandler(fallback_callback, pattern="^fallback:"))
   application.add_handler(CallbackQueryHandler(document_callback, pattern="^doc:"))
   application.add_handler(CallbackQueryHandler(deep_dive_callback, pattern="^deepdive:"))
   application.add_handler(CallbackQueryHandler(new_topic_callback, pattern="^new_topic"))
   application.add_handler(CallbackQueryHandler(retry_last_callback, pattern="^retry_last$"))
   # Роль: apply/clear/create
   application.add_handler(CallbackQueryHandler(role_apply_callback, pattern="^role_apply:"))
   application.add_handler(CallbackQueryHandler(role_clear_callback, pattern="^role_clear$"))
   application.add_handler(CallbackQueryHandler(role_create_callback, pattern="^role_create$"))
   application.add_handler(CallbackQueryHandler(role_custom_apply_callback, pattern="^role_custom_apply$"))
   application.add_handler(CallbackQueryHandler(role_custom_save_callback, pattern="^role_custom_save$"))
   application.add_handler(CallbackQueryHandler(role_custom_retry_callback, pattern="^role_custom_retry$"))
   application.add_handler(CallbackQueryHandler(open_roles_callback, pattern="^open_roles$"))
   application.add_handler(CallbackQueryHandler(role_delete_callback, pattern="^role_delete:"))
   application.add_handler(CallbackQueryHandler(role_rename_menu_callback, pattern="^role_rename_menu$"))
   application.add_handler(CallbackQueryHandler(role_rename_pick_callback, pattern="^role_rename_pick:"))
    
   # Conversation management callbacks
   application.add_handler(CallbackQueryHandler(conv_page_callback, pattern="^conv_page:"))
   application.add_handler(CallbackQueryHandler(conv_switch_callback, pattern="^conv_switch$"))
   application.add_handler(CallbackQueryHandler(conv_switch_to_callback, pattern="^conv_switch_to:"))
   application.add_handler(CallbackQueryHandler(conv_rename_callback, pattern="^conv_rename$"))
   application.add_handler(CallbackQueryHandler(conv_delete_callback, pattern="^conv_delete$"))
   application.add_handler(CallbackQueryHandler(conv_delete_confirm_callback, pattern="^conv_delete_confirm:"))
   application.add_handler(CallbackQueryHandler(conv_delete_cancel_callback, pattern="^conv_delete_cancel$"))

async def role_apply_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
   query = update.callback_query
   await query.answer()
   user_id = query.from_user.id
   chat_state = await db.get_user_chat(user_id)
   key = query.data.split(":", 1)[1]
   
   # Проверяем, это кастомная роль пользователя
   if key.startswith("user_role:"):
       role_id = int(key.split(":")[1])
       role_data = await db.db_query("SELECT title, prompt FROM user_roles WHERE id = $1 AND user_id = $2", (role_id, user_id))
       if not role_data:
           await query.edit_message_text("❌ Кастомная роль не найдена.")
           return
       role = role_data[0]
       # Проверяем, что роль содержит корректный промпт
       if not role.get('prompt'):
           await query.edit_message_text("❌ Кастомная роль содержит некорректный промпт.")
           return
       # Сохраняем только промпт роли (без базового системного промпта)
       # compose_system_instruction будет вызван при использовании
       chat_state.system_prompt = role['prompt']
       await db.update_user_chat(user_id, chat_state)
       await role_conv_metrics.record_role_application(f"user_role:{role_id}")
       await query.edit_message_text(f"✅ Кастомная роль '{role['title']}' применена.")
   else:
       # Предустановленная роль
       meta = prompts.DEFAULT_ROLES.get(key)
       if not meta:
           await query.edit_message_text("❌ Роль не найдена.")
           return
       # Сохраняем только промпт роли (без базового системного промпта)
       # compose_system_instruction будет вызван при использовании
       chat_state.system_prompt = meta.get("prompt")
       await db.update_user_chat(user_id, chat_state)
       await role_conv_metrics.record_role_application(key)
       await query.edit_message_text(f"✅ Роль '{meta.get('title', key)}' применена.")

async def role_clear_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
   query = update.callback_query
   await query.answer()
   user_id = query.from_user.id
   chat_state = await db.get_user_chat(user_id)
   # Очищаем системный промпт (будет использован базовый)
   chat_state.system_prompt = None
   await db.update_user_chat(user_id, chat_state)
   await role_conv_metrics.record_role_clear()
   await query.edit_message_text("🧹 Роль сброшена. Использую базовые правила форматирования.")

async def role_create_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
   query = update.callback_query
   await query.answer()
   begin_custom_role_creation(query.from_user.id)
   await query.message.reply_text("Опишите, какую роль хотите создать (1–2 предложения):")

async def role_custom_apply_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
   query = update.callback_query
   await query.answer()
   user_id = query.from_user.id
   chat_state = await db.get_user_chat(user_id)
   role = get_generated_role(user_id)
   if not role:
       await query.edit_message_text("❌ Нет сгенерированной роли для применения.")
       return
   prompt_text = role.get('prompt') or role.get('system_prompt') or ''
   # Сохраняем только промпт роли (без базового системного промпта)
   # compose_system_instruction будет вызван при использовании
   chat_state.system_prompt = prompt_text
   await db.update_user_chat(user_id, chat_state)
   clear_custom_role_state(user_id)
   await query.edit_message_text(f"✅ Роль '{role.get('title','Кастомная роль')}' применена.")

async def role_custom_save_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
   query = update.callback_query
   await query.answer()
   user_id = query.from_user.id
   role = get_generated_role(user_id)
   if not role:
       await query.edit_message_text("❌ Нет сгенерированной роли для сохранения.")
       return
   # Сохраняем в user_roles
   try:
       # Сохраняем
       await db.db_query(
           "INSERT INTO user_roles (user_id, title, prompt) VALUES ($1, $2, $3)",
           (user_id, role.get('title', 'Моя роль'), role.get('prompt') or role.get('system_prompt', ''))
       )
       await role_conv_metrics.record_custom_role_creation()
       # И сразу применяем
       prompt_text = role.get('prompt') or role.get('system_prompt') or ''
       chat_state = await db.get_user_chat(user_id)
       # Сохраняем только промпт роли (без базового системного промпта)
       # compose_system_instruction будет вызван при использовании
       chat_state.system_prompt = prompt_text
       await db.update_user_chat(user_id, chat_state)
       clear_custom_role_state(user_id)
       await query.edit_message_text("💾 Роль сохранена и применена.")
   except Exception as e:
       await query.edit_message_text(f"❌ Ошибка сохранения роли: {e}")

async def role_custom_retry_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
   query = update.callback_query
   await query.answer()
   user_id = query.from_user.id
   last_prompt = get_last_custom_role_prompt(user_id)
   if not last_prompt:
       await query.edit_message_text("❌ Нет предыдущего запроса для повтора.")
       return
   # Запускаем повтор генерации как в messages.handle_request
   chat_state = await db.get_user_chat(user_id)
   
   # Используем универсальную функцию для получения ключа (поддерживает и Gemini, и OpenRouter)
   from app.config import settings
   model_for_role = chat_state.model or settings.DEFAULT_MODEL
   key_data, model_used, resolution = await agent._resolve_ai_request(model_for_role)
   if not key_data:
       await query.edit_message_text("❌ Нет доступных ключей API для генерации роли.")
       return
   progress_msg = await query.message.reply_text("🛠️ Генерирую роль…")
   set_generating_custom_role(user_id, True)
   history = [{'role': 'user', 'parts': [last_prompt]}]
   
   # Используем универсальную функцию для получения ответа (поддерживает и Gemini, и OpenRouter)
   response_text, _ = await agent._get_ai_response(
       key_data['api_key'], 
       history, 
       model_used, 
       system_instruction=prompts.PROMPT_ENGINEER_SYSTEM_PROMPT, 
       user_id=user_id, 
       chat_id=user_id
   )
   
   # Инкрементируем использование ключа
   await agent._increment_key_usage(key_data['key_hash'], model_used)
   
   # Логируем ответ модели для отладки
   logging.info(f"Model response for role retry: {response_text[:500]}...")
   
   role_obj = prompts.extract_json_object(response_text)
   if not role_obj:
       # Обработка явной 503 ошибки из текста
       if "503" in (response_text or "") or "unavailable" in (response_text or "").lower():
           await progress_msg.edit_text("🔄 Сервер перегружен. Попробуйте ещё раз через несколько секунд.")
       else:
           logging.error(f"Failed to parse role JSON on retry. Response: {response_text}")
           await progress_msg.edit_text("❌ Снова не удалось сгенерировать роль. Попробуйте изменить описание.")
       set_generating_custom_role(user_id, False)
       return
   set_last_custom_role_prompt(user_id, last_prompt)
   from app.state import set_generated_role
   set_generated_role(user_id, role_obj)
   title = role_obj.get('title', 'Кастомная роль')
   purpose = role_obj.get('purpose', '')
   style = ", ".join(role_obj.get('style', [])[:3])
   preview = (
       f"🆕 *Новая роль:* {title}\n\n"
       f"🎯 Цель: {purpose}\n"
       f"🧭 Стиль: {style}\n\n"
       f"Применить сейчас или сохранить?"
   )
   kb = [
       [InlineKeyboardButton("✅ Применить", callback_data="role_custom_apply")],
       [InlineKeyboardButton("💾 Сохранить", callback_data="role_custom_save")],
       [InlineKeyboardButton("❌ Отмена", callback_data="role_clear")]
   ]
   formatted_text, parse_mode = TelegramFormatter.format_text(preview)
   await progress_msg.edit_text(formatted_text, parse_mode=parse_mode, reply_markup=InlineKeyboardMarkup(kb))
   set_generating_custom_role(user_id, False)

async def role_delete_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
   query = update.callback_query
   await query.answer()
   user_id = query.from_user.id
   if not await db.is_authorized(user_id):
       return
   try:
       role_id = int(query.data.split(":")[1])
       await db.db_query("DELETE FROM user_roles WHERE id = $1 AND user_id = $2", (role_id, user_id))
       await query.edit_message_text("🗑️ Роль удалена.")
   except Exception as e:
       await query.edit_message_text(f"❌ Ошибка удаления роли: {e}")

async def open_roles_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
   query = update.callback_query
   await query.answer()
   user_id = query.from_user.id
   if not await db.is_authorized(user_id):
       return
   # Отображаем меню ролей так же, как и команда /roles
   from app.handlers.commands import roles_command
   await roles_command(DummyUpdate(query.message, query.from_user), context)

# ============================================================================
# CONVERSATION MANAGEMENT CALLBACKS
# ============================================================================

async def conv_page_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка пагинации списка бесед"""
    query = update.callback_query
    await query.answer()
    
    page = int(query.data.split(":")[1])
    user_id = query.from_user.id
    
    # Получаем беседы для страницы
    limit = 5
    offset = (page - 1) * limit
    conversations = await db.get_user_conversations(user_id, limit, offset)
    total_count = await db.get_conversation_count(user_id)
    
    if not conversations:
        await query.edit_message_text("📝 У вас пока нет сохранённых бесед.")
        return
    
    text = f"📝 *Сохранённые беседы* (страница {page})\n\n"
    
    for conv in conversations:
        role_info = f" | {conv['role_title']}" if conv['role_title'] else ""
        created = conv['created_at'].strftime("%d.%m.%Y %H:%M") if conv['created_at'] else "Неизвестно"
        text += f"🆔 *{conv['id']}* | {conv['title']}{role_info}\n"
        text += f"📅 {created} | 💬 {conv['token_budget'] or 0} токенов\n\n"
    
    # Кнопки навигации
    keyboard = []
    if page > 1:
        keyboard.append([InlineKeyboardButton("⬅️ Предыдущая", callback_data=f"conv_page:{page-1}")])
    if len(conversations) == limit and offset + limit < total_count:
        keyboard.append([InlineKeyboardButton("➡️ Следующая", callback_data=f"conv_page:{page+1}")])
    
    # Кнопки действий
    if conversations:
        keyboard.append([InlineKeyboardButton("🔄 Переключиться", callback_data="conv_switch")])
        keyboard.append([InlineKeyboardButton("✏️ Переименовать", callback_data="conv_rename")])
        keyboard.append([InlineKeyboardButton("🗑️ Удалить", callback_data="conv_delete")])
    
    reply_markup = InlineKeyboardMarkup(keyboard) if keyboard else None
    await query.edit_message_text(text, parse_mode='Markdown', reply_markup=reply_markup)

async def conv_switch_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Переключение на беседу"""
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    
    # Получаем список бесед для выбора
    conversations = await db.get_user_conversations(user_id, 10, 0)
    if not conversations:
        await query.edit_message_text("📝 У вас нет сохранённых бесед.")
        return
    
    text = "🔄 *Выберите беседу для переключения:*\n\n"
    buttons = []
    
    for conv in conversations:
        role_info = f" | {conv['role_title']}" if conv['role_title'] else ""
        created = conv['created_at'].strftime("%d.%m %H:%M") if conv['created_at'] else "Неизвестно"
        text += f"🆔 *{conv['id']}* | {conv['title']}{role_info}\n"
        text += f"📅 {created}\n\n"
        
        buttons.append([InlineKeyboardButton(
            f"🆔 {conv['id']} | {conv['title'][:30]}{'...' if len(conv['title']) > 30 else ''}", 
            callback_data=f"conv_switch_to:{conv['id']}"
        )])
    
    await query.edit_message_text(text, parse_mode='Markdown', reply_markup=InlineKeyboardMarkup(buttons))

async def conv_switch_to_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Переключение на конкретную беседу"""
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    
    conv_id = int(query.data.split(":")[1])
    
    try:
        success = await db.switch_to_conversation(user_id, conv_id)
        if success:
            await role_conv_metrics.record_conversation_switched()
            await query.edit_message_text(f"✅ Переключились на беседу ID: {conv_id}")
        else:
            await query.edit_message_text("❌ Ошибка при переключении на беседу.")
    except Exception as e:
        logging.error(f"Error switching to conversation {conv_id}: {e}")
        await query.edit_message_text("❌ Ошибка при переключении на беседу.")

async def conv_rename_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Переименование беседы"""
    query = update.callback_query
    await query.answer()
    
    await query.edit_message_text(
        "✏️ Введите ID беседы и новое название:\n\n"
        "Формат: /rename <ID> <новое название>\n"
        "Пример: /rename 123 Моя новая беседа"
    )

async def conv_delete_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Удаление беседы"""
    query = update.callback_query
    await query.answer()
    
    await query.edit_message_text(
        "🗑️ Введите ID беседы для удаления:\n\n"
        "Используйте /conversations для просмотра списка бесед.\n"
        "⚠️ Это действие нельзя отменить!"
    )

async def conv_delete_confirm_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Подтверждение удаления беседы"""
    query = update.callback_query
    await query.answer()
    
    conv_id = int(query.data.split(":")[1])
    user_id = query.from_user.id
    
    success = await db.delete_conversation(user_id, conv_id)
    
    if success:
        await role_conv_metrics.record_conversation_deleted()
        await query.edit_message_text(f"✅ Беседа {conv_id} удалена")
    else:
        await query.edit_message_text("❌ Ошибка при удалении беседы")

async def conv_delete_cancel_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Отмена удаления беседы"""
    query = update.callback_query
    await query.answer()
    
    await query.edit_message_text("❌ Удаление отменено")
