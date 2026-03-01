"""
AI Photo & Media Group handlers — single photo processing, media groups,
concurrent image downloads, and complex media group search.
"""

import asyncio
import logging

from PIL import Image
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Message

from app import prompts
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
from app.repos.chats import get_user_chat, update_user_chat
from app.utils.messaging import send_long_message
from app.utils.stage_indicators import STAGES_PHOTO, update_stage


async def _handle_photo(
    placeholder_message: Message, original_message: Message, chat_state: ChatState
):
    try:
        photo_file = await original_message.photo[-1].get_file()
        photo_data = await photo_file.download_as_bytearray()
        img = bytes(photo_data)
        prompt = original_message.caption or "Опиши это изображение."

        # Add инструкции по форматированию к промпту for fromображений
        formatted_prompt = f"""# РОЛЬ И ЗАДАЧА
Ты — эксперт по аналfromу fromображений for Telegram-бота. Твоя задача — описать image, используя правильное форматирование и предоставляя детальную, полезную информацию.

# КОНТЕКСТ
**Запрос user:** {prompt}

# ПОШАГОВЫЙ АНАЛИЗ
1. **Внимательно fromучи image**
2. **Определи основные объекты и детали**
3. **Структурируй описание логично**
4. **Примени правильное MarkdownV2 форматирование**

# FEW-SHOT ПРИМЕРЫ
## Пример 1: Пейзаж
**Изображение:** Горный пейзаж с заснеженными вершинами
**Правильное описание:**
*Горный пейзаж* с заснеженными вершинами на фоне голубого неба.

_Детали:_
- Снежные пики отражают солнечный свет
- Внfromу виднеется зеленый лес
- Облака создают драматическую атмосферу

## Пример 2: Портрет
**Изображение:** Человек в деловом костюме
**Правильное описание:**
*Человек* в деловом костюме с уверенным выражением лица.

_Характеристики:_
- Темный костюм с галстуком
- Профессиональная поза
- Фон размыт for акцента на лице

## Пример 3: Технический объект
**Изображение:** Соtemporary автомобиль
**Правильное описание:**
*Соtemporary автомобиль* с обтекаемым дfromайном и спортивными линиями.

_Особенности:_
- Аэродинамическая форма кузова
- LED фары и стоп-сигналы
- Спортивные колесные диски

# ПРАВИЛА ФОРМАТИРОВАНИЯ
## ✅ РАЗРЕШЕНО
- `*жирный text*` for keyевых объектов и характеристик
- `_курсив_` for вторичных деталей и описаний
- `` `код` `` for технических терминов
- `[text ссылки](URL)` for ссылок (if onменимо)
- `- ` for списков характеристик

## ❌ ЗАПРЕЩЕНО
- HTML теги: `<b>`, `<i>`, `<code>`, `<a>`
- Двойные символы: `**text**`, `__text__`
- LaTeX математические выражения: `$...$`, `$$...$$`

# СТРУКТУРА ОПИСАНИЯ
1. **Основной объект** - что fromображено
2. **Ключевые характеристики** - цвет, размер, стиль
3. **Конtext и окружение** - где, когда, в какой обстановке
4. **Детали и особенности** - уникальные элементы
5. **Общее впеchatление** - настроение, атмосфера

# ВАЖНЫЕ ПРАВИЛА
- Будь конкретным и детальным
- Используй описательные onлагательные
- Структурируй информацию по пунктам
- Применяй правильное форматирование
- Не используй технический жаргон without объяснений
- Следуй структуре onмеров выше

# ФИНАЛЬНАЯ ПРОВЕРКА
Перед отправкой описания убедись, что:
- [ ] Описание полностью описывает image
- [ ] Информация структурирована согласно onмерам
- [ ] Использован правильный MarkdownV2 синтаксис
- [ ] Нет HTML тегов or LaTeX синтаксиса
- [ ] Тон описания информативный и дружелюбный

Опиши изображение, следуя указанным инструкциям и структуре примеров."""

        # Create parts for Gemini API: text + image
        parts = [formatted_prompt, img] if img else [formatted_prompt]

        await update_stage(placeholder_message, STAGES_PHOTO, 1)

        response_text, _ = await _get_ai_response_with_routing(
            chat_state.model or settings.DEFAULT_MODEL,
            [{"role": "user", "parts": parts}],
            user_id=original_message.from_user.id,
            chat_id=placeholder_message.chat.id if placeholder_message.chat else None,
        )

        # Check ошибки от роутера
        if await handle_ai_response_error(response_text, placeholder_message):
            return

        if response_text and response_text.strip():
            # Add role button and new topic button to photo responses
            buttons = [
                [
                    InlineKeyboardButton(
                        "🎭 Выбрать роль ИИ", callback_data="open_roles:from_response"
                    )
                ],
                [
                    InlineKeyboardButton(
                        "✨ Начать новую тему", callback_data="new_topic"
                    )
                ],
            ]
            reply_markup = InlineKeyboardMarkup(buttons)
            await send_long_message(
                placeholder_message, response_text, reply_markup=reply_markup
            )
            # Save context images в истории
            chat_state.history.append({"role": "user", "parts": [formatted_prompt]})
            chat_state.history.append({"role": "model", "parts": [response_text]})
            await update_user_chat(original_message.from_user.id, chat_state)
        else:
            # Add role button and new topic button to error responses too
            buttons = [
                [
                    InlineKeyboardButton(
                        "🎭 Выбрать роль ИИ", callback_data="open_roles:from_response"
                    )
                ],
                [
                    InlineKeyboardButton(
                        "✨ Начать новую тему", callback_data="new_topic"
                    )
                ],
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
            await placeholder_message.edit_text(
                "❌ Произошла ошибка при обработке изображения."
            )
        except Exception as edit_error:
            logging.error("Could not edit placeholder message: %s", edit_error)
            # Fallback на new message
            await original_message.reply_text(
                "❌ Произошла ошибка при обработке изображения."
            )


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
    logging.info(
        f"🔄 Обрабатываю группу из {count} изображений для пользователя {user_id}"
    )

    # Check, есть ли searchовый префикс в caption
    search_prefix = None
    if caption:
        if caption.startswith("??"):
            search_prefix = "??"
        elif caption.startswith("?"):
            search_prefix = "?"

    # If есть searchовый префикс, use сложный search
    if search_prefix:
        await _handle_complex_media_group_search(
            placeholder_message, messages, caption, search_prefix, chat_state
        )
    else:
        # Обычная обработка groups fromображений
        await _handle_media_group_photos(
            placeholder_message, messages, caption, chat_state
        )


async def _download_images_concurrently(
    messages: list[Message], log_context: str = ""
) -> list[Image.Image]:
    """
    Downloads images from a list of messages concurrently.
    """

    async def download_one(index, message) -> None:
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

    return [img for img in results if img is not None]


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
            await placeholder_message.edit_text(
                "❌ Не удалось загрузить ни одного изображения из группы."
            )
            return

        await update_stage(placeholder_message, STAGES_PHOTO, 1)

        # Build промпт for groups fromображений
        count = len(images) if images else 0
        prompt = caption or f"Опиши эти {count} изображения."

        # Add инструкции по форматированию
        formatted_prompt = f"""# РОЛЬ И ЗАДАЧА
Ты — эксперт по аналfromу групп fromображений for Telegram-бота. Твоя задача — описать группу fromображений, используя правильное форматирование и предоставляя детальную, полезную информацию.

# КОНТЕКСТ
**Запрос user:** {prompt}

# ПОШАГОВЫЙ АНАЛИЗ
1. **Внимательно fromучи каждое image**
2. **Определи основные объекты и детали**
3. **Проаналfromируй связи between imagesми**
4. **Структурируй описание логично**
5. **Примени правильное MarkdownV2 форматирование**

# FEW-SHOT ПРИМЕРЫ
## Пример 1: Последовательность событий
**Группа:** 3 images процесса onготовления блюда
**Правильное описание:**
*Группа fromображений показывает процесс onготовления блюда:*

_Изображение 1:_ Подготовка ингредиентов на кухонном столе
_Изображение 2:_ Процесс готовки на плите
_Изображение 3:_ Готовое блюдо на тарелке

## Пример 2: Разные аспекты темы
**Группа:** 4 images разных типов автомобилей
**Правильное описание:**
*Коллекция различных типов автомобилей:*

_Изображение 1:_ *Спортивный автомобиль* с обтекаемым дfromайном
_Изображение 2:_ *Внедорожник* с высоким клиренсом
_Изображение 3:_ *Семейный седан* с практичным салоном
_Изображение 4:_ *Электромобиль* с современным дfromайном

## Пример 3: Сравнение or контраст
**Группа:** 2 images старого и нового здания
**Правильное описание:**
*Сравнение архитектурных стилей:*

_Изображение 1:_ *Классическое здание* с традиционными элементами
_Изображение 2:_ *Современное здание* с инновационным дfromайном

# ПРАВИЛА ФОРМАТИРОВАНИЯ
## ✅ РАЗРЕШЕНО
- `*жирный text*` for keyевых объектов и характеристик
- `_курсив_` for вторичных деталей и описаний
- `` `код` `` for технических терминов
- `[text ссылки](URL)` for ссылок (if onменимо)
- `- ` for списков характеристик

## ❌ ЗАПРЕЩЕНО
- HTML теги: `<b>`, `<i>`, `<code>`, `<a>`
- Двойные символы: `**text**`, `__text__`
- LaTeX математические выражения: `$...$`, `$$...$$`

# СТРУКТУРА ОПИСАНИЯ ГРУППЫ
1. **Общий context** - что представляет group fromображений
2. **Индивидуальные описания** - каждое image отдельно
3. **Связи и отношения** - как images связаны between собой
4. **Общие темы** - что объединяет все images
5. **Общее впеchatление** - итоговое восonятие groups

# ВАЖНЫЕ ПРАВИЛА
- Пронумеруй images for ясности
- Опиши каждое image отдельно
- Выдели связи between imagesми
- Будь конкретным и детальным
- Используй описательные onлагательные
- Структурируй информацию по пунктам
- Применяй правильное форматирование
- Не используй технический жаргон without объяснений
- Следуй структуре onмеров выше

# ФИНАЛЬНАЯ ПРОВЕРКА
Перед отправкой описания убедись, что:
- [ ] Описание полностью описывает группу fromображений
- [ ] Каждое image описано отдельно
- [ ] Выделены связи between imagesми
- [ ] Информация структурирована согласно onмерам
- [ ] Использован правильный MarkdownV2 синтаксис
- [ ] Нет HTML тегов or LaTeX синтаксиса
- [ ] Тон описания информативный и дружелюбный

Опиши группу изображений, следуя указанным инструкциям и структуре примеров."""

        # Create parts for Gemini API: text + все images
        parts = [formatted_prompt] + (images or [])

        # Get user_id и chat_id for логирования
        user_id = (
            placeholder_message.from_user.id if placeholder_message.from_user else None
        )
        chat_id = placeholder_message.chat.id if placeholder_message.chat else None

        response_text, _ = await _get_ai_response_with_routing(
            chat_state.model or settings.DEFAULT_MODEL,
            [{"role": "user", "parts": parts}],
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
        await send_long_message(
            placeholder_message,
            response_text or "Не удалось обработать группу изображений.",
            reply_markup=reply_markup,
        )


        count = len(images) if images else 0
        logging.info("✅ Группа из %s изображений обработана успешно", count)

    except Exception as e:
        logging.error("Error processing media group photos: %s", e, exc_info=True)
        try:
            await placeholder_message.edit_text(
                "❌ Произошла ошибка при обработке группы изображений."
            )
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
        placeholder_message = await placeholder_message.reply_text(
            "🖼️ Анализирую группу изображений..."
        )

    vision_model = settings.RESEARCH_MODEL

    try:
        # Load все images from groups
        images = await _download_images_concurrently(
            messages, log_context="для анализа"
        )

        if not images:
            await placeholder_message.edit_text(
                "❌ Не удалось загрузить ни одного изображения для анализа."
            )
            return

        # Аналfromируем группу fromображений for searchа
        analysis_prompt = f"""{prompts.IMAGE_ANALYSIS_PROMPT}

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
        user_id = (
            placeholder_message.from_user.id if placeholder_message.from_user else None
        )
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
                await placeholder_message.edit_text(
                    "Не удалось проанализировать группу изображений для поиска."
                )
            except Exception as edit_error:
                logging.error("Could not edit placeholder message: %s", edit_error)
            return

        # Get оригинальное message user for локалfromации
        count = len(images) if images else 0
        original_user_message = caption or f"Опиши эти {count} изображения."

        if search_prefix == "?":
            await _handle_qna_search(
                placeholder_message, original_user_message, chat_state, search_query
            )
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
            await placeholder_message.edit_text(
                "❌ Произошла ошибка при анализе группы изображений."
            )
        except Exception as edit_error:
            logging.error("Could not edit placeholder message: %s", edit_error)
