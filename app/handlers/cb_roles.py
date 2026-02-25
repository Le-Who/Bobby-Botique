"""
Role management callbacks — apply, clear, create, delete, rename, detail, nav, page.
"""

import logging
import asyncio
import telegram
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes

from app import database as db
from app import prompts
from app.config import settings
from app.utils.formatting import TelegramFormatter
from app.metrics import role_conv_metrics
from app.state import (
    begin_custom_role_creation,
    get_generated_role,
    clear_custom_role_state,
    get_last_custom_role_prompt,
    set_generating_custom_role,
    set_last_custom_role_prompt,
    set_generated_role,
)
from app.handlers import menus


class DummyUpdate:
    """Helper class to mock an Update object for calling commands from callbacks."""

    def __init__(self, msg, user) -> None:
        self.message = msg
        self.effective_user = user


async def role_rename_menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    if not await db.is_authorized(user_id):
        return
    roles = await db.db_query(
        "SELECT id, title FROM user_roles WHERE user_id = $1 ORDER BY created_at DESC",
        (user_id,),
    )
    if not roles:
        # UX: Add back button even for empty state
        await query.edit_message_text(
            "У вас пока нет кастомных ролей.",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("⬅️ Назад", callback_data="open_roles")]]
            ),
        )
        return
    buttons = []
    for r in roles:
        buttons.append(
            [
                InlineKeyboardButton(
                    f"✏️ {r['title']}", callback_data=f"role_rename_pick:{r['id']}"
                )
            ]
        )

    # UX: Add Back button and use edit_message_text
    buttons.append([InlineKeyboardButton("⬅️ Назад", callback_data="open_roles")])
    await query.edit_message_text(
        "Выберите роль для переименования:", reply_markup=InlineKeyboardMarkup(buttons)
    )


async def role_rename_pick_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    if not await db.is_authorized(user_id):
        return
    role_id = int(query.data.split(":")[1])
    context.user_data["rename_role_id"] = role_id
    kb = InlineKeyboardMarkup(
        [[InlineKeyboardButton("❌ Отмена", callback_data="role_rename_cancel")]]
    )
    await query.message.reply_text(
        "Введите новое название роли одной строкой:", reply_markup=kb
    )


async def start_menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()

    user_id = query.from_user.id
    chat_state = await db.get_user_chat(user_id)

    formatted_text, parse_mode, reply_markup = await menus.get_start_menu_content(chat_state)

    await query.edit_message_text(
        formatted_text, parse_mode=parse_mode, reply_markup=reply_markup
    )


async def role_apply_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    user_id = query.from_user.id
    chat_state = await db.get_user_chat(user_id)
    key = query.data.split(":", 1)[1]
    role_title = ""

    # Get data roles via хелпер
    role_data = await db.get_role_data(key, user_id)

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
    await db.update_user_chat(user_id, chat_state)

    # Update menu
    # Save state
    await db.update_user_chat(user_id, chat_state)

    # Update menu - возвращаемся в Hub
    text, parse_mode, reply_markup = await menus.get_roles_menu_content(
        user_id, chat_state, view_mode="hub"
    )
    try:
        await query.edit_message_text(
            text, parse_mode=parse_mode, reply_markup=reply_markup
        )
    except telegram.error.BadRequest as e:
        if "Message is not modified" not in str(e):
            raise e
    await query.answer(f"✅ Роль '{role_title}' применена.")


async def role_clear_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    user_id = query.from_user.id
    chat_state = await db.get_user_chat(user_id)
    # Clean up системный промпт (будет использован базовый)
    chat_state.system_prompt = None
    await db.update_user_chat(user_id, chat_state)
    await role_conv_metrics.record_role_clear()

    # Update menu
    # Update menu - возвращаемся в Hub
    text, parse_mode, reply_markup = await menus.get_roles_menu_content(
        user_id, chat_state, view_mode="hub"
    )
    try:
        await query.edit_message_text(
            text, parse_mode=parse_mode, reply_markup=reply_markup
        )
    except telegram.error.BadRequest as e:
        if "Message is not modified" not in str(e):
            raise e
    await query.answer("🧹 Роль сброшена.")


async def role_create_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    begin_custom_role_creation(query.from_user.id)
    kb = InlineKeyboardMarkup(
        [[InlineKeyboardButton("❌ Отмена", callback_data="role_create_cancel")]]
    )
    await query.message.reply_text(
        "Опишите, какую роль хотите создать (1–2 предложения):",
        reply_markup=kb,
    )


async def role_create_cancel_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Отмена создания кастомной роли — возврат в hub."""
    query = update.callback_query
    await query.answer("❌ Создание роли отменено")
    user_id = query.from_user.id
    clear_custom_role_state(user_id)
    chat_state = await db.get_user_chat(user_id)
    text, parse_mode, reply_markup = await menus.get_roles_menu_content(
        user_id, chat_state, view_mode="hub"
    )
    try:
        await query.edit_message_text(
            text, parse_mode=parse_mode, reply_markup=reply_markup
        )
    except telegram.error.BadRequest as e:
        if "Message is not modified" not in str(e):
            raise e


async def role_rename_cancel_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Отмена переименования роли — возврат в hub."""
    query = update.callback_query
    await query.answer("❌ Переименование отменено")
    context.user_data.pop("rename_role_id", None)
    user_id = query.from_user.id
    chat_state = await db.get_user_chat(user_id)
    text, parse_mode, reply_markup = await menus.get_roles_menu_content(
        user_id, chat_state, view_mode="hub"
    )
    try:
        await query.edit_message_text(
            text, parse_mode=parse_mode, reply_markup=reply_markup
        )
    except telegram.error.BadRequest as e:
        if "Message is not modified" not in str(e):
            raise e


async def role_custom_apply_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    chat_state = await db.get_user_chat(user_id)
    role = get_generated_role(user_id)
    if not role:
        await query.edit_message_text("❌ Нет сгенерированной роли для применения.")
        return
    prompt_text = role.get("prompt") or role.get("system_prompt") or ""
    # Save only промпт roles (without базового системного промпта)
    # compose_system_instruction будет вызван on использовании
    chat_state.system_prompt = prompt_text
    await db.update_user_chat(user_id, chat_state)
    clear_custom_role_state(user_id)
    kb = InlineKeyboardMarkup(
        [[InlineKeyboardButton("🎭 Меню ролей", callback_data="open_roles")]]
    )
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
        await query.edit_message_text("❌ Нет сгенерированной роли для сохранения.")
        return
    # Save в user_roles
    try:
        # Save
        await db.db_query(
            "INSERT INTO user_roles (user_id, title, prompt) VALUES ($1, $2, $3)",
            (
                user_id,
                role.get("title", "Моя роль"),
                role.get("prompt") or role.get("system_prompt", ""),
            ),
        )
        await role_conv_metrics.record_custom_role_creation()
        # И сразу onменяем
        prompt_text = role.get("prompt") or role.get("system_prompt") or ""
        chat_state = await db.get_user_chat(user_id)
        # Save only промпт roles (without базового системного промпта)
        # compose_system_instruction будет вызван on использовании
        chat_state.system_prompt = prompt_text
        await db.update_user_chat(user_id, chat_state)
        clear_custom_role_state(user_id)
        kb = InlineKeyboardMarkup(
            [[InlineKeyboardButton("🎭 Меню ролей", callback_data="open_roles")]]
        )
        await query.edit_message_text(
            "💾 Роль сохранена и применена.", reply_markup=kb
        )
    except Exception as e:
        await query.edit_message_text(f"❌ Ошибка сохранения роли: {e}")


async def role_custom_retry_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    last_prompt = get_last_custom_role_prompt(user_id)
    if not last_prompt:
        await query.edit_message_text("❌ Нет предыдущего запроса для повтора.")
        return
    # Запускаем повтор генерации как в messages.handle_request
    chat_state = await db.get_user_chat(user_id)

    # Используем универсальную функцию for получения keyа (поддерживает и Gemini, и OpenRouter)
    from app.handlers.ai_core import _resolve_ai_request, _get_ai_response, _increment_key_usage

    model_for_role = chat_state.model or settings.DEFAULT_MODEL
    key_data, model_used, resolution = await _resolve_ai_request(model_for_role)
    if not key_data:
        await query.edit_message_text("❌ Нет доступных ключей API для генерации роли.")
        return
    progress_msg = await query.message.reply_text("🛠️ Генерирую роль…")
    set_generating_custom_role(user_id, True)
    history = [{"role": "user", "parts": [last_prompt]}]

    # Используем универсальную функцию for получения responseа (поддерживает и Gemini, и OpenRouter)
    response_text, _ = await _get_ai_response(
        key_data["api_key"],
        history,
        model_used,
        system_instruction=prompts.PROMPT_ENGINEER_SYSTEM_PROMPT,
        user_id=user_id,
        chat_id=user_id,
    )

    # Инкрементируем использование keyа
    await _increment_key_usage(key_data["key_hash"], model_used)

    # Log response models for отладки
    logging.info("Model response for role retry: %s...", response_text[:500])

    role_obj = prompts.extract_json_object(response_text)
    if not role_obj:
        # Processing явной 503 ошибки from textа
        if (
            "503" in (response_text or "")
            or "unavailable" in (response_text or "").lower()
        ):
            await progress_msg.edit_text(
                "🔄 Сервер перегружен. Попробуйте ещё раз через несколько секунд."
            )
        else:
            logging.error(
                f"Failed to parse role JSON on retry. Response: {response_text}"
            )
            await progress_msg.edit_text(
                "❌ Снова не удалось сгенерировать роль. Попробуйте изменить описание."
            )
        set_generating_custom_role(user_id, False)
        return
    set_last_custom_role_prompt(user_id, last_prompt)

    set_generated_role(user_id, role_obj)
    title = role_obj.get("title", "Кастомная роль")
    purpose = role_obj.get("purpose", "")
    style = ", ".join(role_obj.get("style", [])[:3])
    preview = (
        f"🆕 *Новая роль:* {title}\n\n"
        f"🎯 Цель: {purpose}\n"
        f"🧭 Стиль: {style}\n\n"
        f"Применить сейчас или сохранить?"
    )
    kb = [
        [InlineKeyboardButton("✅ Применить", callback_data="role_custom_apply")],
        [InlineKeyboardButton("💾 Сохранить", callback_data="role_custom_save")],
        [InlineKeyboardButton("❌ Отмена", callback_data="role_clear")],
    ]
    formatted_text, parse_mode = TelegramFormatter.format_text(preview)
    await progress_msg.edit_text(
        formatted_text, parse_mode=parse_mode, reply_markup=InlineKeyboardMarkup(kb)
    )
    set_generating_custom_role(user_id, False)


async def role_delete_ask_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    role_id = query.data.split(":")[1]

    bg_text = (
        "⚠️ *Удаление роли*\n\n"
        "Вы уверены, что хотите удалить эту роль? Это действие нельзя отменить."
    )
    kb = [
        [
            InlineKeyboardButton(
                "🗑️ Да, удалить навсегда", callback_data=f"role_delete_confirm:{role_id}"
            )
        ],
        [
            InlineKeyboardButton(
                "❌ Отмена", callback_data=f"role_delete_cancel:{role_id}"
            )
        ],
    ]
    formatted, pm = TelegramFormatter.format_text(bg_text)
    await query.edit_message_text(
        formatted, parse_mode=pm, reply_markup=InlineKeyboardMarkup(kb)
    )


async def role_delete_cancel_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    query = update.callback_query
    await query.answer()
    role_id = query.data.split(":")[1]

    # Returnся в детали roles
    user_id = query.from_user.id
    chat_state = await db.get_user_chat(user_id)

    text, parse_mode, reply_markup = await menus.get_roles_menu_content(
        user_id, chat_state, view_mode="role_details", role_key=f"user_role:{role_id}"
    )
    await query.edit_message_text(
        text, parse_mode=parse_mode, reply_markup=reply_markup
    )


async def role_delete_confirm_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    query = update.callback_query
    user_id = query.from_user.id
    if not await db.is_authorized(user_id):
        await query.answer("❌ Нет доступа")
        return
    try:
        role_id = int(query.data.split(":")[1])
        # Check, не активна ли эта role сейчас
        chat_state = await db.get_user_chat(user_id)

        # Get промпт удаляемой roles, чтобы проверить, активна ли она
        role_data = await db.db_query(
            "SELECT prompt FROM user_roles WHERE id = $1 AND user_id = $2",
            (role_id, user_id),
        )

        await db.db_query(
            "DELETE FROM user_roles WHERE id = $1 AND user_id = $2", (role_id, user_id)
        )

        # If удаляемая role была активна, сбрасываем ее
        if role_data and chat_state.system_prompt == role_data[0]["prompt"]:
            chat_state.system_prompt = None
            await db.update_user_chat(user_id, chat_state)

        # Update menu - переходим в list "Мои roles"
        text, parse_mode, reply_markup = await menus.get_roles_menu_content(
            user_id, chat_state, view_mode="my_roles", page=0
        )

        try:
            await query.edit_message_text(
                text, parse_mode=parse_mode, reply_markup=reply_markup
            )
        except telegram.error.BadRequest as e:
            if "Message is not modified" not in str(e):
                raise e

        await query.answer("🗑️ Роль удалена.")

    except Exception as e:
        logging.error("Error deleting role: %s", e)
        await query.answer("❌ Ошибка удаления роли")


async def role_detail_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()

    user_id = query.from_user.id
    chat_state = await db.get_user_chat(user_id)

    role_key = query.data.split(":", 1)[1]

    text, parse_mode, reply_markup = await menus.get_roles_menu_content(
        user_id, chat_state, view_mode="role_details", role_key=role_key
    )

    try:
        await query.edit_message_text(
            text, parse_mode=parse_mode, reply_markup=reply_markup
        )
    except telegram.error.BadRequest as e:
        if "Message is not modified" not in str(e):
            raise e


async def role_view_prompt_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()

    role_key = query.data.split(":", 1)[1]
    user_id = query.from_user.id

    # Fetch role data using helper
    role_data = await db.get_role_data(role_key, user_id)
    prompt = role_data.get("prompt") if role_data else ""

    if prompt:
        # Send as a new message so user can copy it easily
        await query.message.reply_text(
            f"📝 *Полный промпт роли:*\n\n`{prompt}`", parse_mode="Markdown"
        )
    else:
        await query.message.reply_text("❌ Не удалось найти промпт.")


async def role_nav_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()

    user_id = query.from_user.id
    chat_state = await db.get_user_chat(user_id)

    view_mode = query.data.split(":")[1]

    text, parse_mode, reply_markup = await menus.get_roles_menu_content(
        user_id, chat_state, view_mode=view_mode
    )

    try:
        await query.edit_message_text(
            text, parse_mode=parse_mode, reply_markup=reply_markup
        )
    except telegram.error.BadRequest as e:
        if "Message is not modified" not in str(e):
            raise e


async def role_page_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()

    user_id = query.from_user.id
    chat_state = await db.get_user_chat(user_id)

    parts = query.data.split(":")
    view_mode = parts[1]
    page = int(parts[2])

    text, parse_mode, reply_markup = await menus.get_roles_menu_content(
        user_id, chat_state, view_mode=view_mode, page=page
    )

    try:
        await query.edit_message_text(
            text, parse_mode=parse_mode, reply_markup=reply_markup
        )
    except telegram.error.BadRequest as e:
        if "Message is not modified" not in str(e):
            raise e


async def open_roles_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    if not await db.is_authorized(user_id):
        return
    # Отображаем menu ролей так же, как и команда /roles
    from app.handlers.commands import roles_command

    await roles_command(DummyUpdate(query.message, query.from_user), context)
