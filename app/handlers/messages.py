# /app/handlers/messages.py

import asyncio
import logging
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes, MessageHandler, filters, Application
from telegram.error import BadRequest, NetworkError

from app.config import settings
from app import database as db
from app import state
from app.document_processor import process_uploaded_document
from app.metrics import metrics_collector, role_conv_metrics
from app.utils.formatting import TelegramFormatter
from app.utils.api_logger import api_logger
from app import prompts
from app.handlers import agent
from app.state import (
    is_awaiting_custom_role_input,
    set_generated_role,
    clear_custom_role_state,
    set_last_custom_role_prompt,
    set_generating_custom_role,
)
from app.security import check_user_rate_limit
from app.handlers import menus
from app.request_context import set_request_id
from app.tracing import bind_request_span

# Глобальный limitер for тяжёлых AI-задач, чтобы fromбежать перегрузки event loop/провайдеров
_HEAVY_REQUEST_LIMIT = max(
    1, int(getattr(settings, "MAX_CONCURRENT_HEAVY_REQUESTS", 4))
)
_HEAVY_REQUEST_SEMAPHORE = asyncio.Semaphore(_HEAVY_REQUEST_LIMIT)

# Глобальный dictionary for хранения групп fromображений
MEDIA_GROUPS = {}
MEDIA_GROUPS_TTL = {}  # TTL for автоматической очистки старых групп
MEDIA_GROUP_TIMEOUT = 300  # 5 минут timeout for groups fromображений


async def cleanup_old_media_groups() -> None:
    """Очищает старые группы изображений для предотвращения утечки памяти"""
    current_time = asyncio.get_event_loop().time()
    expired_groups = []

    for media_group_id, created_at in MEDIA_GROUPS_TTL.items():
        if current_time - created_at > MEDIA_GROUP_TIMEOUT:
            expired_groups.append(media_group_id)

    for media_group_id in expired_groups:
        MEDIA_GROUPS.pop(media_group_id, None)
        MEDIA_GROUPS_TTL.pop(media_group_id, None)
        logging.info("🧹 Очищена устаревшая группа изображений: %s", media_group_id)

    if expired_groups:
        logging.info("🧹 Очищено %s устаревших групп изображений", len(expired_groups))


# Запускаем периодическую очистку on импорте модуля
_cleanup_task = None


async def start_media_groups_cleanup() -> None:
    """Запускает периодическую очистку групп изображений"""
    global _cleanup_task
    if _cleanup_task and not _cleanup_task.done():
        return

    async def cleanup_loop() -> None:
        while True:
            try:
                await asyncio.sleep(60)  # Check каждую минуту
                await cleanup_old_media_groups()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logging.error("Error in media groups cleanup: %s", e)
                await asyncio.sleep(60)

    _cleanup_task = asyncio.create_task(cleanup_loop())
    logging.info("Media groups cleanup task started")


async def _handle_conversation_rename(
    update: Update, context: ContextTypes.DEFAULT_TYPE, user_id: int
) -> bool:
    """Обрабатывает переименование беседы. Возвращает True, если обработано."""
    rename_conv_id = context.user_data.get("rename_conv_id")
    if rename_conv_id and update.message and update.message.text:
        try:
            new_title = update.message.text.strip()
            if 1 <= len(new_title) <= 100:
                await db.rename_conversation(user_id, rename_conv_id, new_title)
                context.user_data.pop("rename_conv_id", None)
                await role_conv_metrics.record_conversation_renamed()

                # Показываем list бесед с обновленным названием
                (
                    text,
                    parse_mode,
                    reply_markup,
                ) = await menus.get_conversations_menu_content(user_id, 1)

                await update.message.reply_text(
                    f"✅ Беседа переименована в: {new_title}"
                )
                await update.message.reply_text(
                    text, parse_mode=parse_mode, reply_markup=reply_markup
                )
                return True
            else:
                await update.message.reply_text(
                    "❌ Название должно быть от 1 до 100 символов. Попробуйте снова."
                )
                return True
        except Exception as e:
            logging.error("Error renaming conversation: %s", e)
            await update.message.reply_text(
                "❌ Не удалось переименовать беседу. Попробуйте позже."
            )
            context.user_data.pop("rename_conv_id", None)
            return True
    return False


async def _handle_manual_role_input(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    user_id: int,
) -> bool:
    """Handle text input during manual role creation (title → prompt → preview).
    
    Returns True if the message was consumed by manual role creation flow.
    """
    from app.state import (
        is_awaiting_manual_role_title,
        is_awaiting_manual_role_prompt,
        set_manual_role_title,
        get_manual_role_title,
        clear_manual_role_state,
    )

    message_text = (update.message.text or "").strip() if update.message else ""
    if not message_text:
        return False

    # Step 1: User sends title
    if is_awaiting_manual_role_title(user_id):
        if len(message_text) > 100:
            await update.message.reply_text(
                "⚠️ Название слишком длинное (макс. 100 символов). Попробуйте короче."
            )
            return True
        set_manual_role_title(user_id, message_text)
        kb = InlineKeyboardMarkup(
            [[InlineKeyboardButton("↩️ Отмена", callback_data="role_manual_cancel")]]
        )
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
        # Store prompt in state (NOT context.user_data — it doesn't
        # survive between the text-message Update and the button callback).
        from app.state import set_manual_role_prompt, finish_manual_role_input
        set_manual_role_prompt(user_id, message_text)
        # Mark input phase as done, but KEEP title+prompt for save callback
        finish_manual_role_input(user_id)
        preview_len = 200
        prompt_preview = (
            message_text[:preview_len] + "..." if len(message_text) > preview_len else message_text
        )
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


async def _handle_custom_role_generation(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    user_id: int,
    chat_id: int,
    message_text: str,
) -> bool:
    """Обрабатывает генерацию кастомной роли. Возвращает True, если обработано."""
    if is_awaiting_custom_role_input(user_id):
        # If мы ждем ввода описания roles
        logging.info("User %s sent custom role description: %s", user_id, message_text)

        # Get settings и keys
        chat_state = await db.get_user_chat(user_id)
        from app.config import settings

        model_for_role = chat_state.model or settings.DEFAULT_MODEL

        # Используем универсальную функцию for получения keyа
        key_data, model_used, resolution = await agent._resolve_ai_request(
            model_for_role
        )

        if not key_data:
            kb = InlineKeyboardMarkup([
                [InlineKeyboardButton("🎭 Меню ролей", callback_data="open_roles")],
                [InlineKeyboardButton("⬅️ Меню", callback_data="start_menu")],
            ])
            await update.message.reply_text(
                "❌ Нет доступных ключей API для генерации роли.\n"
                "Попробуйте позже или создайте роль вручную.",
                reply_markup=kb,
            )
            clear_custom_role_state(user_id)
            return True

        progress_msg = await update.message.reply_text("🛠️ Генерирую роль…")
        set_generating_custom_role(user_id, True)

        # Build request
        history = [{"role": "user", "parts": [message_text]}]

        try:
            # Используем универсальную функцию for получения responseа
            response_text, _ = await agent._get_ai_response(
                key_data["api_key"],
                history,
                model_used,
                system_instruction=prompts.PROMPT_ENGINEER_SYSTEM_PROMPT,
                user_id=user_id,
                chat_id=chat_id,
            )

            # Инкрементируем использование keyа
            await agent._increment_key_usage(key_data["key_hash"], model_used)

            # Log response models for отладки
            logging.info(
                f"Model response for role generation: {response_text[:500]}..."
            )

            role_obj = prompts.extract_json_object(response_text)

            if not role_obj:
                # Build error keyboard with retry/cancel
                error_kb = InlineKeyboardMarkup(
                    [
                        [
                            InlineKeyboardButton(
                                "🔄 Попробовать снова",
                                callback_data="role_create",
                            )
                        ],
                        [
                            InlineKeyboardButton(
                                "❌ Отмена",
                                callback_data="role_create_cancel",
                            )
                        ],
                    ]
                )
                # Processing явной 503 ошибки from textа
                if (
                    "503" in (response_text or "")
                    or "unavailable" in (response_text or "").lower()
                ):
                    await progress_msg.edit_text(
                        "🔄 Сервер перегружен. Попробуйте ещё раз через несколько секунд.",
                        reply_markup=error_kb,
                    )
                else:
                    logging.error(
                        f"Failed to parse role JSON. Response: {response_text}"
                    )
                    await progress_msg.edit_text(
                        "❌ Не удалось сгенерировать роль. Попробуйте изменить описание.",
                        reply_markup=error_kb,
                    )
                set_generating_custom_role(user_id, False)
                return True

            # Успешно сгенерировали
            set_last_custom_role_prompt(user_id, message_text)
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
                [
                    InlineKeyboardButton(
                        "✅ Применить", callback_data="role_custom_apply"
                    )
                ],
                [
                    InlineKeyboardButton(
                        "💾 Сохранить", callback_data="role_custom_save"
                    )
                ],
                [
                    InlineKeyboardButton(
                        "🔄 Попробовать ещё раз", callback_data="role_custom_retry"
                    )
                ],
                [InlineKeyboardButton("❌ Отмена", callback_data="role_clear")],
            ]

            formatted_text, parse_mode = TelegramFormatter.format_text(preview)
            await progress_msg.edit_text(
                formatted_text,
                parse_mode=parse_mode,
                reply_markup=InlineKeyboardMarkup(kb),
            )

        except Exception as e:
            logging.error("Error generating custom role: %s", e, exc_info=True)
            error_kb = InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            "🔄 Попробовать снова",
                            callback_data="role_create",
                        )
                    ],
                    [
                        InlineKeyboardButton(
                            "❌ Отмена",
                            callback_data="role_create_cancel",
                        )
                    ],
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


async def _handle_role_rename(
    update: Update, context: ContextTypes.DEFAULT_TYPE, user_id: int
) -> bool:
    """Обрабатывает переименование роли. Возвращает True, если обработано."""
    if (
        context.user_data.get("rename_role_id")
        and update.message
        and update.message.text
    ):
        try:
            new_title = update.message.text.strip()
            role_id = int(context.user_data.get("rename_role_id"))
            if 1 <= len(new_title) <= 100:
                await db.db_query(
                    "UPDATE user_roles SET title = $1 WHERE id = $2 AND user_id = $3",
                    (new_title, role_id, user_id),
                )
                context.user_data.pop("rename_role_id", None)
                await update.message.reply_text(f"✅ Роль переименована в: {new_title}")
                # Return в детали roles
                chat_state = await db.get_user_chat(user_id)
                text, parse_mode, reply_markup = await menus.get_roles_menu_content(
                    user_id,
                    chat_state,
                    view_mode="role_details",
                    role_key=f"user_role:{role_id}",
                )
                await update.message.reply_text(
                    text, parse_mode=parse_mode, reply_markup=reply_markup
                )
                return True
            else:
                await update.message.reply_text(
                    "❌ Название должно быть от 1 до 100 символов. Попробуйте снова."
                )
                return True
        except Exception as e:
            logging.error("Error renaming role: %s", e)
            await update.message.reply_text(
                "❌ Не удалось переименовать роль. Попробуйте позже."
            )
            context.user_data.pop("rename_role_id", None)
            return True
    return False


async def _process_media_group_update(
    update: Update, context: ContextTypes.DEFAULT_TYPE, user_id: int, chat_id: int
) -> bool:
    """Обрабатывает обновление, если это часть медиа-группы. Возвращает True, если обработано."""
    is_photo = bool(update.message.photo)
    media_group_id = update.message.media_group_id if update.message else None

    if is_photo and media_group_id:
        logging.info(
            f"📸 Получено изображение с media_group_id {media_group_id} от пользователя {user_id}"
        )

        # Initialize группу, if её нет
        if media_group_id not in MEDIA_GROUPS:
            current_time = asyncio.get_event_loop().time()
            MEDIA_GROUPS[media_group_id] = {
                "user_id": user_id,
                "chat_id": chat_id,
                "messages": [],
                "caption": update.message.caption,
                "created_at": current_time,
                "placeholder_message": None,
                "processing_scheduled": False,
            }
            # Устанавливаем TTL for автоматической очистки
            MEDIA_GROUPS_TTL[media_group_id] = current_time
            # Запускаем очистку on первом создании groups
            if _cleanup_task is None or _cleanup_task.done():
                asyncio.create_task(start_media_groups_cleanup())

        # Add message в группу
        MEDIA_GROUPS[media_group_id]["messages"].append(update.message)

        # If это первое message groups, создаем placeholder и планируем обработку
        if len(MEDIA_GROUPS[media_group_id]["messages"]) == 1:
            placeholder_message = await update.message.reply_text(
                "🖼️ Обрабатываю изображение..."
            )
            MEDIA_GROUPS[media_group_id]["placeholder_message"] = placeholder_message
            logging.info("📸 Создан placeholder для media_group_id %s", media_group_id)

            # Планируем обработку via 1 секунду
            if not MEDIA_GROUPS[media_group_id]["processing_scheduled"]:
                MEDIA_GROUPS[media_group_id]["processing_scheduled"] = True
                asyncio.create_task(
                    delayed_process_media_group(media_group_id, context, 1.0)
                )

        return True
    return False


async def _handle_document_mode_interaction(
    update: Update, context: ContextTypes.DEFAULT_TYPE, user_id: int
) -> bool:
    """Обрабатывает взаимодействие в режиме документов. Возвращает True, если обработано."""
    from app.state import is_in_document_mode, get_selected_document_id

    if is_in_document_mode(user_id):
        document_id = get_selected_document_id(user_id)
        logging.info(
            "User %s is in document mode, document_id: %s", user_id, document_id
        )
        if document_id:
            await handle_document_question(update, context, document_id)
        else:
            await update.message.reply_text(
                "📋 Вы находитесь в режиме работы с документами.\n\n"
                "💡 *Доступные действия:*\n"
                "• Загрузите новый документ\n"
                "• Выберите документ из списка\n"
                "• Используйте кнопки под сообщениями\n\n"
                "🔄 *Для выхода из режима документов:*\n"
                "• Нажмите кнопку '❌ Отменить работу с документами'\n"
                "• Или отправьте команду /documents"
            )
        return True
    return False


async def handle_request(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Обрабатывает входящие сообщения"""
    # Validation входных данных
    if not update or not update.effective_user:
        logging.error("Invalid update object received")
        return

    user_id = update.effective_user.id
    chat_id = update.effective_chat.id

    # Hydrate persisted user state from DB (lazy, fast no-op if already loaded)
    from app.state import ensure_state_loaded
    await ensure_state_loaded(user_id)

    request_id = set_request_id(f"tgmsg-{chat_id}-{getattr(update, 'update_id', 'na')}")

    # Correlation contract: request_id is propagated as trace_id baseline.
    with bind_request_span(request_id, span_name="telegram-message"):
        # Validation user_id
        if not isinstance(user_id, int) or user_id <= 0:
            logging.error("Invalid user_id: %s", user_id)
            return

        # Process медиа-groups
        if await _process_media_group_update(update, context, user_id, chat_id):
            return

        # Детальное логирование Telegram API requestа
        message_type = (
            "photo"
            if update.message.photo
            else "text"
            if update.message.text
            else "other"
        )
        start_time = api_logger.log_telegram_request(
            method="handle_message",
            chat_id=chat_id,
            user_id=user_id,
            message_type=message_type,
        )

        # Validation textа messages
        message_text = (
            update.message.text if update.message and update.message.text else "No text"
        )
        if len(message_text) > settings.TELEGRAM_MESSAGE_LIMIT:
            logging.warning(
                "Message too long from user %s: %d chars", user_id, len(message_text)
            )
            await update.message.reply_text(
                "❌ Сообщение слишком длинное. Максимум 4096 символов.\n"
                "Сократите текст и отправьте снова."
            )
            return

    logging.info("Received message from user %s: %s", user_id, message_text[:100])

    # Check rate limit for защиты от злоупотреблений
    if not await check_user_rate_limit(user_id):
        logging.warning("Rate limit exceeded for user %s", user_id)
        await update.message.reply_text(
            "⏱️ Превышен лимит запросов. Пожалуйста, подождите немного перед следующим запросом."
        )
        return

    if not await db.is_authorized(user_id):
        logging.warning("Unauthorized user %s attempted to use bot", user_id)
        return

    # Process documents
    if update.message.document:
        logging.info(
            "Processing document from user %s: %s",
            user_id,
            update.message.document.file_name,
        )
        await handle_document(update, context)
        return

    # Переименование roles
    if await _handle_role_rename(update, context, user_id):
        return

    # Переименование беседы
    if await _handle_conversation_rename(update, context, user_id):
        return

    # Ручное создание роли (без AI)
    if await _handle_manual_role_input(update, context, user_id):
        return

    # Генерация кастомной roles
    if await _handle_custom_role_generation(
        update, context, user_id, chat_id, message_text
    ):
        return

    # Check, находится ли user в режиме работы с documentами
    if await _handle_document_mode_interaction(update, context, user_id):
        return

    # Save afterдний userский ввод for buttons "🔁 Попробовать ещё раз"
    try:
        from app.state import set_last_sent_message

        if update.message and update.message.text:
            set_last_sent_message(user_id, update.message.text)
    except Exception:
        logging.exception("Error saving last sent message text")

    # Check, есть ли image (одиночное)
    is_photo = bool(update.message.photo)

    if is_photo:
        logging.info("Processing single photo from user %s", user_id)
        placeholder_message = await update.message.reply_text(
            "🖼️ Обрабатываю изображение..."
        )
    else:
        logging.info("Processing text message from user %s", user_id)
        placeholder_message = await update.message.reply_text("🤔 Думаю...")

    # ── 3-stage heartbeat: reassure user during long waits ──────────
    _WAIT_STAGES = [
        (15, "⏳ Обрабатываю ваш запрос..."),
        (30, "⏳ Ответ генерируется, подождите ещё немного..."),
        (50, "⏳ Запрос обрабатывается дольше обычного. Пожалуйста, подождите..."),
    ]
    done_event = asyncio.Event()

    async def _heartbeat() -> None:
        try:
            elapsed = 0
            for threshold, text in _WAIT_STAGES:
                wait_for = threshold - elapsed
                if wait_for <= 0:
                    continue
                try:
                    await asyncio.wait_for(done_event.wait(), timeout=wait_for)
                    return  # Main task finished — stop heartbeat
                except asyncio.TimeoutError:
                    pass
                elapsed = threshold
                try:
                    await placeholder_message.edit_text(text)
                except Exception:
                    pass  # Message already edited by main task or deleted
        except asyncio.CancelledError:
            pass  # Cleanly stop when task_wrapper cancels us

    heartbeat_task = asyncio.create_task(_heartbeat())

    # Обычная обработка сообщений
    async def task_wrapper() -> None:
        try:
            async with _HEAVY_REQUEST_SEMAPHORE:
                async with state.get_user_lock(user_id):
                    logging.info("Starting task processing for user %s", user_id)

                    # Восстанавливаем обработку via agent
                    try:
                        from app.handlers.agent import process_long_request

                        await process_long_request(placeholder_message, update, context)
                    except ImportError:
                        # Fallback if agent недоступен
                        await placeholder_message.edit_text(
                            "🤔 Обрабатываю ваш запрос... (упрощенный режим)"
                        )

                    logging.info("Completed task processing for user %s", user_id)

                # Log успешный response Telegram API
                api_logger.log_telegram_response(
                    start_time=start_time,
                    method="handle_message",
                    success=True,
                    chat_id=chat_id,
                    user_id=user_id,
                )

        except Exception as e:
            logging.error(
                f"Error in task wrapper for user {user_id}: {e}", exc_info=True
            )
            try:
                from app.errors import build_retry_and_roles_keyboard
                await placeholder_message.edit_text(
                    "❌ Произошла ошибка при обработке запроса. Попробуйте ещё раз.",
                    reply_markup=build_retry_and_roles_keyboard()
                )
            except (BadRequest, NetworkError) as edit_error:
                logging.error("Could not edit placeholder message: %s", edit_error)

            # Log error Telegram API
            api_logger.log_telegram_response(
                start_time=start_time,
                method="handle_message",
                success=False,
                chat_id=chat_id,
                user_id=user_id,
                error=str(e),
            )
        finally:
            done_event.set()  # Signal heartbeat to stop
            heartbeat_task.cancel()

    # Запускаем обработку в фоне
    asyncio.create_task(task_wrapper())


async def delayed_process_media_group(
    media_group_id: str, context: ContextTypes.DEFAULT_TYPE, delay: float
) -> None:
    """Отложенная обработка группы изображений"""
    await asyncio.sleep(delay)

    if media_group_id in MEDIA_GROUPS:
        group_data = MEDIA_GROUPS[media_group_id]
        message_count = len(group_data["messages"])

        logging.info(
            f"⏰ Отложенная обработка media_group_id {media_group_id}: {message_count} сообщений"
        )

        try:
            # If это действительно group (больше 1 images), обрабатываем как группу
            if message_count > 1:
                logging.info("🔄 Обрабатываю группу из %s изображений", message_count)
                await process_media_group(media_group_id, context)
            else:
                # If это одиночное image, обрабатываем via стандартный путь
                logging.info(
                    "📸 Одиночное изображение, перенаправляю в стандартную обработку"
                )
                await process_single_image_from_group(media_group_id, context)
        finally:
            # Clean up группу after обработки
            MEDIA_GROUPS.pop(media_group_id, None)
            MEDIA_GROUPS_TTL.pop(media_group_id, None)
            logging.info(
                f"🧹 Очищена обработанная группа изображений: {media_group_id}"
            )


async def process_single_image_from_group(
    media_group_id: str, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Обрабатывает одиночное изображение из группы"""
    if media_group_id not in MEDIA_GROUPS:
        return

    group_data = MEDIA_GROUPS[media_group_id]
    message = group_data["messages"][0]
    placeholder_message = group_data["placeholder_message"]

    # Create мок Update for совместимости
    mock_update = type(
        "MockUpdate",
        (),
        {
            "message": message,
            "effective_user": message.from_user,
            "effective_chat": message.chat,
        },
    )()

    try:
        # Process via стандартный путь
        from app.handlers.agent import process_long_request

        await process_long_request(placeholder_message, mock_update, context)
    except Exception as e:
        logging.error("Error processing single image from group: %s", e)
        try:
            from app.errors import build_retry_and_roles_keyboard
            await placeholder_message.edit_text(
                "❌ Произошла ошибка при обработке изображения. Попробуйте ещё раз.",
                reply_markup=build_retry_and_roles_keyboard(include_roles=False)
            )
        except (BadRequest, NetworkError) as edit_error:
            logging.error("Could not edit placeholder message: %s", edit_error)
    finally:
        # Clean up группу (вkeyая TTL)
        if media_group_id in MEDIA_GROUPS:
            del MEDIA_GROUPS[media_group_id]
        if media_group_id in MEDIA_GROUPS_TTL:
            del MEDIA_GROUPS_TTL[media_group_id]
        logging.info("🧹 Очищена одиночная группа изображений %s", media_group_id)


async def process_media_group(media_group_id: str, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Обрабатывает группу изображений как единое целое"""
    if media_group_id not in MEDIA_GROUPS:
        logging.error("Media group %s not found", media_group_id)
        return

    group_data = MEDIA_GROUPS[media_group_id]
    user_id = group_data["user_id"]
    group_data["chat_id"]
    messages = group_data["messages"]
    caption = group_data["caption"]
    placeholder_message = group_data["placeholder_message"]

    # Безопасная проверка количества сообщений
    message_count = len(messages) if messages else 0
    logging.info(
        f"🔄 Обрабатываю группу изображений {media_group_id}: {message_count} изображений"
    )

    # Check, что это действительно group
    if message_count <= 1:
        logging.warning(
            f"Media group {media_group_id} содержит только {message_count} сообщений, перенаправляю в одиночную обработку"
        )
        await process_single_image_from_group(media_group_id, context)
        return

    try:
        async with _HEAVY_REQUEST_SEMAPHORE:
            async with state.get_user_lock(user_id):
                # Create мок Update for совместимости с существующим кодом
                mock_update = type(
                    "MockUpdate",
                    (),
                    {
                        "message": messages[
                            0
                        ],  # Используем первое message как основное
                        "effective_user": messages[0].from_user,
                        "effective_chat": messages[0].chat,
                    },
                )()

                # Process группу via agent
                from app.handlers.agent import process_media_group_request

                await process_media_group_request(
                    placeholder_message, mock_update, context, messages, caption
                )

    except Exception as e:
        logging.error(
            f"Error processing media group {media_group_id}: {e}", exc_info=True
        )
        try:
            from app.errors import build_retry_and_roles_keyboard
            await placeholder_message.edit_text(
                "❌ Произошла ошибка при обработке группы изображений. Попробуйте ещё раз.",
                reply_markup=build_retry_and_roles_keyboard(include_roles=False)
            )
        except (BadRequest, NetworkError) as edit_error:
            logging.error("Could not edit placeholder message: %s", edit_error)

    finally:
        # Clean up группу from памяти (вkeyая TTL)
        if media_group_id in MEDIA_GROUPS:
            del MEDIA_GROUPS[media_group_id]
        if media_group_id in MEDIA_GROUPS_TTL:
            del MEDIA_GROUPS_TTL[media_group_id]
        logging.info("🧹 Очищена группа изображений %s", media_group_id)


async def handle_document_question(
    update: Update, context: ContextTypes.DEFAULT_TYPE, document_id: int
) -> None:
    """Обрабатывает вопрос по конкретному документу"""
    user_id = update.effective_user.id
    user_message = update.message.text

    try:
        from app.document_processor import get_document_content, get_document_by_id

        # Get информацию о documentе
        document = await get_document_by_id(document_id, user_id)
        if not document:
            await update.message.reply_text(
                "❌ Документ не найден.",
                reply_markup=InlineKeyboardMarkup(
                    [[InlineKeyboardButton("📄 К документам", callback_data="open_documents")]]
                )
            )
            from app.state import clear_document_state

            clear_document_state(user_id)
            return

        # Get содержимое documentа
        document_content = await get_document_content(document_id, user_id)
        if not document_content:
            await update.message.reply_text(
                "❌ Не удалось получить содержимое документа.",
                reply_markup=InlineKeyboardMarkup(
                    [[InlineKeyboardButton("📄 К документам", callback_data="open_documents")]]
                )
            )
            return

        # Process вопрос via AI
        from app.handlers.agent import _handle_document_question
        from app import database as db

        chat_state = await db.get_user_chat(user_id)

        # Передаем оригинальное message user как placeholder
        # _handle_document_question сама создаст нужное message
        await _handle_document_question(
            update.message, user_id, user_message, chat_state
        )

    except Exception as e:
        logging.error("Error handling document question: %s", e)
        await update.message.reply_text(
            f"❌ Произошла ошибка при обработке вопроса. Попробуйте переформулировать.",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("📄 К документам", callback_data="open_documents")]]
            )
        )


async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Обрабатывает загруженные документы"""
    user_id = update.effective_user.id

    # Check, что это действительно document, а не image
    if not update.message.document:
        return  # If это не document, просто выходим

    document = update.message.document

    # Check размер fileа (максимум 50MB)
    if document.file_size > 50 * 1024 * 1024:
        await update.message.reply_text(
            "❌ Файл слишком большой. Максимальный размер: 50MB.\n"
            "Попробуйте файл меньшего размера."
        )
        return

    # Check тип fileа
    supported_formats = [".pdf", ".docx", ".doc"]
    file_ext = (
        document.file_name.lower().split(".")[-1] if "." in document.file_name else ""
    )

    if f".{file_ext}" not in supported_formats:
        await update.message.reply_text(
            f"❌ Неподдерживаемый формат файла `.{file_ext}`.\n"
            f"Отправьте PDF или DOCX."
        )
        return

    # Send message о начале обработки
    processing_msg = await update.message.reply_text("📄 Обрабатываю документ...")

    try:
        # Скачиваем file во temporary file на диске instead of ОЗУ
        import tempfile
        import os

        file = await document.get_file()

        tmp_fd, tmp_path = tempfile.mkstemp(suffix=f".{file_ext}")
        os.close(tmp_fd)

        try:
            await file.download_to_drive(custom_path=tmp_path)

            # Process document с диска
            result = await process_uploaded_document(
                tmp_path, document.file_name, user_id, is_path=True
            )
        finally:
            if os.path.exists(tmp_path):
                try:
                    os.unlink(tmp_path)
                except OSError as cleanup_error:
                    logging.warning(
                        f"Failed to cleanup temp doc file {tmp_path}: {cleanup_error}"
                    )

        if result.get("error"):
            if result.get("error") == "duplicate":
                # Process дубликат
                duplicate_info = result.get("duplicate_info", {})

                # Правильно обрабатываем datetime
                created_date = duplicate_info.get("created_at", "Unknown")
                if hasattr(created_date, "strftime"):
                    # Это объект datetime
                    date_str = created_date.strftime("%Y-%m-%d")
                else:
                    # Это строка
                    date_str = (
                        str(created_date)[:10]
                        if created_date != "Unknown"
                        else "Unknown"
                    )

                duplicate_text = (
                    f"⚠️ *Файл уже загружен*\n\n"
                    f"Файл `{document.file_name}` уже был загружен ранее как:\n"
                    f"📄 *{duplicate_info.get('filename', 'Unknown')}*\n"
                    f"📅 Загружен: {date_str}\n\n"
                    f"Хотите использовать существующий документ?"
                )

                # Create buttons for работы с дубликатом
                keyboard = [
                    [
                        InlineKeyboardButton(
                            "✅ Использовать существующий",
                            callback_data=f"doc:use_existing:{duplicate_info.get('id')}",
                        )
                    ],
                    [
                        InlineKeyboardButton(
                            "📄 Загрузить как новый", callback_data="doc:force_upload"
                        )
                    ],
                    [InlineKeyboardButton("❌ Отмена", callback_data="doc:cancel")],
                ]

                formatted_text, parse_mode = TelegramFormatter.format_text(
                    duplicate_text
                )
                await processing_msg.edit_text(
                    formatted_text,
                    parse_mode=parse_mode,
                    reply_markup=InlineKeyboardMarkup(keyboard),
                )
                return
            else:
                await processing_msg.edit_text(
                    f"❌ Ошибка обработки: {result['error']}"
                )
                return

        # Get статистику user for отображения limitов
        from app.document_processor import document_processor

        user_stats = await document_processor.get_user_document_stats(user_id)

        # Send result
        success_text = (
            f"✅ Документ обработан успешно!\n\n"
            f"📄 *{document.file_name}*\n"
            f"📊 Страниц: {result.get('pages', 'N/A')}\n"
            f"📝 Символов: {result.get('text_length', 0):,}\n"
        )

        if result.get("paragraphs"):
            success_text += f"📄 Параграфов: {result['paragraphs']}\n"
        if result.get("tables"):
            success_text += f"📊 Таблиц: {result['tables']}\n"

        success_text += f"\n📋 *Ваши документы:* {user_stats['document_count']}/5\n"
        if user_stats["limit_reached"]:
            success_text += "⚠️ Достигнут лимит документов (5). Старые документы будут автоматически удалены.\n"

        success_text += '\n💡 *Как задавать вопросы:*\n• Просто напишите ваш вопрос\n• Например: "Какие основные пункты?", "Что говорится о...?"\n• Система автоматически найдет ответ в документе\n\n'
        success_text += "📅 *Срок хранения:* 3 дня (автоматическая очистка)"

        # Create buttons for управления documentом
        keyboard = [
            [
                InlineKeyboardButton(
                    "📄 Загрузить другой документ", callback_data="doc:upload_new"
                )
            ],
            [
                InlineKeyboardButton(
                    "📋 Выбрать документ", callback_data="doc:select_document"
                )
            ],
            [
                InlineKeyboardButton(
                    "❌ Отменить работу с документами", callback_data="doc:cancel"
                )
            ],
        ]

        formatted_text, parse_mode = TelegramFormatter.format_text(success_text)
        await processing_msg.edit_text(
            formatted_text,
            parse_mode=parse_mode,
            reply_markup=InlineKeyboardMarkup(keyboard),
        )

        # Устанавливаем state работы с documentами
        from app.state import set_document_mode

        set_document_mode(user_id, True)

        # Write metrics
        await metrics_collector.record_api_call("document_processing")

    except Exception as e:
        error_msg = f"❌ Произошла ошибка при обработке документа: {str(e)[:100]}"
        logging.error(
            f"Error processing document for user {user_id}: {e}", exc_info=True
        )
        from app.utils.keyboards import error_with_back_keyboard
        await processing_msg.edit_text(
            error_msg,
            reply_markup=error_with_back_keyboard("open_documents", "📄 К документам")
        )
        await metrics_collector.record_error("document_processing", str(e))


def register(application: Application) -> None:
    application.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, handle_request)
    )
    application.add_handler(MessageHandler(filters.PHOTO, handle_request))
    application.add_handler(MessageHandler(filters.Document.ALL, handle_request))
