"""
AI Photo & Media Group handlers — single photo processing, media groups,
concurrent image downloads, and complex media group search.
"""

import asyncio
import logging

from PIL import Image
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Message

from app.config import settings
from app.database import ChatState
from app.handlers.ai_core import (
    _get_ai_response_with_routing,
    handle_ai_response_error,
)
from app.handlers.ai_search import (
    _handle_qna_search,
    _handle_research_agent,
)
from app.prompt_registry import get_registry
from app.repos.chats import get_user_chat, update_user_chat
from app.utils.heartbeat import stop_heartbeat
from app.utils.messaging import send_long_message
from app.utils.stage_indicators import STAGES_PHOTO, update_stage


async def _handle_photo(placeholder_message: Message, original_message: Message, chat_state: ChatState):
    stop_heartbeat(placeholder_message.message_id)
    try:
        photo_file = await original_message.photo[-1].get_file()
        photo_data = await photo_file.download_as_bytearray()
        img = bytes(photo_data)
        prompt = original_message.caption or "Опиши это изображение."

        from app.prompt_registry import FORMATTING_RULES_COMPACT

        # Add инструкции по форматированию к промпту for fromображений
        formatted_prompt = f"""# РОЛЬ И ЗАДАЧА
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

        # Create parts for Gemini API: text + image
        parts = [formatted_prompt, img] if img else [formatted_prompt]

        await update_stage(placeholder_message, STAGES_PHOTO, 1)

        # We need the current model, so we resolve it first
        from app.handlers.ai_core import _resolve_ai_request
        from app.streaming import stream_and_display
        _, model_used, _ = await _resolve_ai_request(chat_state.model or settings.DEFAULT_MODEL)
        history = [{"role": "user", "parts": parts}]

        response_text, success, stream_last_msg = await stream_and_display(
            placeholder_message,
            model_name=model_used,
            history=history,
            system_instruction=None,
            thinking_level=chat_state.thinking_level,
            user_id=original_message.from_user.id,
            bot=placeholder_message.get_bot(),
            chat_id=placeholder_message.chat.id if placeholder_message.chat else None,
            chat_type=placeholder_message.chat.type if placeholder_message.chat else "private",
        )

        streamed = bool(success and response_text)

        if not streamed:
            response_text, _ = await _get_ai_response_with_routing(
                model_used,
                history,
                user_id=original_message.from_user.id,
                chat_id=placeholder_message.chat.id if placeholder_message.chat else None,
            )

        # Check ошибки от роутера
        if await handle_ai_response_error(response_text, placeholder_message):
            return

        if response_text and response_text.strip():
            # Add role button and new topic button to photo responses
            buttons = [
                [InlineKeyboardButton("🎭 Выбрать роль ИИ", callback_data="open_roles:from_response")],
                [InlineKeyboardButton("✨ Начать новую тему", callback_data="new_topic")],
            ]
            reply_markup = InlineKeyboardMarkup(buttons)
            
            if not streamed:
                await send_long_message(placeholder_message, response_text, reply_markup=reply_markup)
            else:
                button_msg = stream_last_msg if stream_last_msg else placeholder_message
                try:
                    await button_msg.edit_reply_markup(reply_markup=reply_markup)
                except Exception as e:
                    if "not modified" not in str(e).lower():
                        logging.warning("Final button edit failed: %s", e)
                        
            # Save context images в истории
            chat_state.history.append({"role": "user", "parts": [formatted_prompt]})
            chat_state.history.append({"role": "model", "parts": [response_text]})
            await update_user_chat(original_message.from_user.id, chat_state)
        else:
            # Add role button and new topic button to error responses too
            buttons = [
                [InlineKeyboardButton("🎭 Выбрать роль ИИ", callback_data="open_roles:from_response")],
                [InlineKeyboardButton("✨ Начать новую тему", callback_data="new_topic")],
            ]
            reply_markup = InlineKeyboardMarkup(buttons)
            await send_long_message(
                placeholder_message,
                "Не удалось обработать изображение.",
                reply_markup=reply_markup,
            )
            logging.warning(
                f"Empty response from Gemini API for image processing by user {original_message.from_user.id}"
            )

    except Exception as e:
        logging.error("Error processing photo: %s", e, exc_info=True)
        try:
            await placeholder_message.edit_text("❌ Произошла ошибка при обработке изображения.")
        except Exception as edit_error:
            logging.error("Could not edit placeholder message: %s", edit_error)
            # Fallback на new message
            await original_message.reply_text("❌ Произошла ошибка при обработке изображения.")


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


async def _download_images_concurrently(messages: list[Message], log_context: str = "") -> list[Image.Image]:
    """
    Downloads images from a list of messages concurrently.
    """

    async def download_one(index: int, message: Message) -> bytes | None:
        try:
            photo_file = await message.photo[-1].get_file()
            photo_data = await photo_file.download_as_bytearray()
            img = bytes(photo_data)

            # Format log message
            count = len(messages)
            log_msg = f"📸 Загружено изображение {index + 1}/{count}"
            if log_context:
                log_msg += f" {log_context}"
            logging.info(log_msg)

            return img
        except Exception as e:
            logging.error("Error loading image %s: %s", index + 1, e, exc_info=True)
            return None

    tasks = [download_one(i, msg) for i, msg in enumerate(messages)]
    results = await asyncio.gather(*tasks)

    return [img for img in results if img is not None]  # type: ignore[misc]  # download_one returns bytes


async def _handle_media_group_photos(
    placeholder_message: Message,
    messages: list[Message],
    caption: str,
    chat_state: ChatState,
):
    """Обрабатывает группу изображений для обычного описания"""
    try:
        # Load все images from groups
        images = await _download_images_concurrently(messages)

        if not images:
            await placeholder_message.edit_text("❌ Не удалось загрузить ни одного изображения из группы.")
            return

        await update_stage(placeholder_message, STAGES_PHOTO, 1)

        # Build промпт for groups fromображений
        count = len(images) if images else 0
        prompt = caption or f"Опиши эти {count} изображения."

        from app.prompt_registry import FORMATTING_RULES_COMPACT as _FRC

        # Add инструкции по форматированию
        formatted_prompt = f"""# РОЛЬ И ЗАДАЧА
Ты — эксперт по анализу групп изображений для Telegram-бота. Опиши группу изображений, используя правильное форматирование.

# КОНТЕКСТ
**Запрос пользователя:** {prompt}

# ИНСТРУКЦИИ
1. Внимательно изучи каждое изображение
2. Определи основные объекты и детали
3. Проанализируй связи между изображениями
4. Структурируй описание логично
5. Примени стандартное Markdown форматирование

{_FRC}

# СТРУКТУРА ОПИСАНИЯ ГРУППЫ
1. **Общий контекст** — что представляет группа изображений
2. **Индивидуальные описания** — каждое изображение отдельно
3. **Связи и отношения** — как изображения связаны между собой

Опиши группу изображений, следуя указанным инструкциям."""

        # Create parts for Gemini API: text + все images
        parts = [formatted_prompt] + (images or [])

        # Get user_id и chat_id for логирования
        user_id = placeholder_message.from_user.id if placeholder_message.from_user else None
        chat_id = placeholder_message.chat.id if placeholder_message.chat else None

        from app.handlers.ai_core import _resolve_ai_request
        from app.streaming import stream_and_display

        _, model_used, _ = await _resolve_ai_request(chat_state.model or settings.DEFAULT_MODEL)
        history = [{"role": "user", "parts": parts}]

        response_text, success, stream_last_msg = await stream_and_display(
            placeholder_message,
            model_name=model_used,
            history=history,
            system_instruction=None,
            thinking_level=chat_state.thinking_level,
            user_id=user_id,
            bot=placeholder_message.get_bot(),
            chat_id=chat_id,
            chat_type=placeholder_message.chat.type if placeholder_message.chat else "private",
        )

        streamed = bool(success and response_text)

        if not streamed:
            response_text, _ = await _get_ai_response_with_routing(
                model_used,
                history,
                user_id=user_id,
                chat_id=chat_id,
            )

        # Check ошибки от роутера
        if await handle_ai_response_error(response_text, placeholder_message):
            return

        # Add role button and new topic button to media group responses
        buttons = [
            [InlineKeyboardButton("🎭 Выбрать роль ИИ", callback_data="open_roles:from_response")],
            [InlineKeyboardButton("✨ Начать новую тему", callback_data="new_topic")],
        ]
        reply_markup = InlineKeyboardMarkup(buttons)
        
        err_msg = "Не удалось обработать группу изображений."
        if not streamed:
            await send_long_message(
                placeholder_message,
                response_text or err_msg,
                reply_markup=reply_markup,
            )
        else:
            button_msg = stream_last_msg if stream_last_msg else placeholder_message
            try:
                await button_msg.edit_reply_markup(reply_markup=reply_markup)
            except Exception as e:
                if "not modified" not in str(e).lower():
                    logging.warning("Final button edit failed: %s", e)

        count = len(images) if images else 0
        logging.info("✅ Группа из %s изображений обработана успешно", count)

    except Exception as e:
        logging.error("Error processing media group photos: %s", e, exc_info=True)
        try:
            await placeholder_message.edit_text("❌ Произошла ошибка при обработке группы изображений.")
        except Exception as edit_error:
            logging.error("Could not edit placeholder message: %s", edit_error)


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
        images = await _download_images_concurrently(messages, log_context="для анализа")

        if not images:
            await placeholder_message.edit_text("❌ Не удалось загрузить ни одного изображения для анализа.")
            return

        # Аналfromируем группу fromображений for searchа
        analysis_prompt = f"""{get_registry().get("image_analysis").text}

# ДОПОЛНИТЕЛЬНЫЕ ИНСТРУКЦИИ ДЛЯ ГРУППЫ ИЗОБРАЖЕНИЙ
## Аналfrom groups
- Проаналfromируй все images как единый context
- Выдели общие темы, объекты or концепции
- Учти взаимосвязи between imagesми
- Создай searchовый request, который охватывает весь context groups

## Специальные случаи
- If images показывают afterдовательность or процесс, отрази это в requestе
- If images демонстрируют разные аспекты одной темы, объедини их
- If images показывают сравнение or контраст, укажи это

# FEW-SHOT ПРИМЕРЫ ДЛЯ ГРУПП
## Пример 1: Последовательность событий
**Группа:** 3 images процесса onготовления блюда
**Правильный request:** `cooking process step by step recipe preparation`

## Пример 2: Разные аспекты темы
**Группа:** 4 images разных типов автомобилей
**Правильный request:** `car types comparison sedan SUV sports luxury vehicles`

## Пример 3: Контраст or сравнение
**Группа:** 2 images старого и нового здания
**Правильный request:** `architecture evolution old vs new building comparison`

# ФОРМАТ ВЫВОДА
Верни ТОЛЬКО searchовый request without:
- Кавычек
- Двоеточий
- Объяснений
- Вводных фраз
- Дополнительного форматирования

**Пример правильного вывода:**
```
cooking process step by step recipe preparation
```"""

        # Create parts for аналfromа: промпт + все images
        parts = [analysis_prompt] + (images or [])

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

        # Get оригинальное message user for локалfromации
        count = len(images) if images else 0
        original_user_message = caption or f"Опиши эти {count} изображения."

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

        count = len(images) if images else 0
        logging.info("✅ Группа из %s изображений проанализирована для поиска", count)

    except Exception as e:
        logging.error("Error processing complex media group search: %s", e, exc_info=True)
        try:
            await placeholder_message.edit_text("❌ Произошла ошибка при анализе группы изображений.")
        except Exception as edit_error:
            logging.error("Could not edit placeholder message: %s", edit_error)
