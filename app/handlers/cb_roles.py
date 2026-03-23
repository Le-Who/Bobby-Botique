"""
Role management callbacks — apply, clear, create, delete, rename, detail, nav, page.
"""

import logging

import telegram
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from app.config import settings
from app.handlers import menus
from app.metrics import role_conv_metrics
from app.prompt_registry import get_registry
from app.repos.chats import get_user_chat, update_user_chat
from app.repos.conversations import get_role_data
from app.repos.roles import (
    create_custom_role,
    delete_custom_role,
    get_custom_role_prompt,
    get_user_custom_roles,
)
from app.repos.users import is_authorized as _is_authorized
from app.state import (
    begin_custom_role_creation,
    clear_custom_role_state,
    get_generated_role,
    get_last_custom_role_prompt,
    set_generated_role,
    set_generating_custom_role,
    set_last_custom_role_prompt,
)
from app.utils.formatting import TelegramFormatter
from app.utils.json_utils import extract_json_object


class DummyUpdate:
    """Helper class to mock an Update object for calling commands from callbacks."""

    def __init__(self, msg, user) -> None:
        self.message = msg
        self.effective_user = user


async def role_rename_menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    if not await _is_authorized(user_id):
        return
    roles = await get_user_custom_roles(user_id)
    if not roles:
        # UX: Add back button even for empty state
        await query.edit_message_text(
            "У вас пока нет кастомных ролей.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Назад", callback_data="open_roles")]]),
        )
        return
    buttons = []
    for r in roles:
        buttons.append([InlineKeyboardButton(f"✏️ {r['title']}", callback_data=f"role_rename_pick:{r['id']}")])

    # UX: Add Back button and use edit_message_text
    buttons.append([InlineKeyboardButton("⬅️ Назад", callback_data="open_roles")])
    await query.edit_message_text("Выберите роль для переименования:", reply_markup=InlineKeyboardMarkup(buttons))


async def role_rename_pick_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    if not await _is_authorized(user_id):
        return
    role_id = int(query.data.split(":")[1])
    if context.user_data is not None:
        context.user_data["rename_role_id"] = role_id
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("↩️ Отмена", callback_data="role_rename_cancel")]])
    await query.edit_message_text("✏️ Введите новое название роли одной строкой:", reply_markup=kb)


async def start_menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()

    user_id = query.from_user.id
    chat_state = await get_user_chat(user_id)

    formatted_text, parse_mode, reply_markup = await menus.get_start_menu_content(chat_state)

    await query.edit_message_text(formatted_text, parse_mode=parse_mode, reply_markup=reply_markup)


async def role_apply_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    user_id = query.from_user.id
    chat_state = await get_user_chat(user_id)
    key = query.data.split(":", 1)[1]
    role_title = ""

    # Get data roles via хелпер
    role_data = await get_role_data(key, user_id)

    if not role_data:
        await query.answer("❌ Роль не найдена.")
        return

    if not role_data.get("prompt"):
        await query.answer("❌ Выбранная роль содержит некорректный промпт.")
        return

    chat_state.system_prompt = role_data["prompt"]
    role_title = role_data["title"]

    # Write метрику (use key for консистентности)
    await role_conv_metrics.record_role_application(role_data["key"])

    # Save state
    await update_user_chat(user_id, chat_state)

    # Update menu - возвращаемся в Hub
    text, parse_mode, reply_markup = await menus.get_roles_menu_content(user_id, chat_state, view_mode="hub")
    try:
        await query.edit_message_text(text, parse_mode=parse_mode, reply_markup=reply_markup)
    except telegram.error.BadRequest as e:
        if "Message is not modified" not in str(e):
            raise e
    await query.answer(f"✅ Роль '{role_title}' применена.")


async def role_clear_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    user_id = query.from_user.id
    chat_state = await get_user_chat(user_id)
    # Clean up системный промпт (будет использован базовый)
    chat_state.system_prompt = None
    await update_user_chat(user_id, chat_state)
    await role_conv_metrics.record_role_clear()

    # Update menu
    # Update menu - возвращаемся в Hub
    text, parse_mode, reply_markup = await menus.get_roles_menu_content(user_id, chat_state, view_mode="hub")
    try:
        await query.edit_message_text(text, parse_mode=parse_mode, reply_markup=reply_markup)
    except telegram.error.BadRequest as e:
        if "Message is not modified" not in str(e):
            raise e
    await query.answer("🧹 Роль сброшена.")


async def role_create_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    begin_custom_role_creation(query.from_user.id)
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("↩️ Отмена", callback_data="role_create_cancel")]])
    await query.edit_message_text(
        "📝 Опишите, какую роль хотите создать (1–2 предложения):",
        reply_markup=kb,
    )


async def role_create_cancel_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Отмена создания кастомной роли — возврат в hub."""
    query = update.callback_query
    await query.answer("❌ Создание роли отменено")
    user_id = query.from_user.id
    clear_custom_role_state(user_id)
    chat_state = await get_user_chat(user_id)
    text, parse_mode, reply_markup = await menus.get_roles_menu_content(user_id, chat_state, view_mode="hub")
    try:
        await query.edit_message_text(text, parse_mode=parse_mode, reply_markup=reply_markup)
    except telegram.error.BadRequest as e:
        if "Message is not modified" not in str(e):
            raise e


async def role_rename_cancel_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Отмена переименования роли — возврат в hub."""
    query = update.callback_query
    await query.answer("❌ Переименование отменено")
    context.user_data.pop("rename_role_id", None)
    user_id = query.from_user.id
    chat_state = await get_user_chat(user_id)
    text, parse_mode, reply_markup = await menus.get_roles_menu_content(user_id, chat_state, view_mode="hub")
    try:
        await query.edit_message_text(text, parse_mode=parse_mode, reply_markup=reply_markup)
    except telegram.error.BadRequest as e:
        if "Message is not modified" not in str(e):
            raise e


async def role_custom_apply_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    chat_state = await get_user_chat(user_id)
    role = get_generated_role(user_id)
    if not role:
        from app.utils.keyboards import error_with_back_keyboard

        await query.edit_message_text(
            "❌ Нет сгенерированной роли для применения.",
            reply_markup=error_with_back_keyboard("open_roles", "🎭 Меню ролей"),
        )
        return
    prompt_text = role.get("prompt") or role.get("system_prompt") or ""
    # Save only промпт roles (without базового системного промпта)
    # compose_system_instruction будет вызван on использовании
    chat_state.system_prompt = prompt_text
    await update_user_chat(user_id, chat_state)
    clear_custom_role_state(user_id)
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("🎭 Меню ролей", callback_data="open_roles")]])
    await query.edit_message_text(
        f"✅ Роль '{role.get('title', 'Кастомная роль')}' применена.",
        reply_markup=kb,
    )


async def role_custom_save_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    role = get_generated_role(user_id)
    if not role:
        from app.utils.keyboards import error_with_back_keyboard

        await query.edit_message_text(
            "❌ Нет сгенерированной роли для сохранения.",
            reply_markup=error_with_back_keyboard("open_roles", "🎭 Меню ролей"),
        )
        return
    # Save в user_roles
    try:
        # Save
        await create_custom_role(
            user_id,
            role.get("title", "Моя роль"),
            role.get("prompt") or role.get("system_prompt", ""),
        )
        await role_conv_metrics.record_custom_role_creation()
        # И сразу onменяем
        prompt_text = role.get("prompt") or role.get("system_prompt") or ""
        chat_state = await get_user_chat(user_id)
        # Save only промпт roles (without базового системного промпта)
        # compose_system_instruction будет вызван on использовании
        chat_state.system_prompt = prompt_text
        await update_user_chat(user_id, chat_state)
        clear_custom_role_state(user_id)
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("🎭 Меню ролей", callback_data="open_roles")]])
        await query.edit_message_text("💾 Роль сохранена и применена.", reply_markup=kb)
    except Exception as e:
        logging.error("Error saving custom role: %s", e, exc_info=True)
        from app.utils.keyboards import error_with_back_keyboard

        await query.edit_message_text(
            "❌ Ошибка сохранения роли. Попробуйте позже.",
            reply_markup=error_with_back_keyboard("open_roles", "🎭 Меню ролей"),
        )


async def role_custom_retry_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    last_prompt = get_last_custom_role_prompt(user_id)
    if not last_prompt:
        from app.utils.keyboards import error_with_back_keyboard

        await query.edit_message_text(
            "❌ Нет предыдущего запроса для повтора.",
            reply_markup=error_with_back_keyboard("open_roles", "🎭 Меню ролей"),
        )
        return
    # Запускаем повтор генерации как в messages.handle_request
    chat_state = await get_user_chat(user_id)

    # Используем универсальную функцию for получения keyа (поддерживает и Gemini, и OpenRouter)
    from app.handlers.ai_core import (
        _get_ai_response,
        _increment_key_usage,
        _resolve_ai_request,
    )

    model_for_role = chat_state.model or settings.DEFAULT_MODEL
    key_data, model_used, _ = await _resolve_ai_request(model_for_role)
    if not key_data:
        from app.utils.keyboards import error_with_back_keyboard

        await query.edit_message_text(
            "❌ Нет доступных ключей API для генерации роли.",
            reply_markup=error_with_back_keyboard("open_roles", "🎭 Меню ролей"),
        )
        return
    progress_msg = await query.message.reply_text("🛠️ Генерирую роль…")
    set_generating_custom_role(user_id, True)
    history = [{"role": "user", "parts": [last_prompt]}]

    # Используем универсальную функцию for получения responseа (поддерживает и Gemini, и OpenRouter)
    response_text, _ = await _get_ai_response(
        key_data["api_key"],
        history,
        model_used,
        system_instruction=get_registry().get("prompt_engineer").text,
        user_id=user_id,
        chat_id=user_id,
    )

    # Инкрементируем использование keyа
    await _increment_key_usage(key_data["key_hash"], model_used)

    # Log response models for отладки
    logging.info("Model response for role retry: %s...", response_text[:500])

    role_obj = extract_json_object(response_text)
    if not role_obj:
        # Processing явной 503 ошибки from textа
        if "503" in (response_text or "") or "unavailable" in (response_text or "").lower():
            await progress_msg.edit_text("🔄 Сервер перегружен. Попробуйте ещё раз через несколько секунд.")
        else:
            logging.error("Failed to parse role JSON on retry. Response: %s", response_text)
            await progress_msg.edit_text("❌ Снова не удалось сгенерировать роль. Попробуйте изменить описание.")
        set_generating_custom_role(user_id, False)
        return
    set_last_custom_role_prompt(user_id, last_prompt)

    set_generated_role(user_id, role_obj)
    title = role_obj.get("title", "Кастомная роль")
    purpose = role_obj.get("purpose", "")
    style = ", ".join(role_obj.get("style", [])[:3])
    preview = f"🆕 *Новая роль:* {title}\n\n🎯 Цель: {purpose}\n🧭 Стиль: {style}\n\nПрименить сейчас или сохранить?"
    kb = [
        [InlineKeyboardButton("✅ Применить", callback_data="role_custom_apply")],
        [InlineKeyboardButton("💾 Сохранить", callback_data="role_custom_save")],
        [InlineKeyboardButton("❌ Отмена", callback_data="role_clear")],
    ]
    formatted_text, parse_mode = TelegramFormatter.format_text(preview)
    await progress_msg.edit_text(formatted_text, parse_mode=parse_mode, reply_markup=InlineKeyboardMarkup(kb))
    set_generating_custom_role(user_id, False)


async def role_delete_ask_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    role_id = query.data.split(":")[1]

    bg_text = "⚠️ *Удаление роли*\n\nВы уверены, что хотите удалить эту роль? Это действие нельзя отменить."
    kb = [
        [
            InlineKeyboardButton(
                "🗑️ Да, удалить навсегда",
                callback_data=f"role_delete_confirm:{role_id}",
            )
        ],
        [InlineKeyboardButton("❌ Отмена", callback_data=f"role_delete_cancel:{role_id}")],
    ]
    formatted, pm = TelegramFormatter.format_text(bg_text)
    await query.edit_message_text(formatted, parse_mode=pm, reply_markup=InlineKeyboardMarkup(kb))


async def role_delete_cancel_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    role_id = query.data.split(":")[1]

    # Returnся в детали roles
    user_id = query.from_user.id
    chat_state = await get_user_chat(user_id)

    text, parse_mode, reply_markup = await menus.get_roles_menu_content(
        user_id, chat_state, view_mode="role_details", role_key=f"user_role:{role_id}"
    )
    await query.edit_message_text(text, parse_mode=parse_mode, reply_markup=reply_markup)


async def role_delete_confirm_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    user_id = query.from_user.id
    if not await _is_authorized(user_id):
        await query.answer("❌ Нет доступа")
        return
    try:
        role_id = int(query.data.split(":")[1])
        # Check, не активна ли эта role сейчас
        chat_state = await get_user_chat(user_id)

        # Get промпт удаляемой roles, чтобы проверить, активна ли она
        role_prompt = await get_custom_role_prompt(role_id, user_id)

        await delete_custom_role(role_id, user_id)

        # If удаляемая role была активна, сбрасываем ее
        if role_prompt and chat_state.system_prompt == role_prompt:
            chat_state.system_prompt = None
            await update_user_chat(user_id, chat_state)

        # Update menu - переходим в list "Мои roles"
        text, parse_mode, reply_markup = await menus.get_roles_menu_content(
            user_id, chat_state, view_mode="my_roles", page=0
        )

        try:
            await query.edit_message_text(text, parse_mode=parse_mode, reply_markup=reply_markup)
        except telegram.error.BadRequest as e:
            if "Message is not modified" not in str(e):
                raise e

        await query.answer("🗑️ Роль удалена.")

    except Exception as e:
        logging.error("Error deleting role: %s", e, exc_info=True)
        await query.answer("❌ Ошибка удаления роли")


async def role_detail_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()

    user_id = query.from_user.id
    chat_state = await get_user_chat(user_id)

    role_key = query.data.split(":", 1)[1]

    text, parse_mode, reply_markup = await menus.get_roles_menu_content(
        user_id, chat_state, view_mode="role_details", role_key=role_key
    )

    try:
        await query.edit_message_text(text, parse_mode=parse_mode, reply_markup=reply_markup)
    except telegram.error.BadRequest as e:
        if "Message is not modified" not in str(e):
            raise e


async def role_view_prompt_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()

    role_key = query.data.split(":", 1)[1]
    user_id = query.from_user.id

    # Fetch role data using helper
    role_data = await get_role_data(role_key, user_id)
    prompt = role_data.get("prompt") if role_data else ""

    if prompt:
        from app.utils.keyboards import error_with_back_keyboard

        kb = error_with_back_keyboard(f"role_detail:{role_key}", "⬅️ Назад к роли")
        view_text = f"📝 *Полный промпт роли:*\n\n`{prompt}`"
        fmt_text, fmt_pm = TelegramFormatter.format_text(view_text)
        await query.edit_message_text(
            fmt_text,
            parse_mode=fmt_pm,
            reply_markup=kb,
        )
    else:
        from app.utils.keyboards import error_with_back_keyboard

        await query.edit_message_text(
            "❌ Не удалось найти промпт.",
            reply_markup=error_with_back_keyboard("open_roles", "🎭 Меню ролей"),
        )


# ── Edit prompt callbacks ────────────────────────────────────────────────────


async def role_edit_prompt_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Shows the full prompt (copyable) + buttons for manual replace / AI enhance."""
    query = update.callback_query
    await query.answer()

    role_key = query.data.split(":", 1)[1]
    user_id = query.from_user.id

    role_data = await get_role_data(role_key, user_id)
    if not role_data or not role_data.get("is_custom"):
        from app.utils.keyboards import error_with_back_keyboard

        await query.edit_message_text(
            "❌ Можно редактировать только кастомные роли.",
            reply_markup=error_with_back_keyboard("open_roles", "🎭 Меню ролей"),
        )
        return

    prompt = role_data.get("prompt", "")
    title = role_data.get("title", "")

    edit_text = (
        f"✏️ **Редактирование роли** «{title}»\n\n"
        f"📋 **Текущий промпт** (удерживайте для копирования):\n"
        f"`{prompt}`\n\n"
        f"Выберите способ редактирования:"
    )
    kb = InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("📝 Заменить вручную", callback_data=f"role_edit_manual:{role_key}")],
            [InlineKeyboardButton("✨ Улучшить через AI", callback_data=f"role_edit_ai:{role_key}")],
            [InlineKeyboardButton("↩️ Отмена", callback_data=f"role_detail:{role_key}")],
        ]
    )
    fmt_text, fmt_pm = TelegramFormatter.format_text(edit_text)
    await query.edit_message_text(fmt_text, parse_mode=fmt_pm, reply_markup=kb)


async def role_edit_manual_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Sets up state for manual prompt replacement."""
    query = update.callback_query
    await query.answer()

    role_key = query.data.split(":", 1)[1]

    if not role_key.startswith("user_role:"):
        return

    role_id = int(role_key.split(":")[1])

    if context.user_data is not None:
        context.user_data["edit_prompt_role_id"] = role_id
        context.user_data["edit_prompt_role_key"] = role_key

    kb = InlineKeyboardMarkup([[InlineKeyboardButton("↩️ Отмена", callback_data=f"role_edit_cancel:{role_key}")]])
    await query.edit_message_text(
        "📝 Отправьте новый промпт для этой роли:",
        reply_markup=kb,
    )


async def role_edit_ai_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Sets up state for AI-enhanced prompt editing."""
    query = update.callback_query
    await query.answer()

    role_key = query.data.split(":", 1)[1]
    user_id = query.from_user.id

    if not role_key.startswith("user_role:"):
        return

    role_id = int(role_key.split(":")[1])
    role_data = await get_role_data(role_key, user_id)
    if not role_data:
        from app.utils.keyboards import error_with_back_keyboard

        await query.edit_message_text(
            "❌ Роль не найдена.",
            reply_markup=error_with_back_keyboard("open_roles", "🎭 Меню ролей"),
        )
        return

    if context.user_data is not None:
        context.user_data["edit_prompt_ai_role_id"] = role_id
        context.user_data["edit_prompt_ai_role_key"] = role_key
        context.user_data["edit_prompt_ai_current"] = role_data.get("prompt", "")

    kb = InlineKeyboardMarkup([[InlineKeyboardButton("↩️ Отмена", callback_data=f"role_edit_cancel:{role_key}")]])
    await query.edit_message_text(
        "✨ Опишите, что нужно изменить в промпте.\n"
        "Например: «добавь правило всегда отвечать примерами» или «сделай тон более формальным».",
        reply_markup=kb,
    )


async def role_edit_ai_tweak_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Transition from AI preview to manual edit, keeping text visible for copying."""
    query = update.callback_query
    await query.answer()

    role_key = query.data.split(":", 1)[1]

    if not role_key.startswith("user_role:"):
        return

    role_id = int(role_key.split(":")[1])

    if context.user_data is not None:
        # Clear AI save state so we don't accidentally save
        context.user_data.pop("edit_prompt_ai_save_role_id", None)
        context.user_data.pop("edit_prompt_ai_save_role_key", None)

        # Transition to manual edit state
        context.user_data["edit_prompt_role_id"] = role_id
        context.user_data["edit_prompt_role_key"] = role_key

    # Keep the text, just update the keyboard and add instructions
    if context.user_data and "edit_prompt_ai_preview" in context.user_data:
        preview = context.user_data["edit_prompt_ai_preview"]
        text = (
            f"✨ **Улучшенный промпт** (скопируйте текст ниже):\n\n"
            f"`{preview}`\n\n"
            f"📝 Отправьте измененный текст промпта следующим сообщением."
        )
    else:
        text = "📝 Ок, отправьте новый текст промпта следующим сообщением."

    kb = InlineKeyboardMarkup([[InlineKeyboardButton("↩️ Отмена", callback_data=f"role_edit_cancel:{role_key}")]])
    from app.utils.formatting import TelegramFormatter

    fmt_text, fmt_pm = TelegramFormatter.format_text(text)
    await query.edit_message_text(fmt_text, parse_mode=fmt_pm, reply_markup=kb)


async def role_edit_cancel_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Cancel edit prompt — clear state and return to role details."""
    query = update.callback_query
    await query.answer("❌ Редактирование отменено")

    role_key = query.data.split(":", 1)[1]

    # Clear all edit prompt state
    if context.user_data is not None:
        context.user_data.pop("edit_prompt_role_id", None)
        context.user_data.pop("edit_prompt_role_key", None)
        context.user_data.pop("edit_prompt_ai_role_id", None)
        context.user_data.pop("edit_prompt_ai_role_key", None)
        context.user_data.pop("edit_prompt_ai_current", None)

    user_id = query.from_user.id
    chat_state = await get_user_chat(user_id)
    text, parse_mode, reply_markup = await menus.get_roles_menu_content(
        user_id, chat_state, view_mode="role_details", role_key=role_key
    )
    try:
        await query.edit_message_text(text, parse_mode=parse_mode, reply_markup=reply_markup)
    except telegram.error.BadRequest as e:
        if "Message is not modified" not in str(e):
            raise e


async def role_edit_ai_save_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Save the AI-enhanced prompt that was previewed."""
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id

    # Retrieve the previewed prompt from user_data
    new_prompt = context.user_data.get("edit_prompt_ai_preview") if context.user_data else None
    role_id = context.user_data.get("edit_prompt_ai_save_role_id") if context.user_data else None
    role_key = context.user_data.get("edit_prompt_ai_save_role_key") if context.user_data else None

    if not new_prompt or not role_id:
        from app.utils.keyboards import error_with_back_keyboard

        await query.edit_message_text(
            "❌ Нет данных для сохранения. Попробуйте ещё раз.",
            reply_markup=error_with_back_keyboard("open_roles", "🎭 Меню ролей"),
        )
        return

    from app.repos.roles import update_custom_role_prompt

    success = await update_custom_role_prompt(role_id, user_id, new_prompt)
    if not success:
        from app.utils.keyboards import error_with_back_keyboard

        await query.edit_message_text(
            "❌ Не удалось обновить промпт.",
            reply_markup=error_with_back_keyboard("open_roles", "🎭 Меню ролей"),
        )
        return

    # If this role is currently active, update system_prompt
    chat_state = await get_user_chat(user_id)
    from app.repos.roles import get_custom_role_prompt as _get_old

    # The role's old prompt may have been the active one — check against the stored current
    old_prompt = context.user_data.get("edit_prompt_ai_current") if context.user_data else None
    if old_prompt and chat_state.system_prompt == old_prompt:
        chat_state.system_prompt = new_prompt
        from app.repos.chats import update_user_chat

        await update_user_chat(user_id, chat_state)

    # Clean up state
    if context.user_data is not None:
        for key in [
            "edit_prompt_ai_preview",
            "edit_prompt_ai_save_role_id",
            "edit_prompt_ai_save_role_key",
            "edit_prompt_ai_current",
        ]:
            context.user_data.pop(key, None)

    # Return to role details
    if not role_key:
        role_key = f"user_role:{role_id}"

    text, parse_mode, reply_markup = await menus.get_roles_menu_content(
        user_id, chat_state, view_mode="role_details", role_key=role_key
    )
    try:
        await query.edit_message_text(
            f"✅ Промпт обновлён через AI!\n\n{text}",
            parse_mode=parse_mode,
            reply_markup=reply_markup,
        )
    except telegram.error.BadRequest:
        pass


async def role_nav_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()

    user_id = query.from_user.id
    chat_state = await get_user_chat(user_id)

    view_mode = query.data.split(":")[1]

    text, parse_mode, reply_markup = await menus.get_roles_menu_content(user_id, chat_state, view_mode=view_mode)

    try:
        await query.edit_message_text(text, parse_mode=parse_mode, reply_markup=reply_markup)
    except telegram.error.BadRequest as e:
        if "Message is not modified" not in str(e):
            raise e


async def role_page_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()

    user_id = query.from_user.id
    chat_state = await get_user_chat(user_id)

    parts = query.data.split(":")
    view_mode = parts[1]
    page = int(parts[2])

    text, parse_mode, reply_markup = await menus.get_roles_menu_content(
        user_id, chat_state, view_mode=view_mode, page=page
    )

    try:
        await query.edit_message_text(text, parse_mode=parse_mode, reply_markup=reply_markup)
    except telegram.error.BadRequest as e:
        if "Message is not modified" not in str(e):
            raise e


async def open_roles_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Opens roles hub inline — no flooding."""
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    if not await _is_authorized(user_id):
        return
    chat_state = await get_user_chat(user_id)
    text, parse_mode, reply_markup = await menus.get_roles_menu_content(user_id, chat_state)

    # When triggered from an AI response, send as a new message
    # to preserve the original response text.
    from_response = query.data == "open_roles:from_response"

    try:
        if from_response:
            await query.message.reply_text(text, parse_mode=parse_mode, reply_markup=reply_markup)
        else:
            await query.edit_message_text(text, parse_mode=parse_mode, reply_markup=reply_markup)
    except telegram.error.BadRequest:
        pass


# ── Manual role creation callbacks ────────────────────────────────────────────


async def role_create_manual_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Start manual role creation — asks user for a title."""
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    from app.state import begin_manual_role_creation

    begin_manual_role_creation(user_id)
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("↩️ Отмена", callback_data="role_manual_cancel")]])
    create_text = "📝 **Создание роли вручную**\n\nВведите **название** для новой роли (1 строка):"
    fmt_text, fmt_pm = TelegramFormatter.format_text(create_text)
    await query.edit_message_text(
        fmt_text,
        parse_mode=fmt_pm,
        reply_markup=kb,
    )


async def role_manual_cancel_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Cancel manual role creation — return to roles hub."""
    query = update.callback_query
    await query.answer("↩️ Создание роли отменено")
    user_id = query.from_user.id
    from app.state import clear_manual_role_state

    clear_manual_role_state(user_id)
    chat_state = await get_user_chat(user_id)
    text, parse_mode, reply_markup = await menus.get_roles_menu_content(user_id, chat_state, view_mode="hub")
    try:
        await query.edit_message_text(text, parse_mode=parse_mode, reply_markup=reply_markup)
    except telegram.error.BadRequest as e:
        if "Message is not modified" not in str(e):
            raise e


async def role_manual_save_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Save the manually created role to DB and apply it."""
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    from app.state import (
        clear_manual_role_state,
        get_manual_role_prompt,
        get_manual_role_title,
    )

    title = get_manual_role_title(user_id)
    prompt_text = get_manual_role_prompt(user_id)
    if not title or not prompt_text:
        from app.utils.keyboards import error_with_back_keyboard

        await query.edit_message_text(
            "⚠️ Данные роли не найдены. Возможно, бот был перезапущен.\n\nПожалуйста, создайте роль заново.",
            reply_markup=error_with_back_keyboard(
                "role_create_manual",
                "📝 Создать заново",
                extra_buttons=[[InlineKeyboardButton("🎭 Меню ролей", callback_data="open_roles")]],
            ),
        )
        return
    try:
        await create_custom_role(user_id, title, prompt_text)
        await role_conv_metrics.record_custom_role_creation()
        # Apply role immediately
        chat_state = await get_user_chat(user_id)
        chat_state.system_prompt = prompt_text
        await update_user_chat(user_id, chat_state)
        clear_manual_role_state(user_id)
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("🎭 Меню ролей", callback_data="open_roles")]])
        save_text = f"💾 Роль **{title}** сохранена и применена!"
        fmt_text, fmt_pm = TelegramFormatter.format_text(save_text)
        await query.edit_message_text(
            fmt_text,
            parse_mode=fmt_pm,
            reply_markup=kb,
        )
    except Exception as e:
        logging.error("Error saving manual role: %s", e, exc_info=True)
        from app.utils.keyboards import error_with_back_keyboard

        await query.edit_message_text(
            "❌ Ошибка сохранения роли. Попробуйте позже.",
            reply_markup=error_with_back_keyboard("open_roles", "🎭 Меню ролей"),
        )
