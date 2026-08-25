"""
AI Photo & Media Group handlers — single photo processing, media groups,
concurrent image downloads, and complex media group search.
"""

import asyncio
import logging
import time
from typing import Any

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Message

from app.config import (
    DEFAULT_GEMINI_MODELS,
    GEMINI_ECONOMY_MODEL,
    GEMINI_PRIMARY_MODEL,
    normalize_gemini_chat_model,
    settings,
)
from app.database import ChatState
from app.handlers.ai_core import (
    _get_ai_response_with_routing,
    handle_ai_response_error,
)
from app.handlers.ai_search import (
    _handle_qna_search,
    _handle_research_agent,
)
from app.prompt_registry import FORMATTING_RULES_COMPACT, get_registry
from app.repos.chats import ensure_chat_generation, get_user_chat, update_user_chat
from app.utils.heartbeat import stop_heartbeat
from app.utils.image_utils import TaggedImage, save_image_as_bytes
from app.utils.stage_indicators import STAGES_PHOTO, update_stage
from app.utils.tg_file import get_file_bytes
from app.utils.vision_intent import classify_vision_intent

# ── Shared helpers (DRY) ─────────────────────────────────────────────────────


def _setting(name: str, fallback: Any) -> Any:
    value = getattr(settings, name, None) if settings is not None else None
    return fallback if value is None else value


def _available_models() -> list[str]:
    value = _setting("AVAILABLE_MODELS", DEFAULT_GEMINI_MODELS)
    return value if isinstance(value, list) and value else DEFAULT_GEMINI_MODELS


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


def _build_ocr_prompt(user_caption: str | None) -> str:
    """Build a prompt focused purely on OCR / text extraction.

    Args:
        user_caption: Original user caption to pass context.

    Returns:
        Structured OCR prompt instructing the model to extract text verbatim.
    """
    prompt = user_caption or "Извлеки текст с изображения."
    return f"""# РОЛЬ И ЗАДАЧА
Ты — высокоточная система оптического распознавания символов (OCR). Твоя единственная задача — распознать и извлечь весь текст с изображения verbatim (дословно).

# КОНТЕКСТ
**Запрос пользователя:** {prompt}

# ИНСТРУКЦИИ
1. Извлеки абсолютно весь видимый текст с изображения дословно (verbatim).
2. Не добавляй никаких описаний изображения, комментариев, мета-информации, пояснений или вводных фраз (например, не пиши "Вот текст с картинки:").
3. Сохраняй оригинальную структуру абзацев, переносов строк и списков, если это возможно.
4. Если на изображении нет никакого текста, ответь ровно одной фразой: "[На изображении не обнаружен текст]".
5. Выведи только текст (ТОЛЬКО распознанный текст) без какого-либо описания и ничего больше.
"""


def _pick_ocr_model() -> str:
    """Pick the best model for OCR tasks based on availability.

    Prefers gemini-3.5-flash -> gemini-3.1-flash-lite.
    """
    available = _available_models()
    preferred = [
        GEMINI_PRIMARY_MODEL,
        GEMINI_ECONOMY_MODEL,
    ]
    for pref in preferred:
        if pref in available:
            return pref
    return normalize_gemini_chat_model(_setting("DEFAULT_MODEL", GEMINI_PRIMARY_MODEL))


def _vision_reply_markup() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("🎭 Выбрать роль ИИ", callback_data="open_roles:from_response")],
            [InlineKeyboardButton("✨ Начать новую тему", callback_data="new_topic")],
        ]
    )


async def _process_ai_vision(
    placeholder_message: Message,
    parts: list,
    chat_state: ChatState,
    user_id: int | None = None,
    chat_id: int | None = None,
    is_ocr: bool = False,
):
    """Generate and deliver one vision response, returning its typed outcome."""
    from app.providers.freetheai_image import FTA_IMAGE_MODELS

    if is_ocr:
        raw_model = _pick_ocr_model()
    else:
        default_model = _setting("DEFAULT_MODEL", GEMINI_PRIMARY_MODEL)
        raw_model = chat_state.model or default_model
        # FTA image-generation models (img/*, vhr/*) cannot analyse received photos.
        # Fall back to the default Gemini vision model silently.
        if raw_model in FTA_IMAGE_MODELS:
            logging.debug(
                "Vision request: model %s is image-gen-only — falling back to %s",
                raw_model,
                default_model,
            )
            raw_model = default_model

    history = [{"role": "user", "parts": parts}]
    _vision_t0 = time.monotonic()

    from app.providers.request_factory import generation_request_from_history
    from app.providers.stream_types import Workload
    from app.response_delivery.delivery import (
        TelegramTarget,
        get_telegram_response_delivery,
    )
    from app.response_delivery.outcomes import CompleteDelivery, PartialDelivery
    from app.response_delivery.presentation import FixedPresentation

    request = await generation_request_from_history(
        models=(raw_model,),
        history=history,
        user_id=user_id,
        chat_id=chat_id,
        thinking_level=chat_state.thinking_level,
        workload=Workload.INTERACTIVE,
        allow_deferred=False,
    )
    outcome = await get_telegram_response_delivery().stream(
        TelegramTarget(
            placeholder_message=placeholder_message,
            bot=placeholder_message.get_bot(),
            chat_id=chat_id,
            private_content=True,
        ),
        request,
        presentation=FixedPresentation(
            actions=_vision_reply_markup(),
            recovery_actions=_vision_reply_markup(),
            failure_actions=_vision_reply_markup(),
            long_read_title="Анализ изображения",
        ),
    )
    if isinstance(outcome, (CompleteDelivery, PartialDelivery)):
        model_used = raw_model
        metadata = (
            outcome.completion
            if isinstance(outcome, CompleteDelivery)
            else outcome.terminal
        )
        route = getattr(metadata, "route", None)
        if route is not None:
            model_used = route.actual_model

        from app.metrics import metrics_collector as _mc

        await _mc.record_api_call("gemini_vision", model_used, user_id=user_id)
        await _mc.record_request("photo", time.monotonic() - _vision_t0, success=True)

    return outcome


# ── Single photo handler ────────────────────────────────────────────────────


async def _handle_photo(placeholder_message: Message, original_message: Message, chat_state: ChatState):
    user_id = int(original_message.from_user.id)
    known_epoch = (
        None
        if getattr(chat_state, "_has_persisted_chat", True) is False
        else int(chat_state.memory_epoch)
    )
    expected_epoch = await ensure_chat_generation(user_id, expected_epoch=known_epoch)
    if expected_epoch is None:
        return
    chat_state.memory_epoch = expected_epoch
    chat_state._has_persisted_chat = True
    from app.repos.memory_consent import private_data_lease

    async with private_data_lease(
        user_id,
        expected_epoch,
        purpose="conversation:vision",
        require_ltm=False,
    ) as lease_current:
        if not lease_current:
            return
        await _handle_photo_leased_impl(
            placeholder_message,
            original_message,
            chat_state,
            _expected_epoch=expected_epoch,
        )


async def _handle_photo_leased_impl(
    placeholder_message: Message,
    original_message: Message,
    chat_state: ChatState,
    *,
    _expected_epoch: int,
):
    stop_heartbeat(placeholder_message.message_id)
    try:
        photo_file = await original_message.photo[-1].get_file()
        img_raw = await get_file_bytes(original_message.get_bot(), photo_file)

        # Determine intent (OCR vs description)
        intent = await classify_vision_intent(original_message.caption)
        is_ocr = (intent == "ocr")

        if is_ocr:
            formatted_prompt = _build_ocr_prompt(original_message.caption)
            task_type = "ocr"
        else:
            formatted_prompt = _build_vision_prompt(original_message.caption, image_count=1)
            task_type = "describe"

        # Pre-compress with cache_key for retry savings
        file_unique_id = original_message.photo[-1].file_unique_id
        compressed = await save_image_as_bytes(img_raw, task_type=task_type, cache_key=file_unique_id)

        # Wrap as TaggedImage so providers skip recompression
        if compressed:
            tagged = TaggedImage(
                data=compressed,
                cache_key=file_unique_id,
                task_type=task_type,
                pre_compressed=True,
            )
            parts = [formatted_prompt, tagged]
        else:
            parts = [formatted_prompt, img_raw] if img_raw else [formatted_prompt]

        await update_stage(placeholder_message, STAGES_PHOTO, 1)

        user_id = original_message.from_user.id
        chat_id = placeholder_message.chat.id if placeholder_message.chat else None

        outcome = await _process_ai_vision(
            placeholder_message,
            parts,
            chat_state,
            user_id=user_id,
            chat_id=chat_id,
            is_ocr=is_ocr,
        )

        from app.response_delivery.outcomes import CompleteDelivery, PartialDelivery

        if not isinstance(outcome, (CompleteDelivery, PartialDelivery)):
            return

        response_text = outcome.content_text
        chat_state.history.append({"role": "user", "parts": [formatted_prompt]})
        chat_state.history.append({"role": "model", "parts": [response_text]})
        await update_user_chat(user_id, chat_state, expected_epoch=_expected_epoch)

        # ── Store photo description in long-term memory (background) ──
        _photo_bytes = compressed or img_raw
        from app.repos.memory_consent import capture_epoch

        _memory_epoch = capture_epoch(chat_state)
        if _photo_bytes and _memory_epoch is not None:
            from app.repos.memory_autosave import submit_memory_task

            def _bg_photo_ltm():
                async def _store():
                    from app.utils.multimodal_processor import process_media_for_memory

                    await process_media_for_memory(
                        _photo_bytes,
                        user_id,
                        media_type="image",
                        telegram_file_id=file_unique_id,
                        expected_epoch=_memory_epoch,
                    )

                return _store()

            submit_memory_task(user_id, _bg_photo_ltm, retry=2)

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


def _media_group_user_id(messages: list[Message]) -> int:
    """Return the human sender, rejecting mixed/bot-authored media groups."""
    if not messages or messages[0].from_user is None:
        raise ValueError("media group has no authoritative sender")
    user_id = int(messages[0].from_user.id)
    if any(message.from_user is None or int(message.from_user.id) != user_id for message in messages):
        raise ValueError("media group contains messages from different senders")
    return user_id


async def _handle_media_group_photos(
    placeholder_message: Message,
    messages: list[Message],
    caption: str,
    chat_state: ChatState,
):
    user_id = _media_group_user_id(messages)
    known_epoch = (
        None
        if getattr(chat_state, "_has_persisted_chat", True) is False
        else int(chat_state.memory_epoch)
    )
    expected_epoch = await ensure_chat_generation(user_id, expected_epoch=known_epoch)
    if expected_epoch is None:
        return
    chat_state.memory_epoch = expected_epoch
    chat_state._has_persisted_chat = True
    from app.repos.memory_consent import private_data_lease

    async with private_data_lease(
        user_id,
        expected_epoch,
        purpose="conversation:vision-group",
        require_ltm=False,
    ) as lease_current:
        if not lease_current:
            return
        await _handle_media_group_photos_leased_impl(
            placeholder_message,
            messages,
            caption,
            chat_state,
        )


async def _handle_media_group_photos_leased_impl(
    placeholder_message: Message,
    messages: list[Message],
    caption: str,
    chat_state: ChatState,
):
    """Обрабатывает группу изображений для обычного описания"""
    try:
        user_id = _media_group_user_id(messages)
        # Load все images from groups
        images = await _download_images_concurrently(messages, placeholder=placeholder_message)

        if not images:
            await placeholder_message.edit_text("❌ Не удалось загрузить ни одного изображения из группы.")
            return

        await update_stage(placeholder_message, STAGES_PHOTO, 1)

        # Classify intent for group
        intent = await classify_vision_intent(caption)
        is_ocr = (intent == "ocr")

        image_count = len(images)
        if is_ocr:
            formatted_prompt = _build_ocr_prompt(caption)
            task_type = "ocr"
        else:
            formatted_prompt = _build_vision_prompt(caption, image_count=image_count)
            task_type = "describe"

        # Wrap raw bytes as TaggedImage
        tagged_images = [
            TaggedImage(data=img, task_type=task_type)
            for img in images  # type: ignore[arg-type]  # download_one returns bytes
        ]

        # Create parts for Gemini API: text + все images
        parts = [formatted_prompt] + tagged_images

        # Get user_id и chat_id for логирования
        chat_id = placeholder_message.chat.id if placeholder_message.chat else None

        outcome = await _process_ai_vision(
            placeholder_message,
            parts,
            chat_state,
            user_id=user_id,
            chat_id=chat_id,
            is_ocr=is_ocr,
        )

        from app.response_delivery.outcomes import CompleteDelivery, PartialDelivery

        if not isinstance(outcome, (CompleteDelivery, PartialDelivery)):
            return

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
    user_id = _media_group_user_id(messages)
    known_epoch = (
        None
        if getattr(chat_state, "_has_persisted_chat", True) is False
        else int(chat_state.memory_epoch)
    )
    expected_epoch = await ensure_chat_generation(user_id, expected_epoch=known_epoch)
    if expected_epoch is None:
        return
    chat_state.memory_epoch = expected_epoch
    chat_state._has_persisted_chat = True
    from app.repos.memory_consent import private_data_lease

    async with private_data_lease(
        user_id,
        expected_epoch,
        purpose="conversation:vision-research",
        require_ltm=False,
    ) as lease_current:
        if not lease_current:
            return
        await _handle_complex_media_group_search_leased_impl(
            placeholder_message,
            messages,
            caption,
            search_prefix,
            chat_state,
        )


async def _handle_complex_media_group_search_leased_impl(
    placeholder_message: Message,
    messages: list[Message],
    caption: str,
    search_prefix: str,
    chat_state: ChatState,
):
    """Обрабатывает группу изображений для сложного поиска"""
    user_id = _media_group_user_id(messages)

    try:
        await placeholder_message.edit_text("🖼️ Анализирую группу изображений...")
    except Exception as edit_error:
        logging.error("Could not edit placeholder message: %s", edit_error)
        placeholder_message = await placeholder_message.reply_text("🖼️ Анализирую группу изображений...")

    vision_model = _setting("RESEARCH_MODEL", GEMINI_PRIMARY_MODEL)

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
            await _handle_qna_search(
                placeholder_message,
                original_user_message,
                chat_state,
                search_query,
                user_id=user_id,
            )
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
