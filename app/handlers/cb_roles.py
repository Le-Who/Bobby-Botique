"""
Role management callbacks — apply, clear, create, delete, rename, detail, nav, page.
"""

import asyncio
import logging

import telegram
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from app.config import settings
from app.handlers import menus
from app.handlers.interaction_work import (
    begin_text_input_intent,
    bind_context_text_input,
    prepare_text_input,
    reject_busy_interaction,
)
from app.metrics import role_conv_metrics
from app.observability.content import content_fields
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
    ensure_state_loaded,
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
    input_intent = begin_text_input_intent(query.from_user.id)
    await query.answer()
    user_id = query.from_user.id
    if not await _is_authorized(user_id):
        return
    role_id = int(query.data.split(":")[1])
    if not await prepare_text_input(user_id, query.message, query.answer, context.user_data, input_intent=input_intent):
        return
    from app.state import begin_interaction

    owner = begin_interaction(user_id, "rename")
    bind_context_text_input(owner, context.user_data)
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
    if await reject_busy_interaction(user_id, query.answer):
        return
    key = query.data.split(":", 1)[1]
    role_title = ""

    chat_state, role_data = await asyncio.gather(get_user_chat(user_id), get_role_data(key, user_id))

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
    if await reject_busy_interaction(user_id, query.answer):
        return
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
    input_intent = begin_text_input_intent(query.from_user.id)
    await query.answer()
    if not await prepare_text_input(
        query.from_user.id, query.message, query.answer, context.user_data, input_intent=input_intent
    ):
        return
    begin_custom_role_creation(query.from_user.id)
    from app.state import get_user_state

    owner = get_user_state(query.from_user.id).interaction
    bind_context_text_input(owner, context.user_data)
    token = owner.token
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("↩️ Отмена", callback_data=f"role_create_cancel:{token}")]])
    await query.edit_message_text(
        "📝 Опишите, какую роль хотите создать (1–2 предложения):",
        reply_markup=kb,
    )


async def role_create_cancel_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Отмена создания кастомной роли — возврат в hub."""
    query = update.callback_query
    user_id = query.from_user.id
    await ensure_state_loaded(user_id)
    from app.state import cancel_interaction, get_user_state

    owner = get_user_state(user_id).interaction
    if owner is not None and owner.kind != "role":
        await query.answer("Запрос уже устарел.")
        return
    expected = (
        owner.token
        if owner is not None and owner.kind == "role"
        else (get_generated_role(user_id) or {}).get("_interaction_token")
    )
    if _callback_token(query, "role_create_cancel") != expected:
        await query.answer("Запрос уже устарел.")
        return
    clear_custom_role_state(user_id)
    await cancel_interaction(user_id)
    await query.answer("❌ Создание роли отменено")
    chat_state = await get_user_chat(user_id)
    text, parse_mode, reply_markup = await menus.get_roles_menu_content(user_id, chat_state, view_mode="hub")
    if get_user_state(user_id).interaction is not None:
        return
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


def _callback_token(query, prefix: str) -> str | None:
    data = getattr(query, "data", "") or ""
    return data.removeprefix(prefix + ":") if data.startswith(prefix + ":") else None


def _role_preview_owner(user_id: int, query, prefix: str):
    from app.state import begin_interaction, get_user_state

    state = get_user_state(user_id)
    role = get_generated_role(user_id)
    if not role or _callback_token(query, prefix) != role.get("_interaction_token"):
        return None
    owner = state.interaction
    if owner is not None:
        if owner.kind != "role" or owner.task is not None:
            return None
        if role.get("_interaction_token") and owner.token != role["_interaction_token"]:
            return None
    else:
        owner = begin_interaction(user_id, "role")
        if role.get("_interaction_token"):
            owner.token = role["_interaction_token"]
        owner.epoch = role.get("_memory_epoch")
    return owner


async def _use_custom_role(update: Update, *, save: bool) -> None:
    from app.handlers.interaction_work import interaction_current, private_interaction
    from app.state import run_interaction

    query = update.callback_query
    user_id = query.from_user.id
    await ensure_state_loaded(user_id)
    if await reject_busy_interaction(user_id, query.answer):
        return
    await query.answer()
    prefix = "role_custom_save" if save else "role_custom_apply"
    owner = _role_preview_owner(user_id, query, prefix)
    if owner is None:
        await query.edit_message_text("Запрос уже устарел. Создайте роль заново.")
        return
    role = get_generated_role(user_id)
    assert role is not None

    async def apply() -> None:
        try:
            chat_state = await get_user_chat(user_id)
            async with private_interaction(owner, chat_state) as allowed:
                if not allowed:
                    return
                prompt = role.get("prompt") or role.get("system_prompt") or ""
                if save:
                    await create_custom_role(user_id, role.get("title", "Моя роль"), prompt)
                    if not await interaction_current(owner):
                        return
                    await role_conv_metrics.record_custom_role_creation()
                if not await interaction_current(owner):
                    return
                chat_state.system_prompt = prompt
                applied = await update_user_chat(user_id, chat_state, expected_epoch=owner.epoch)
                if not await interaction_current(owner):
                    return
                if applied is False:
                    await query.edit_message_text("Не удалось применить роль: запрос устарел. Выберите роль заново.")
                    return
                kb = InlineKeyboardMarkup([[InlineKeyboardButton("🎭 Меню ролей", callback_data="open_roles")]])
                text = (
                    "💾 Роль сохранена и применена."
                    if save
                    else f"✅ Роль '{role.get('title', 'Кастомная роль')}' применена."
                )
                await query.edit_message_text(text, reply_markup=kb)
                if owner.current():
                    clear_custom_role_state(user_id)
        except Exception as exc:
            logging.error("Error applying custom role: %s", exc, exc_info=True)
            if await interaction_current(owner):
                await query.edit_message_text("❌ Ошибка сохранения роли. Попробуйте позже.")

    await run_interaction(owner, apply)


async def role_custom_apply_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _use_custom_role(update, save=False)


async def role_custom_save_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _use_custom_role(update, save=True)


async def role_custom_retry_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    from app.handlers.msg_roles import generate_custom_role
    from app.state import begin_interaction

    query = update.callback_query
    user_id = query.from_user.id
    await ensure_state_loaded(user_id)
    if await reject_busy_interaction(user_id, query.answer):
        return
    await query.answer()
    role = get_generated_role(user_id)
    if role is not None and _role_preview_owner(user_id, query, "role_custom_retry") is None:
        await query.edit_message_text("Запрос уже устарел. Создайте роль заново.")
        return
    last_prompt = get_last_custom_role_prompt(user_id)
    if not last_prompt:
        await query.edit_message_text("❌ Нет предыдущего запроса для повтора.")
        return
    if role is None and _callback_token(query, "role_custom_retry") is not None:
        await query.edit_message_text("Запрос уже устарел. Создайте роль заново.")
        return
    begin_interaction(user_id, "role")
    await generate_custom_role(user_id, user_id, last_prompt, query.message.reply_text, get_chat=get_user_chat)


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

        chat_state, role_prompt = await asyncio.gather(get_user_chat(user_id), get_custom_role_prompt(role_id, user_id))

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


def _start_role_edit(user_id: int, data):
    from app.handlers.msg_roles import clear_role_edit_state
    from app.state import begin_interaction, get_user_state

    clear_role_edit_state(data)
    owner = begin_interaction(user_id, "role_edit")
    bind_context_text_input(owner, data)
    state = get_user_state(user_id)
    state.awaiting_custom_role_input = False
    state.awaiting_manual_role_title = False
    state.awaiting_manual_role_prompt = False
    if data is not None:
        data["role_edit_token"] = owner.token
    return owner


def _edit_callback_owner(user_id: int, query, data):
    from app.state import begin_interaction, get_user_state

    token = (data or {}).get("role_edit_token")
    parts = (getattr(query, "data", "") or "").split(":")
    supplied = parts[-1] if token and len(parts) > 1 else None
    if supplied != token:
        return None
    owner = get_user_state(user_id).interaction
    if owner is not None:
        if owner.kind != "role_edit" or owner.task is not None or (token and owner.token != token):
            return None
    elif token:
        return None
    else:
        owner = begin_interaction(user_id, "role_edit")
    return owner


def _edit_role_key(query, data) -> str:
    key = query.data.split(":", 1)[1]
    token = (data or {}).get("role_edit_token")
    if token and key.endswith(":" + token):
        key = key[: -(len(token) + 1)]
    return key


async def role_edit_manual_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    input_intent = begin_text_input_intent(query.from_user.id)
    await query.answer()
    user_id = query.from_user.id
    role_key = query.data.split(":", 1)[1]
    if not role_key.startswith("user_role:"):
        return
    if not await prepare_text_input(user_id, query.message, query.answer, context.user_data, input_intent=input_intent):
        return
    role_id = int(role_key.split(":")[1])
    owner = _start_role_edit(user_id, context.user_data)
    if context.user_data is not None:
        context.user_data.update(edit_prompt_role_id=role_id, edit_prompt_role_key=role_key)
    kb = InlineKeyboardMarkup(
        [[InlineKeyboardButton("↩️ Отмена", callback_data=f"role_edit_cancel:{role_key}:{owner.token}")]]
    )
    await query.edit_message_text("📝 Отправьте новый промпт для этой роли:", reply_markup=kb)


async def role_edit_ai_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    input_intent = begin_text_input_intent(query.from_user.id)
    await query.answer()
    role_key = query.data.split(":", 1)[1]
    user_id = query.from_user.id
    if not role_key.startswith("user_role:"):
        return
    if not await prepare_text_input(user_id, query.message, query.answer, context.user_data, input_intent=input_intent):
        return
    role_id = int(role_key.split(":")[1])
    owner = _start_role_edit(user_id, context.user_data)
    role_data = await get_role_data(role_key, user_id)
    if not owner.current():
        return
    if await reject_busy_interaction(user_id, query.answer):
        return
    if not role_data:
        await query.edit_message_text("❌ Роль не найдена.")
        return
    if context.user_data is not None:
        context.user_data.update(
            edit_prompt_ai_role_id=role_id,
            edit_prompt_ai_role_key=role_key,
            edit_prompt_ai_current=role_data.get("prompt", ""),
        )
    kb = InlineKeyboardMarkup(
        [[InlineKeyboardButton("↩️ Отмена", callback_data=f"role_edit_cancel:{role_key}:{owner.token}")]]
    )
    await query.edit_message_text(
        "✨ Опишите, что нужно изменить в промпте.\nНапример: «добавь правило всегда отвечать примерами» или «сделай тон более формальным».",
        reply_markup=kb,
    )


async def role_edit_ai_tweak_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    user_id = query.from_user.id
    if await reject_busy_interaction(user_id, query.answer):
        return
    await query.answer()
    owner = _edit_callback_owner(user_id, query, context.user_data)
    if owner is None:
        await query.edit_message_text("Запрос уже устарел. Откройте редактирование роли заново.")
        return
    role_key = _edit_role_key(query, context.user_data)
    if role_key != (context.user_data or {}).get("edit_prompt_ai_save_role_key"):
        await query.edit_message_text("Запрос уже устарел.")
        return
    preview = context.user_data.get("edit_prompt_ai_preview", "")
    owner = _start_role_edit(user_id, context.user_data)
    context.user_data.update(edit_prompt_role_id=int(role_key.split(":")[1]), edit_prompt_role_key=role_key)
    text = f"✨ **Улучшенный промпт** (скопируйте текст ниже):\n\n`{preview}`\n\n📝 Отправьте измененный текст промпта следующим сообщением."
    kb = InlineKeyboardMarkup(
        [[InlineKeyboardButton("↩️ Отмена", callback_data=f"role_edit_cancel:{role_key}:{owner.token}")]]
    )
    text, parse_mode = TelegramFormatter.format_text(text)
    await query.edit_message_text(text, parse_mode=parse_mode, reply_markup=kb)


async def role_edit_cancel_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    from app.handlers.msg_roles import clear_role_edit_state
    from app.state import cancel_interaction

    query = update.callback_query
    user_id = query.from_user.id
    # Cancellation is allowed during the owned provider call, unlike save/tweak.
    from app.state import get_user_state

    owner = get_user_state(user_id).interaction
    token = (context.user_data or {}).get("role_edit_token")
    if token and (owner is None or owner.token != token or not query.data.endswith(":" + token)):
        await query.answer("Запрос уже устарел.")
        return
    if not token and owner is not None:
        await query.answer("Запрос уже устарел.")
        return
    role_key = _edit_role_key(query, context.user_data)
    clear_role_edit_state(context.user_data)
    await cancel_interaction(user_id)
    await query.answer("❌ Редактирование отменено")
    chat_state = await get_user_chat(user_id)
    text, parse_mode, reply_markup = await menus.get_roles_menu_content(
        user_id, chat_state, view_mode="role_details", role_key=role_key
    )
    try:
        await query.edit_message_text(text, parse_mode=parse_mode, reply_markup=reply_markup)
    except telegram.error.BadRequest as exc:
        if "Message is not modified" not in str(exc):
            raise


async def role_edit_ai_save_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    from app.handlers.interaction_work import interaction_current, private_interaction
    from app.handlers.msg_roles import clear_role_edit_state
    from app.state import run_interaction

    query = update.callback_query
    user_id = query.from_user.id
    await ensure_state_loaded(user_id)
    if await reject_busy_interaction(user_id, query.answer):
        return
    await query.answer()
    data = context.user_data
    owner = _edit_callback_owner(user_id, query, data)
    new_prompt = (data or {}).get("edit_prompt_ai_preview")
    role_id = (data or {}).get("edit_prompt_ai_save_role_id")
    role_key = (data or {}).get("edit_prompt_ai_save_role_key", f"user_role:{role_id}")
    old_prompt = (data or {}).get("edit_prompt_ai_current")
    if owner is None or not new_prompt or not role_id:
        await query.edit_message_text("Запрос уже устарел. Откройте редактирование роли заново.")
        return

    async def save() -> None:
        from app.repos.roles import get_custom_role_prompt, update_custom_role_prompt

        chat_state = await get_user_chat(user_id)
        async with private_interaction(owner, chat_state) as allowed:
            if not allowed:
                return
            # A newer edit of the same saved role also invalidates this preview.
            source = await get_custom_role_prompt(role_id, user_id)
            if not await interaction_current(owner):
                return
            if source != old_prompt:
                await query.edit_message_text("Промпт роли уже изменился. Откройте редактирование заново.")
                return
            success = await update_custom_role_prompt(role_id, user_id, new_prompt)
            if not await interaction_current(owner):
                return
            if not success:
                await query.edit_message_text("❌ Не удалось обновить промпт.")
                return
            if old_prompt and chat_state.system_prompt == old_prompt:
                chat_state.system_prompt = new_prompt
                applied = await update_user_chat(user_id, chat_state, expected_epoch=owner.epoch)
                if not await interaction_current(owner):
                    return
                if applied is False:
                    await query.edit_message_text(
                        "Промпт роли обновлён. Не удалось применить его к текущему диалогу: запрос устарел. Выберите роль заново."
                    )
                    return
            text, parse_mode, reply_markup = await menus.get_roles_menu_content(
                user_id, chat_state, view_mode="role_details", role_key=role_key
            )
            if await interaction_current(owner):
                await query.edit_message_text(
                    f"✅ Промпт обновлён через AI!\n\n{text}", parse_mode=parse_mode, reply_markup=reply_markup
                )
                if owner.current():
                    clear_role_edit_state(data)

    await run_interaction(owner, save)


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
    input_intent = begin_text_input_intent(query.from_user.id)
    await query.answer()
    user_id = query.from_user.id
    if not await prepare_text_input(user_id, query.message, query.answer, context.user_data, input_intent=input_intent):
        return
    from app.state import begin_manual_role_creation

    begin_manual_role_creation(user_id)
    from app.state import get_user_state

    owner = get_user_state(user_id).interaction
    bind_context_text_input(owner, context.user_data)
    token = owner.token
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("↩️ Отмена", callback_data=f"role_manual_cancel:{token}")]])
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
    user_id = query.from_user.id
    await ensure_state_loaded(user_id)
    from app.state import get_user_state

    owner = get_user_state(user_id).interaction
    expected = owner.token if owner is not None and owner.kind == "manual_role" else None
    if (owner is not None and owner.kind != "manual_role") or _callback_token(query, "role_manual_cancel") != expected:
        await query.answer("Запрос уже устарел.")
        return
    from app.state import cancel_interaction, clear_manual_role_state

    clear_manual_role_state(user_id)
    await cancel_interaction(user_id)
    await query.answer("↩️ Создание роли отменено")
    chat_state = await get_user_chat(user_id)
    text, parse_mode, reply_markup = await menus.get_roles_menu_content(user_id, chat_state, view_mode="hub")
    if get_user_state(user_id).interaction is not None:
        return
    try:
        await query.edit_message_text(text, parse_mode=parse_mode, reply_markup=reply_markup)
    except telegram.error.BadRequest as e:
        if "Message is not modified" not in str(e):
            raise e


async def role_manual_save_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    from app.handlers.interaction_work import interaction_current, private_interaction
    from app.state import (
        begin_interaction,
        clear_manual_role_state,
        get_manual_role_prompt,
        get_manual_role_title,
        get_user_state,
        run_interaction,
    )

    query = update.callback_query
    user_id = query.from_user.id
    await ensure_state_loaded(user_id)
    if await reject_busy_interaction(user_id, query.answer):
        return
    await query.answer()
    state = get_user_state(user_id)
    owner = state.interaction
    supplied = _callback_token(query, "role_manual_save")
    expected = owner.token if owner is not None and owner.kind == "manual_role" else None
    if supplied != expected or (owner is not None and (owner.kind != "manual_role" or owner.task is not None)):
        await query.edit_message_text("Запрос уже устарел. Создайте роль заново.")
        return
    title = get_manual_role_title(user_id)
    prompt_text = get_manual_role_prompt(user_id)
    if not title or not prompt_text:
        await query.edit_message_text("⚠️ Данные роли не найдены. Пожалуйста, создайте роль заново.")
        return
    if owner is None:
        owner = begin_interaction(user_id, "manual_role")

    async def save() -> None:
        try:
            chat_state = await get_user_chat(user_id)
            async with private_interaction(owner, chat_state) as allowed:
                if not allowed:
                    return
                await create_custom_role(user_id, title, prompt_text)
                if not await interaction_current(owner):
                    return
                await role_conv_metrics.record_custom_role_creation()
                if not await interaction_current(owner):
                    return
                chat_state.system_prompt = prompt_text
                applied = await update_user_chat(user_id, chat_state, expected_epoch=owner.epoch)
                if not await interaction_current(owner):
                    return
                if applied is False:
                    await query.edit_message_text(
                        "Роль сохранена. Не удалось применить её к текущему диалогу: запрос устарел. Выберите роль заново."
                    )
                    return
                kb = InlineKeyboardMarkup([[InlineKeyboardButton("🎭 Меню ролей", callback_data="open_roles")]])
                text, parse_mode = TelegramFormatter.format_text(f"💾 Роль **{title}** сохранена и применена!")
                await query.edit_message_text(text, parse_mode=parse_mode, reply_markup=kb)
                if owner.current():
                    clear_manual_role_state(user_id)
        except Exception as exc:
            logging.error("Error saving manual role: %s", exc, exc_info=True)
            if await interaction_current(owner):
                await query.edit_message_text("❌ Ошибка сохранения роли. Попробуйте позже.")

    await run_interaction(owner, save)
