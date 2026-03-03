# /app/handlers/msg_media.py
"""Media group handling extracted from messages.py.

Manages incoming photo groups: accumulation, deferred processing,
single-image fallback, and TTL-based cleanup.
"""

import asyncio
import logging
import time as _time_module
import types

from telegram.error import BadRequest, NetworkError
from telegram.ext import ContextTypes

from app import state

# ── Shared state for media group accumulation ────────────────────────────────
MEDIA_GROUPS: dict = {}
MEDIA_GROUPS_TTL: dict = {}
MEDIA_GROUP_TIMEOUT = 300  # 5 minutes
MEDIA_GROUPS_MAX_SIZE = 500  # OOM protection
_media_groups_lock = asyncio.Lock()
_background_tasks: set = set()
_cleanup_task = None


# ── Cleanup ──────────────────────────────────────────────────────────────────

async def cleanup_old_media_groups() -> None:
    """Remove stale media groups to prevent memory leaks."""
    current_time = _time_module.monotonic()

    async with _media_groups_lock:
        expired_groups = [
            mg_id
            for mg_id, created_at in MEDIA_GROUPS_TTL.items()
            if current_time - created_at > MEDIA_GROUP_TIMEOUT
        ]
        for media_group_id in expired_groups:
            MEDIA_GROUPS.pop(media_group_id, None)
            MEDIA_GROUPS_TTL.pop(media_group_id, None)
            logging.info("🧹 Очищена устаревшая группа изображений: %s", media_group_id)

    if expired_groups:
        logging.info("🧹 Очищено %s устаревших групп изображений", len(expired_groups))


async def start_media_groups_cleanup() -> None:
    """Start the periodic cleanup loop for stale media groups."""
    global _cleanup_task
    if _cleanup_task and not _cleanup_task.done():
        return

    async def cleanup_loop() -> None:
        while True:
            try:
                await asyncio.sleep(60)
                await cleanup_old_media_groups()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logging.error("Error in media groups cleanup: %s", e, exc_info=True)
                await asyncio.sleep(60)

    _cleanup_task = asyncio.create_task(cleanup_loop())
    logging.info("Media groups cleanup task started")


# ── Media group update handling ──────────────────────────────────────────────

async def process_media_group_update(
    update, context: ContextTypes.DEFAULT_TYPE, user_id: int, chat_id: int
) -> bool:
    """Handle an update if it's part of a media group. Returns True if consumed."""
    is_photo = bool(update.message.photo)
    media_group_id = update.message.media_group_id if update.message else None

    if is_photo and media_group_id:
        logging.info(
            "📸 Получено изображение с media_group_id %s от пользователя %s",
            media_group_id, user_id,
        )

        async with _media_groups_lock:
            if media_group_id not in MEDIA_GROUPS and len(MEDIA_GROUPS) >= MEDIA_GROUPS_MAX_SIZE:
                logging.warning(
                    "⚠️ MEDIA_GROUPS at capacity (%s), rejecting media_group_id %s",
                    MEDIA_GROUPS_MAX_SIZE, media_group_id,
                )
                await update.message.reply_text(
                    "⚠️ Слишком много одновременных медиа-групп. Попробуйте позже."
                )
                return True

            if media_group_id not in MEDIA_GROUPS:
                current_time = _time_module.monotonic()
                MEDIA_GROUPS[media_group_id] = {
                    "user_id": user_id,
                    "chat_id": chat_id,
                    "messages": [],
                    "caption": update.message.caption,
                    "created_at": current_time,
                    "placeholder_message": None,
                    "processing_scheduled": False,
                }
                MEDIA_GROUPS_TTL[media_group_id] = current_time
                if _cleanup_task is None or _cleanup_task.done():
                    asyncio.create_task(start_media_groups_cleanup())  # noqa: RUF006

            MEDIA_GROUPS[media_group_id]["messages"].append(update.message)
            is_first = len(MEDIA_GROUPS[media_group_id]["messages"]) == 1
            should_schedule = is_first and not MEDIA_GROUPS[media_group_id]["processing_scheduled"]

        if is_first:
            placeholder_message = await update.message.reply_text(
                "🖼️ Обрабатываю изображение..."
            )
            async with _media_groups_lock:
                if media_group_id in MEDIA_GROUPS:
                    MEDIA_GROUPS[media_group_id]["placeholder_message"] = placeholder_message
            logging.info("📸 Создан placeholder для media_group_id %s", media_group_id)

            if should_schedule:
                async with _media_groups_lock:
                    if media_group_id in MEDIA_GROUPS:
                        MEDIA_GROUPS[media_group_id]["processing_scheduled"] = True
                _process_task = asyncio.create_task(
                    delayed_process_media_group(media_group_id, context, 1.0)
                )
                _background_tasks.add(_process_task)
                _process_task.add_done_callback(_background_tasks.discard)

        return True
    return False


# ── Deferred processing ─────────────────────────────────────────────────────

async def delayed_process_media_group(
    media_group_id: str, context: ContextTypes.DEFAULT_TYPE, delay: float
) -> None:
    """Deferred media group processing. Owns cleanup of MEDIA_GROUPS entry."""
    await asyncio.sleep(delay)

    if media_group_id not in MEDIA_GROUPS:
        return

    group_data = MEDIA_GROUPS[media_group_id]
    message_count = len(group_data["messages"])

    logging.info(
        "⏰ Отложенная обработка media_group_id %s: %s сообщений",
        media_group_id, message_count,
    )

    try:
        if message_count > 1:
            logging.info("🔄 Обрабатываю группу из %s изображений", message_count)
            await _process_media_group(media_group_id, context)
        else:
            logging.info("📸 Одиночное изображение, перенаправляю в стандартную обработку")
            await _process_single_image_from_group(media_group_id, context)
    finally:
        async with _media_groups_lock:
            MEDIA_GROUPS.pop(media_group_id, None)
            MEDIA_GROUPS_TTL.pop(media_group_id, None)
        logging.info("🧹 Очищена обработанная группа изображений: %s", media_group_id)


async def _process_single_image_from_group(
    media_group_id: str, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Process a single image that arrived via a media group."""
    if media_group_id not in MEDIA_GROUPS:
        return

    group_data = MEDIA_GROUPS[media_group_id]
    message = group_data["messages"][0]
    placeholder_message = group_data["placeholder_message"]

    mock_update = types.SimpleNamespace(
        message=message,
        effective_user=message.from_user,
        effective_chat=message.chat,
        update_id=None,
        callback_query=None,
    )

    try:
        from app.handlers.agent import process_long_request
        await process_long_request(placeholder_message, mock_update, context)
    except Exception as e:
        logging.error("Error processing single image from group: %s", e, exc_info=True)
        try:
            from app.errors import build_retry_and_roles_keyboard
            await placeholder_message.edit_text(
                "❌ Произошла ошибка при обработке изображения. Попробуйте ещё раз.",
                reply_markup=build_retry_and_roles_keyboard(include_roles=False)
            )
        except (BadRequest, NetworkError) as edit_error:
            logging.error("Could not edit placeholder message: %s", edit_error)


async def _process_media_group(
    media_group_id: str, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Process a group of images as a single unit."""
    from app.handlers.messages import _HEAVY_REQUEST_SEMAPHORE

    if media_group_id not in MEDIA_GROUPS:
        logging.error("Media group %s not found", media_group_id)
        return

    group_data = MEDIA_GROUPS[media_group_id]
    user_id = group_data["user_id"]
    messages = group_data["messages"]
    caption = group_data["caption"]
    placeholder_message = group_data["placeholder_message"]

    message_count = len(messages) if messages else 0
    logging.info(
        "🔄 Обрабатываю группу изображений %s: %s изображений",
        media_group_id, message_count,
    )

    if message_count <= 1:
        logging.warning(
            "Media group %s содержит только %s сообщений, перенаправляю в одиночную обработку",
            media_group_id, message_count,
        )
        await _process_single_image_from_group(media_group_id, context)
        return

    try:
        async with _HEAVY_REQUEST_SEMAPHORE, state.get_user_lock(user_id):
            mock_update = types.SimpleNamespace(
                message=messages[0],
                effective_user=messages[0].from_user,
                effective_chat=messages[0].chat,
                update_id=None,
                callback_query=None,
            )

            from app.handlers.agent import process_media_group_request
            await process_media_group_request(
                placeholder_message, mock_update, context, messages, caption
            )

    except Exception as e:
        logging.error(
            "Error processing media group %s: %s", media_group_id, e, exc_info=True
        )
        try:
            from app.errors import build_retry_and_roles_keyboard
            await placeholder_message.edit_text(
                "❌ Произошла ошибка при обработке группы изображений. Попробуйте ещё раз.",
                reply_markup=build_retry_and_roles_keyboard(include_roles=False)
            )
        except (BadRequest, NetworkError) as edit_error:
            logging.error("Could not edit placeholder message: %s", edit_error)
