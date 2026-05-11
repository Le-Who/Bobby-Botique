# /app/handlers/msg_roles.py
"""Role and conversation rename state-machine handlers.

Extracted from messages.py to isolate the text-input state machines
for role creation (AI-generated and manual) and conversation renaming.
"""

import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from app.handlers import menus
from app.i18n import t
from app.metrics import role_conv_metrics
from app.prompt_registry import get_registry
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
from app.utils.json_utils import extract_json_object


async def handle_edit_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE, user_id: int) -> bool:
    """Handle text input for role prompt editing (manual or AI-enhanced).

    Returns True if the message was consumed by the edit prompt flow.
    """
    if not update.message or not update.message.text:
        return False

    message_text = update.message.text.strip()
    if not message_text:
        return False

    # ── Mode 1: Manual prompt replacement ────────────────────────────────
    edit_role_id = context.user_data.get("edit_prompt_role_id") if context.user_data else None
    if edit_role_id:
        try:
            role_id = int(edit_role_id)
            role_key = context.user_data.get("edit_prompt_role_key", f"user_role:{role_id}")

            from app.repos.roles import get_custom_role_prompt, update_custom_role_prompt

            old_prompt = await get_custom_role_prompt(role_id, user_id)
            success = await update_custom_role_prompt(role_id, user_id, message_text)

            # Clean up state
            context.user_data.pop("edit_prompt_role_id", None)
            context.user_data.pop("edit_prompt_role_key", None)

            if not success:
                await update.message.reply_text(t("role.prompt_update_failed"))
                return True

            # If this role is currently active, update system_prompt
            chat_state = await get_user_chat(user_id)
            if old_prompt and chat_state.system_prompt == old_prompt:
                chat_state.system_prompt = message_text
                from app.repos.chats import update_user_chat

                await update_user_chat(user_id, chat_state)

            await update.message.reply_text(t("role.prompt_updated"))

            # Show updated role details
            text, parse_mode, reply_markup = await menus.get_roles_menu_content(
                user_id, chat_state, view_mode="role_details", role_key=role_key
            )
            await update.message.reply_text(text, parse_mode=parse_mode, reply_markup=reply_markup)
            return True

        except Exception as e:
            logging.error("Error updating role prompt: %s", e, exc_info=True)
            await update.message.reply_text(t("role.prompt_update_error"))
            context.user_data.pop("edit_prompt_role_id", None)
            context.user_data.pop("edit_prompt_role_key", None)
            return True

    # ── Mode 2: AI-enhanced prompt editing ───────────────────────────────
    ai_role_id = context.user_data.get("edit_prompt_ai_role_id") if context.user_data else None
    if ai_role_id:
        try:
            role_id = int(ai_role_id)
            role_key = context.user_data.get("edit_prompt_ai_role_key", f"user_role:{role_id}")
            current_prompt = context.user_data.get("edit_prompt_ai_current", "")

            # Clear the "awaiting" state so we don't loop
            context.user_data.pop("edit_prompt_ai_role_id", None)
            context.user_data.pop("edit_prompt_ai_role_key", None)

            progress_msg = await update.message.reply_text(t("role.ai_enhancing"))

            # Build the enhancement prompt — minimal, no safety injection
            enhance_instruction = (
                "Generate an enhanced version of this prompt "
                "(reply with only the enhanced prompt — no conversation, "
                "explanations, lead-in, bullet points, placeholders, or surrounding quotes):\n\n"
                f"{current_prompt}\n\n"
                f"User's requested changes: {message_text}"
            )

            # Use the same AI pipeline as role generation
            from app.config import settings
            from app.handlers.ai_core import _get_ai_response, _increment_key_usage, _resolve_ai_request

            chat_state = await get_user_chat(user_id)
            model_for_edit = chat_state.model or settings.DEFAULT_MODEL
            key_data, model_used, _ = await _resolve_ai_request(model_for_edit)

            if not key_data:
                await progress_msg.edit_text(t("error.no_api_keys"))
                return True

            history = [{"role": "user", "parts": [enhance_instruction]}]
            response_text, _ = await _get_ai_response(
                key_data["api_key"],
                history,
                model_used,
                user_id=user_id,
                chat_id=user_id,
            )

            await _increment_key_usage(key_data["key_hash"], model_used)

            if not response_text or not response_text.strip():
                await progress_msg.edit_text(t("role.ai_no_result"))
                return True

            enhanced_prompt = response_text.strip()

            # Store preview for save callback
            if context.user_data is not None:
                context.user_data["edit_prompt_ai_preview"] = enhanced_prompt
                context.user_data["edit_prompt_ai_save_role_id"] = role_id
                context.user_data["edit_prompt_ai_save_role_key"] = role_key
                # Keep current prompt for active-role check on save
                context.user_data["edit_prompt_ai_current"] = current_prompt

            preview_text = t("role.ai_enhanced_preview", prompt=enhanced_prompt)
            kb = InlineKeyboardMarkup(
                [
                    [InlineKeyboardButton(t("role.btn_save"), callback_data="role_edit_ai_save")],
                    [InlineKeyboardButton(t("role.btn_edit"), callback_data=f"role_edit_ai_tweak:{role_key}")],
                    [InlineKeyboardButton(t("role.btn_cancel"), callback_data=f"role_edit_cancel:{role_key}")],
                ]
            )
            fmt_text, fmt_pm = TelegramFormatter.format_text(preview_text)
            await progress_msg.edit_text(fmt_text, parse_mode=fmt_pm, reply_markup=kb)
            return True

        except Exception as e:
            logging.error("Error in AI prompt enhancement: %s", e, exc_info=True)
            await update.message.reply_text(t("role.ai_enhance_error"))
            # Clean up
            if context.user_data is not None:
                context.user_data.pop("edit_prompt_ai_role_id", None)
                context.user_data.pop("edit_prompt_ai_role_key", None)
                context.user_data.pop("edit_prompt_ai_current", None)
            return True

    return False


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
                await update.message.reply_text(t("role.renamed", title=new_title))
                await update.message.reply_text(text, parse_mode=parse_mode, reply_markup=reply_markup)
                return True
            else:
                await update.message.reply_text(t("role.name_length_error"))
                return True
        except Exception as e:
            logging.error("Error renaming conversation: %s", e, exc_info=True)
            await update.message.reply_text(t("role.rename_error"))
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
            await update.message.reply_text(t("role.title_too_long"))
            return True
        set_manual_role_title(user_id, message_text)
        kb = InlineKeyboardMarkup([[InlineKeyboardButton(t("role.btn_cancel"), callback_data="role_manual_cancel")]])
        title_text = t("role.title_set", title=message_text)
        fmt_text, fmt_pm = TelegramFormatter.format_text(title_text)
        await update.message.reply_text(
            fmt_text,
            parse_mode=fmt_pm,
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
                [InlineKeyboardButton(t("role.btn_save_apply"), callback_data="role_manual_save")],
                [InlineKeyboardButton(t("role.btn_cancel"), callback_data="role_manual_cancel")],
            ]
        )
        preview_text = t("role.preview_title", title=title, prompt=prompt_preview)
        fmt_text, fmt_pm = TelegramFormatter.format_text(preview_text)
        await update.message.reply_text(
            fmt_text,
            parse_mode=fmt_pm,
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
        from app.handlers import agent
        model_for_role = chat_state.model or settings.DEFAULT_MODEL
        key_data, model_used, _ = await agent._resolve_ai_request(model_for_role)

        if not key_data:
            kb = InlineKeyboardMarkup(
                [
                    [InlineKeyboardButton(t("role.btn_roles_menu"), callback_data="open_roles")],
                    [InlineKeyboardButton(t("menu.back_to_menu"), callback_data="start_menu")],
                ]
            )
            await update.message.reply_text(
                t("role.no_api_keys"),
                reply_markup=kb,
            )
            clear_custom_role_state(user_id)
            return True

        progress_msg = await update.message.reply_text(t("role.generating"))
        set_generating_custom_role(user_id, True)

        history = [{"role": "user", "parts": [message_text]}]

        try:
            response_text, _ = await agent._get_ai_response(
                key_data["api_key"],
                history,
                model_used,
                system_instruction=get_registry().get("prompt_engineer").text,
                user_id=user_id,
                chat_id=chat_id,
            )
            await agent._increment_key_usage(key_data["key_hash"], model_used)

            logging.info("Model response for role generation: %.500s...", response_text)

            role_obj = extract_json_object(response_text)

            if not role_obj:
                error_kb = InlineKeyboardMarkup(
                    [
                        [InlineKeyboardButton(t("role.btn_retry"), callback_data="role_create")],
                        [InlineKeyboardButton(t("btn.cancel"), callback_data="role_create_cancel")],
                    ]
                )
                if "503" in (response_text or "") or "unavailable" in (response_text or "").lower():
                    await progress_msg.edit_text(
                        t("role.server_overloaded"),
                        reply_markup=error_kb,
                    )
                else:
                    logging.error("Failed to parse role JSON. Response: %s", response_text)
                    await progress_msg.edit_text(
                        t("role.generation_failed"),
                        reply_markup=error_kb,
                    )
                set_generating_custom_role(user_id, False)
                return True

            set_last_custom_role_prompt(user_id, message_text)
            set_generated_role(user_id, role_obj)

            title = role_obj.get("title", t("role.custom_default_title"))
            purpose = role_obj.get("purpose", "")
            style = ", ".join(role_obj.get("style", [])[:3])

            preview = t("role.new_preview", title=title, purpose=purpose, style=style)

            kb_rows = [
                [InlineKeyboardButton(t("role.btn_apply"), callback_data="role_custom_apply")],
                [InlineKeyboardButton(t("role.btn_save"), callback_data="role_custom_save")],
                [InlineKeyboardButton(t("role.btn_retry_custom"), callback_data="role_custom_retry")],
                [InlineKeyboardButton(t("btn.cancel"), callback_data="role_clear")],
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
                    [InlineKeyboardButton(t("role.btn_retry"), callback_data="role_create")],
                    [InlineKeyboardButton(t("btn.cancel"), callback_data="role_create_cancel")],
                ]
            )
            await progress_msg.edit_text(
                t("role.generation_error"),
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
                await update.message.reply_text(t("role.role_renamed", title=new_title))
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
                await update.message.reply_text(t("role.name_length_error"))
                return True
        except Exception as e:
            logging.error("Error renaming role: %s", e, exc_info=True)
            await update.message.reply_text(t("role.role_rename_error"))
            context.user_data.pop("rename_role_id", None)
            return True
    return False
