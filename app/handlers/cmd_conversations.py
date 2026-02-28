# /app/handlers/cmd_conversations.py
"""Conversation management commands: save, list, switch, rename, delete."""

from datetime import datetime

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from app import prompts
from app.handlers import menus
from app.metrics import role_conv_metrics
from app.repos.chats import get_user_chat
from app.repos.conversations import rename_conversation, save_conversation, switch_to_conversation
from app.utils.decorators import authorized_only


@authorized_only
async def save_conversation_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    # context используется for получения argumentов команды
    """Сохранить текущую беседу"""
    user_id = update.effective_user.id

    args = context.args
    if not args:
        # AI-powered auto-title from first messages
        chat_state = await get_user_chat(user_id)
        if chat_state and chat_state.history:
            from app.repos.analytics import generate_auto_title

            title = generate_auto_title(
                chat_state.history if isinstance(chat_state.history, list) else []
            )
        else:
            title = f"Беседа от {datetime.now().strftime('%d.%m.%Y %H:%M')}"
    else:
        title = " ".join(args)

    if len(title) > 100:
        title = title[:97] + "..."

    # Определяем current role
    chat_state = await get_user_chat(user_id)
    role_type = None
    role_id = None

    if chat_state and chat_state.system_prompt:
        # Check, есть ли активная role
        for key, role_data in prompts.DEFAULT_ROLES.items():
            if role_data["prompt"] in chat_state.system_prompt:
                role_type = "role"
                role_id = key
                break

    conv_id = await save_conversation(user_id, title, role_type, role_id)
    if conv_id:
        await role_conv_metrics.record_conversation_saved()
        await update.message.reply_text(f"✅ Беседа сохранена с ID: {conv_id}")
    else:
        await update.message.reply_text("❌ Ошибка при сохранении беседы")


@authorized_only
async def conversations_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    # context используется for получения argumentов команды
    """Показать список сохранённых бесед"""
    user_id = update.effective_user.id

    # Parse arguments for пагинации
    page = 1
    if context.args and context.args[0].isdigit():
        page = int(context.args[0])

    text, parse_mode, reply_markup = await menus.get_conversations_menu_content(
        user_id, page
    )

    if reply_markup is None:
        await update.message.reply_text(text)
    else:
        await update.message.reply_text(
            text, parse_mode=parse_mode, reply_markup=reply_markup
        )


@authorized_only
async def switch_conversation_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    # context используется for получения argumentов команды
    """Переключиться на беседу"""
    user_id = update.effective_user.id

    args = context.args
    if not args or not args[0].isdigit():
        await update.message.reply_text(
            "Использование: /switch <ID беседы>\n\nИспользуйте /conversations для просмотра списка бесед."
        )
        return

    conv_id = int(args[0])
    success = await switch_to_conversation(user_id, conv_id)

    if success:
        await role_conv_metrics.record_conversation_switched()
        await update.message.reply_text(f"✅ Переключились на беседу {conv_id}")
    else:
        await update.message.reply_text(
            "❌ Беседа не найдена или у вас нет доступа к ней"
        )


@authorized_only
async def rename_conversation_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    # context используется for получения argumentов команды
    """Переименовать беседу"""
    user_id = update.effective_user.id

    args = context.args
    if len(args) < 2 or not args[0].isdigit():
        await update.message.reply_text(
            "Использование: /rename <ID беседы> <новое название>"
        )
        return

    conv_id = int(args[0])
    new_title = " ".join(args[1:])

    if len(new_title) > 100:
        await update.message.reply_text(
            "❌ Название беседы слишком длинное (максимум 100 символов)"
        )
        return

    success = await rename_conversation(user_id, conv_id, new_title)

    if success:
        await role_conv_metrics.record_conversation_renamed()
        await update.message.reply_text(
            f"✅ Беседа {conv_id} переименована в '{new_title}'"
        )
    else:
        await update.message.reply_text(
            "❌ Беседа не найдена или у вас нет доступа к ней"
        )


@authorized_only
async def delete_conversation_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    # context используется for получения argumentов команды
    """Удалить беседу"""

    args = context.args
    if not args or not args[0].isdigit():
        await update.message.reply_text(
            "Использование: /delete <ID беседы>\n\nИспользуйте /conversations для просмотра списка бесед."
        )
        return

    conv_id = int(args[0])

    # Подтверждение удаления
    keyboard = [
        [
            InlineKeyboardButton(
                "✅ Да, удалить", callback_data=f"conv_delete_confirm:{conv_id}"
            )
        ],
        [InlineKeyboardButton("❌ Отмена", callback_data="conv_delete_cancel")],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await update.message.reply_text(
        f"⚠️ Вы уверены, что хотите удалить беседу {conv_id}?\n\nЭто действие нельзя отменить!",
        reply_markup=reply_markup,
    )
