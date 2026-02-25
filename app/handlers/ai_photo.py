"""
AI Photo & Media Group handlers — single photo processing, media groups,
concurrent image downloads, and complex media group search.
"""

import logging
import io
import asyncio
from typing import List

from PIL import Image
from telegram import Message, InlineKeyboardButton, InlineKeyboardMarkup

from app.config import settings
from app import database as db
from app.utils.messaging import send_long_message
from app import prompts
from app.metrics import metrics_collector

from app.utils.stage_indicators import update_stage, STAGES_PHOTO

from app.handlers.ai_core import (
    handle_ai_response_error,
    _get_ai_response_with_routing,
)
from app.handlers.ai_search import (
    _handle_qna_search,
    _handle_research_agent,
)


async def _handle_photo(
    placeholder_message: Message, original_message: Message, chat_state: db.ChatState
):
    try:
        photo_file = await original_message.photo[-1].get_file()
        photo_data = await photo_file.download_as_bytearray()
        img = bytes(photo_data)
        prompt = original_message.caption or "Опиши это изображение."

        # Добавляем инструкции по форматированию к промпту для изображений
        formatted_prompt = f"""# РОЛЬ И ЗАДАЧА
Ты — эксперт по анализу изображений для Telegram-бота. Твоя задача — описать изображение, используя правильное форматирование и предоставляя детальную, полезную информацию.

# КОНТЕКСТ
**Запрос пользователя:** {prompt}

# ПОШАГОВЫЙ АНАЛИЗ
1. **Внимательно изучи изображение**
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
- Внизу виднеется зеленый лес
- Облака создают драматическую атмосферу

## Пример 2: Портрет
**Изображение:** Человек в деловом костюме
**Правильное описание:**
*Человек* в деловом костюме с уверенным выражением лица.

_Характеристики:_
- Темный костюм с галстуком
- Профессиональная поза
- Фон размыт для акцента на лице

## Пример 3: Технический объект
**Изображение:** Современный автомобиль
**Правильное описание:**
*Современный автомобиль* с обтекаемым дизайном и спортивными линиями.

_Особенности:_
- Аэродинамическая форма кузова
- LED фары и стоп-сигналы
- Спортивные колесные диски

# ПРАВИЛА ФОРМАТИРОВАНИЯ
## ✅ РАЗРЕШЕНО
- `*жирный текст*` для ключевых объектов и характеристик
- `_курсив_` для вторичных деталей и описаний
- `` `код` `` для технических терминов
- `[текст ссылки](URL)` для ссылок (если применимо)
- `- ` для списков характеристик

## ❌ ЗАПРЕЩЕНО
- HTML теги: `<b>`, `<i>`, `<code>`, `<a>`
- Двойные символы: `**текст**`, `__текст__`
- LaTeX математические выражения: `$...$`, `$$...$$`

# СТРУКТУРА ОПИСАНИЯ
1. **Основной объект** - что изображено
2. **Ключевые характеристики** - цвет, размер, стиль
3. **Контекст и окружение** - где, когда, в какой обстановке
4. **Детали и особенности** - уникальные элементы
5. **Общее впечатление** - настроение, атмосфера

# ВАЖНЫЕ ПРАВИЛА
- Будь конкретным и детальным
- Используй описательные прилагательные
- Структурируй информацию по пунктам
- Применяй правильное форматирование
- Не используй технический жаргон без объяснений
- Следуй структуре примеров выше

# ФИНАЛЬНАЯ ПРОВЕРКА
Перед отправкой описания убедись, что:
- [ ] Описание полностью описывает изображение
- [ ] Информация структурирована согласно примерам
- [ ] Использован правильный MarkdownV2 синтаксис
- [ ] Нет HTML тегов или LaTeX синтаксиса
- [ ] Тон описания информативный и дружелюбный

Опиши изображение, следуя указанным инструкциям и структуре примеров."""

        # Создаем parts для Gemini API: текст + изображение
        parts = [formatted_prompt, img] if img else [formatted_prompt]

        await update_stage(placeholder_message, STAGES_PHOTO, 1)

        response_text, _ = await _get_ai_response_with_routing(
            chat_state.model or settings.DEFAULT_MODEL,
            [{"role": "user", "parts": parts}],
            user_id=original_message.from_user.id,
            chat_id=placeholder_message.chat.id if placeholder_message.chat else None,
        )

        # Проверяем ошибки от роутера
        if await handle_ai_response_error(response_text, placeholder_message):
            return

        if response_text and response_text.strip():
            # Add role button and new topic button to photo responses
            buttons = [
                [
                    InlineKeyboardButton(
                        "🎭 Выбрать роль ИИ", callback_data="open_roles"
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
            # Сохраняем контекст изображения в истории
            chat_state.history.append({"role": "user", "parts": [formatted_prompt]})
            chat_state.history.append({"role": "model", "parts": [response_text]})
            await db.update_user_chat(original_message.from_user.id, chat_state)
        else:
            # Add role button and new topic button to error responses too
            buttons = [
                [
                    InlineKeyboardButton(
                        "🎭 Выбрать роль ИИ", callback_data="open_roles"
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
        logging.error(f"Error processing photo: {e}")
        try:
            await placeholder_message.edit_text(
                "❌ Произошла ошибка при обработке изображения."
            )
        except Exception as edit_error:
            logging.error(f"Could not edit placeholder message: {edit_error}")
            # Fallback на новое сообщение
            await original_message.reply_text(
                "❌ Произошла ошибка при обработке изображения."
            )


async def process_media_group_request(
    placeholder_message: Message,
    update,
    context,
    messages: List[Message],
    caption: str,
):
    # context используется для совместимости с другими функциями
    """Обрабатывает группу изображений как единое целое"""
    user_id = update.effective_user.id
    chat_state = await db.get_user_chat(user_id)

    count = len(messages) if messages else 0
    logging.info(
        f"🔄 Обрабатываю группу из {count} изображений для пользователя {user_id}"
    )

    # Проверяем, есть ли поисковый префикс в caption
    search_prefix = None
    if caption:
        if caption.startswith("??"):
            search_prefix = "??"
        elif caption.startswith("?"):
            search_prefix = "?"

    # Если есть поисковый префикс, используем сложный поиск
    if search_prefix:
        await _handle_complex_media_group_search(
            placeholder_message, messages, caption, search_prefix, chat_state
        )
    else:
        # Обычная обработка группы изображений
        await _handle_media_group_photos(
            placeholder_message, messages, caption, chat_state
        )


async def _download_images_concurrently(
    messages: List[Message], log_context: str = ""
) -> List[Image.Image]:
    """
    Downloads images from a list of messages concurrently.
    """

    async def download_one(index, message):
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
            logging.error(f"Error loading image {index + 1}: {e}")
            return None

    tasks = [download_one(i, msg) for i, msg in enumerate(messages)]
    results = await asyncio.gather(*tasks)

    return [img for img in results if img is not None]


async def _handle_media_group_photos(
    placeholder_message: Message,
    messages: List[Message],
    caption: str,
    chat_state: db.ChatState,
):
    """Обрабатывает группу изображений для обычного описания"""
    try:
        # Загружаем все изображения из группы
        images = await _download_images_concurrently(messages)

        if not images:
            await placeholder_message.edit_text(
                "❌ Не удалось загрузить ни одного изображения из группы."
            )
            return

        await update_stage(placeholder_message, STAGES_PHOTO, 1)

        # Формируем промпт для группы изображений
        count = len(images) if images else 0
        prompt = caption or f"Опиши эти {count} изображения."

        # Добавляем инструкции по форматированию
        formatted_prompt = f"""# РОЛЬ И ЗАДАЧА
Ты — эксперт по анализу групп изображений для Telegram-бота. Твоя задача — описать группу изображений, используя правильное форматирование и предоставляя детальную, полезную информацию.

# КОНТЕКСТ
**Запрос пользователя:** {prompt}

# ПОШАГОВЫЙ АНАЛИЗ
1. **Внимательно изучи каждое изображение**
2. **Определи основные объекты и детали**
3. **Проанализируй связи между изображениями**
4. **Структурируй описание логично**
5. **Примени правильное MarkdownV2 форматирование**

# FEW-SHOT ПРИМЕРЫ
## Пример 1: Последовательность событий
**Группа:** 3 изображения процесса приготовления блюда
**Правильное описание:**
*Группа изображений показывает процесс приготовления блюда:*

_Изображение 1:_ Подготовка ингредиентов на кухонном столе
_Изображение 2:_ Процесс готовки на плите
_Изображение 3:_ Готовое блюдо на тарелке

## Пример 2: Разные аспекты темы
**Группа:** 4 изображения разных типов автомобилей
**Правильное описание:**
*Коллекция различных типов автомобилей:*

_Изображение 1:_ *Спортивный автомобиль* с обтекаемым дизайном
_Изображение 2:_ *Внедорожник* с высоким клиренсом
_Изображение 3:_ *Семейный седан* с практичным салоном
_Изображение 4:_ *Электромобиль* с современным дизайном

## Пример 3: Сравнение или контраст
**Группа:** 2 изображения старого и нового здания
**Правильное описание:**
*Сравнение архитектурных стилей:*

_Изображение 1:_ *Классическое здание* с традиционными элементами
_Изображение 2:_ *Современное здание* с инновационным дизайном

# ПРАВИЛА ФОРМАТИРОВАНИЯ
## ✅ РАЗРЕШЕНО
- `*жирный текст*` для ключевых объектов и характеристик
- `_курсив_` для вторичных деталей и описаний
- `` `код` `` для технических терминов
- `[текст ссылки](URL)` для ссылок (если применимо)
- `- ` для списков характеристик

## ❌ ЗАПРЕЩЕНО
- HTML теги: `<b>`, `<i>`, `<code>`, `<a>`
- Двойные символы: `**текст**`, `__текст__`
- LaTeX математические выражения: `$...$`, `$$...$$`

# СТРУКТУРА ОПИСАНИЯ ГРУППЫ
1. **Общий контекст** - что представляет группа изображений
2. **Индивидуальные описания** - каждое изображение отдельно
3. **Связи и отношения** - как изображения связаны между собой
4. **Общие темы** - что объединяет все изображения
5. **Общее впечатление** - итоговое восприятие группы

# ВАЖНЫЕ ПРАВИЛА
- Пронумеруй изображения для ясности
- Опиши каждое изображение отдельно
- Выдели связи между изображениями
- Будь конкретным и детальным
- Используй описательные прилагательные
- Структурируй информацию по пунктам
- Применяй правильное форматирование
- Не используй технический жаргон без объяснений
- Следуй структуре примеров выше

# ФИНАЛЬНАЯ ПРОВЕРКА
Перед отправкой описания убедись, что:
- [ ] Описание полностью описывает группу изображений
- [ ] Каждое изображение описано отдельно
- [ ] Выделены связи между изображениями
- [ ] Информация структурирована согласно примерам
- [ ] Использован правильный MarkdownV2 синтаксис
- [ ] Нет HTML тегов или LaTeX синтаксиса
- [ ] Тон описания информативный и дружелюбный

Опиши группу изображений, следуя указанным инструкциям и структуре примеров."""

        # Создаем parts для Gemini API: текст + все изображения
        parts = [formatted_prompt] + (images or [])

        # Получаем user_id и chat_id для логирования
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

        # Проверяем ошибки от роутера
        if await handle_ai_response_error(response_text, placeholder_message):
            return

        # Add role button and new topic button to media group responses
        buttons = [
            [InlineKeyboardButton("🎭 Выбрать роль ИИ", callback_data="open_roles")],
            [InlineKeyboardButton("✨ Начать новую тему", callback_data="new_topic")],
        ]
        reply_markup = InlineKeyboardMarkup(buttons)
        await send_long_message(
            placeholder_message,
            response_text or "Не удалось обработать группу изображений.",
            reply_markup=reply_markup,
        )


        count = len(images) if images else 0
        logging.info(f"✅ Группа из {count} изображений обработана успешно")

    except Exception as e:
        logging.error(f"Error processing media group photos: {e}")
        try:
            await placeholder_message.edit_text(
                "❌ Произошла ошибка при обработке группы изображений."
            )
        except Exception as edit_error:
            logging.error(f"Could not edit placeholder message: {edit_error}")


async def _handle_complex_media_group_search(
    placeholder_message: Message,
    messages: List[Message],
    caption: str,
    search_prefix: str,
    chat_state: db.ChatState,
):
    """Обрабатывает группу изображений для сложного поиска"""
    user_id = placeholder_message.from_user.id

    try:
        await placeholder_message.edit_text("🖼️ Анализирую группу изображений...")
    except Exception as edit_error:
        logging.error(f"Could not edit placeholder message: {edit_error}")
        placeholder_message = await placeholder_message.reply_text(
            "🖼️ Анализирую группу изображений..."
        )

    vision_model = settings.RESEARCH_MODEL

    try:
        # Загружаем все изображения из группы
        images = await _download_images_concurrently(
            messages, log_context="для анализа"
        )

        if not images:
            await placeholder_message.edit_text(
                "❌ Не удалось загрузить ни одного изображения для анализа."
            )
            return

        # Анализируем группу изображений для поиска
        analysis_prompt = f"""{prompts.IMAGE_ANALYSIS_PROMPT}

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

        # Создаем parts для анализа: промпт + все изображения
        parts = [analysis_prompt] + (images or [])

        # Получаем user_id и chat_id для логирования
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

        # Проверяем ошибки от роутера
        if await handle_ai_response_error(search_query, placeholder_message):
            return

        if not search_query:
            try:
                await placeholder_message.edit_text(
                    "Не удалось проанализировать группу изображений для поиска."
                )
            except Exception as edit_error:
                logging.error(f"Could not edit placeholder message: {edit_error}")
            return

        # Получаем оригинальное сообщение пользователя для локализации
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
        logging.info(f"✅ Группа из {count} изображений проанализирована для поиска")

    except Exception as e:
        logging.error(f"Error processing complex media group search: {e}")
        try:
            await placeholder_message.edit_text(
                "❌ Произошла ошибка при анализе группы изображений."
            )
        except Exception as edit_error:
            logging.error(f"Could not edit placeholder message: {edit_error}")
