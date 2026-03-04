# /app/handlers/msg_roles.py
"""Role and conversation rename state-machine handlers.

Extracted from messages.py to isolate the text-input state machines
for role creation (AI-generated and manual) and conversation renaming.
"""

import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from app import prompts, state
from app.handlers import agent, menus
from app.metrics import role_conv_metrics
from app.repos.chats import get_user_chat
from app.repos.conversations import rename_conversation
from app.state import (
    clear_custom_role_state,
    is_awaiting_custom_role_input,
    set_generated_role,
    set_generating_custom_role,
    set_last_custom_role_prompt,
)
from app.utils.formatting import TelegramFormatter


async def handle_conversation_rename(update: Update, context: ContextTypes.DEFAULT_TYPE, user_id: int) -> bool:
    """Handle text input for conversation rename. Returns True if consumed."""
    rename_conv_id = context.user_data.get("rename_conv_id")
    if rename_conv_id and update.message and update.message.text:
        try:
            new_title = update.message.text.strip()
            if 1 <= len(new_title) <= 100:
                await rename_conversation(user_id, rename_conv_id, new_title)
                context.user_data.pop("rename_conv_id", None)
                await role_conv_metrics.record_conversation_renamed()

                text, parse_mode, reply_markup = await menus.get_conversations_menu_content(user_id, 1)
                await update.message.reply_text(f"✅ Беседа переименована в: {new_title}")
                await update.message.reply_text(text, parse_mode=parse_mode, reply_markup=reply_markup)
                return True
            else:
                await update.message.reply_text("❌ Название должно быть от 1 до 100 символов. Попробуйте снова.")
                return True
        except Exception as e:
            logging.error("Error renaming conversation: %s", e, exc_info=True)
            await update.message.reply_text("❌ Не удалось переименовать беседу. Попробуйте позже.")
            context.user_data.pop("rename_conv_id", None)
            return True
    return False


async def handle_manual_role_input(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    user_id: int,
) -> bool:
    """Handle text input during manual role creation (title → prompt → preview).

    Returns True if the message was consumed by manual role creation flow.
    """
    from app.state import (
        get_manual_role_title,
        is_awaiting_manual_role_prompt,
        is_awaiting_manual_role_title,
        set_manual_role_title,
    )

    message_text = (update.message.text or "").strip() if update.message else ""
    if not message_text:
        return False

    # Step 1: User sends title
    if is_awaiting_manual_role_title(user_id):
        if len(message_text) > 100:
            await update.message.reply_text("⚠️ Название слишком длинное (макс. 100 символов). Попробуйте короче.")
            return True
        set_manual_role_title(user_id, message_text)
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("↩️ Отмена", callback_data="role_manual_cancel")]])
        await update.message.reply_text(
            f"✅ Название: **{message_text}**\n\n"
            f"Теперь введите **системный промпт** (инструкцию для бота).\n"
            f"Можно несколько строк — это будет поведение вашей роли:",
            parse_mode="Markdown",
            reply_markup=kb,
        )
        return True

    # Step 2: User sends prompt text
    if is_awaiting_manual_role_prompt(user_id):
        title = get_manual_role_title(user_id)
        from app.state import finish_manual_role_input, set_manual_role_prompt

        set_manual_role_prompt(user_id, message_text)
        finish_manual_role_input(user_id)
        preview_len = 200
        prompt_preview = message_text[:preview_len] + "..." if len(message_text) > preview_len else message_text
        kb = InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("💾 Сохранить и применить", callback_data="role_manual_save")],
                [InlineKeyboardButton("↩️ Отмена", callback_data="role_manual_cancel")],
            ]
        )
        await update.message.reply_text(
            f"📋 **Предпросмотр новой роли**\n\n"
            f"🏷 **Название:** {title}\n"
            f"📝 **Промпт:**\n`{prompt_preview}`\n\n"
            f"Нажмите кнопку ниже, чтобы сохранить:",
            parse_mode="Markdown",
            reply_markup=kb,
        )
        return True

    return False


async def handle_custom_role_generation(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    user_id: int,
    chat_id: int,
    message_text: str,
) -> bool:
    """Handle AI-powered custom role generation. Returns True if consumed."""
    if is_awaiting_custom_role_input(user_id):
        logging.info("User %s sent custom role description: %s", user_id, message_text)

        chat_state = await get_user_chat(user_id)
        from app.config import settings

        model_for_role = chat_state.model or settings.DEFAULT_MODEL
        key_data, model_used, _ = await agent._resolve_ai_request(model_for_role)

        if not key_data:
            kb = InlineKeyboardMarkup(
                [
                    [InlineKeyboardButton("🎭 Меню ролей", callback_data="open_roles")],
                    [InlineKeyboardButton("⬅️ Меню", callback_data="start_menu")],
                ]
            )
            await update.message.reply_text(
                "❌ Нет доступных ключей API для генерации роли.\nПопробуйте позже или создайте роль вручную.",
                reply_markup=kb,
            )
            clear_custom_role_state(user_id)
            return True

        progress_msg = await update.message.reply_text("🛠️ Генерирую роль…")
        set_generating_custom_role(user_id, True)

        history = [{"role": "user", "parts": [message_text]}]

        try:
            response_text, _ = await agent._get_ai_response(
                key_data["api_key"],
                history,
                model_used,
                system_instruction=prompts.PROMPT_ENGINEER_SYSTEM_PROMPT,
                user_id=user_id,
                chat_id=chat_id,
            )
            await agent._increment_key_usage(key_data["key_hash"], model_used)

            logging.info(f"Model response for role generation: {response_text[:500]}...")

            role_obj = prompts.extract_json_object(response_text)

            if not role_obj:
                error_kb = InlineKeyboardMarkup(
                    [
                        [InlineKeyboardButton("🔄 Попробовать снова", callback_data="role_create")],
                        [InlineKeyboardButton("❌ Отмена", callback_data="role_create_cancel")],
                    ]
                )
                if "503" in (response_text or "") or "unavailable" in (response_text or "").lower():
                    await progress_msg.edit_text(
                        "🔄 Сервер перегружен. Попробуйте ещё раз через несколько секунд.",
                        reply_markup=error_kb,
                    )
                else:
                    logging.error(f"Failed to parse role JSON. Response: {response_text}")
                    await progress_msg.edit_text(
                        "❌ Не удалось сгенерировать роль. Попробуйте изменить описание.",
                        reply_markup=error_kb,
                    )
                set_generating_custom_role(user_id, False)
                return True

            set_last_custom_role_prompt(user_id, message_text)
            set_generated_role(user_id, role_obj)

            title = role_obj.get("title", "Кастомная роль")
            purpose = role_obj.get("purpose", "")
            style = ", ".join(role_obj.get("style", [])[:3])

            preview = (
                f"🆕 *Новая роль:* {title}\n\n🎯 Цель: {purpose}\n🧭 Стиль: {style}\n\nПрименить сейчас или сохранить?"
            )

            kb_rows = [
                [InlineKeyboardButton("✅ Применить", callback_data="role_custom_apply")],
                [InlineKeyboardButton("💾 Сохранить", callback_data="role_custom_save")],
                [InlineKeyboardButton("🔄 Попробовать ещё раз", callback_data="role_custom_retry")],
                [InlineKeyboardButton("❌ Отмена", callback_data="role_clear")],
            ]

            formatted_text, parse_mode = TelegramFormatter.format_text(preview)
            await progress_msg.edit_text(
                formatted_text,
                parse_mode=parse_mode,
                reply_markup=InlineKeyboardMarkup(kb_rows),
            )

        except Exception as e:
            logging.error("Error generating custom role: %s", e, exc_info=True)
            error_kb = InlineKeyboardMarkup(
                [
                    [InlineKeyboardButton("🔄 Попробовать снова", callback_data="role_create")],
                    [InlineKeyboardButton("❌ Отмена", callback_data="role_create_cancel")],
                ]
            )
            await progress_msg.edit_text(
                "❌ Произошла ошибка при генерации роли.",
                reply_markup=error_kb,
            )
        finally:
            set_generating_custom_role(user_id, False)

        return True
    return False


async def handle_role_rename(update: Update, context: ContextTypes.DEFAULT_TYPE, user_id: int) -> bool:
    """Handle text input for role renaming. Returns True if consumed."""
    rename_role_raw = context.user_data.get("rename_role_id") if context.user_data else None
    if rename_role_raw and update.message and update.message.text:
        try:
            new_title = update.message.text.strip()
            role_id = int(rename_role_raw)
            if 1 <= len(new_title) <= 100:
                from app.repos.roles import rename_custom_role

                await rename_custom_role(role_id, user_id, new_title)
                context.user_data.pop("rename_role_id", None)
                await update.message.reply_text(f"✅ Роль переименована в: {new_title}")
                chat_state = await get_user_chat(user_id)
                text, parse_mode, reply_markup = await menus.get_roles_menu_content(
                    user_id,
                    chat_state,
                    view_mode="role_details",
                    role_key=f"user_role:{role_id}",
                )
                await update.message.reply_text(text, parse_mode=parse_mode, reply_markup=reply_markup)
                return True
            else:
                await update.message.reply_text("❌ Название должно быть от 1 до 100 символов. Попробуйте снова.")
                return True
        except Exception as e:
            logging.error("Error renaming role: %s", e, exc_info=True)
            await update.message.reply_text("❌ Не удалось переименовать роль. Попробуйте позже.")
            context.user_data.pop("rename_role_id", None)
            return True
    return False
