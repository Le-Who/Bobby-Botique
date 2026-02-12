import asyncio
import logging
import telegram
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
from app.metrics import role_conv_metrics
from app.state import get_last_custom_role_prompt, set_generating_custom_role, set_last_custom_role_prompt
from app.utils.decorators import admin_only
from app.handlers import menus
from app.document_processor import get_user_documents, delete_user_document, get_document_by_id
from app.state import clear_document_state, set_document_mode, get_selected_document_id
from app.utils.keyboards import build_keyboard, back_button, cancel_button, confirm_cancel_row

# Лимитер для тяжёлых callback-веток (complex/fallback/deepdive и т.п.)
_HEAVY_CALLBACK_LIMIT = max(1, int(getattr(settings, "MAX_CONCURRENT_HEAVY_CALLBACKS", 4)))
_HEAVY_CALLBACK_SEMAPHORE = asyncio.Semaphore(_HEAVY_CALLBACK_LIMIT)

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
    
    # Обновляем меню с новой выбранной моделью
    formatted_text, parse_mode, reply_markup = menus.get_model_menu_content(chat_state, context)

    # Определяем имя для тоста
    is_openrouter = "/" in model_name
    display_name = model_name.split("/")[-1] if is_openrouter else model_name
    
    try:
        await query.edit_message_text(formatted_text, parse_mode=parse_mode, reply_markup=reply_markup)
    except telegram.error.BadRequest as e:
        if "Message is not modified" in str(e):
            pass
        else:
            raise e
    await query.answer(f"✅ Модель изменена на {display_name}")

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
    user_lock = state.get_user_lock(user_id)

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
            async with _HEAVY_CALLBACK_SEMAPHORE:
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
    user_lock = state.get_user_lock(user_id)

    if user_lock.locked():
        return

    async def task_wrapper():
        async with _HEAVY_CALLBACK_SEMAPHORE:
            async with user_lock:
                if action == "confirm":
                    chat_state = await db.get_user_chat(user_id)
                    user_message = original_message.text
                    await agent._handle_regular_chat(placeholder_message, user_id, user_message, chat_state, model_override=model_override)

    asyncio.create_task(task_wrapper())

async def _handle_document_upload_new(query, context, user_id):
    text = "📄 **Загрузите новый документ**\n\nОтправьте PDF или DOCX файл, и я обработаю его для вас."
    formatted_text, parse_mode = TelegramFormatter.format_text(text)
    await query.edit_message_text(
        formatted_text,
        parse_mode=parse_mode,
        reply_markup=build_keyboard(back_button("doc:list"))
    )

async def _handle_document_list(query, context, user_id):
    text, parse_mode, reply_markup = await menus.get_documents_menu_content(user_id)
    await query.edit_message_text(text, parse_mode=parse_mode, reply_markup=reply_markup)

async def _handle_document_cancel(query, context, user_id):
    clear_document_state(user_id)
    
    text = "✅ **Режим работы с документами отключен**\n\nТеперь ваши сообщения будут обрабатываться в обычном режиме чата.\nЧтобы снова работать с документами, загрузите новый файл или используйте команду /documents."
    formatted_text, parse_mode = TelegramFormatter.format_text(text)
    await query.edit_message_text(
        formatted_text,
        parse_mode=parse_mode
    )

async def _handle_document_clear_all(query, context, user_id):
    # Получаем все документы пользователя
    documents = await get_user_documents(user_id)
    if not documents:
        await query.answer("У вас нет документов для удаления.")
        return
    
    text = "⚠️ **Вы уверены?**\n\nЭто действие удалит **ВСЕ** ваши загруженные документы.\nЭто действие нельзя отменить."
    formatted_text, parse_mode = TelegramFormatter.format_text(text)
    await query.edit_message_text(
        formatted_text,
        parse_mode=parse_mode,
        reply_markup=build_keyboard(
            confirm_cancel_row("doc:clear_all_confirm", "doc:list", "✅ Да, удалить все", "❌ Отмена")
        )
    )

async def _handle_document_clear_all_confirm(query, context, user_id):
    # Получаем все документы пользователя
    documents = await get_user_documents(user_id)
    if not documents:
        await query.answer("У вас нет документов для удаления.")
        text, parse_mode, reply_markup = await menus.get_documents_menu_content(user_id)
        await query.edit_message_text(text, parse_mode=parse_mode, reply_markup=reply_markup)
        return

    # Удаляем все документы
    deleted_count = 0
    for doc in documents:
        success = await delete_user_document(doc['id'], user_id)
        if success:
            deleted_count += 1
    
    # Очищаем состояние работы с документами
    clear_document_state(user_id)
    
    # Обновляем меню
    text, parse_mode, reply_markup = await menus.get_documents_menu_content(user_id)
    await query.edit_message_text(text, parse_mode=parse_mode, reply_markup=reply_markup)
    await query.answer(f"🗑️ Удалено {deleted_count} документов.")

async def _handle_document_use_existing(query, context, user_id):
    # Используем существующий документ
    document_id = int(query.data.split(':')[2])

    document = await get_document_by_id(document_id, user_id)
    if not document:
        await query.edit_message_text("❌ Документ не найден.")
        return

    # Устанавливаем состояние работы с документами
    set_document_mode(user_id, True, document_id)

    text = f"✅ **Используется существующий документ**\n\n📄 **{document['filename']}**\n📊 Страниц: {document['pages']}\n📅 Загружен: {document['created_at'][:10]}\n\nТеперь вы можете задавать вопросы по этому документу.\n\n💡 **Просто напишите ваш вопрос** - система автоматически найдет ответ в документе.\n\n🔄 **Для выхода из режима документов:**\n• Нажмите кнопку '❌ Отмена' ниже\n• Или отправьте команду /documents"
    formatted_text, parse_mode = TelegramFormatter.format_text(text)
    await query.edit_message_text(
        formatted_text,
        parse_mode=parse_mode,
        reply_markup=build_keyboard(cancel_button("doc:cancel"))
    )

async def _handle_document_force_upload(query, context, user_id):
    text = "📄 *Загрузите файл как новый документ*\n\nОтправьте файл еще раз, и он будет сохранен как новый документ."
    formatted_text, parse_mode = TelegramFormatter.format_text(text)
    await query.edit_message_text(
        formatted_text,
        parse_mode=parse_mode,
        reply_markup=build_keyboard(
            back_button("doc:list"),
            cancel_button("doc:cancel")
        )
    )

async def _handle_document_select_document(query, context, user_id):
    # Показываем меню выбора документа
    documents = await get_user_documents(user_id)
    if not documents:
        # Если документов нет, показываем главное меню документов
        text, parse_mode, reply_markup = await menus.get_documents_menu_content(user_id)
        try:
            await query.edit_message_text(text, parse_mode=parse_mode, reply_markup=reply_markup)
        except telegram.error.BadRequest as e:
            if "Message is not modified" in str(e):
                pass
            else:
                raise e
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
    
    text = "📋 **Выберите документ для работы:**\n\nНажмите на документ, чтобы начать работу с ним."
    formatted_text, parse_mode = TelegramFormatter.format_text(text)
    await query.edit_message_text(
        formatted_text,
        parse_mode=parse_mode,
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

async def _handle_document_select(query, context, user_id):
    # Выбираем конкретный документ
    document_id = int(query.data.split(':')[2])

    document = await get_document_by_id(document_id, user_id)
    if not document:
        await query.edit_message_text("❌ Документ не найден.")
        return
    
    # Устанавливаем состояние работы с документами
    set_document_mode(user_id, True, document_id)

    text = f"✅ **Выбран документ**\n\n📄 **{document['filename']}**\n📊 Страниц: {document['pages']}\n📅 Загружен: {document['created_at'][:10]}\n\nТеперь вы можете задавать вопросы по этому документу.\n\n💡 **Просто напишите ваш вопрос** - система автоматически найдет ответ в документе.\n\n🔄 **Для выхода из режима документов:**\n• Нажмите кнопку '❌ Отмена' ниже\n• Или отправьте команду /documents"
    formatted_text, parse_mode = TelegramFormatter.format_text(text)
    await query.edit_message_text(
        formatted_text,
        parse_mode=parse_mode,
        reply_markup=build_keyboard(
            back_button("doc:select_document", "⬅️ Назад к списку"),
            cancel_button("doc:cancel")
        )
    )

async def _handle_document_delete_document(query, context, user_id):
    # Удаляем конкретный документ
    document_id = int(query.data.split(':')[2])

    document = await get_document_by_id(document_id, user_id)
    if not document:
        await query.answer("❌ Документ не найден.")
        return
    
    success = await delete_user_document(document_id, user_id)
    if success:
        # Проверяем, был ли это выбранный документ
        selected_doc_id = get_selected_document_id(user_id)
        if selected_doc_id == document_id:
            # Если удалили выбранный документ, очищаем состояние
            clear_document_state(user_id)
        
        # Возвращаемся к списку документов (он был родительским меню для select_document)
        # В идеале нужно понять, откуда пришли, но здесь логичнее вернуться в список
        # Однако, кнопка удаления была в списке выбора, так что мы просто обновляем список

        # Здесь есть нюанс: кнопка удаления была в меню `select_document` (список кнопок).
        # Если мы хотим обновить ЭТОТ же список (но без удаленного файла), нам нужно вызвать логику `select_document` снова.
        # Но `select_document` - это не главное меню, а подменю.
        # Попробуем обновить именно подменю выбора.

        documents = await get_user_documents(user_id)
        if not documents:
            # Если документов не осталось, показываем главное меню документов
            text, parse_mode, reply_markup = await menus.get_documents_menu_content(user_id)
            await query.edit_message_text(text, parse_mode=parse_mode, reply_markup=reply_markup)
        else:
            # Иначе перестраиваем список выбора
            keyboard = []
            for doc in documents[:10]:
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
            await query.edit_message_text(formatted_text, parse_mode=parse_mode, reply_markup=InlineKeyboardMarkup(keyboard))

        await query.answer(f"🗑️ Документ '{document['filename']}' удален.")
    else:
        await query.answer("❌ Ошибка при удалении документа.")

async def document_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обрабатывает callback-кнопки для управления документами"""
    query = update.callback_query
    await query.answer()

    action = query.data.split(':')[1]
    user_id = query.from_user.id

    handlers = {
        "upload_new": _handle_document_upload_new,
        "list": _handle_document_list,
        "cancel": _handle_document_cancel,
        "clear_all": _handle_document_clear_all,
        "clear_all_confirm": _handle_document_clear_all_confirm,
        "use_existing": _handle_document_use_existing,
        "force_upload": _handle_document_force_upload,
        "select_document": _handle_document_select_document,
        "select": _handle_document_select,
        "delete_document": _handle_document_delete_document,
    }

    handler = handlers.get(action)
    if handler:
        await handler(query, context, user_id)
    else:
        logging.warning(f"Unknown document action: {action}")

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
        # UX: Add back button even for empty state
        await query.edit_message_text(
            "У вас пока нет кастомных ролей.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Назад", callback_data="open_roles")]])
        )
        return
    buttons = []
    for r in roles:
        buttons.append([InlineKeyboardButton(f"✏️ {r['title']}", callback_data=f"role_rename_pick:{r['id']}")])

    # UX: Add Back button and use edit_message_text
    buttons.append([InlineKeyboardButton("⬅️ Назад", callback_data="open_roles")])
    await query.edit_message_text("Выберите роль для переименования:", reply_markup=InlineKeyboardMarkup(buttons))

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

async def toggle_search_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user_id = query.from_user.id

    chat_state = await db.get_user_chat(user_id)
    chat_state.search_enabled = not chat_state.search_enabled
    await db.update_user_chat(user_id, chat_state)

    formatted_text, parse_mode, reply_markup = menus.get_start_menu_content(chat_state)

    await query.edit_message_text(formatted_text, parse_mode=parse_mode, reply_markup=reply_markup)

    status_text = "ВКЛЮЧЕН" if chat_state.search_enabled else "ВЫКЛЮЧЕН"
    await query.answer(f"Поиск {status_text}")

def _add_fast_callback(application: Application, callback, pattern: str):
   """Register lightweight UI callbacks in non-blocking mode."""
   application.add_handler(CallbackQueryHandler(callback, pattern=pattern, block=False), group=-1)


def register(application: Application):
   # Быстрый канал для UI-настроек: callback выполняется без блокировки update loop.
   _add_fast_callback(application, toggle_search_callback, "^toggle_search$")
   _add_fast_callback(application, new_chat_callback, "^new_chat$")
   _add_fast_callback(application, model_menu_callback, "^model_menu$")
   _add_fast_callback(application, help_callback, "^help$")
   _add_fast_callback(application, start_menu_callback, "^start_menu$")
   _add_fast_callback(application, model_button_callback, "^model")
   _add_fast_callback(application, open_roles_callback, "^open_roles$")
   _add_fast_callback(application, role_apply_callback, "^role_apply:")
   _add_fast_callback(application, role_clear_callback, "^role_clear$")
   _add_fast_callback(application, role_nav_callback, "^role_nav:")
   _add_fast_callback(application, role_page_callback, "^role_page:")
   _add_fast_callback(application, conv_page_callback, "^conv_page:")
   _add_fast_callback(application, conv_switch_callback, "^conv_switch$")
   _add_fast_callback(application, conv_switch_to_callback, "^conv_switch_to:")

   # Обрабатываем оба формата: model:0 (новый) и model_none (разделитель)
   application.add_handler(CallbackQueryHandler(complex_search_callback, pattern="^complex:"))
   application.add_handler(CallbackQueryHandler(fallback_callback, pattern="^fallback:"))
   application.add_handler(CallbackQueryHandler(document_callback, pattern="^doc:"))
   application.add_handler(CallbackQueryHandler(deep_dive_callback, pattern="^deepdive:"))
   application.add_handler(CallbackQueryHandler(new_topic_callback, pattern="^new_topic"))
   application.add_handler(CallbackQueryHandler(retry_last_callback, pattern="^retry_last$"))
   # Роль: apply/clear/create
   application.add_handler(CallbackQueryHandler(role_create_callback, pattern="^role_create$"))
   application.add_handler(CallbackQueryHandler(role_custom_apply_callback, pattern="^role_custom_apply$"))
   application.add_handler(CallbackQueryHandler(role_custom_save_callback, pattern="^role_custom_save$"))
   application.add_handler(CallbackQueryHandler(role_custom_retry_callback, pattern="^role_custom_retry$"))
   # New Role management
   application.add_handler(CallbackQueryHandler(role_detail_callback, pattern="^role_detail:"))
   application.add_handler(CallbackQueryHandler(role_view_prompt_callback, pattern="^role_view_prompt:"))
   application.add_handler(CallbackQueryHandler(role_delete_ask_callback, pattern="^role_delete_ask:"))
   application.add_handler(CallbackQueryHandler(role_delete_confirm_callback, pattern="^role_delete_confirm:"))
   application.add_handler(CallbackQueryHandler(role_delete_cancel_callback, pattern="^role_delete_cancel:"))
   
   application.add_handler(CallbackQueryHandler(role_rename_menu_callback, pattern="^role_rename_menu$"))
   application.add_handler(CallbackQueryHandler(role_rename_pick_callback, pattern="^role_rename_pick:"))
    
   # Role Navigation (New)
   application.add_handler(CallbackQueryHandler(lambda u,c: u.callback_query.answer(), pattern="^noop$"))

   # Conversation management callbacks
   application.add_handler(CallbackQueryHandler(conv_rename_callback, pattern="^conv_rename$"))
   application.add_handler(CallbackQueryHandler(conv_rename_ask_callback, pattern="^conv_rename_ask:"))
   application.add_handler(CallbackQueryHandler(conv_rename_cancel_callback, pattern="^conv_rename_cancel$"))
   application.add_handler(CallbackQueryHandler(conv_delete_callback, pattern="^conv_delete$"))
   application.add_handler(CallbackQueryHandler(conv_delete_ask_callback, pattern="^conv_delete_ask:"))
   application.add_handler(CallbackQueryHandler(conv_delete_confirm_callback, pattern="^conv_delete_confirm:"))
   application.add_handler(CallbackQueryHandler(conv_delete_cancel_callback, pattern="^conv_delete_cancel$"))

   # Refresh metrics
   application.add_handler(CallbackQueryHandler(refresh_metrics_callback, pattern="^refresh_metrics$"))

async def start_menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    user_id = query.from_user.id
    chat_state = await db.get_user_chat(user_id)

    formatted_text, parse_mode, reply_markup = menus.get_start_menu_content(chat_state)

    await query.edit_message_text(formatted_text, parse_mode=parse_mode, reply_markup=reply_markup)

async def role_apply_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
   query = update.callback_query
   user_id = query.from_user.id
   chat_state = await db.get_user_chat(user_id)
   key = query.data.split(":", 1)[1]
   role_title = ""
   
   # Проверяем, это кастомная роль пользователя
   if key.startswith("user_role:"):
       role_id = int(key.split(":")[1])
       role_data = await db.db_query("SELECT title, prompt FROM user_roles WHERE id = $1 AND user_id = $2", (role_id, user_id))
       if not role_data:
           await query.answer("❌ Кастомная роль не найдена.")
           return
       role = role_data[0]
       # Проверяем, что роль содержит корректный промпт
       if not role.get('prompt'):
           await query.answer("❌ Кастомная роль содержит некорректный промпт.")
           return

       chat_state.system_prompt = role['prompt']
       role_title = role['title']
       await role_conv_metrics.record_role_application(f"user_role:{role_id}")
   else:
       # Предустановленная роль
       meta = prompts.DEFAULT_ROLES.get(key)
       if not meta:
           await query.answer("❌ Роль не найдена.")
           return

       chat_state.system_prompt = meta.get("prompt")
       role_title = meta.get("title", key)
       await role_conv_metrics.record_role_application(key)

   # Сохраняем состояние
   await db.update_user_chat(user_id, chat_state)

   # Обновляем меню
   # Сохраняем состояние
   await db.update_user_chat(user_id, chat_state)

   # Обновляем меню - возвращаемся в Hub
   text, parse_mode, reply_markup = await menus.get_roles_menu_content(user_id, chat_state, view_mode="hub")
   try:
       await query.edit_message_text(text, parse_mode=parse_mode, reply_markup=reply_markup)
   except telegram.error.BadRequest as e:
       if "Message is not modified" not in str(e):
           raise e
   await query.answer(f"✅ Роль '{role_title}' применена.")

async def role_clear_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
   query = update.callback_query
   user_id = query.from_user.id
   chat_state = await db.get_user_chat(user_id)
   # Очищаем системный промпт (будет использован базовый)
   chat_state.system_prompt = None
   await db.update_user_chat(user_id, chat_state)
   await role_conv_metrics.record_role_clear()

   # Обновляем меню
   # Обновляем меню - возвращаемся в Hub
   text, parse_mode, reply_markup = await menus.get_roles_menu_content(user_id, chat_state, view_mode="hub")
   try:
       await query.edit_message_text(text, parse_mode=parse_mode, reply_markup=reply_markup)
   except telegram.error.BadRequest as e:
       if "Message is not modified" not in str(e):
           raise e
   await query.answer("🧹 Роль сброшена.")

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

async def role_delete_ask_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    role_id = query.data.split(":")[1]
    
    bg_text = (
        "⚠️ *Удаление роли*\n\n"
        "Вы уверены, что хотите удалить эту роль? Это действие нельзя отменить."
    )
    kb = [
        [InlineKeyboardButton("🗑️ Да, удалить навсегда", callback_data=f"role_delete_confirm:{role_id}")],
        [InlineKeyboardButton("❌ Отмена", callback_data=f"role_delete_cancel:{role_id}")]
    ]
    formatted, pm = TelegramFormatter.format_text(bg_text)
    await query.edit_message_text(formatted, parse_mode=pm, reply_markup=InlineKeyboardMarkup(kb))

async def role_delete_cancel_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    role_id = query.data.split(":")[1]
    
    # Возвращаемся в детали роли
    user_id = query.from_user.id
    chat_state = await db.get_user_chat(user_id)
    
    text, parse_mode, reply_markup = await menus.get_roles_menu_content(user_id, chat_state, view_mode="role_details", role_key=f"user_role:{role_id}")
    await query.edit_message_text(text, parse_mode=parse_mode, reply_markup=reply_markup)

async def role_delete_confirm_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user_id = query.from_user.id
    if not await db.is_authorized(user_id):
        await query.answer("❌ Нет доступа")
        return
    try:
        role_id = int(query.data.split(":")[1])
        # Проверяем, не активна ли эта роль сейчас
        chat_state = await db.get_user_chat(user_id)

        # Получаем промпт удаляемой роли, чтобы проверить, активна ли она
        role_data = await db.db_query("SELECT prompt FROM user_roles WHERE id = $1 AND user_id = $2", (role_id, user_id))

        await db.db_query("DELETE FROM user_roles WHERE id = $1 AND user_id = $2", (role_id, user_id))

        # Если удаляемая роль была активна, сбрасываем ее
        if role_data and chat_state.system_prompt == role_data[0]['prompt']:
            chat_state.system_prompt = None
            await db.update_user_chat(user_id, chat_state)

        # Обновляем меню - переходим в список "Мои роли"
        text, parse_mode, reply_markup = await menus.get_roles_menu_content(user_id, chat_state, view_mode="my_roles", page=0)
        
        try:
            await query.edit_message_text(text, parse_mode=parse_mode, reply_markup=reply_markup)
        except telegram.error.BadRequest as e:
            if "Message is not modified" not in str(e):
                raise e

        await query.answer("🗑️ Роль удалена.")
        
    except Exception as e:
        logging.error(f"Error deleting role: {e}")
        await query.answer("❌ Ошибка удаления роли")

async def role_detail_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    user_id = query.from_user.id
    chat_state = await db.get_user_chat(user_id)
    
    role_key = query.data.split(":", 1)[1]
    
    text, parse_mode, reply_markup = await menus.get_roles_menu_content(user_id, chat_state, view_mode="role_details", role_key=role_key)
    
    try:
        await query.edit_message_text(text, parse_mode=parse_mode, reply_markup=reply_markup)
    except telegram.error.BadRequest as e:
        if "Message is not modified" not in str(e):
            raise e

async def role_view_prompt_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    role_key = query.data.split(":", 1)[1]
    user_id = query.from_user.id
    
    # Need to fetch role data again to get prompt
    prompt = ""
    # Code duplication avoidance: ideally extract "get_role_prompt(key, uid)" helper.
    # For now, minimal inline logic is fine.
    
    if role_key.startswith("user_role:"):
        try:
            r_id = int(role_key.split(":")[1])
            res = await db.db_query("SELECT prompt FROM user_roles WHERE id = $1 AND user_id = $2", (r_id, user_id))
            if res:
                prompt = res[0]['prompt']
        except:
            pass
    elif role_key in prompts.DEFAULT_ROLES:
        prompt = prompts.DEFAULT_ROLES[role_key].get('prompt', '')
        
    if prompt:
        # Send as a new message so user can copy it easily
        await query.message.reply_text(f"📝 *Полный промпт роли:*\n\n`{prompt}`", parse_mode="Markdown")
    else:
        await query.message.reply_text("❌ Не удалось найти промпт.")

async def role_nav_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    user_id = query.from_user.id
    chat_state = await db.get_user_chat(user_id)
    
    view_mode = query.data.split(":")[1]
    
    text, parse_mode, reply_markup = await menus.get_roles_menu_content(user_id, chat_state, view_mode=view_mode)
    
    try:
        await query.edit_message_text(text, parse_mode=parse_mode, reply_markup=reply_markup)
    except telegram.error.BadRequest as e:
        if "Message is not modified" not in str(e):
            raise e

async def role_page_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    user_id = query.from_user.id
    chat_state = await db.get_user_chat(user_id)
    
    parts = query.data.split(":")
    view_mode = parts[1]
    page = int(parts[2])
    
    text, parse_mode, reply_markup = await menus.get_roles_menu_content(user_id, chat_state, view_mode=view_mode, page=page)
    
    try:
        await query.edit_message_text(text, parse_mode=parse_mode, reply_markup=reply_markup)
    except telegram.error.BadRequest as e:
        if "Message is not modified" not in str(e):
            raise e

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

async def send_conversation_selection(query: telegram.CallbackQuery, user_id: int, action_prefix: str, title: str):
    """
    Helper to send a list of conversations for selection.
    
    Args:
        query: The callback query object
        user_id: The user ID
        action_prefix: The prefix for the callback data (e.g. 'conv_switch_to', 'conv_delete_ask')
        title: The title text to display
    """
    # Получаем список бесед для выбора
    conversations = await db.get_user_conversations(user_id, 10, 0)
    if not conversations:
        await query.edit_message_text("📝 У вас нет сохранённых бесед.")
        return
    
    text = f"{title}\n\n"
    buttons = []
    
    for conv in conversations:
        role_info = f" | {conv['role_title']}" if conv['role_title'] else ""
        created = conv['created_at'].strftime("%d.%m %H:%M") if conv['created_at'] else "Неизвестно"
        text += f"🆔 *{conv['id']}* | {conv['title']}{role_info}\n"
        text += f"📅 {created}\n\n"
        
        buttons.append([InlineKeyboardButton(
            f"🆔 {conv['id']} | {conv['title'][:30]}{'...' if len(conv['title']) > 30 else ''}", 
            callback_data=f"{action_prefix}:{conv['id']}"
        )])
    
    buttons.append([InlineKeyboardButton("⬅️ Назад", callback_data="conv_page:1")])

    await query.edit_message_text(text, parse_mode='Markdown', reply_markup=InlineKeyboardMarkup(buttons))

async def conv_page_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка пагинации списка бесед"""
    query = update.callback_query
    await query.answer()

    page = int(query.data.split(":")[1])
    user_id = query.from_user.id

    text, parse_mode, reply_markup = await menus.get_conversations_menu_content(user_id, page)

    if reply_markup is None:
        await query.edit_message_text(text)
    else:
        await query.edit_message_text(text, parse_mode=parse_mode, reply_markup=reply_markup)

async def conv_switch_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Переключение на беседу"""
    query = update.callback_query
    await query.answer()
    await send_conversation_selection(
        query,
        query.from_user.id,
        "conv_switch_to",
        "🔄 *Выберите беседу для переключения:*"
    )

async def conv_switch_to_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Переключение на конкретную беседу"""
    query = update.callback_query
    user_id = query.from_user.id
    
    conv_id = int(query.data.split(":")[1])
    
    try:
        success = await db.switch_to_conversation(user_id, conv_id)
        if success:
            await role_conv_metrics.record_conversation_switched()
            # Показываем список бесед с тостом
            text, parse_mode, reply_markup = await menus.get_conversations_menu_content(user_id, 1)
            await query.edit_message_text(text, parse_mode=parse_mode, reply_markup=reply_markup)
            await query.answer(f"✅ Переключились на беседу ID: {conv_id}")
        else:
            await query.answer("❌ Ошибка при переключении на беседу.")
    except Exception as e:
        logging.error(f"Error switching to conversation {conv_id}: {e}")
        await query.answer("❌ Ошибка при переключении на беседу.")

async def conv_rename_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Переименование беседы"""
    query = update.callback_query
    await query.answer()
    await send_conversation_selection(
        query,
        query.from_user.id,
        "conv_rename_ask",
        "✏️ *Выберите беседу для переименования:*"
    )

async def conv_delete_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Удаление беседы"""
    query = update.callback_query
    await query.answer()
    await send_conversation_selection(
        query,
        query.from_user.id,
        "conv_delete_ask",
        "🗑️ *Выберите беседу для удаления:*"
    )

async def conv_delete_ask_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Спрашивает подтверждение удаления беседы"""
    query = update.callback_query
    await query.answer()

    conv_id = int(query.data.split(":")[1])

    keyboard = [
        [InlineKeyboardButton("✅ Да, удалить", callback_data=f"conv_delete_confirm:{conv_id}")],
        [InlineKeyboardButton("❌ Отмена", callback_data="conv_delete_cancel")]
    ]
    
    await query.edit_message_text(
        f"⚠️ Вы уверены, что хотите удалить беседу {conv_id}?\n\nЭто действие нельзя отменить!",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

async def conv_rename_ask_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Спрашивает новое название беседы"""
    query = update.callback_query
    await query.answer()

    conv_id = int(query.data.split(":")[1])
    context.user_data['rename_conv_id'] = conv_id

    keyboard = [[InlineKeyboardButton("❌ Отмена", callback_data="conv_rename_cancel")]]

    await query.edit_message_text(
        f"✏️ Введите новое название для беседы {conv_id} (одной строкой):",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

async def conv_rename_cancel_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Отмена переименования"""
    query = update.callback_query
    await query.answer("❌ Переименование отменено")

    context.user_data.pop('rename_conv_id', None)

    await send_conversation_selection(
        query,
        query.from_user.id,
        "conv_rename_ask",
        "✏️ *Выберите беседу для переименования:*"
    )

async def conv_delete_confirm_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Подтверждение удаления беседы"""
    query = update.callback_query
    
    conv_id = int(query.data.split(":")[1])
    user_id = query.from_user.id
    
    success = await db.delete_conversation(user_id, conv_id)
    
    if success:
        await role_conv_metrics.record_conversation_deleted()

        # Обновляем список
        text, parse_mode, reply_markup = await menus.get_conversations_menu_content(user_id, 1)
        await query.edit_message_text(text, parse_mode=parse_mode, reply_markup=reply_markup)

        await query.answer(f"✅ Беседа {conv_id} удалена")
    else:
        await query.answer("❌ Ошибка при удалении беседы")

async def conv_delete_cancel_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Отмена удаления беседы"""
    query = update.callback_query
    
    text, parse_mode, reply_markup = await menus.get_conversations_menu_content(query.from_user.id, 1)
    await query.edit_message_text(text, parse_mode=parse_mode, reply_markup=reply_markup)
    await query.answer("❌ Удаление отменено")

@admin_only
async def refresh_metrics_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Refreshes the metrics dashboard."""
    query = update.callback_query

    try:
        text = await menus.get_metrics_content()
        formatted_text, parse_mode = TelegramFormatter.format_text(text)

        keyboard = [[InlineKeyboardButton("🔄 Обновить", callback_data="refresh_metrics")]]

        await query.edit_message_text(
            formatted_text,
            parse_mode=parse_mode,
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        await query.answer("🔄 Метрики обновлены")

    except telegram.error.BadRequest as e:
        if "Message is not modified" in str(e):
            await query.answer("✅ Данные актуальны", show_alert=False)
        else:
            logging.error(f"Error refreshing metrics: {e}")
            await query.answer("❌ Ошибка обновления")
    except Exception as e:
        logging.error(f"Error in refresh metrics callback: {e}", exc_info=True)
        await query.answer("❌ Внутренняя ошибка")
