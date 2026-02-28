"""
AI Document handler — processes questions about uploaded documents.
"""

import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Message

from app.config import settings
from app.database import ChatState
from app.handlers.ai_core import (
    _get_ai_response_with_routing,
    handle_ai_response_error,
)
from app.metrics import metrics_collector
from app.utils.messaging import send_long_message
from app.utils.stage_indicators import STAGES_DOCUMENT, update_stage


async def _handle_document_question(
    placeholder_message: Message,
    user_id: int,
    user_message: str,
    chat_state: ChatState,
):
    """Обрабатывает вопросы по загруженным документам"""
    try:
        # Get afterдний document user
        from app.document_processor import get_document_content, get_user_documents

        documents = await get_user_documents(user_id)
        if not documents:
            try:
                await placeholder_message.edit_text(
                    "❌ У вас нет загруженных документов. Сначала загрузите документ."
                )
            except Exception as edit_error:
                logging.error("Could not edit placeholder message: %s", edit_error)
                # Fallback на new message
                await placeholder_message.reply_text(
                    "❌ У вас нет загруженных документов. Сначала загрузите документ."
                )
            return

        # Берем самый afterдний document
        latest_document = documents[0]
        document_content = await get_document_content(latest_document["id"], user_id)

        if not document_content:
            try:
                await placeholder_message.edit_text(
                    "❌ Не удалось получить содержимое документа."
                )
            except Exception as edit_error:
                logging.error("Could not edit placeholder message: %s", edit_error)
                # Fallback на new message
                await placeholder_message.reply_text(
                    "❌ Не удалось получить содержимое документа."
                )
            return

        try:
            await update_stage(placeholder_message, STAGES_DOCUMENT, 0)
        except Exception as edit_error:
            logging.error("Could not edit placeholder message: %s", edit_error)
            # If не можем отредактировать, отправляем new message
            placeholder_message = await placeholder_message.reply_text(
                "📄 Анализирую документ..."
            )

        # Ограничиваем размер contextа documentа
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
            logging.error("Failed to convert document content to string: %s", e, exc_info=True)
            try:
                await placeholder_message.edit_text(
                    "❌ Ошибка обработки содержимого документа."
                )
            except Exception as edit_error:
                logging.error("Could not edit placeholder message: %s", edit_error)
            return

        content_length = len(safe_document_content) if safe_document_content else 0
        logging.info(
            f"Processing document question for user {user_id}, document: {latest_document['filename']}, content length: {content_length}"
        )

        # Create промпт for вопроса по documentу
        document_prompt = f"""# РОЛЬ И ЗАДАЧА
Ты — эксперт по аналfromу documentов for Telegram-бота. Твоя задача — отвеchatь на вопросы user по содержимому documentа, используя правильное форматирование и предоставляя точную, полезную информацию.

# КОНТЕКСТ
**Содержимое documentа:**
{safe_document_content}

**Вопрос user:** {user_message}

# ПОШАГОВЫЙ ПРОЦЕСС
1. **Внимательно прочитай содержимое documentа**
2. **Найди информацию, относящуюся к вопросу**
3. **Структурируй response логично**
4. **Примени правильное MarkdownV2 форматирование**

# FEW-SHOT ПРИМЕРЫ
## Пример 1: Технический вопрос
**Вопрос:** "Какие технологии упоминаются в documentе?"
**Правильный response:**
*Технологии, упомянутые в documentе:*
- Docker — for контейнерfromации
- Python — main язык программирования
- PostgreSQL — база данных
- Redis — cache-система

## Пример 2: Поиск конкретной информации
**Вопрос:** "Какая версия Python используется?"
**Правильный response:**
*Версия Python:*
Согласно documentу, используется Python версии `3.9` or выше.

_Дополнительные требования:_
- Поддержка async/await синтаксиса
- Совместимость с afterдними библиотеками

## Пример 3: Объяснение концепции
**Вопрос:** "Объясни архитектуру системы"
**Правильный response:**
*Архитектура системы:*
Документ описывает микросервисную архитектуру с следующими компонентами:

_Основные сервисы:_
- API Gateway — точка входа
- User Service — управление userми
- Database Service — работа с данными

# ПРАВИЛА ФОРМАТИРОВАНИЯ
## ✅ РАЗРЕШЕНО
- `*жирный text*` for keyевых терминов и заголовков
- `_курсив_` for вторичного акцента и определений
- `` `код` `` for технических терминов, команд и кода
- `[text ссылки](URL)` for ссылок (if есть в documentе)
- `- ` for списков

## ❌ ЗАПРЕЩЕНО
- HTML теги: `<b>`, `<i>`, `<code>`, `<a>`, `<strong>`, `<em>`
- Двойные символы: `**text**`, `__text__`
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
- `a+b` - without пробелов
- `x=y` - without пробелов

# СТРУКТУРИРОВАНИЕ ОТВЕТОВ
## Для технических вопросов:
1. *Краткий response* - main информация
2. _Детали_ - дополнительные сведения
3. - Список keyевых элементов
4. [Ссылки на ресурсы](URL) - if есть в documentе

## Для searchа информации:
1. *Найденная информация* - что обнаружено
2. _Конtext_ - где и как это упоминается
3. - Дополнительные детали

## Для объяснения концепций:
1. *Определение* - основное понятие
2. _Принципы работы_ - как это функционирует
3. - Практические onменения

# ВАЖНЫЕ ПРАВИЛА
- Отвечай ТОЛЬКО на основе содержимого documentа
- If информации недостаточно, честно скажи об этом
- Не используй предварительные знания
- Структурируй response согласно onмерам выше
- Применяй правильное форматирование

# ФИНАЛЬНАЯ ПРОВЕРКА
Перед отправкой responseа убедись, что:
- [ ] Ответ основан на содержимом documentа
- [ ] Информация структурирована согласно onмерам
- [ ] Использован правильный MarkdownV2 синтаксис
- [ ] Математические выражения отформатированы правильно
- [ ] Нет HTML тегов or LaTeX синтаксиса
- [ ] Все спецсимволы правильно экранированы

Ответь на вопрос пользователя, основываясь на содержимом документа. Если в документе нет информации для ответа, честно скажи об этом."""

        # Create parts for Gemini API: промпт
        parts = [document_prompt] if document_prompt else []
        response_text, _ = await _get_ai_response_with_routing(
            settings.DEFAULT_MODEL,
            [{"role": "user", "parts": parts}],
            user_id=user_id,
            chat_id=placeholder_message.chat.id if placeholder_message.chat else None,
        )

        if response_text:
            # Check, является ли response ошибкой
            from app.errors import build_retry_and_roles_keyboard

            # Используем универсальную функцию обработки ошибок
            if await handle_ai_response_error(response_text, placeholder_message):
                return  # Error обработана, выходим
            else:
                # Успешный response - показываем обычные buttons for documentов
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

                # Send response с buttonми
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
                logging.error("Could not edit placeholder message: %s", edit_error)
                # Fallback на new message
                try:
                    from app.errors import build_retry_and_roles_keyboard

                    await placeholder_message.reply_text(
                        "❌ Не удалось получить ответ от AI.",
                        reply_markup=build_retry_and_roles_keyboard(),
                    )
                except Exception:
                    pass

    except Exception as e:
        logging.error("Error processing document question: %s", e, exc_info=True)
        try:
            await placeholder_message.edit_text(
                f"❌ Произошла ошибка при обработке вопроса по документу: {str(e)}"
            )
        except Exception as edit_error:
            logging.error("Could not edit placeholder message: %s", edit_error)
            # Fallback на new message
            await placeholder_message.reply_text(
                f"❌ Произошла ошибка при обработке вопроса по документу: {str(e)}"
            )
