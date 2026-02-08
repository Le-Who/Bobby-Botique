import logging
from datetime import datetime
from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from app.utils.formatting import TelegramFormatter, format_key_for_display
from app import database as db
from app.config import settings, get_model_hash, get_openrouter_keys
from app import prompts
from app.metrics import get_system_status_data
from app.document_processor import get_user_documents

def get_start_menu_content(chat_state):
    search_status = "🟢 ВКЛЮЧЕН" if chat_state.search_enabled else "🔴 ВЫКЛЮЧЕН"
    prompt_status = f"`{chat_state.system_prompt[:50]}...`" if chat_state.system_prompt else "Не задана"
    search_icon = "🟢" if chat_state.search_enabled else "🔴"

    start_text = (
        "🤖 **Добро пожаловать в Gemini Bot!**\n\n"
        "Я ваш умный ассистент с возможностями:\n"
        "• 💬 Обычный чат с AI\n"
        "• 🔍 Веб-поиск и анализ\n"
        "• 🖼️ Поиск по изображениям\n"
        "• 📄 Обработка документов\n\n"
        "**📊 Ваши настройки:**\n"
        f"• Модель: `{chat_state.model}`\n"
        f"• Поиск: {search_status}\n"
        f"• Инструкция: {prompt_status}\n\n"
        "**🚀 Быстрый старт:**\n"
        "• Просто напишите сообщение для чата\n"
        "• `? вопрос` — быстрый ответ\n"
        "• `?? вопрос` — глубокий анализ\n"
        "• Отправьте фото для анализа\n\n"
        "**⚙️ Основные команды:**\n"
        "• `/help` — подробная справка\n"
        "• `/res` — режим поиска вкл/выкл\n"
        "• `/newchat` — новый чат\n"
        "• `/model` — выбрать модель\n"
        "• `/setprompt` — задать инструкцию\n"
        "• `/documents` — управление документами\n"
        "• `/metrics` — статистика системы\n"
        "• `/roles` — выбор ролей и создание своей\n\n"
        "**💡 Совет:** Начните с простого вопроса!"
    )

    formatted_text, parse_mode = TelegramFormatter.format_text(start_text)

    keyboard = [
        [
            InlineKeyboardButton("🆕 Новый чат", callback_data="new_chat"),
            InlineKeyboardButton("⚙️ Модели", callback_data="model_menu")
        ],
        [
            InlineKeyboardButton("🎭 Роли", callback_data="open_roles"),
            InlineKeyboardButton("📚 Справка", callback_data="help")
        ],
        [
            InlineKeyboardButton(f"🌐 Поиск: {search_icon}", callback_data="toggle_search")
        ]
    ]

    return formatted_text, parse_mode, InlineKeyboardMarkup(keyboard)

def _generate_model_buttons(models, current_model, start_index, is_openrouter=False):
    """
    Генерирует список кнопок для выбора модели.

    Args:
        models (list): Список моделей.
        current_model (str): Текущая выбранная модель.
        start_index (int): Начальный индекс для callback_data.
        is_openrouter (bool): Флаг, указывающий на использование OpenRouter.

    Returns:
        tuple: (список строк кнопок, следующий индекс)
    """
    rows = []
    current_index = start_index

    for m in models:
        # Проверяем, выбрана ли модель
        is_selected = (m == current_model)
        selected_mark = "✅ " if is_selected else ""

        # Определяем отображение
        if is_openrouter:
            display_name = m.split("/")[-1] if "/" in m else m
            icon = "🌐"
        else:
            display_name = m
            icon = "🤖"

        # Формируем кнопку
        model_hash = get_model_hash(m)
        text = f"{selected_mark}{icon} {display_name}"
        callback_data = f"model:{current_index}:{model_hash}"

        rows.append([InlineKeyboardButton(text, callback_data=callback_data)])
        current_index += 1

    return rows, current_index

def get_model_menu_content(chat_state, context):
    current_model = chat_state.model

    # Определяем, какой провайдер используется для текущей модели
    openrouter_available = bool(get_openrouter_keys())

    # Создаем единый список всех моделей для индексации
    all_models = []
    if settings.AVAILABLE_MODELS:
        all_models.extend(settings.AVAILABLE_MODELS)
    if openrouter_available and settings.OPENROUTER_AVAILABLE_MODELS:
        all_models.extend(settings.OPENROUTER_AVAILABLE_MODELS)

    if not all_models:
        return "❌ Нет доступных моделей. Проверьте настройки.", None, None

    # Сохраняем маппинг моделей в context для использования в callback
    if context and hasattr(context, 'user_data'):
        # Ensure user_data exists if it's None (though ContextTypes usually ensures it's a dict-like)
        if context.user_data is None:
            context.user_data = {}
        context.user_data['model_list'] = all_models

    keyboard = []
    model_index = 0

    # Добавляем модели Gemini
    if settings.AVAILABLE_MODELS:
        buttons, model_index = _generate_model_buttons(
            settings.AVAILABLE_MODELS,
            current_model,
            model_index,
            is_openrouter=False
        )
        keyboard.extend(buttons)

    # Добавляем разделитель, если есть оба провайдера
    if settings.AVAILABLE_MODELS and openrouter_available and settings.OPENROUTER_AVAILABLE_MODELS:
        keyboard.append([InlineKeyboardButton("─────────────", callback_data="model_none")])

    # Добавляем модели OpenRouter, если доступны
    if openrouter_available and settings.OPENROUTER_AVAILABLE_MODELS:
        buttons, model_index = _generate_model_buttons(
            settings.OPENROUTER_AVAILABLE_MODELS,
            current_model,
            model_index,
            is_openrouter=True
        )
        keyboard.extend(buttons)

    # Формируем текст с информацией о текущей модели
    is_current_openrouter = "/" in current_model if current_model else False
    provider_name = "OpenRouter" if is_current_openrouter else "Google Gemini"
    text = f"**Выберите модель для разговора:**\n\n"
    text += f"**Текущая модель:** `{current_model}`\n"
    text += f"**Провайдер:** {provider_name}\n\n"
    text += "Нажмите на модель для выбора."

    keyboard.append([InlineKeyboardButton("⬅️ Назад", callback_data="start_menu")])

    formatted_text, parse_mode = TelegramFormatter.format_text(text)
    return formatted_text, parse_mode, InlineKeyboardMarkup(keyboard)

async def _get_roles_hub_content(user_id, active_role_title, current_prompt):
    """Генерирует контент для главной страницы меню ролей (Hub)."""
    # Получаем количество кастомных ролей для бейджика
    custom_count_res = await db.db_query("SELECT COUNT(*) as count FROM user_roles WHERE user_id = $1", (user_id,))
    custom_count = custom_count_res[0]['count'] if custom_count_res else 0

    text = (
        f"🎭 **Управление ролями**\n\n"
        f"Ниже вы можете выбрать готовую роль или создать свою.\n"
        f"Роль определяет стиль общения и задачи бота.\n\n"
        f"🔋 **Активная роль:**\n"
        f"✨ **{active_role_title}**\n"
    )

    keyboard = []

    # 1. Кнопка сброса (если роль активна)
    if current_prompt:
         keyboard.append([InlineKeyboardButton("🛑 Отключить роль", callback_data="role_clear")])

    # 2. Основные разделы навигации
    keyboard.append([
        InlineKeyboardButton(f"📂 Мои роли ({custom_count})", callback_data="role_nav:my_roles"),
        InlineKeyboardButton("📚 Каталог ролей", callback_data="role_nav:system_roles")
    ])

    # 3. Быстрые действия
    keyboard.append([InlineKeyboardButton("➕ Создать новую роль", callback_data="role_create")])

    # 4. Назад
    keyboard.append([InlineKeyboardButton("⬅️ Назад в меню", callback_data="start_menu")])

    formatted_text, parse_mode = TelegramFormatter.format_text(text)
    return formatted_text, parse_mode, InlineKeyboardMarkup(keyboard)

async def _get_roles_details_content(user_id, role_key, active_role_key):
    """Генерирует контент для просмотра деталей роли."""
    if not role_key:
        return "Ошибка: не указана роль", None, None

    # Ищем данные роли
    title = "Неизвестная роль"
    prompt = ""
    is_custom = False
    role_id = None # Для кастомных

    if role_key.startswith("user_role:"):
        # Кастомная роль
        try:
            r_id = int(role_key.split(":")[1])
            res = await db.db_query("SELECT id, title, prompt FROM user_roles WHERE id = $1 AND user_id = $2", (r_id, user_id))
            if res:
                title = res[0]['title']
                prompt = res[0]['prompt']
                is_custom = True
                role_id = r_id
            else:
                return "Роль не найдена или удалена.", None, None
        except:
             return "Ошибка ключа роли", None, None
    else:
        # Системная роль
        if role_key in prompts.DEFAULT_ROLES:
            meta = prompts.DEFAULT_ROLES[role_key]
            title = meta.get('title', role_key)
            prompt = meta.get('prompt', '')
        else:
            return "Системная роль не найдена", None, None

    is_active = (role_key == active_role_key)
    status_icon = "✅" if is_active else "⚪️"
    status_text = "АКТИВНА" if is_active else "Не активна"

    preview_len = 150
    prompt_preview = prompt[:preview_len] + "..." if len(prompt) > preview_len else prompt

    text = (
        f"ℹ️ **Детали роли**\n\n"
        f"🏷 **Название:** {title}\n"
        f"🔋 **Статус:** {status_icon} {status_text}\n\n"
        f"📝 **Промпт (отрывок):**\n*{prompt_preview}*\n"
    )

    keyboard = []

    # 1. Применить/Снять
    if is_active:
         keyboard.append([InlineKeyboardButton("🛑 Отключить эту роль", callback_data="role_clear")])
    else:
         keyboard.append([InlineKeyboardButton("✅ Применить роль", callback_data=f"role_apply:{role_key}")])

    # 2. Действия над ролью
    row_actions = []
    row_actions.append(InlineKeyboardButton("👁️ Промпт", callback_data=f"role_view_prompt:{role_key}"))

    if is_custom:
        row_actions.append(InlineKeyboardButton("✏️ Переим.", callback_data=f"role_rename_pick:{role_id}"))
        row_actions.append(InlineKeyboardButton("🗑️ Удалить", callback_data=f"role_delete_ask:{role_id}"))

    keyboard.append(row_actions)

    # 3. Назад
    # Определяем, куда возвращаться (в Мои или Системные)
    back_view = "my_roles" if is_custom else "system_roles"
    keyboard.append([InlineKeyboardButton("⬅️ Назад к списку", callback_data=f"role_nav:{back_view}")])

    formatted_text, parse_mode = TelegramFormatter.format_text(text)
    return formatted_text, parse_mode, InlineKeyboardMarkup(keyboard)

async def _get_roles_list_content(user_id, view_mode, page, active_role_key):
    """Генерирует контент для списков ролей (мои роли, системные роли)."""
    ITEMS_PER_PAGE = 6
    if view_mode == "my_roles":
        roles = await db.db_query(
            "SELECT id, title FROM user_roles WHERE user_id = $1 ORDER BY created_at DESC",
            (user_id,)
        )
        title_header = "📂 **Ваши личные роли**"
        empty_text = "У вас пока нет сохраненных ролей."

        # Формируем items для пагинатора
        items = []
        for r in roles:
            key = f"user_role:{r['id']}"
            is_active = "✅ " if key == active_role_key else ""
            items.append({
                'text': f"{is_active}{r['title']}",
                'callback': f"role_detail:{key}",
                'delete_callback': None
            })

    elif view_mode == "system_roles":
        title_header = "📚 **Каталог встроенных ролей**"
        empty_text = "Список стандартных ролей пуст (странно!)."

        items = []
        for key, meta in prompts.DEFAULT_ROLES.items():
            is_active = "✅ " if key == active_role_key else ""
            items.append({
                'text': f"{is_active}{meta.get('title', key)}",
                'callback': f"role_detail:{key}",
                'delete_callback': None
            })
    else:
        return f"Ошибка режима: {view_mode}", None, None

    # Пагинация
    total_items = len(items)
    total_pages = (total_items + ITEMS_PER_PAGE - 1) // ITEMS_PER_PAGE

    # Корректируем страницу если вышли за пределы
    if page < 0: page = 0
    if page >= total_pages and total_pages > 0: page = total_pages - 1

    start_idx = page * ITEMS_PER_PAGE
    end_idx = start_idx + ITEMS_PER_PAGE
    current_items = items[start_idx:end_idx]

    text = (
        f"{title_header}\n"
        f"Страница {page + 1} из {max(1, total_pages)}\n\n"
    )
    if not items:
        text += f"_{empty_text}_"

    keyboard = []

    for item in current_items:
        # Одна широкая кнопка
        keyboard.append([InlineKeyboardButton(item['text'], callback_data=item['callback'])])

    # Кнопки пагинации
    nav_row = []
    if total_pages > 1:
        if page > 0:
            nav_row.append(InlineKeyboardButton("⬅️", callback_data=f"role_page:{view_mode}:{page-1}"))
        else:
            nav_row.append(InlineKeyboardButton("⏺️", callback_data="noop")) # Placeholder

        nav_row.append(InlineKeyboardButton(f"{page+1}/{total_pages}", callback_data="noop"))

        if page < total_pages - 1:
            nav_row.append(InlineKeyboardButton("➡️", callback_data=f"role_page:{view_mode}:{page+1}"))
        else:
            nav_row.append(InlineKeyboardButton("⏺️", callback_data="noop")) # Placeholder

    if nav_row:
        keyboard.append(nav_row)

    # Кнопки управления (только для "Мои роли")
    if view_mode == "my_roles":
        keyboard.append([InlineKeyboardButton("➕ Создать", callback_data="role_create")])

    # Кнопка Назад (в Хаб)
    keyboard.append([InlineKeyboardButton("↩️ Назад в меню ролей", callback_data="role_nav:hub")])

    formatted_text, parse_mode = TelegramFormatter.format_text(text)
    return formatted_text, parse_mode, InlineKeyboardMarkup(keyboard)

async def get_roles_menu_content(user_id, chat_state, view_mode="hub", page=0, role_key=None):
    """
    Генерирует контент для меню ролей в стиле "Smart Hub".
    view_mode: 'hub' | 'my_roles' | 'system_roles' | 'role_details'
    page: номер страницы для списков
    role_key: ключ роли для просмотра деталей (id для кастомных, key для системных)
    """

    # 1. Определяем активную роль
    current_prompt = chat_state.system_prompt
    active_role_title = "👤 Базовая (без роли)"
    active_role_key = None

    # Пытаемся найти название активной роли
    if current_prompt:
        for key, meta in prompts.DEFAULT_ROLES.items():
            if meta.get("prompt") == current_prompt:
                active_role_title = f"{meta.get('title', key)}"
                active_role_key = key
                break

        if not active_role_key:
            # Ищем в кастомных
            custom_roles = await db.db_query(
                "SELECT id, title, prompt FROM user_roles WHERE user_id = $1",
                (user_id,)
            )
            for role in custom_roles:
                if role.get("prompt") == current_prompt:
                    active_role_title = f"🎭 {role['title']}"
                    active_role_key = f"user_role:{role['id']}"
                    break

            # Если всё ещё не нашли, но промпт есть
            if not active_role_key:
                active_role_title = "📝 Пользовательская инструкция"
                active_role_key = "custom_prompt"

    # ==========================
    # DISPATCH TO HELPERS
    # ==========================
    if view_mode == "hub":
        return await _get_roles_hub_content(user_id, active_role_title, current_prompt)

    elif view_mode == "role_details":
        return await _get_roles_details_content(user_id, role_key, active_role_key)

    elif view_mode in ["my_roles", "system_roles"]:
        return await _get_roles_list_content(user_id, view_mode, page, active_role_key)

    else:
        return f"Ошибка режима: {view_mode}", None, None

async def get_metrics_content():
    """Generates the metrics report text."""
    # Получаем все данные
    data = await get_system_status_data()
    metrics = data['metrics_summary']
    gemini_data = data['gemini']
    tavily_data = data['tavily']

    # Формируем основной текст
    text = (
        "📊 *Полная сводка системы:*\n\n"
        "*🚀 Производительность:*\n"
        f"• Всего запросов: `{metrics['total_requests']}`\n"
        f"• Среднее время ответа: `{metrics['average_response_time']:.2f}s`\n"
        f"• Процент ошибок: `{metrics['error_rate']:.1f}%`\n"
        f"• Попадания в кэш: `{metrics['cache_hit_rate']:.1f}%`\n"
        f"• Поисковых запросов: `{metrics['search_queries']}`\n\n"
    )

    # Добавляем использование API и моделей
    if metrics.get('api_calls'):
        text += "*🔌 Использование API:*\n"
        for api, count in metrics['api_calls'].items():
            if isinstance(api, str) and isinstance(count, (int, float)):
                text += f"• {api}: `{count}`\n"
        text += "\n"

    if metrics.get('model_usage'):
        text += "*🤖 Использование моделей:*\n"
        for model, count in metrics['model_usage'].items():
            # Пропускаем записи, которые содержат имена файлов (это ошибки в логике)
            if isinstance(model, str) and isinstance(count, (int, float)) and not any(char in model for char in ['/', '\\', '.pdf', '.docx', '.doc']):
                text += f"• {model}: `{count}`\n"
        text += "\n"

    # Добавляем статус ключей Gemini
    if gemini_data['keys']:
        text += "*🔑 Статус ключей Gemini (сегодня):*\n"

        usage_map = gemini_data['usage_map']

        for key_row in gemini_data['keys']:
            display_name = format_key_for_display(key_row['api_key'])
            usage_data = usage_map.get(key_row['key_hash'], [])

            if not usage_data:
                text += f"• `{display_name}`: не использовался\n"
            else:
                for usage in usage_data:
                    model_name = usage['model_name']
                    count = usage['request_count']
                    limit = settings.DAILY_LIMITS.get(model_name, 'N/A')
                    text += f"• `{display_name}` ({model_name}): {count} / {limit}\n"
        text += f"Сброс лимитов: *{gemini_data['reset_time']}* по Киеву\n\n"

    # Добавляем статус кредитов Tavily
    if tavily_data['keys']:
        text += "*💳 Кредиты Tavily (текущий месяц):*\n"

        tavily_usage_map = tavily_data['usage_map']

        for key_row in tavily_data['keys']:
            display_name = format_key_for_display(key_row['api_key'])
            count = tavily_usage_map.get(key_row['key_hash'], 0)
            limit = settings.TAVILY_MONTHLY_CREDIT_LIMIT
            text += f"• `{display_name}`: {count} / {limit}\n"
        text += "Сброс лимитов: 1-го числа каждого месяца\n\n"

    # Добавляем историю за последние дни
    if metrics['daily_metrics']:
        text += "*📈 История за последние дни:*\n"
        for date_str, daily_data in list(metrics['daily_metrics'].items())[:5]:  # Последние 5 дней
            requests = daily_data.get('requests', 0)
            errors = daily_data.get('errors', 0)
            text += f"• {date_str}: {requests} запросов, {errors} ошибок\n"
        text += "\n"

    # Добавляем последние ошибки
    if metrics['recent_errors']:
        text += "*⚠️ Последние ошибки:*\n"
        for error in metrics['recent_errors'][:3]:  # Последние 3 ошибки
            text += f"• {error['type']}: {error['message'][:40]}...\n"

    # Add timestamp for live update feedback
    text += f"\n_Обновлено: {datetime.now().strftime('%H:%M:%S UTC')}_"

    return text

async def get_documents_menu_content(user_id):
    documents = await get_user_documents(user_id)

    if not documents:
        text = (
            "📋 *Ваши документы*\n\n"
            "У вас пока нет загруженных документов.\n\n"
            "💡 *Как загрузить документ:*\n"
            "• Отправьте PDF или DOCX файл\n"
            "• Максимальный размер: 50MB\n"
            "• После загрузки вы сможете задавать вопросы по содержимому\n\n"
            "📋 *Политика хранения:*\n"
            "• Максимум документов: 5\n"
            "• Срок хранения: 3 дня"
        )
    else:
        text = "📋 *Ваши документы:*\n\n"
        for i, doc in enumerate(documents[:10], 1):
            text += f"{i}. *{doc['filename']}*\n"
            text += f"   📄 Страниц: {doc['pages']}\n"
            text += f"   📅 Загружен: {doc['created_at'][:10]}\n"
            text += f"   📊 Размер: {doc['file_size']:,} символов\n\n"
        if len(documents) > 10:
            text += f"... и еще {len(documents) - 10} документов\n\n"
        text += (
            "💡 *Действия:*\n"
            "• Отправьте новый документ для загрузки\n"
            "• Задайте вопрос по последнему документу\n"
            "• Используйте кнопки под сообщениями для управления\n\n"
            "📋 *Политика хранения:*\n"
            "• Максимум документов: 5\n"
            "• Срок хранения: 3 дня"
        )

    keyboard = [
        [InlineKeyboardButton("📄 Загрузить новый документ", callback_data="doc:upload_new")],
        [InlineKeyboardButton("📋 Выбрать документ", callback_data="doc:select_document")],
        [InlineKeyboardButton("🗑️ Очистить все документы", callback_data="doc:clear_all")]
    ]
    formatted_text, parse_mode = TelegramFormatter.format_text(text)
    return formatted_text, parse_mode, InlineKeyboardMarkup(keyboard)

async def get_conversations_menu_content(user_id, page=1):
    limit = 5
    offset = (page - 1) * limit

    conversations = await db.get_user_conversations(user_id, limit, offset)
    total_count = await db.get_conversation_count(user_id)

    if not conversations:
        return "📝 У вас пока нет сохранённых бесед.\n\nИспользуйте /save <название> для сохранения текущей беседы.", None, None

    text = f"📝 *Сохранённые беседы* (страница {page})\n\n"

    for conv in conversations:
        role_info = f" | {conv['role_title']}" if conv['role_title'] else ""
        created = conv['created_at'].strftime("%d.%m.%Y %H:%M") if conv['created_at'] else "Неизвестно"
        text += f"🆔 *{conv['id']}* | {conv['title']}{role_info}\n"
        text += f"📅 {created} | 💬 {conv['token_budget'] or 0} токенов\n\n"

    # Кнопки навигации
    keyboard = []
    nav_row = []
    if page > 1:
        nav_row.append(InlineKeyboardButton("⬅️ Предыдущая", callback_data=f"conv_page:{page-1}"))
    if len(conversations) == limit and offset + limit < total_count:
        nav_row.append(InlineKeyboardButton("➡️ Следующая", callback_data=f"conv_page:{page+1}"))

    if nav_row:
        keyboard.append(nav_row)

    # Кнопки действий
    if conversations:
        keyboard.append([InlineKeyboardButton("🔄 Переключиться", callback_data="conv_switch")])
        keyboard.append([InlineKeyboardButton("✏️ Переименовать", callback_data="conv_rename")])
        keyboard.append([InlineKeyboardButton("🗑️ Удалить", callback_data="conv_delete")])

    return text, 'Markdown', InlineKeyboardMarkup(keyboard)
