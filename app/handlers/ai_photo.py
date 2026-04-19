"""
AI Photo & Media Group handlers — single photo processing, media groups,
concurrent image downloads, and complex media group search.
"""

import asyncio
import logging
import time

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Message

from app.config import settings
from app.database import ChatState
from app.handlers.ai_core import (
    _get_ai_response_with_routing,
    _resolve_ai_request,
    handle_ai_response_error,
)
from app.handlers.ai_search import (
    _handle_qna_search,
    _handle_research_agent,
)
from app.prompt_registry import FORMATTING_RULES_COMPACT, get_registry
from app.repos.chats import get_user_chat, update_user_chat
from app.streaming import stream_and_display
from app.utils.heartbeat import stop_heartbeat
from app.utils.image_utils import TaggedImage, save_image_as_bytes
from app.utils.messaging import send_long_message
from app.utils.stage_indicators import STAGES_PHOTO, update_stage
from app.utils.tg_file import get_file_bytes

# Sentinel returned by _process_ai_vision when error was already displayed to user.
_VISION_ERROR_HANDLED = object()

# ── Shared helpers (DRY) ─────────────────────────────────────────────────────


def _build_vision_prompt(user_caption: str | None, image_count: int = 1) -> str:
    """Build a formatted prompt for image analysis.

    Args:
        user_caption: User-provided caption text, or ``None`` for default.
        image_count: Number of images (1 = single photo, >1 = group).

    Returns:
        Formatted prompt string with formatting rules embedded.
    """
    if image_count <= 1:
        prompt = user_caption or "Опиши это изображение."
        return f"""# РОЛЬ И ЗАДАЧА
Ты — эксперт по анализу изображений для Telegram-бота. Опиши изображение, используя правильное форматирование.

# КОНТЕКСТ
**Запрос пользователя:** {prompt}

# ИНСТРУКЦИИ
1. Внимательно изучи изображение
2. Определи основные объекты и детали
3. Структурируй описание логично
4. Примени стандартное Markdown форматирование

{FORMATTING_RULES_COMPACT}

# СТРУКТУРА ОПИСАНИЯ
1. **Основной объект** — что изображено
2. **Ключевые характеристики** — цвет, размер, стиль
3. **Контекст и окружение** — где, когда
4. **Детали и особенности** — уникальные элементы

Опиши изображение, следуя указанным инструкциям."""

    # Group prompt
    prompt = user_caption or f"Опиши эти {image_count} изображения."
    return f"""# РОЛЬ И ЗАДАЧА
Ты — эксперт по анализу групп изображений для Telegram-бота. Опиши группу изображений, используя правильное форматирование.

# КОНТЕКСТ
**Запрос пользователя:** {prompt}

# ИНСТРУКЦИИ
1. Внимательно изучи каждое изображение
2. Определи основные объекты и детали
3. Проанализируй связи между изображениями
4. Структурируй описание логично
5. Примени стандартное Markdown форматирование

{FORMATTING_RULES_COMPACT}

# СТРУКТУРА ОПИСАНИЯ ГРУППЫ
1. **Общий контекст** — что представляет группа изображений
2. **Индивидуальные описания** — каждое изображение отдельно
3. **Связи и отношения** — как изображения связаны между собой

Опиши группу изображений, следуя указанным инструкциям."""


async def _send_vision_response(
    placeholder_message: Message,
    response_text: str | None,
    streamed: bool,
    stream_last_msg: Message | None,
    error_fallback: str = "Не удалось обработать изображение.",
) -> bool:
    """Attach action buttons and send/edit the AI vision response.

    Returns:
        ``True`` if a valid response was sent, ``False`` if fallback error was used.
    """
    buttons = [
        [InlineKeyboardButton("🎭 Выбрать роль ИИ", callback_data="open_roles:from_response")],
        [InlineKeyboardButton("✨ Начать новую тему", callback_data="new_topic")],
    ]
    reply_markup = InlineKeyboardMarkup(buttons)

    if response_text and response_text.strip():
        if not streamed:
            await send_long_message(placeholder_message, response_text, reply_markup=reply_markup)
        else:
            button_msg = stream_last_msg if stream_last_msg else placeholder_message
            try:
                await button_msg.edit_reply_markup(reply_markup=reply_markup)
            except Exception as e:
                if "not modified" not in str(e).lower():
                    logging.warning("Final button edit failed: %s", e)
        return True

    # Empty/None response — send error fallback
    await send_long_message(placeholder_message, error_fallback, reply_markup=reply_markup)
    return False


async def _process_ai_vision(
    placeholder_message: Message,
    parts: list,
    chat_state: ChatState,
    user_id: int | None = None,
    chat_id: int | None = None,
) -> tuple[str | None | object, bool, Message | None]:
    """Shared AI vision processing: resolve model → stream → fallback → error check.

    Returns:
        Tuple of ``(response_text, streamed, stream_last_msg)``.
        ``response_text`` is ``_VISION_ERROR_HANDLED`` sentinel if an error was
        already displayed to the user.  It may be ``None`` or empty string for
        genuinely empty AI responses — callers must handle that case.
    """
    _, model_used, _ = await _resolve_ai_request(chat_state.model or settings.DEFAULT_MODEL)
    history = [{"role": "user", "parts": parts}]
    _vision_t0 = time.monotonic()

    response_text, success, stream_last_msg, _tokens, _was_interrupted, _voice_req = await stream_and_display(
        placeholder_message,
        model_name=model_used,
        history=history,
        system_instruction=None,
        thinking_level=chat_state.thinking_level,
        user_id=user_id,
        bot=placeholder_message.get_bot(),
        chat_id=chat_id or 0,
    )

    streamed = bool(success and response_text)

    if not streamed:
        response_text, _ = await _get_ai_response_with_routing(
            model_used,
            history,
            user_id=user_id,
            chat_id=chat_id,
        )

    # Check ошибки от роутера — if handled, return sentinel
    if await handle_ai_response_error(response_text, placeholder_message):
        return _VISION_ERROR_HANDLED, False, None

    # ── Metrics ───────────────────────────────────────────────────
    from app.metrics import metrics_collector as _mc

    await _mc.record_api_call("gemini_vision", model_used, user_id=user_id)
    await _mc.record_request("photo", time.monotonic() - _vision_t0, success=streamed)

    return response_text, streamed, stream_last_msg


# ── Single photo handler ────────────────────────────────────────────────────


async def _handle_photo(placeholder_message: Message, original_message: Message, chat_state: ChatState):
    stop_heartbeat(placeholder_message.message_id)
    try:
        photo_file = await original_message.photo[-1].get_file()
        img_raw = await get_file_bytes(original_message.get_bot(), photo_file)

        # Pre-compress with cache_key for retry savings
        file_unique_id = original_message.photo[-1].file_unique_id
        compressed = await save_image_as_bytes(img_raw, task_type="describe", cache_key=file_unique_id)

        formatted_prompt = _build_vision_prompt(original_message.caption, image_count=1)

        # Wrap as TaggedImage so providers skip recompression
        if compressed:
            tagged = TaggedImage(
                data=compressed,
                cache_key=file_unique_id,
                task_type="describe",
                pre_compressed=True,
            )
            parts = [formatted_prompt, tagged]
        else:
            parts = [formatted_prompt, img_raw] if img_raw else [formatted_prompt]

        await update_stage(placeholder_message, STAGES_PHOTO, 1)

        user_id = original_message.from_user.id
        chat_id = placeholder_message.chat.id if placeholder_message.chat else None

        response_text, streamed, stream_last_msg = await _process_ai_vision(
            placeholder_message,
            parts,
            chat_state,
            user_id=user_id,
            chat_id=chat_id,
        )

        if response_text is _VISION_ERROR_HANDLED:
            # Error already displayed to user by _process_ai_vision
            return

        sent_ok = await _send_vision_response(
            placeholder_message,
            response_text,  # type: ignore[arg-type]
            streamed,
            stream_last_msg,
            error_fallback="Не удалось обработать изображение.",
        )

        if sent_ok:
            # Save context in history
            chat_state.history.append({"role": "user", "parts": [formatted_prompt]})
            chat_state.history.append({"role": "model", "parts": [response_text]})
            await update_user_chat(user_id, chat_state)

            # ── Store photo description in long-term memory (background) ──
            _photo_bytes = compressed or img_raw
            if _photo_bytes and chat_state.ltm_enabled:
                from app.utils.background_tasks import submit_retryable

                def _bg_photo_ltm():
                    async def _store():
                        from app.utils.multimodal_processor import process_media_for_memory

                        await process_media_for_memory(
                            _photo_bytes,
                            user_id,
                            media_type="image",
                            telegram_file_id=file_unique_id,
                        )

                    return _store()

                submit_retryable(_bg_photo_ltm, retry=2)
        else:
            logging.warning("Empty response from Gemini API for image processing by user %s", user_id)

    except Exception as e:
        logging.error("Error processing photo: %s", e, exc_info=True)
        try:
            await placeholder_message.edit_text("❌ Произошла ошибка при обработке изображения.")
        except Exception as edit_error:
            logging.error("Could not edit placeholder message: %s", edit_error)
            # Fallback на new message
            await original_message.reply_text("❌ Произошла ошибка при обработке изображения.")


# ── Media group dispatcher ──────────────────────────────────────────────────


async def process_media_group_request(
    placeholder_message: Message,
    update,
    context,
    messages: list[Message],
    caption: str,
) -> None:
    # context используется for совместимости с другими функциями
    """Обрабатывает группу изображений как единое целое"""
    user_id = update.effective_user.id
    chat_state = await get_user_chat(user_id)

    count = len(messages) if messages else 0
    logging.info("Processing group of %d images for user %s", count, user_id)

    # Check, есть ли searchовый префикс в caption
    search_prefix = None
    if caption:
        if caption.startswith("??"):
            search_prefix = "??"
        elif caption.startswith("?"):
            search_prefix = "?"

    # If есть searchовый префикс, use сложный search
    if search_prefix:
        await _handle_complex_media_group_search(placeholder_message, messages, caption, search_prefix, chat_state)
    else:
        # Обычная обработка groups fromображений
        await _handle_media_group_photos(placeholder_message, messages, caption, chat_state)


# Limit concurrent Telegram file downloads to avoid overwhelming the API
_DL_SEMAPHORE = asyncio.Semaphore(5)


async def _download_images_concurrently(
    messages: list[Message],
    log_context: str = "",
    placeholder: Message | None = None,
) -> list[bytes]:
    """Downloads images from a list of messages concurrently.

    Features:
      - Semaphore-limited to 5 parallel downloads.
      - Optional debounced progress indicator via ``placeholder``.
      - Partial-failure tolerant: logs and counts failures.
    """
    total = len(messages)
    progress = {"done": 0, "failed": 0}
    # THREAD-SAFETY: progress updates are guarded by _progress_lock.
    # Safe in both single-threaded asyncio and if migrated to to_thread().
    _progress_lock = asyncio.Lock()

    async def download_one(index: int, message: Message) -> bytes | None:
        async with _DL_SEMAPHORE:
            try:
                photo_file = await message.photo[-1].get_file()
                img = await get_file_bytes(message.get_bot(), photo_file)
                async with _progress_lock:
                    progress["done"] += 1

                log_msg = f"📸 Загружено изображение {index + 1}/{total}"
                if log_context:
                    log_msg += f" {log_context}"
                logging.info(log_msg)

                return img
            except Exception as e:
                async with _progress_lock:
                    progress["failed"] += 1
                logging.error("Error loading image %s: %s", index + 1, e, exc_info=True)
                return None

    # Background progress updater (debounced at 2s intervals)
    async def _update_progress() -> None:
        if not placeholder:
            return
        while progress["done"] + progress["failed"] < total:
            await asyncio.sleep(2.0)
            try:
                await placeholder.edit_text(f"📸 Загружено {progress['done']}/{total}...")
            except Exception:
                pass  # Telegram rate-limit or message already edited

    async with asyncio.TaskGroup() as tg:
        progress_task = tg.create_task(_update_progress())
        tasks = [tg.create_task(download_one(i, msg)) for i, msg in enumerate(messages)]

        # Await all downloads inside the block
        results = await asyncio.gather(*tasks)

        # Cancel the progress task so we don't wait for its sleep to finish
        progress_task.cancel()

    if progress["failed"]:
        logging.warning("%d of %d images failed to download", progress["failed"], total)

    return [img for img in results if img is not None]  # type: ignore[misc]  # download_one returns bytes


# ── Media group photo handler ───────────────────────────────────────────────


async def _handle_media_group_photos(
    placeholder_message: Message,
    messages: list[Message],
    caption: str,
    chat_state: ChatState,
):
    """Обрабатывает группу изображений для обычного описания"""
    try:
        # Load все images from groups
        images = await _download_images_concurrently(messages, placeholder=placeholder_message)

        if not images:
            await placeholder_message.edit_text("❌ Не удалось загрузить ни одного изображения из группы.")
            return

        await update_stage(placeholder_message, STAGES_PHOTO, 1)

        image_count = len(images)
        formatted_prompt = _build_vision_prompt(caption, image_count=image_count)

        # Wrap raw bytes as TaggedImage with task_type="describe"
        tagged_images = [
            TaggedImage(data=img, task_type="describe")
            for img in images  # type: ignore[arg-type]  # download_one returns bytes
        ]

        # Create parts for Gemini API: text + все images
        parts = [formatted_prompt] + tagged_images

        # Get user_id и chat_id for логирования
        user_id = placeholder_message.from_user.id if placeholder_message.from_user else None
        chat_id = placeholder_message.chat.id if placeholder_message.chat else None

        response_text, streamed, stream_last_msg = await _process_ai_vision(
            placeholder_message,
            parts,
            chat_state,
            user_id=user_id,
            chat_id=chat_id,
        )

        if response_text is _VISION_ERROR_HANDLED:
            # Error already displayed to user
            return

        await _send_vision_response(
            placeholder_message,
            response_text,  # type: ignore[arg-type]
            streamed,
            stream_last_msg,
            error_fallback="Не удалось обработать группу изображений.",
        )

        logging.info("✅ Группа из %s изображений обработана успешно", image_count)

    except Exception as e:
        logging.error("Error processing media group photos: %s", e, exc_info=True)
        try:
            await placeholder_message.edit_text("❌ Произошла ошибка при обработке группы изображений.")
        except Exception as edit_error:
            logging.error("Could not edit placeholder message: %s", edit_error)


# ── Complex media group search handler ──────────────────────────────────────


async def _handle_complex_media_group_search(
    placeholder_message: Message,
    messages: list[Message],
    caption: str,
    search_prefix: str,
    chat_state: ChatState,
):
    """Обрабатывает группу изображений для сложного поиска"""
    user_id = placeholder_message.from_user.id

    try:
        await placeholder_message.edit_text("🖼️ Анализирую группу изображений...")
    except Exception as edit_error:
        logging.error("Could not edit placeholder message: %s", edit_error)
        placeholder_message = await placeholder_message.reply_text("🖼️ Анализирую группу изображений...")

    vision_model = settings.RESEARCH_MODEL

    try:
        # Load все images from groups
        images = await _download_images_concurrently(
            messages, log_context="для анализа", placeholder=placeholder_message
        )

        if not images:
            await placeholder_message.edit_text("❌ Не удалось загрузить ни одного изображения для анализа.")
            return

        # Анализируем группу изображений for searchа
        analysis_prompt = f"""{get_registry().get("image_analysis").text}

# ДОПОЛНИТЕЛЬНЫЕ ИНСТРУКЦИИ ДЛЯ ГРУППЫ ИЗОБРАЖЕНИЙ
## Анализ группы
- Проанализируй все изображения как единый контекст
- Выдели общие темы, объекты или концепции
- Учти взаимосвязи между изображениями
- Создай поисковый запрос, который охватывает весь контекст группы

## Специальные случаи
- Если изображения показывают последовательность или процесс, отрази это в запросе
- Если изображения демонстрируют разные аспекты одной темы, объедини их
- Если изображения показывают сравнение или контраст, укажи это

# FEW-SHOT ПРИМЕРЫ ДЛЯ ГРУПП
## Пример 1: Последовательность событий
**Группа:** 3 изображения процесса приготовления блюда
**Правильный запрос:** `cooking process step by step recipe preparation`

## Пример 2: Разные аспекты темы
**Группа:** 4 изображения разных типов автомобилей
**Правильный запрос:** `car types comparison sedan SUV sports luxury vehicles`

## Пример 3: Контраст или сравнение
**Группа:** 2 изображения старого и нового здания
**Правильный запрос:** `architecture evolution old vs new building comparison`

# ФОРМАТ ВЫВОДА
Верни ТОЛЬКО поисковый запрос без:
- Кавычек
- Двоеточий
- Объяснений
- Вводных фраз
- Дополнительного форматирования

**Пример правильного вывода:**
```
cooking process step by step recipe preparation
```"""

        # Wrap raw bytes as TaggedImage with task_type="search"
        tagged_images = [
            TaggedImage(data=img, task_type="search")
            for img in images  # type: ignore[arg-type]  # download_one returns bytes
        ]

        # Create parts for анализа: промпт + все images
        parts = [analysis_prompt] + tagged_images

        # Get user_id и chat_id for логирования
        user_id = placeholder_message.from_user.id if placeholder_message.from_user else None
        chat_id = placeholder_message.chat.id if placeholder_message.chat else None

        search_query, _ = await _get_ai_response_with_routing(
            vision_model,
            [{"role": "user", "parts": parts}],
            user_id=user_id,
            chat_id=chat_id,
        )

        # Check ошибки от роутера
        if await handle_ai_response_error(search_query, placeholder_message):
            return

        if not search_query:
            try:
                await placeholder_message.edit_text("Не удалось проанализировать группу изображений для поиска.")
            except Exception as edit_error:
                logging.error("Could not edit placeholder message: %s", edit_error)
            return

        # Get оригинальное message user for локализации
        image_count = len(images)
        original_user_message = caption or f"Опиши эти {image_count} изображения."

        if search_prefix == "?":
            await _handle_qna_search(placeholder_message, original_user_message, chat_state, search_query)
        else:
            await _handle_research_agent(
                placeholder_message,
                user_id,
                original_user_message,
                chat_state,
                search_query=search_query,
            )

        logging.info("✅ Группа из %s изображений проанализирована для поиска", image_count)

    except Exception as e:
        logging.error("Error processing complex media group search: %s", e, exc_info=True)
        try:
            await placeholder_message.edit_text("❌ Произошла ошибка при анализе группы изображений.")
        except Exception as edit_error:
            logging.error("Could not edit placeholder message: %s", edit_error)
