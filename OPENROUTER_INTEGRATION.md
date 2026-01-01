# Интеграция OpenRouter API

Этот документ описывает, как интегрирован OpenRouter API в бота и как его использовать.

## Обзор

Бот теперь поддерживает работу как с Google Gemini API, так и с OpenRouter API. OpenRouter предоставляет единый интерфейс для доступа к различным языковым моделям, включая GPT-4, Claude, Gemini и другие.

## Настройка

### 1. Получение API ключа OpenRouter

1. Зарегистрируйтесь на [OpenRouter.ai](https://openrouter.ai/)
2. Перейдите в раздел "API Keys" и создайте новый ключ
3. Сохраните ключ в безопасном месте

### 2. Настройка переменных окружения

Добавьте следующие переменные в ваш `.env` файл или настройки хостинга:

```bash
# OpenRouter API ключи (через запятую для нескольких ключей)
OPENROUTER_API_KEYS=sk-or-v1-xxxxx,sk-or-v1-yyyyy

# Включить использование OpenRouter вместо Gemini (по умолчанию False)
USE_OPENROUTER=false
```

**Примечание:** Если `OPENROUTER_API_KEYS` не установлен, бот будет использовать только Gemini API.

### 3. Выбор моделей

В файле `app/config.py` настроены модели OpenRouter по умолчанию:

```python
OPENROUTER_DEFAULT_MODEL: str = "openai/gpt-4o-mini"
OPENROUTER_QNA_MODEL: str = "openai/gpt-4o-mini"
OPENROUTER_RESEARCH_MODEL: str = "openai/gpt-4o"
OPENROUTER_URL_SELECTION_MODEL: str = "openai/gpt-4o-mini"
```

Вы можете изменить эти модели в соответствии с вашими потребностями. Полный список доступных моделей: https://openrouter.ai/docs/models

## Как это работает

### Автоматическое переключение

Бот автоматически выбирает между Gemini и OpenRouter на основе:
1. Настройки `USE_OPENROUTER` (по умолчанию `False`)
2. Наличия ключей OpenRouter в конфигурации

### Маппинг моделей

Когда используется OpenRouter, модели Gemini автоматически маппятся на соответствующие модели OpenRouter:

- `gemini-2.5-flash-exp` → `openai/gpt-4o-mini` (по умолчанию)
- `gemini-2.5-pro` → `openai/gpt-4o`
- `gemini-2.5-flash-lite` → `openai/gpt-4o-mini`

### Универсальные функции

В `app/handlers/agent.py` добавлены универсальные функции:

- `_resolve_ai_request()` - получает ключ и модель (Gemini или OpenRouter)
- `_get_ai_response()` - получает ответ от AI (Gemini или OpenRouter)
- `_increment_key_usage()` - инкрементирует использование ключа

Эти функции автоматически выбирают правильный провайдер на основе настроек.

## Структура базы данных

Добавлены новые таблицы для OpenRouter:

```sql
CREATE TABLE IF NOT EXISTS openrouter_api_keys (
    key_hash TEXT PRIMARY KEY,
    api_key TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS openrouter_key_usage (
    key_hash TEXT,
    model_name TEXT,
    usage_date DATE,
    request_count INTEGER DEFAULT 0,
    PRIMARY KEY (key_hash, model_name, usage_date)
);
```

## API функции

### `get_openrouter_response()`

Функция в `app/services.py` для получения ответов от OpenRouter API:

```python
response_text, token_count = await services.get_openrouter_response(
    api_key="sk-or-v1-xxxxx",
    history=[{'role': 'user', 'parts': ['Привет!']}],
    model_name="openai/gpt-4o",
    system_instruction="Ты полезный ассистент",
    user_id=123,
    chat_id=456
)
```

### Формат истории

OpenRouter использует OpenAI-совместимый формат. История автоматически преобразуется из формата Gemini:

**Gemini формат:**
```python
[{'role': 'user', 'parts': ['Текст сообщения']}]
```

**OpenRouter формат:**
```python
[{'role': 'user', 'content': 'Текст сообщения'}]
```

### Поддержка изображений

OpenRouter поддерживает изображения через base64 кодирование. Изображения автоматически конвертируются при использовании OpenRouter.

## Управление ключами

### Получение доступного ключа

```python
key_data = await db.get_available_openrouter_key("openai/gpt-4o")
# Возвращает: {'key_hash': '...', 'api_key': 'sk-or-v1-xxxxx'}
```

### Инкремент использования

```python
await db.increment_openrouter_key_usage(key_hash, "openai/gpt-4o")
```

## Логирование и метрики

Все запросы к OpenRouter логируются через `api_logger` и учитываются в метриках через `metrics_collector`:

- Тип провайдера: `"openrouter"`
- Модель: полное имя модели (например, `"openai/gpt-4o"`)
- Токены: из ответа API (если доступно)

## Обработка ошибок

OpenRouter API обрабатывает следующие ошибки:

- **429** - Превышен лимит запросов
- **401** - Неверный API ключ
- **402** - Недостаточно средств на счету
- **503** - Сервер перегружен (с автоматическим retry)

## Выбор модели через команду /model

Пользователи могут выбирать модель на ходу через команду `/model`:

1. **Выполните команду `/model`** в боте
2. **Выберите провайдер** (Google Gemini или OpenRouter)
3. **Выберите конкретную модель** из списка

### Как это работает

- **Модели Gemini** имеют имена без "/" (например, `gemini-2.5-flash-exp`)
- **Модели OpenRouter** имеют имена с "/" (например, `openai/gpt-4o`)

Бот автоматически определяет провайдер на основе имени модели:
- Если модель содержит "/" → используется OpenRouter
- Иначе → используется Gemini

### Примеры использования

#### Выбор модели Gemini
```
/model → Выбрать "gemini-2.5-pro"
```

#### Выбор модели OpenRouter
```
/model → Выбрать "gpt-4o" (отображается как "gpt-4o", полное имя "openai/gpt-4o")
```

### Включение OpenRouter глобально

```bash
# В .env файле
USE_OPENROUTER=true
OPENROUTER_API_KEYS=sk-or-v1-xxxxx
```

**Примечание:** Даже если `USE_OPENROUTER=false`, пользователи могут выбрать модели OpenRouter через `/model`, если ключи OpenRouter настроены.

### Использование в коде

```python
from app.config import get_use_openrouter, get_openrouter_keys

use_openrouter = get_use_openrouter() and get_openrouter_keys()

if use_openrouter:
    # Используем OpenRouter
    response = await services.get_openrouter_response(...)
else:
    # Используем Gemini
    response = await services.get_gemini_response(...)
```

## Преимущества OpenRouter

1. **Доступ к множеству моделей** - GPT-4, Claude, Gemini и другие через единый API
2. **Гибкость** - легко переключаться между моделями
3. **Единый интерфейс** - OpenAI-совместимый формат
4. **Мониторинг** - встроенная аналитика использования

## Ограничения

1. **Изображения** - OpenRouter поддерживает изображения, но формат может отличаться от Gemini
2. **Лимиты** - лимиты OpenRouter зависят от выбранной модели и тарифа
3. **Стоимость** - каждая модель имеет свою стоимость использования

## Дополнительные ресурсы

- [Документация OpenRouter](https://openrouter.ai/docs)
- [Список моделей](https://openrouter.ai/docs/models)
- [Цены](https://openrouter.ai/docs/pricing)

