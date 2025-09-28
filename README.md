# 🤖 GeminiBot v2 - Интеллектуальный Telegram-бот с ИИ

[![Python](https://img.shields.io/badge/Python-3.11+-blue.svg)](https://python.org)
[![Telegram](https://img.shields.io/badge/Telegram-Bot-26A5E4.svg)](https://telegram.org)
[![Gemini](https://img.shields.io/badge/Google-Gemini-4285F4.svg)](https://ai.google.dev)
[![Docker](https://img.shields.io/badge/Docker-Container-2496ED.svg)](https://docker.com)
[![License](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)

## 📋 Оглавление

- [🎯 Описание проекта](#-описание-проекта)
- [✨ Основные возможности](#-основные-возможности)
- [🏗️ Архитектура системы](#️-архитектура-системы)
- [📦 Установка и настройка](#-установка-и-настройка)
- [🚀 Развертывание](#-развертывание)
- [🔧 Конфигурация](#-конфигурация)
- [📚 API и команды](#-api-и-команды)
- [🛠️ Разработка](#️-разработка)
- [📊 Мониторинг](#-мониторинг)
- [🔒 Безопасность](#-безопасность)
- [📈 Производительность](#-производительность)
- [🐛 Устранение неполадок](#-устранение-неполадок)
- [📄 Лицензия](#-лицензия)

## 🎯 Описание проекта

**GeminiBot v2** — это продвинутый Telegram-бот, использующий возможности Google Gemini AI для предоставления интеллектуальных ответов, анализа документов, веб-поиска и многого другого. Бот построен на современной асинхронной архитектуре с поддержкой множественных AI моделей, системы ролей, управления документами и комплексного мониторинга.

### 🌟 Ключевые особенности

- **🤖 Множественные AI модели**: Поддержка различных моделей Gemini (Gemini-2.5-flash, Gemini-2.5-pro, Gemini-2.5-flash-lite)
- **🔍 Интеллектуальный поиск**: Интеграция с Tavily API для веб-поиска и Q&A
- **📄 Обработка документов**: Поддержка PDF и DOCX файлов с возможностью Q&A по содержимому
- **🎭 Система ролей**: Предустановленные и пользовательские роли для настройки поведения бота
- **💬 Управление беседами**: Сохранение, переключение и управление диалогами
- **📊 Мониторинг**: Комплексная система метрик, логирования и health checks
- **🔒 Безопасность**: Row Level Security (RLS), валидация входных данных, rate limiting

## ✨ Основные возможности

### 🤖 ИИ и машинное обучение
- **Мультимодальность**: Обработка текста, изображений и документов
- **Контекстная память**: Сохранение истории диалогов с умным сжатием
- **Адаптивные ответы**: Настройка стиля общения через роли
- **Глубокий анализ**: Режим "Deep Dive" для комплексного исследования тем

### 🔍 Поиск и исследования
- **Веб-поиск**: Интеграция с Tavily для актуальной информации
- **Q&A режим**: Быстрые ответы на вопросы
- **Исследовательский режим**: Комплексный анализ с множественными источниками
- **Анализ изображений**: Поиск по содержимому фотографий

### 📄 Работа с документами
- **Поддержка форматов**: PDF, DOCX, DOC
- **Извлечение текста**: Автоматическая обработка и индексация
- **Q&A по документам**: Вопросы по содержимому загруженных файлов
- **Управление**: Загрузка, просмотр, удаление документов

### 🎭 Персонализация
- **Предустановленные роли**: Программист, Учитель, Аналитик, Креативщик, Переводчик
- **Пользовательские роли**: Создание собственных ролей с помощью ИИ
- **Настройка поведения**: Кастомизация стиля общения и специализации

### 💬 Управление беседами
- **Сохранение диалогов**: Создание именованных бесед
- **Переключение**: Быстрое переключение между разными темами
- **Управление**: Переименование, удаление, просмотр истории

## 🏗️ Архитектура системы

### 📁 Структура проекта

```
gemaibotv2/
├── 📄 bot.py                          # Главный файл приложения
├── 📄 requirements.txt                 # Python зависимости
├── 📄 Dockerfile                      # Docker конфигурация
├── 📄 docker-compose.yml              # Docker Compose
├── 📄 northflank.yaml                 # Northflank конфигурация
├── 📄 render.yaml                     # Render конфигурация
├── 📁 app/                            # Основной код приложения
│   ├── 📁 config/                     # Конфигурация
│   │   └── 📄 __init__.py
│   ├── 📁 handlers/                   # Обработчики Telegram
│   │   ├── 📄 commands.py            # Команды бота
│   │   ├── 📄 messages.py            # Обработка сообщений
│   │   ├── 📄 callbacks.py           # Callback кнопки
│   │   └── 📄 agent.py               # AI агент
│   ├── 📁 utils/                      # Утилиты
│   │   ├── 📄 formatting.py          # Форматирование текста
│   │   ├── 📄 messaging.py           # Отправка сообщений
│   │   ├── 📄 network.py             # Сетевые утилиты
│   │   └── 📄 api_logger.py           # Логирование API
│   ├── 📄 config.py                   # Настройки приложения
│   ├── 📄 database.py                 # База данных
│   ├── 📄 services.py                 # Внешние сервисы
│   ├── 📄 document_processor.py       # Обработка документов
│   ├── 📄 memory_manager.py           # Управление памятью
│   ├── 📄 queue.py                    # Очередь задач
│   ├── 📄 metrics.py                  # Метрики
│   ├── 📄 cache.py                    # Кэширование
│   ├── 📄 alerts.py                   # Уведомления
│   ├── 📄 health.py                   # Health checks
│   ├── 📄 prompts.py                  # AI промпты
│   ├── 📄 security.py                 # Безопасность
│   └── 📄 state.py                    # Состояние приложения
├── 📁 data/                           # Данные приложения
└── 📄 README.md                       # Документация
```

### 🔄 Архитектурные компоненты

#### 1. **Основное приложение** (`bot.py`)
- Инициализация и запуск бота
- Управление жизненным циклом
- Graceful shutdown
- Health checks и мониторинг

#### 2. **Обработчики** (`app/handlers/`)
- **commands.py**: Регистрация и обработка команд
- **messages.py**: Обработка входящих сообщений
- **callbacks.py**: Обработка callback кнопок
- **agent.py**: AI агент для обработки запросов

#### 3. **Сервисы** (`app/services.py`)
- Интеграция с Gemini API
- Интеграция с Tavily API
- Обработка ошибок и retry логика

#### 4. **База данных** (`app/database.py`)
- PostgreSQL с asyncpg
- Connection pooling
- Row Level Security (RLS)
- Миграции схемы

#### 5. **Утилиты** (`app/utils/`)
- Форматирование текста для Telegram
- Отправка длинных сообщений
- Сетевые утилиты с retry
- Логирование API вызовов

#### 6. **Мониторинг** (`app/metrics.py`, `app/health.py`)
- Сбор метрик использования
- Health checks
- Алерты и уведомления

### 🔧 Технологический стек

#### **Backend**
- **Python 3.11+**: Основной язык программирования
- **asyncio**: Асинхронное программирование
- **python-telegram-bot**: Telegram Bot API
- **asyncpg**: PostgreSQL драйвер
- **redis**: Кэширование и очереди

#### **AI и ML**
- **google-genai**: Google Gemini AI
- **tavily-python**: Веб-поиск API
- **PIL/Pillow**: Обработка изображений

#### **База данных**
- **PostgreSQL**: Основная база данных
- **Redis**: Кэширование и сессии
- **Row Level Security**: Изоляция данных пользователей

#### **Инфраструктура**
- **Docker**: Контейнеризация
- **Docker Compose**: Локальная разработка
- **Flask**: Web сервер для health checks
- **Hypercorn**: ASGI сервер

#### **Мониторинг**
- **psutil**: Системные метрики
- **logging**: Структурированное логирование
- **metrics**: Сбор статистики

## 📦 Установка и настройка

### 🔧 Системные требования

- **Python**: 3.11 или выше
- **PostgreSQL**: 13+ (рекомендуется Supabase)
- **Redis**: 6+ (опционально)
- **Docker**: 20+ (для контейнеризации)

### 🚀 Быстрый старт

#### 1. **Клонирование репозитория**
```bash
git clone https://github.com/Le-Who/gemaibotv2.git
cd gemaibotv2
```

#### 2. **Установка зависимостей**
```bash
pip install -r requirements.txt
```

#### 3. **Настройка переменных окружения**
Создайте файл `.env`:
```env
# Telegram Bot
TELEGRAM_BOT_TOKEN=your_bot_token_here

# Google Gemini AI
GEMINI_API_KEYS=key1,key2,key3

# Tavily Search
TAVILY_API_KEYS=key1,key2,key3

# Database
DATABASE_URL=postgresql://user:password@host:port/database

# Redis (опционально)
REDIS_URL=redis://localhost:6379

# Admin
ADMIN_ID=your_telegram_user_id

# Optional
LOG_JSON=true
ENABLE_PERSISTENT_QUEUE=true
```

#### 4. **Инициализация базы данных**
```bash
python -c "from app.database import init_db; import asyncio; asyncio.run(init_db())"
```

#### 5. **Запуск бота**
```bash
python bot.py
```

### 🐳 Docker развертывание

#### **Локальная разработка**
```bash
# Сборка и запуск
docker-compose up --build

# Только сборка
docker-compose build

# Остановка
docker-compose down
```

#### **Production развертывание**
```bash
# Сборка production образа
docker build -t gemaibotv2:latest .

# Запуск контейнера
docker run -d \
  --name gemaibotv2 \
  --env-file .env \
  -p 10000:10000 \
  -v ./data:/app/data \
  gemaibotv2:latest
```

## 🚀 Развертывание

### 🌐 Cloud платформы

#### **Render.com**
1. Подключите GitHub репозиторий
2. Настройте переменные окружения
3. Используйте `render.yaml` для конфигурации
4. Деплой автоматически запустится

#### **Northflank.com**
1. Создайте новый проект
2. Подключите Git репозиторий
3. Настройте переменные окружения
4. Используйте `northflank.yaml` для конфигурации

#### **Docker Hub**
```bash
# Сборка и пуш образа
docker build -t yourusername/gemaibotv2:latest .
docker push yourusername/gemaibotv2:latest
```

### 🔧 Переменные окружения

#### **Обязательные**
```env
TELEGRAM_BOT_TOKEN=1234567890:ABCdefGHIjklMNOpqrsTUVwxyz
GEMINI_API_KEYS=key1,key2,key3
TAVILY_API_KEYS=key1,key2,key3
DATABASE_URL=postgresql://user:password@host:port/database
ADMIN_ID=123456789
```

#### **Опциональные**
```env
REDIS_URL=redis://localhost:6379
LOG_JSON=true
ENABLE_PERSISTENT_QUEUE=true
PORT=10000
MAX_DOCUMENTS_PER_USER=5
MAX_FILE_SIZE_MB=10
```

### 📊 Health Checks

Бот предоставляет несколько endpoints для мониторинга:

- **`/`**: Основная страница
- **`/health`**: Проверка здоровья системы
- **`/status`**: Детальная статистика
- **`/keys`**: Статус API ключей
- **`/keys/<model>`**: Статус ключей для конкретной модели

## 🔧 Конфигурация

### ⚙️ Настройки приложения

#### **Модели AI**
```python
# Доступные модели Gemini
AVAILABLE_MODELS = [
    "gemini-2.5-flash",
    "gemini-2.5-pro", 
    "gemini-2.5-flash-lite"
]

# Модели по умолчанию
DEFAULT_MODEL = "gemini-2.5-flash"
RESEARCH_MODEL = "gemini-2.5-pro"
QNA_MODEL = "gemini-2.5-flash"
```

#### **Лимиты использования**
```python
# Дневные лимиты для API ключей
DAILY_LIMITS = {
    "gemini-2.5-flash": 250,
    "gemini-2.5-pro": 100,
    "gemini-2.5-flash-lite": 1000
}

# Лимиты для документов
MAX_DOCUMENTS_PER_USER = 5
MAX_FILE_SIZE_MB = 10
```

#### **Настройки безопасности**
```python
# Настройки безопасности Gemini
SAFETY_SETTINGS = [
    types.HarmCategory.HARM_CATEGORY_HATE_SPEECH,
    types.HarmCategory.HARM_CATEGORY_HARASSMENT,
    types.HarmCategory.HARM_CATEGORY_SEXUALLY_EXPLICIT,
    types.HarmCategory.HARM_CATEGORY_DANGEROUS_CONTENT
]
```

### 🎭 Система ролей

#### **Предустановленные роли**
- **Программист**: Помощь в программировании и разработке
- **Учитель**: Образовательная поддержка и объяснения
- **Аналитик**: Анализ данных и бизнес-процессов
- **Креативщик**: Креативные задачи и идеи
- **Переводчик**: Переводы и языковая поддержка

#### **Пользовательские роли**
Пользователи могут создавать собственные роли с помощью команды `/roles` и описания желаемого поведения.

### 💬 Управление беседами

#### **Сохранение бесед**
- Автоматическое сохранение при переключении тем
- Именование бесед для удобства
- Ограничение количества бесед на пользователя

#### **Переключение между беседами**
- Быстрое переключение через inline кнопки
- Сохранение контекста каждой беседы
- Управление историей сообщений

## 📚 API и команды

### 🤖 Команды бота

#### **Основные команды**
- **`/start`**: Запуск бота и приветствие
- **`/help`**: Подробная справка по командам
- **`/newchat`**: Начать новую беседу
- **`/model`**: Выбор AI модели

#### **Управление ролями**
- **`/roles`**: Управление ролями ИИ
- **`/setprompt`**: Настройка системного промпта

#### **Поиск и исследования**
- **`/res`**: Включение/выключение режима поиска
- **`?`**: Быстрый поиск (Q&A режим)
- **`??`**: Глубокое исследование

#### **Документы**
- **`/documents`**: Управление документами
- **Загрузка файлов**: PDF, DOCX, DOC

#### **Управление беседами**
- **`/save`**: Сохранить текущую беседу
- **`/conversations`**: Список сохраненных бесед
- **`/switch`**: Переключение между беседами
- **`/rename`**: Переименование беседы
- **`/delete`**: Удаление беседы

#### **Административные команды**
- **`/admin`**: Админ панель
- **`/metrics`**: Статистика использования
- **`/cachestats`**: Статистика кэша
- **`/queuestats`**: Статистика очереди задач
- **`/docstats`**: Статистика документов
- **`/rolemetrics`**: Статистика ролей

### 🔍 Режимы работы

#### **Обычный чат**
- Стандартное общение с ИИ
- Сохранение контекста беседы
- Поддержка изображений

#### **Режим поиска**
- Автоматический веб-поиск
- Анализ источников
- Синтез информации

#### **Q&A режим**
- Быстрые ответы на вопросы
- Использование актуальной информации
- Оптимизированные промпты

#### **Deep Dive режим**
- Комплексное исследование тем
- Множественные источники
- Детальный анализ

### 📄 Обработка документов

#### **Поддерживаемые форматы**
- **PDF**: Извлечение текста с PyPDF2
- **DOCX**: Обработка с python-docx
- **DOC**: Конвертация в DOCX

#### **Возможности**
- Загрузка до 5 документов на пользователя
- Максимальный размер: 10MB
- Q&A по содержимому документов
- Автоматическая очистка старых файлов

## 🛠️ Разработка

### 🏗️ Структура кода

#### **Обработчики** (`app/handlers/`)
```python
# commands.py - Регистрация команд
@bot.command_handler('/start')
async def start_command(update, context):
    # Обработка команды /start

# messages.py - Обработка сообщений  
async def handle_request(update, context):
    # Обработка входящих сообщений

# callbacks.py - Callback кнопки
async def model_button_callback(update, context):
    # Обработка нажатий кнопок
```

#### **Сервисы** (`app/services.py`)
```python
# Интеграция с Gemini API
async def get_gemini_response(api_key, history, model):
    # Получение ответа от Gemini

# Интеграция с Tavily API  
async def tavily_search_agent(query, search_type):
    # Выполнение поиска
```

#### **База данных** (`app/database.py`)
```python
# Управление соединениями
async def db_query(query, params):
    # Выполнение SQL запросов

# RLS политики
async def setup_row_level_security():
    # Настройка безопасности
```

### 🧪 Тестирование

#### **Запуск тестов**
```bash
# Все тесты
python -m pytest

# Конкретный тест
python -m pytest test_basic_functionality.py

# С покрытием
python -m pytest --cov=app
```

#### **Доступные тесты**
- `test_basic_functionality.py`: Основная функциональность
- `test_database_fixes.py`: Тесты базы данных
- `test_gemini_api_fix.py`: Тесты Gemini API
- `test_redis_fix.py`: Тесты Redis
- `test_roles_conversations.py`: Тесты ролей и бесед

### 🔧 Разработка функций

#### **Добавление новой команды**
```python
# В app/handlers/commands.py
@bot.command_handler('/newcommand')
async def new_command(update, context):
    """Описание команды"""
    # Логика команды
    pass
```

#### **Добавление нового обработчика**
```python
# В app/handlers/messages.py
async def handle_new_message_type(update, context):
    """Обработка нового типа сообщений"""
    # Логика обработки
    pass
```

#### **Добавление новой роли**
```python
# В app/config.py
PREDEFINED_ROLES = {
    "new_role": {
        "name": "Новая роль",
        "description": "Описание роли",
        "system_prompt": "Системный промпт"
    }
}
```

### 📝 Логирование

#### **Структурированные логи**
```python
import logging

# Настройка логирования
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)

# Использование
logger = logging.getLogger(__name__)
logger.info("Информационное сообщение")
logger.error("Ошибка: %s", error_message)
```

#### **API логирование**
```python
# В app/utils/api_logger.py
api_logger.log_gemini_request(
    model="gemini-1.5-flash",
    prompt_length=1000,
    has_images=True,
    user_id=123456
)
```

## 📊 Мониторинг

### 📈 Метрики

#### **Сбор метрик**
- **API вызовы**: Количество и типы запросов
- **Использование моделей**: Статистика по моделям
- **Ошибки**: Типы и частота ошибок
- **Кэш**: Hit rate и производительность
- **Документы**: Количество и размеры
- **Роли**: Использование ролей
- **Беседы**: Активность пользователей

#### **Просмотр метрик**
```bash
# Через команды бота
/metrics          # Общая статистика
/cachestats       # Статистика кэша
/queuestats       # Статистика очереди
/docstats         # Статистика документов
/rolemetrics      # Статистика ролей
```

#### **Health Checks**
```bash
# Проверка здоровья системы
curl http://localhost:10000/health

# Детальная статистика
curl http://localhost:10000/status

# Статус API ключей
curl http://localhost:10000/keys
```

### 🚨 Алерты и уведомления

#### **Автоматические уведомления**
- Превышение лимитов API ключей
- Критические ошибки системы
- Проблемы с базой данных
- Высокое использование памяти

#### **Настройка алертов**
```python
# В app/alerts.py
async def send_alert(message: str, level: str = "warning"):
    """Отправка уведомления администратору"""
    # Логика отправки алертов
```

### 📊 Дашборд мониторинга

#### **Основные показатели**
- **Активные пользователи**: Количество уникальных пользователей
- **API запросы**: Общее количество запросов
- **Использование моделей**: Распределение по моделям
- **Ошибки**: Количество и типы ошибок
- **Производительность**: Время ответа и throughput

#### **Системные метрики**
- **Память**: Использование RAM
- **CPU**: Загрузка процессора
- **Диск**: Использование дискового пространства
- **Сеть**: Трафик и соединения

## 🔒 Безопасность

### 🛡️ Защита данных

#### **Row Level Security (RLS)**
```sql
-- Политики безопасности для пользователей
CREATE POLICY user_isolation ON users
    FOR ALL TO authenticated
    USING (user_id = current_setting('app.current_user_id')::int);

-- Политики для документов
CREATE POLICY document_isolation ON user_documents
    FOR ALL TO authenticated
    USING (user_id = current_setting('app.current_user_id')::int);
```

#### **Валидация входных данных**
```python
# Валидация пользовательского ввода
def validate_user_input(text: str) -> bool:
    """Проверка безопасности пользовательского ввода"""
    # Проверка длины
    if len(text) > MAX_MESSAGE_LENGTH:
        return False
    
    # Проверка на вредоносный контент
    if contains_malicious_content(text):
        return False
    
    return True
```

#### **Rate Limiting**
```python
# Ограничение частоты запросов
async def check_rate_limit(user_id: int) -> bool:
    """Проверка лимитов для пользователя"""
    # Проверка количества запросов в минуту
    # Блокировка при превышении лимитов
```

### 🔐 Управление доступом

#### **Административные права**
```python
# Проверка прав администратора
async def is_admin(user_id: int) -> bool:
    """Проверка, является ли пользователь администратором"""
    return user_id == settings.ADMIN_ID
```

#### **Авторизация пользователей**
```python
# Проверка авторизации
async def is_authorized(user_id: int) -> bool:
    """Проверка авторизации пользователя"""
    # Проверка в базе данных
    # Проверка лимитов
    # Проверка блокировки
```

### 🔒 Безопасность API

#### **Защита API ключей**
- Ротация ключей при превышении лимитов
- Мониторинг использования ключей
- Автоматическая замена неработающих ключей

#### **Валидация запросов**
- Проверка размера файлов
- Валидация типов документов
- Ограничение частоты запросов

## 📈 Производительность

### ⚡ Оптимизация

#### **Асинхронное программирование**
```python
# Параллельная обработка запросов
async def process_multiple_requests(requests):
    """Обработка множественных запросов параллельно"""
    tasks = [process_request(req) for req in requests]
    results = await asyncio.gather(*tasks)
    return results
```

#### **Кэширование**
```python
# Redis кэширование
async def get_cached_result(key: str):
    """Получение результата из кэша"""
    cached = await redis.get(key)
    if cached:
        return json.loads(cached)
    return None

async def cache_result(key: str, result: dict, ttl: int = 3600):
    """Сохранение результата в кэш"""
    await redis.setex(key, ttl, json.dumps(result))
```

#### **Connection Pooling**
```python
# Пул соединений с базой данных
async def create_db_pool():
    """Создание пула соединений"""
    return await asyncpg.create_pool(
        DATABASE_URL,
        min_size=5,
        max_size=20,
        command_timeout=30
    )
```

### 📊 Мониторинг производительности

#### **Метрики производительности**
- **Время ответа**: Среднее время обработки запросов
- **Throughput**: Количество запросов в секунду
- **Использование ресурсов**: CPU, память, диск
- **Ошибки**: Частота и типы ошибок

#### **Профилирование**
```python
# Профилирование производительности
import time
import psutil

async def profile_function(func):
    """Профилирование функции"""
    start_time = time.time()
    start_memory = psutil.Process().memory_info().rss
    
    result = await func()
    
    end_time = time.time()
    end_memory = psutil.Process().memory_info().rss
    
    logging.info(f"Function {func.__name__}: "
                f"Time: {end_time - start_time:.2f}s, "
                f"Memory: {(end_memory - start_memory) / 1024 / 1024:.2f}MB")
    
    return result
```

### 🚀 Масштабирование

#### **Горизонтальное масштабирование**
- Множественные экземпляры бота
- Load balancing
- Shared state через Redis

#### **Вертикальное масштабирование**
- Увеличение ресурсов сервера
- Оптимизация кода
- Кэширование результатов

## 🐛 Устранение неполадок

### 🔍 Диагностика проблем

#### **Проверка логов**
```bash
# Просмотр логов приложения
docker logs gemaibotv2

# Фильтрация по уровню
docker logs gemaibotv2 | grep ERROR

# Мониторинг в реальном времени
docker logs -f gemaibotv2
```

#### **Проверка состояния системы**
```bash
# Health check
curl http://localhost:10000/health

# Детальная статистика
curl http://localhost:10000/status

# Проверка API ключей
curl http://localhost:10000/keys
```

### 🚨 Частые проблемы

#### **Проблемы с API ключами**
```bash
# Проверка статуса ключей
curl http://localhost:10000/keys

# Обновление ключей
/updatetavilykeys
/checktavilykeys
```

#### **Проблемы с базой данных**
```python
# Проверка соединения
async def check_database_health():
    try:
        result = await db_query("SELECT 1")
        return True
    except Exception as e:
        logging.error(f"Database health check failed: {e}")
        return False
```

#### **Проблемы с памятью**
```python
# Очистка памяти
async def cleanup_memory():
    """Принудительная очистка памяти"""
    import gc
    collected = gc.collect()
    logging.info(f"Garbage collection freed {collected} objects")
```

### 🔧 Восстановление

#### **Автоматическое восстановление**
- Переподключение к базе данных
- Ротация API ключей
- Очистка кэша при ошибках

#### **Ручное восстановление**
```bash
# Перезапуск бота
docker restart gemaibotv2

# Очистка кэша
/clearcache

# Очистка старых метрик
/clearoldmetrics
```

### 📞 Поддержка

#### **Получение помощи**
- Проверьте логи приложения
- Используйте команды диагностики
- Обратитесь к документации

#### **Отчеты об ошибках**
При обнаружении ошибок:
1. Сохраните логи
2. Опишите шаги воспроизведения
3. Укажите версию системы
4. Приложите конфигурацию

## 📄 Лицензия

Этот проект распространяется под лицензией MIT. См. файл [LICENSE](LICENSE) для подробностей.

## 🤝 Вклад в проект

Мы приветствуем вклад в развитие проекта! Пожалуйста:

1. Форкните репозиторий
2. Создайте ветку для новой функции
3. Внесите изменения
4. Добавьте тесты
5. Создайте Pull Request

## 📞 Контакты

- **Автор**: Le-Who
- **GitHub**: [Le-Who/gemaibotv2](https://github.com/Le-Who/gemaibotv2)
- **Telegram**: [@your_bot_username](https://t.me/your_bot_username)

---

**GeminiBot v2** - Ваш интеллектуальный помощник в Telegram! 🚀
