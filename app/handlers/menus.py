import logging
from datetime import datetime

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from app.config import get_model_hash, get_openrouter_keys, settings
from app.document_processor import get_user_documents
from app.i18n import t
from app.metrics import get_system_status_data
from app.prompt_registry import DEFAULT_ROLES
from app.repos.conversations import (
    get_conversation_count,
    get_role_data,
    get_user_conversations,
)
from app.repos.roles import (
    get_custom_role_count,
    get_user_custom_roles,
    get_user_custom_roles_full,
)
from app.repos.user_stats import get_user_activity_summary
from app.utils.formatting import TelegramFormatter, format_key_for_display


async def get_start_menu_content(chat_state, user_id=None):
    search_icon = "🟢" if chat_state.search_enabled else "🔴"

    # Compact status line
    prompt_label = f"🎭 {chat_state.system_prompt[:40]}…" if chat_state.system_prompt else ""
    status_parts = [f"`{chat_state.model}`", f"Поиск: {search_icon}"]
    if prompt_label:
        status_parts.append(prompt_label)
    status_line = " · ".join(status_parts)

    # Fetch user activity (only if user_id provided)
    req_count = 0
    activity_line = ""
    if user_id:
        try:
            activity_summary = await get_user_activity_summary(user_id)
            req_count = activity_summary["req_count"]
            doc_count = activity_summary["doc_count"]
            conv_count = activity_summary["conv_count"]

            if req_count > 0 or doc_count > 0 or conv_count > 0:
                activity_line = f"📈 Сегодня: {req_count} запр. · {doc_count} док. · {conv_count} бесед\n\n"
        except Exception as e:
            logging.debug("Could not fetch user context for /start: %s", e)

    # Adaptive hint: new user vs returning
    if req_count == 0 and not chat_state.system_prompt:
        hint = "💡 **Совет:** попробуйте 🎭 **Роли** — бот ответит как эксперт. Или отправьте 🖼️ картинку для анализа."
    else:
        hint = "Напишите сообщение — и я отвечу. 👇"

    start_text = f"🤖 **Gemini Bot** — ваш AI-ассистент\n\n📊 {status_line}\n\n{activity_line}{hint}"

    formatted_text, parse_mode = TelegramFormatter.format_text(start_text)

    keyboard = [
        [
            InlineKeyboardButton(t("menu.new_chat"), callback_data="new_chat"),
        ],
        [
            InlineKeyboardButton(t("menu.model"), callback_data="model_menu"),
            InlineKeyboardButton(t("menu.roles"), callback_data="open_roles"),
        ],
        [
            InlineKeyboardButton(t("menu.documents"), callback_data="open_documents"),
            InlineKeyboardButton(t("menu.conversations"), callback_data="open_conversations"),
        ],
        [
            InlineKeyboardButton(f"{t('menu.search_toggle')}: {search_icon}", callback_data="toggle_search"),
            InlineKeyboardButton(t("menu.help"), callback_data="help"),
        ],
    ]

    return formatted_text, parse_mode, InlineKeyboardMarkup(keyboard)


# Model descriptions for decision support
MODEL_HINTS = {
    "gemini-3-flash-preview": "💎 Флагманская модель — максимальный интеллект",
    "gemini-3.1-flash-lite-preview": "🧠 Самая умная из доступных — сложные задачи и код",
    "gemini-2.5-flash": "⚡ Быстрая — баланс скорости и качества",
    "gemini-2.5-flash-lite": "💨 Самая лёгкая — мгновенные ответы",
}


def _generate_model_buttons(models, current_model, start_index, is_openrouter=False):
    """
    Генерирует list кнопок for выбора models.

    Args:
        models (list): Список моделей.
        current_model (str): Текущая выбранная model.
        start_index (int): Начальный индекс for callback_data.
        is_openrouter (bool): Флаг, указывающий на использование OpenRouter.

    Returns:
        tuple: (list строк кнопок, следующий индекс)
    """
    rows = []
    current_index = start_index

    for m in models:
        # Check, выбрана ли model
        is_selected = m == current_model
        selected_mark = "✅ " if is_selected else ""

        # Определяем отображение
        if is_openrouter:
            display_name = m.split("/")[-1] if "/" in m else m
            icon = "🌐"
        else:
            display_name = m
            icon = "🤖"

        # Build кнопку
        model_hash = get_model_hash(m)
        text = f"{selected_mark}{icon} {display_name}"
        callback_data = f"model:{current_index}:{model_hash}"

        rows.append([InlineKeyboardButton(text, callback_data=callback_data)])
        current_index += 1

    return rows, current_index


def get_model_menu_content(chat_state, context):
    current_model = chat_state.model

    # Определяем, какой провайдер используется for текущей models
    openrouter_available = bool(get_openrouter_keys())

    # Create единый list всех моделей for индексации
    all_models = []
    if settings.AVAILABLE_MODELS:
        all_models.extend(settings.AVAILABLE_MODELS)
    if openrouter_available and settings.OPENROUTER_AVAILABLE_MODELS:
        all_models.extend(settings.OPENROUTER_AVAILABLE_MODELS)

    if not all_models:
        from app.utils.keyboards import error_with_back_keyboard

        return (
            "❌ Нет доступных моделей. Проверьте настройки.",
            None,
            error_with_back_keyboard("start_menu", "⬅️ Меню"),
        )

    # Save маппинг моделей в context for использования в callback
    if context and hasattr(context, "user_data"):
        # Ensure user_data exists if it's None (though ContextTypes usually ensures it's a dict-like)
        if context.user_data is None:
            context.user_data = {}
        context.user_data["model_list"] = all_models

    keyboard = []
    model_index = 0

    # Add models Gemini
    if settings.AVAILABLE_MODELS:
        buttons, model_index = _generate_model_buttons(
            settings.AVAILABLE_MODELS, current_model, model_index, is_openrouter=False
        )
        keyboard.extend(buttons)

    # Add разделитель, if есть оба провайдера
    if settings.AVAILABLE_MODELS and openrouter_available and settings.OPENROUTER_AVAILABLE_MODELS:
        keyboard.append([InlineKeyboardButton("─────────────", callback_data="model_none")])

    # Add models OpenRouter, if доступны
    if openrouter_available and settings.OPENROUTER_AVAILABLE_MODELS:
        buttons, model_index = _generate_model_buttons(
            settings.OPENROUTER_AVAILABLE_MODELS,
            current_model,
            model_index,
            is_openrouter=True,
        )
        keyboard.extend(buttons)

    # Build text with model hint for decision support
    is_current_openrouter = "/" in current_model if current_model else False
    provider_name = "OpenRouter" if is_current_openrouter else "Google Gemini"

    text = "🧠 **Выбор модели**\n\n"
    text += f"Текущая: `{current_model}`\n"

    # Show hint for current model
    hint = MODEL_HINTS.get(current_model, "")
    if hint:
        text += f"→ {hint}\n"

    text += f"\nПровайдер: {provider_name}\n"

    # Recommendation for undecided users
    if len(all_models) > 1:
        # Find best recommendation
        rec = None
        for m in ["gemini-2.5-flash", "gemini-2.5-flash-preview-04-17"]:
            if m in all_models and m != current_model:
                rec = m
                break
        if rec:
            text += f"\n💡 Не знаете, что выбрать? `{rec}` — лучший баланс."

    keyboard.append([InlineKeyboardButton(t("menu.back"), callback_data="start_menu")])

    formatted_text, parse_mode = TelegramFormatter.format_text(text)
    return formatted_text, parse_mode, InlineKeyboardMarkup(keyboard)


async def _get_roles_hub_content(user_id, active_role_title, current_prompt):
    """Генерирует контент для главной страницы меню ролей (Hub)."""
    # Get количество кастомных ролей for бейджика
    custom_count = await get_custom_role_count(user_id)

    text = (
        f"🎭 **Роли**\n\n"
        f"Роль — это специализация бота. Выберите готовую "
        f"или создайте свою.\n\n"
        f"✨ Активная: **{active_role_title}**\n"
    )

    keyboard = []

    # 1. Quick-apply: top-3 preset roles for immediate use
    top_roles = list(DEFAULT_ROLES.items())[:3]
    if top_roles:
        quick_row = []
        for key, meta in top_roles:
            title = meta.get("title", key)
            quick_row.append(InlineKeyboardButton(title, callback_data=f"role_apply:{key}"))
        keyboard.append(quick_row)

    # 2. Browse catalogs
    keyboard.append(
        [
            InlineKeyboardButton("📚 Каталог ролей", callback_data="role_nav:system_roles"),
            InlineKeyboardButton(f"👤 Мои роли ({custom_count})", callback_data="role_nav:my_roles"),
        ]
    )

    # 3. Creation (AI + Manual)
    keyboard.append(
        [
            InlineKeyboardButton("✨ Сгенерировать", callback_data="role_create"),
            InlineKeyboardButton("📝 Написать", callback_data="role_create_manual"),
        ]
    )

    # 4. Reset (if role is active)
    if current_prompt:
        keyboard.append([InlineKeyboardButton("🔄 Сбросить к стандартной", callback_data="role_clear")])

    # 5. Back
    keyboard.append([InlineKeyboardButton(t("menu.back"), callback_data="start_menu")])

    formatted_text, parse_mode = TelegramFormatter.format_text(text)
    return formatted_text, parse_mode, InlineKeyboardMarkup(keyboard)


async def _get_roles_details_content(user_id, role_key, active_role_key):
    """Генерирует контент для просмотра деталей роли."""
    if not role_key:
        return "Ошибка: не указана роль", None, None

    # Ищем data roles via хелпер
    role_data = await get_role_data(role_key, user_id)
    if not role_data:
        return "Роль не найдена или удалена.", None, None

    title = role_data["title"]
    prompt = role_data["prompt"]
    is_custom = role_data["is_custom"]
    role_id = role_data["id"]

    is_active = role_key == active_role_key
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
        keyboard.append([InlineKeyboardButton("🔄 Сбросить роль", callback_data="role_clear")])
    else:
        keyboard.append([InlineKeyboardButton("▶️ Активировать", callback_data=f"role_apply:{role_key}")])

    # 2. Действия над roleю
    row_actions = []
    row_actions.append(InlineKeyboardButton("👁️ Промпт", callback_data=f"role_view_prompt:{role_key}"))

    if is_custom:
        row_actions.append(InlineKeyboardButton("✏️ Промпт", callback_data=f"role_edit_prompt:{role_key}"))

    keyboard.append(row_actions)

    # 2b. Second action row for custom roles: rename + delete
    if is_custom:
        row_actions2 = []
        row_actions2.append(InlineKeyboardButton("✏️ Переим.", callback_data=f"role_rename_pick:{role_id}"))
        row_actions2.append(InlineKeyboardButton("🗑️ Удалить", callback_data=f"role_delete_ask:{role_id}"))
        keyboard.append(row_actions2)

    # 3. Назад
    # Определяем, куда возвращаться (в Мои or Системные)
    back_view = "my_roles" if is_custom else "system_roles"
    keyboard.append([InlineKeyboardButton("⬅️ Назад к списку", callback_data=f"role_nav:{back_view}")])

    formatted_text, parse_mode = TelegramFormatter.format_text(text)
    return formatted_text, parse_mode, InlineKeyboardMarkup(keyboard)


async def _get_roles_list_content(user_id, view_mode, page, active_role_key):
    """Генерирует контент для списков ролей (мои роли, системные роли)."""
    ITEMS_PER_PAGE = 6
    if view_mode == "my_roles":
        roles = await get_user_custom_roles(user_id)
        title_header = "📂 **Ваши личные роли**"
        empty_text = "У вас пока нет сохраненных ролей."

        # Build items for пагинатора
        items = []
        for r in roles:
            key = f"user_role:{r['id']}"
            is_active = "✅ " if key == active_role_key else ""
            items.append(
                {
                    "text": f"{is_active}{r['title']}",
                    "callback": f"role_detail:{key}",
                    "delete_callback": None,
                }
            )

    elif view_mode == "system_roles":
        title_header = "📚 **Каталог встроенных ролей**"
        empty_text = "Список стандартных ролей пуст (странно!)."

        items = []
        for key, meta in DEFAULT_ROLES.items():
            is_active = "✅ " if key == active_role_key else ""
            items.append(
                {
                    "text": f"{is_active}{meta.get('title', key)}",
                    "callback": f"role_detail:{key}",
                    "delete_callback": None,
                }
            )
    else:
        return f"Ошибка режима: {view_mode}", None, None

    # Пагинация
    total_items = len(items)
    total_pages = (total_items + ITEMS_PER_PAGE - 1) // ITEMS_PER_PAGE

    # Корректируем страницу if вышли за пределы
    if page < 0:
        page = 0
    if page >= total_pages and total_pages > 0:
        page = total_pages - 1

    start_idx = page * ITEMS_PER_PAGE
    end_idx = start_idx + ITEMS_PER_PAGE
    current_items = items[start_idx:end_idx]

    text = f"{title_header}\nСтраница {page + 1} из {max(1, total_pages)}\n\n"
    if not items:
        text += f"_{empty_text}_"

    keyboard = []

    for item in current_items:
        # Одна широкая button
        keyboard.append([InlineKeyboardButton(item["text"], callback_data=item["callback"])])

    # Кнопки пагинации
    nav_row = []
    if total_pages > 1:
        if page > 0:
            nav_row.append(InlineKeyboardButton("⬅️", callback_data=f"role_page:{view_mode}:{page - 1}"))
        else:
            nav_row.append(InlineKeyboardButton("⏺️", callback_data="noop"))  # Placeholder

        nav_row.append(InlineKeyboardButton(f"{page + 1}/{total_pages}", callback_data="noop"))

        if page < total_pages - 1:
            nav_row.append(InlineKeyboardButton("➡️", callback_data=f"role_page:{view_mode}:{page + 1}"))
        else:
            nav_row.append(InlineKeyboardButton("⏺️", callback_data="noop"))  # Placeholder

    if nav_row:
        keyboard.append(nav_row)

    # Кнопки управления (only for "Мои roles")
    if view_mode == "my_roles":
        keyboard.append(
            [
                InlineKeyboardButton("✨ Сгенерировать", callback_data="role_create"),
                InlineKeyboardButton("📝 Написать", callback_data="role_create_manual"),
            ]
        )

    # Кнопка Назад (в Хаб)
    keyboard.append([InlineKeyboardButton("↩️ Назад в меню ролей", callback_data="role_nav:hub")])

    formatted_text, parse_mode = TelegramFormatter.format_text(text)
    return formatted_text, parse_mode, InlineKeyboardMarkup(keyboard)


async def get_roles_menu_content(user_id, chat_state, view_mode="hub", page=0, role_key=None):
    """
    Генерирует content for menu ролей в стиле "Smart Hub".
    view_mode: 'hub' | 'my_roles' | 'system_roles' | 'role_details'
    page: номер страницы for списков
    role_key: key roles for просмотра деталей (id for кастомных, key for системных)
    """

    # 1. Определяем активную role
    current_prompt = chat_state.system_prompt
    active_role_title = "👤 Базовая (без роли)"
    active_role_key = None

    # Пытаемся найти название активной roles
    if current_prompt:
        for key, meta in DEFAULT_ROLES.items():
            if meta.get("prompt") == current_prompt:
                active_role_title = f"{meta.get('title', key)}"
                active_role_key = key
                break

        if not active_role_key:
            # Ищем в кастомных
            custom_roles = await get_user_custom_roles_full(user_id)
            for role in custom_roles:
                if role.get("prompt") == current_prompt:
                    active_role_title = f"🎭 {role['title']}"
                    active_role_key = f"user_role:{role['id']}"
                    break

            # If всё ещё не нашли, но промпт есть
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
    # Get все data
    data = await get_system_status_data()
    metrics = data["metrics_summary"]
    gemini_data = data["gemini"]
    tavily_data = data["tavily"]

    parts = []

    # Build main text
    parts = []
    parts.append(
        "📊 *Полная сводка системы:*\n\n"
        "*🚀 Производительность:*\n"
        f"• Всего запросов: `{metrics['total_requests']}`\n"
        f"• Среднее время ответа: `{metrics['average_response_time']:.2f}s`\n"
        f"• Процент ошибок: `{metrics['error_rate']:.1f}%`\n"
        f"• Попадания в кэш: `{metrics['cache_hit_rate']:.1f}%`\n"
        f"• Поисковых запросов: `{metrics['search_queries']}`\n\n"
    )

    # Add использование API и моделей
    if metrics.get("api_calls"):
        parts.append("*🔌 Использование API:*\n")
        for api, count in metrics["api_calls"].items():
            if isinstance(api, str) and isinstance(count, (int, float)):
                parts.append(f"• {api}: `{count}`\n")
        parts.append("\n")

    if metrics.get("model_usage"):
        parts.append("*🤖 Использование моделей:*\n")
        for model, count in metrics["model_usage"].items():
            # Пропускаем записи, которые содержат имена fileов (это ошибки в логике)
            if (
                isinstance(model, str)
                and isinstance(count, (int, float))
                and not any(char in model for char in ["/", "\\", ".pdf", ".docx", ".doc"])
            ):
                parts.append(f"• {model}: `{count}`\n")
        parts.append("\n")

    # Add статус keyей Gemini
    if gemini_data["keys"]:
        parts.append("*🔑 Статус ключей Gemini (сегодня):*\n")

        usage_map = gemini_data["usage_map"]

        for key_row in gemini_data["keys"]:
            display_name = format_key_for_display(key_row["api_key"])
            usage_data = usage_map.get(key_row["key_hash"], [])

            if not usage_data:
                parts.append(f"• `{display_name}`: не использовался\n")
            else:
                for usage in usage_data:
                    model_name = usage["model_name"]
                    count = usage["request_count"]
                    limit = settings.DAILY_LIMITS.get(model_name, "N/A")
                    parts.append(f"• `{display_name}` ({model_name}): {count} / {limit}\n")
        parts.append(f"Сброс лимитов: *{gemini_data['reset_time']}* по Киеву\n\n")

    # Add статус кредитов Tavily
    if tavily_data["keys"]:
        parts.append("*💳 Кредиты Tavily (текущий месяц):*\n")

        tavily_usage_map = tavily_data["usage_map"]

        for key_row in tavily_data["keys"]:
            display_name = format_key_for_display(key_row["api_key"])
            count = tavily_usage_map.get(key_row["key_hash"], 0)
            limit = settings.TAVILY_MONTHLY_CREDIT_LIMIT
            parts.append(f"• `{display_name}`: {count} / {limit}\n")
        parts.append("Сброс лимитов: 1-го числа каждого месяца\n\n")

    # Add history за afterдние дни
    if metrics["daily_metrics"]:
        parts.append("*📈 История за последние дни:*\n")
        for date_str, daily_data in list(metrics["daily_metrics"].items())[:5]:  # Последние 5 дней
            requests = daily_data.get("requests", 0)
            errors = daily_data.get("errors", 0)
            parts.append(f"• {date_str}: {requests} запросов, {errors} ошибок\n")
        parts.append("\n")

    # Add afterдние ошибки
    if metrics["recent_errors"]:
        parts.append("*⚠️ Последние ошибки:*\n")
        for error in metrics["recent_errors"][:3]:  # Последние 3 ошибки
            parts.append(f"• {error['type']}: {error['message'][:40]}...\n")

    # Add timestamp for live update feedback
    parts.append(f"\n_Обновлено: {datetime.now().strftime('%H:%M:%S UTC')}_")

    return "".join(parts)


async def get_documents_menu_content(user_id):
    documents = await get_user_documents(user_id)

    if not documents:
        text = (
            "📄 **Документы**\n\n"
            "Загрузите PDF или DOCX — и задавайте вопросы "
            "по содержимому.\n"
            "Бот ответит на основе вашего текста, "
            "а не общих знаний.\n\n"
            "📎 Отправьте файл прямо в чат."
        )
    else:
        parts = [f"📄 **Документы** ({len(documents)})\n\n"]
        for i, doc in enumerate(documents[:10], 1):
            parts.append(
                f"{i}. **{doc['filename']}**\n"
                f"   📄 Страниц: {doc['pages']}\n"
                f"   📅 Загружен: {doc['created_at'][:10]}\n"
                f"   📊 Размер: {doc['file_size']:,} символов\n\n"
            )
        if len(documents) > 10:
            parts.append(f"… и ещё {len(documents) - 10} документов\n\n")
        parts.append("📎 Отправьте новый файл для загрузки.")
        text = "".join(parts)

    keyboard = [
        [InlineKeyboardButton("📄 Загрузить новый документ", callback_data="doc:upload_new")],
        [InlineKeyboardButton("📋 Выбрать документ", callback_data="doc:select_document")],
        [InlineKeyboardButton("🗑️ Удалить все документы", callback_data="doc:clear_all")],
        [InlineKeyboardButton(t("menu.back"), callback_data="start_menu")],
    ]
    formatted_text, parse_mode = TelegramFormatter.format_text(text)
    return formatted_text, parse_mode, InlineKeyboardMarkup(keyboard)


async def get_conversations_menu_content(user_id, page=1):
    limit = 5
    offset = (page - 1) * limit

    conversations = await get_user_conversations(user_id, limit, offset)
    total_count = await get_conversation_count(user_id)

    if not conversations:
        empty_text = (
            "💬 **Сохранённые беседы**\n\n"
            "Сохраняйте важные диалоги и возвращайтесь "
            "к ним в любой момент.\n\n"
            "💡 Используйте /save после важного разговора."
        )
        formatted_empty, pm = TelegramFormatter.format_text(empty_text)
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Назад", callback_data="start_menu")]])
        return formatted_empty, pm, kb

    parts = [f"📝 *Сохранённые беседы* (страница {page})\n\n"]

    for conv in conversations:
        role_info = f" | {conv['role_title']}" if conv["role_title"] else ""
        created = conv["created_at"].strftime("%d.%m.%Y %H:%M") if conv["created_at"] else "Неизвестно"
        parts.append(
            f"🆔 *{conv['id']}* | {conv['title']}{role_info}\n📅 {created} | 💬 {conv['token_budget'] or 0} токенов\n\n"
        )
    text = "".join(parts)

    # Кнопки навигации
    keyboard = []
    nav_row = []
    if page > 1:
        nav_row.append(InlineKeyboardButton("⬅️ Предыдущая", callback_data=f"conv_page:{page - 1}"))
    if len(conversations) == limit and offset + limit < total_count:
        nav_row.append(InlineKeyboardButton("➡️ Следующая", callback_data=f"conv_page:{page + 1}"))

    if nav_row:
        keyboard.append(nav_row)

    # Кнопки действий
    if conversations:
        keyboard.append([InlineKeyboardButton("🔄 Переключиться", callback_data="conv_switch")])
        keyboard.append([InlineKeyboardButton("✏️ Переименовать", callback_data="conv_rename")])
        keyboard.append([InlineKeyboardButton("🗑️ Удалить", callback_data="conv_delete")])

    # Кнопка Назад
    keyboard.append([InlineKeyboardButton("⬅️ Назад", callback_data="start_menu")])

    return text, "Markdown", InlineKeyboardMarkup(keyboard)
