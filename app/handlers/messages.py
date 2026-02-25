# /app/handlers/messages.py

import asyncio
import logging
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes, MessageHandler, filters, Application

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

# Глобальный лимитер для тяжёлых AI-задач, чтобы избежать перегрузки event loop/провайдеров
_HEAVY_REQUEST_LIMIT = max(
    1, int(getattr(settings, "MAX_CONCURRENT_HEAVY_REQUESTS", 4))
)
_HEAVY_REQUEST_SEMAPHORE = asyncio.Semaphore(_HEAVY_REQUEST_LIMIT)

# Глобальный словарь для хранения групп изображений
MEDIA_GROUPS = {}
MEDIA_GROUPS_TTL = {}  # TTL для автоматической очистки старых групп
MEDIA_GROUP_TIMEOUT = 300  # 5 минут таймаут для группы изображений


async def cleanup_old_media_groups():
    """Очищает старые группы изображений для предотвращения утечки памяти"""
    current_time = asyncio.get_event_loop().time()
    expired_groups = []

    for media_group_id, created_at in MEDIA_GROUPS_TTL.items():
        if current_time - created_at > MEDIA_GROUP_TIMEOUT:
            expired_groups.append(media_group_id)

    for media_group_id in expired_groups:
        MEDIA_GROUPS.pop(media_group_id, None)
        MEDIA_GROUPS_TTL.pop(media_group_id, None)
        logging.info(f"🧹 Очищена устаревшая группа изображений: {media_group_id}")

    if expired_groups:
        logging.info(f"🧹 Очищено {len(expired_groups)} устаревших групп изображений")


# Запускаем периодическую очистку при импорте модуля
_cleanup_task = None


async def start_media_groups_cleanup():
    """Запускает периодическую очистку групп изображений"""
    global _cleanup_task
    if _cleanup_task and not _cleanup_task.done():
        return

    async def cleanup_loop():
        while True:
            try:
                await asyncio.sleep(60)  # Проверяем каждую минуту
                await cleanup_old_media_groups()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logging.error(f"Error in media groups cleanup: {e}")
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

                # Показываем список бесед с обновленным названием
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
            logging.error(f"Error renaming conversation: {e}")
            await update.message.reply_text(
                "❌ Не удалось переименовать беседу. Попробуйте позже."
            )
            context.user_data.pop("rename_conv_id", None)
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
        # Если мы ждем ввода описания роли
        logging.info(f"User {user_id} sent custom role description: {message_text}")

        # Получаем настройки и ключи
        chat_state = await db.get_user_chat(user_id)
        from app.config import settings

        model_for_role = chat_state.model or settings.DEFAULT_MODEL

        # Используем универсальную функцию для получения ключа
        key_data, model_used, resolution = await agent._resolve_ai_request(
            model_for_role
        )

        if not key_data:
            await update.message.reply_text(
                "❌ Нет доступных ключей API для генерации роли."
            )
            clear_custom_role_state(user_id)
            return True

        progress_msg = await update.message.reply_text("🛠️ Генерирую роль…")
        set_generating_custom_role(user_id, True)

        # Формируем запрос
        history = [{"role": "user", "parts": [message_text]}]

        try:
            # Используем универсальную функцию для получения ответа
            response_text, _ = await agent._get_ai_response(
                key_data["api_key"],
                history,
                model_used,
                system_instruction=prompts.PROMPT_ENGINEER_SYSTEM_PROMPT,
                user_id=user_id,
                chat_id=chat_id,
            )

            # Инкрементируем использование ключа
            await agent._increment_key_usage(key_data["key_hash"], model_used)

            # Логируем ответ модели для отладки
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
                # Обработка явной 503 ошибки из текста
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
            logging.error(f"Error generating custom role: {e}", exc_info=True)
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
                # Возврат в детали роли
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
            logging.error(f"Error renaming role: {e}")
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

        # Инициализируем группу, если её нет
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
            # Устанавливаем TTL для автоматической очистки
            MEDIA_GROUPS_TTL[media_group_id] = current_time
            # Запускаем очистку при первом создании группы
            if _cleanup_task is None or _cleanup_task.done():
                asyncio.create_task(start_media_groups_cleanup())

        # Добавляем сообщение в группу
        MEDIA_GROUPS[media_group_id]["messages"].append(update.message)

        # Если это первое сообщение группы, создаем placeholder и планируем обработку
        if len(MEDIA_GROUPS[media_group_id]["messages"]) == 1:
            placeholder_message = await update.message.reply_text(
                "🖼️ Обрабатываю изображение..."
            )
            MEDIA_GROUPS[media_group_id]["placeholder_message"] = placeholder_message
            logging.info(f"📸 Создан placeholder для media_group_id {media_group_id}")

            # Планируем обработку через 1 секунду
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


async def handle_request(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обрабатывает входящие сообщения"""
    # Валидация входных данных
    if not update or not update.effective_user:
        logging.error("Invalid update object received")
        return

    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    request_id = set_request_id(f"tgmsg-{chat_id}-{getattr(update, 'update_id', 'na')}")

    # Correlation contract: request_id is propagated as trace_id baseline.
    with bind_request_span(request_id, span_name="telegram-message"):
        # Валидация user_id
        if not isinstance(user_id, int) or user_id <= 0:
            logging.error("Invalid user_id: %s", user_id)
            return

        # Обрабатываем медиа-группы
        if await _process_media_group_update(update, context, user_id, chat_id):
            return

        # Детальное логирование Telegram API запроса
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

        # Валидация текста сообщения
        message_text = (
            update.message.text if update.message and update.message.text else "No text"
        )
        if len(message_text) > settings.TELEGRAM_MESSAGE_LIMIT:
            logging.warning(
                "Message too long from user %s: %d chars", user_id, len(message_text)
            )
            await update.message.reply_text(
                "❌ Сообщение слишком длинное. Максимум 4096 символов."
            )
            return

    logging.info("Received message from user %s: %s", user_id, message_text[:100])

    # Проверка rate limit для защиты от злоупотреблений
    if not await check_user_rate_limit(user_id):
        logging.warning("Rate limit exceeded for user %s", user_id)
        await update.message.reply_text(
            "⏱️ Превышен лимит запросов. Пожалуйста, подождите немного перед следующим запросом."
        )
        return

    if not await db.is_authorized(user_id):
        logging.warning("Unauthorized user %s attempted to use bot", user_id)
        return

    # Обрабатываем документы
    if update.message.document:
        logging.info(
            "Processing document from user %s: %s",
            user_id,
            update.message.document.file_name,
        )
        await handle_document(update, context)
        return

    # Переименование роли
    if await _handle_role_rename(update, context, user_id):
        return

    # Переименование беседы
    if await _handle_conversation_rename(update, context, user_id):
        return

    # Генерация кастомной роли
    if await _handle_custom_role_generation(
        update, context, user_id, chat_id, message_text
    ):
        return

    # Проверяем, находится ли пользователь в режиме работы с документами
    if await _handle_document_mode_interaction(update, context, user_id):
        return

    # Сохраняем последний пользовательский ввод для кнопки "🔁 Попробовать ещё раз"
    try:
        from app.state import get_user_state

        if update.message and update.message.text:
            get_user_state(user_id).last_sent_message_text = update.message.text
    except Exception:
        logging.exception("Error saving last sent message text")

    # Проверяем, есть ли изображение (одиночное)
    is_photo = bool(update.message.photo)

    if is_photo:
        logging.info(f"Processing single photo from user {user_id}")
        placeholder_message = await update.message.reply_text(
            "🖼️ Обрабатываю изображение..."
        )
    else:
        logging.info(f"Processing text message from user {user_id}")
        placeholder_message = await update.message.reply_text("🤔 Думаю...")

    # Обычная обработка сообщений
    async def task_wrapper():
        try:
            async with _HEAVY_REQUEST_SEMAPHORE:
                async with state.get_user_lock(user_id):
                    logging.info("Starting task processing for user %s", user_id)

                    # Восстанавливаем обработку через agent
                    try:
                        from app.handlers.agent import process_long_request

                        await process_long_request(placeholder_message, update, context)
                    except ImportError:
                        # Fallback если agent недоступен
                        await placeholder_message.edit_text(
                            "🤔 Обрабатываю ваш запрос... (упрощенный режим)"
                        )

                    logging.info("Completed task processing for user %s", user_id)

                # Логируем успешный ответ Telegram API
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
                await placeholder_message.edit_text(
                    "❌ Произошла ошибка при обработке запроса."
                )
            except Exception as edit_error:
                logging.error(f"Could not edit placeholder message: {edit_error}")

            # Логируем ошибку Telegram API
            api_logger.log_telegram_response(
                start_time=start_time,
                method="handle_message",
                success=False,
                chat_id=chat_id,
                user_id=user_id,
                error=str(e),
            )

    # Запускаем обработку в фоне
    asyncio.create_task(task_wrapper())


async def delayed_process_media_group(
    media_group_id: str, context: ContextTypes.DEFAULT_TYPE, delay: float
):
    """Отложенная обработка группы изображений"""
    await asyncio.sleep(delay)

    if media_group_id in MEDIA_GROUPS:
        group_data = MEDIA_GROUPS[media_group_id]
        message_count = len(group_data["messages"])

        logging.info(
            f"⏰ Отложенная обработка media_group_id {media_group_id}: {message_count} сообщений"
        )

        try:
            # Если это действительно группа (больше 1 изображения), обрабатываем как группу
            if message_count > 1:
                logging.info(f"🔄 Обрабатываю группу из {message_count} изображений")
                await process_media_group(media_group_id, context)
            else:
                # Если это одиночное изображение, обрабатываем через стандартный путь
                logging.info(
                    "📸 Одиночное изображение, перенаправляю в стандартную обработку"
                )
                await process_single_image_from_group(media_group_id, context)
        finally:
            # Очищаем группу после обработки
            MEDIA_GROUPS.pop(media_group_id, None)
            MEDIA_GROUPS_TTL.pop(media_group_id, None)
            logging.info(
                f"🧹 Очищена обработанная группа изображений: {media_group_id}"
            )


async def process_single_image_from_group(
    media_group_id: str, context: ContextTypes.DEFAULT_TYPE
):
    """Обрабатывает одиночное изображение из группы"""
    if media_group_id not in MEDIA_GROUPS:
        return

    group_data = MEDIA_GROUPS[media_group_id]
    message = group_data["messages"][0]
    placeholder_message = group_data["placeholder_message"]

    # Создаем мок Update для совместимости
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
        # Обрабатываем через стандартный путь
        from app.handlers.agent import process_long_request

        await process_long_request(placeholder_message, mock_update, context)
    except Exception as e:
        logging.error(f"Error processing single image from group: {e}")
        try:
            await placeholder_message.edit_text(
                "❌ Произошла ошибка при обработке изображения."
            )
        except Exception as edit_error:
            logging.error(f"Could not edit placeholder message: {edit_error}")
    finally:
        # Очищаем группу (включая TTL)
        if media_group_id in MEDIA_GROUPS:
            del MEDIA_GROUPS[media_group_id]
        if media_group_id in MEDIA_GROUPS_TTL:
            del MEDIA_GROUPS_TTL[media_group_id]
        logging.info(f"🧹 Очищена одиночная группа изображений {media_group_id}")


async def process_media_group(media_group_id: str, context: ContextTypes.DEFAULT_TYPE):
    """Обрабатывает группу изображений как единое целое"""
    if media_group_id not in MEDIA_GROUPS:
        logging.error(f"Media group {media_group_id} not found")
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

    # Проверяем, что это действительно группа
    if message_count <= 1:
        logging.warning(
            f"Media group {media_group_id} содержит только {message_count} сообщений, перенаправляю в одиночную обработку"
        )
        await process_single_image_from_group(media_group_id, context)
        return

    try:
        async with _HEAVY_REQUEST_SEMAPHORE:
            async with state.get_user_lock(user_id):
                # Создаем мок Update для совместимости с существующим кодом
                mock_update = type(
                    "MockUpdate",
                    (),
                    {
                        "message": messages[
                            0
                        ],  # Используем первое сообщение как основное
                        "effective_user": messages[0].from_user,
                        "effective_chat": messages[0].chat,
                    },
                )()

                # Обрабатываем группу через agent
                from app.handlers.agent import process_media_group_request

                await process_media_group_request(
                    placeholder_message, mock_update, context, messages, caption
                )

    except Exception as e:
        logging.error(
            f"Error processing media group {media_group_id}: {e}", exc_info=True
        )
        try:
            await placeholder_message.edit_text(
                "❌ Произошла ошибка при обработке группы изображений."
            )
        except Exception as edit_error:
            logging.error(f"Could not edit placeholder message: {edit_error}")

    finally:
        # Очищаем группу из памяти (включая TTL)
        if media_group_id in MEDIA_GROUPS:
            del MEDIA_GROUPS[media_group_id]
        if media_group_id in MEDIA_GROUPS_TTL:
            del MEDIA_GROUPS_TTL[media_group_id]
        logging.info(f"🧹 Очищена группа изображений {media_group_id}")


async def handle_document_question(
    update: Update, context: ContextTypes.DEFAULT_TYPE, document_id: int
):
    """Обрабатывает вопрос по конкретному документу"""
    user_id = update.effective_user.id
    user_message = update.message.text

    try:
        from app.document_processor import get_document_content, get_document_by_id

        # Получаем информацию о документе
        document = await get_document_by_id(document_id, user_id)
        if not document:
            await update.message.reply_text("❌ Документ не найден.")
            from app.state import clear_document_state

            clear_document_state(user_id)
            return

        # Получаем содержимое документа
        document_content = await get_document_content(document_id, user_id)
        if not document_content:
            await update.message.reply_text(
                "❌ Не удалось получить содержимое документа."
            )
            return

        # Обрабатываем вопрос через AI
        from app.handlers.agent import _handle_document_question
        from app import database as db

        chat_state = await db.get_user_chat(user_id)

        # Передаем оригинальное сообщение пользователя как placeholder
        # _handle_document_question сама создаст нужное сообщение
        await _handle_document_question(
            update.message, user_id, user_message, chat_state
        )

    except Exception as e:
        logging.error(f"Error handling document question: {e}")
        await update.message.reply_text(
            f"❌ Произошла ошибка при обработке вопроса: {str(e)}"
        )


async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обрабатывает загруженные документы"""
    user_id = update.effective_user.id

    # Проверяем, что это действительно документ, а не изображение
    if not update.message.document:
        return  # Если это не документ, просто выходим

    document = update.message.document

    # Проверяем размер файла (максимум 50MB)
    if document.file_size > 50 * 1024 * 1024:
        await update.message.reply_text(
            "❌ Файл слишком большой. Максимальный размер: 50MB"
        )
        return

    # Проверяем тип файла
    supported_formats = [".pdf", ".docx", ".doc"]
    file_ext = (
        document.file_name.lower().split(".")[-1] if "." in document.file_name else ""
    )

    if f".{file_ext}" not in supported_formats:
        await update.message.reply_text(
            f"❌ Неподдерживаемый формат файла. Поддерживаемые форматы: {', '.join(supported_formats)}"
        )
        return

    # Отправляем сообщение о начале обработки
    processing_msg = await update.message.reply_text("📄 Обрабатываю документ...")

    try:
        # Скачиваем файл во временный файл на диске вместо ОЗУ
        import tempfile
        import os

        file = await document.get_file()

        tmp_fd, tmp_path = tempfile.mkstemp(suffix=f".{file_ext}")
        os.close(tmp_fd)

        try:
            await file.download_to_drive(custom_path=tmp_path)

            # Обрабатываем документ с диска
            result = await process_uploaded_document(
                tmp_path, document.file_name, user_id, is_path=True
            )
        finally:
            if os.path.exists(tmp_path):
                try:
                    os.unlink(tmp_path)
                except Exception as cleanup_error:
                    logging.warning(
                        f"Failed to cleanup temp doc file {tmp_path}: {cleanup_error}"
                    )

        if result.get("error"):
            if result.get("error") == "duplicate":
                # Обрабатываем дубликат
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

                # Создаем кнопки для работы с дубликатом
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

        # Получаем статистику пользователя для отображения лимитов
        from app.document_processor import document_processor

        user_stats = await document_processor.get_user_document_stats(user_id)

        # Отправляем результат
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

        # Создаем кнопки для управления документом
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

        # Устанавливаем состояние работы с документами
        from app.state import set_document_mode

        set_document_mode(user_id, True)

        # Записываем метрики
        await metrics_collector.record_api_call("document_processing")

    except Exception as e:
        error_msg = f"❌ Произошла ошибка при обработке документа: {str(e)[:100]}"
        logging.error(
            f"Error processing document for user {user_id}: {e}", exc_info=True
        )
        await processing_msg.edit_text(error_msg)
        await metrics_collector.record_error("document_processing", str(e))


def register(application: Application):
    application.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, handle_request)
    )
    application.add_handler(MessageHandler(filters.PHOTO, handle_request))
    application.add_handler(MessageHandler(filters.Document.ALL, handle_request))
