"""
AI Document handler — processes questions about uploaded documents.
"""

import logging

from telegram import Message, InlineKeyboardButton, InlineKeyboardMarkup

from app.config import settings
from app import database as db
from app import services
from app.utils.messaging import send_long_message
from app.metrics import metrics_collector
from app.utils.stage_indicators import update_stage, STAGES_DOCUMENT

from app.handlers.ai_core import (
    handle_ai_response_error,
    _get_ai_response_with_routing,
)


async def _handle_document_question(
    placeholder_message: Message,
    user_id: int,
    user_message: str,
    chat_state: db.ChatState,
):
    """Обрабатывает вопросы по загруженным документам"""
    try:
        # Получаем последний документ пользователя
        from app.document_processor import get_user_documents, get_document_content

        documents = await get_user_documents(user_id)
        if not documents:
            try:
                await placeholder_message.edit_text(
                    "❌ У вас нет загруженных документов. Сначала загрузите документ."
                )
            except Exception as edit_error:
                logging.error(f"Could not edit placeholder message: {edit_error}")
                # Fallback на новое сообщение
                await placeholder_message.reply_text(
                    "❌ У вас нет загруженных документов. Сначала загрузите документ."
                )
            return

        # Берем самый последний документ
        latest_document = documents[0]
        document_content = await get_document_content(latest_document["id"], user_id)

        if not document_content:
            try:
                await placeholder_message.edit_text(
                    "❌ Не удалось получить содержимое документа."
                )
            except Exception as edit_error:
                logging.error(f"Could not edit placeholder message: {edit_error}")
                # Fallback на новое сообщение
                await placeholder_message.reply_text(
                    "❌ Не удалось получить содержимое документа."
                )
            return

        try:
            await update_stage(placeholder_message, STAGES_DOCUMENT, 0)
        except Exception as edit_error:
            logging.error(f"Could not edit placeholder message: {edit_error}")
            # Если не можем отредактировать, отправляем новое сообщение
            placeholder_message = await placeholder_message.reply_text(
                "📄 Анализирую документ..."
            )

        # Ограничиваем размер контекста документа
        max_context_length = 30000  # Ограничиваем до 30K символов
        original_length = len(document_content) if document_content else 0
        if document_content and len(document_content) > max_context_length:
            document_content = (
                document_content[:max_context_length]
                + "\n\n[Документ обрезан для экономии токенов]"
            )
            logging.info(
                f"Document content truncated from {original_length} to {len(document_content)} characters"
            )

        # Безопасная обработка document_content
        try:
            safe_document_content = str(document_content)
        except Exception as e:
            logging.error(f"Failed to convert document content to string: {e}")
            try:
                await placeholder_message.edit_text(
                    "❌ Ошибка обработки содержимого документа."
                )
            except Exception as edit_error:
                logging.error(f"Could not edit placeholder message: {edit_error}")
            return

        content_length = len(safe_document_content) if safe_document_content else 0
        logging.info(
            f"Processing document question for user {user_id}, document: {latest_document['filename']}, content length: {content_length}"
        )

        # Создаем промпт для вопроса по документу
        document_prompt = f"""# РОЛЬ И ЗАДАЧА
Ты — эксперт по анализу документов для Telegram-бота. Твоя задача — отвечать на вопросы пользователя по содержимому документа, используя правильное форматирование и предоставляя точную, полезную информацию.

# КОНТЕКСТ
**Содержимое документа:**
{safe_document_content}

**Вопрос пользователя:** {user_message}

# ПОШАГОВЫЙ ПРОЦЕСС
1. **Внимательно прочитай содержимое документа**
2. **Найди информацию, относящуюся к вопросу**
3. **Структурируй ответ логично**
4. **Примени правильное MarkdownV2 форматирование**

# FEW-SHOT ПРИМЕРЫ
## Пример 1: Технический вопрос
**Вопрос:** "Какие технологии упоминаются в документе?"
**Правильный ответ:**
*Технологии, упомянутые в документе:*
- Docker — для контейнеризации
- Python — основной язык программирования
- PostgreSQL — база данных
- Redis — кэш-система

## Пример 2: Поиск конкретной информации
**Вопрос:** "Какая версия Python используется?"
**Правильный ответ:**
*Версия Python:*
Согласно документу, используется Python версии `3.9` или выше.

_Дополнительные требования:_
- Поддержка async/await синтаксиса
- Совместимость с последними библиотеками

## Пример 3: Объяснение концепции
**Вопрос:** "Объясни архитектуру системы"
**Правильный ответ:**
*Архитектура системы:*
Документ описывает микросервисную архитектуру с следующими компонентами:

_Основные сервисы:_
- API Gateway — точка входа
- User Service — управление пользователями
- Database Service — работа с данными

# ПРАВИЛА ФОРМАТИРОВАНИЯ
## ✅ РАЗРЕШЕНО
- `*жирный текст*` для ключевых терминов и заголовков
- `_курсив_` для вторичного акцента и определений
- `` `код` `` для технических терминов, команд и кода
- `[текст ссылки](URL)` для ссылок (если есть в документе)
- `- ` для списков

## ❌ ЗАПРЕЩЕНО
- HTML теги: `<b>`, `<i>`, `<code>`, `<a>`, `<strong>`, `<em>`
- Двойные символы: `**текст**`, `__текст__`
- LaTeX математические выражения: `$...$`, `$$...$$`
- Неэкранированные спецсимволы

# ФОРМАТИРОВАНИЕ МАТЕМАТИЧЕСКИХ ВЫРАЖЕНИЙ
## ✅ ПРАВИЛЬНО
- `2 × 3 = 6` (НЕ `$2 × 3 = 6$`)
- `√2` (НЕ `$√2$`)
- `1/2` (НЕ `$\\frac{{1}}{{2}}$`)
- `2^3 = 8` (НЕ `$2^3 = 8$`)
- `a + b = c` (НЕ `a+b=c`)
- `x = y / z` (НЕ `x=y/z`)

## ❌ НЕПРАВИЛЬНО
- `$1 × 1 = 1$` - LaTeX синтаксис
- `$$√2$$` - LaTeX синтаксис
- `a+b` - без пробелов
- `x=y` - без пробелов

# СТРУКТУРИРОВАНИЕ ОТВЕТОВ
## Для технических вопросов:
1. *Краткий ответ* - основная информация
2. _Детали_ - дополнительные сведения
3. - Список ключевых элементов
4. [Ссылки на ресурсы](URL) - если есть в документе

## Для поиска информации:
1. *Найденная информация* - что обнаружено
2. _Контекст_ - где и как это упоминается
3. - Дополнительные детали

## Для объяснения концепций:
1. *Определение* - основное понятие
2. _Принципы работы_ - как это функционирует
3. - Практические применения

# ВАЖНЫЕ ПРАВИЛА
- Отвечай ТОЛЬКО на основе содержимого документа
- Если информации недостаточно, честно скажи об этом
- Не используй предварительные знания
- Структурируй ответ согласно примерам выше
- Применяй правильное форматирование

# ФИНАЛЬНАЯ ПРОВЕРКА
Перед отправкой ответа убедись, что:
- [ ] Ответ основан на содержимом документа
- [ ] Информация структурирована согласно примерам
- [ ] Использован правильный MarkdownV2 синтаксис
- [ ] Математические выражения отформатированы правильно
- [ ] Нет HTML тегов или LaTeX синтаксиса
- [ ] Все спецсимволы правильно экранированы

Ответь на вопрос пользователя, основываясь на содержимом документа. Если в документе нет информации для ответа, честно скажи об этом."""

        # Создаем parts для Gemini API: промпт
        parts = [document_prompt] if document_prompt else []
        response_text, _ = await _get_ai_response_with_routing(
            settings.DEFAULT_MODEL,
            [{"role": "user", "parts": parts}],
            user_id=user_id,
            chat_id=placeholder_message.chat.id if placeholder_message.chat else None,
        )

        if response_text:
            # Проверяем, является ли ответ ошибкой
            from app.errors import build_retry_and_roles_keyboard

            # Используем универсальную функцию обработки ошибок
            if await handle_ai_response_error(response_text, placeholder_message):
                return  # Ошибка обработана, выходим
            else:
                # Успешный ответ - показываем обычные кнопки для документов
                keyboard = [
                    [
                        InlineKeyboardButton(
                            "📄 Загрузить другой документ",
                            callback_data="doc:upload_new",
                        )
                    ],
                    [
                        InlineKeyboardButton(
                            "📋 Выбрать документ", callback_data="doc:select_document"
                        )
                    ],
                    [
                        InlineKeyboardButton(
                            "❌ Отменить работу с документами",
                            callback_data="doc:cancel",
                        )
                    ],
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

                # Отправляем ответ с кнопками
                await send_long_message(
                    placeholder_message,
                    response_text,
                    reply_markup=InlineKeyboardMarkup(keyboard),
                )
                await metrics_collector.record_api_call("document_question", settings.DEFAULT_MODEL)
        else:
            try:
                from app.errors import build_retry_and_roles_keyboard

                await placeholder_message.edit_text(
                    "❌ Не удалось получить ответ от AI.",
                    reply_markup=build_retry_and_roles_keyboard(),
                )
            except Exception as edit_error:
                logging.error(f"Could not edit placeholder message: {edit_error}")
                # Fallback на новое сообщение
                try:
                    from app.errors import build_retry_and_roles_keyboard

                    await placeholder_message.reply_text(
                        "❌ Не удалось получить ответ от AI.",
                        reply_markup=build_retry_and_roles_keyboard(),
                    )
                except Exception:
                    pass

    except Exception as e:
        logging.error(f"Error processing document question: {e}", exc_info=True)
        try:
            await placeholder_message.edit_text(
                f"❌ Произошла ошибка при обработке вопроса по документу: {str(e)}"
            )
        except Exception as edit_error:
            logging.error(f"Could not edit placeholder message: {edit_error}")
            # Fallback на новое сообщение
            await placeholder_message.reply_text(
                f"❌ Произошла ошибка при обработке вопроса по документу: {str(e)}"
            )
