import os
import pytz
import time
import asyncio
import logging
import json
import hashlib
from typing import List, Dict, Callable, Any, Optional
from pydantic import BaseModel, ValidationError

def _load_and_clean_keys(env_var_name: str, required: bool = True) -> List[str]:
    """
    Manually loads a comma-separated string from env, cleans it, and returns a list.
    This is the most robust way to handle env vars from hosting providers.
    
    Args:
        env_var_name: Name of environment variable
        required: If True, raises error if not set. If False, returns empty list.
    """
    value = os.getenv(env_var_name)
    if not value:
        if required:
            raise ValueError(f"Required environment variable '{env_var_name}' is not set.")
        return []
    
    # Clean the string from quotes and whitespace, then split.
    cleaned_v = value.strip().strip('"').strip("'")
    keys = [key.strip() for key in cleaned_v.split(',') if key.strip()]
    if required and not keys:
        raise ValueError(f"Environment variable '{env_var_name}' is set but contains no valid keys.")
    return keys

def _load_daily_limits() -> Dict[str, int]:
    """
    Загружает DAILY_LIMITS из env переменной в формате JSON или компактном формате.
    
    Формат в env (JSON, рекомендуется):
    DAILY_LIMITS='{"gemini-exp-1206": 250, "gemini-flash-latest": 15}'
    
    Или компактный формат:
    DAILY_LIMITS='gemini-exp-1206:250,gemini-flash-latest:15'
    
    Returns:
        Dict[str, int]: Словарь с лимитами для моделей
    """
    value = os.getenv("DAILY_LIMITS")
    
    # Значения по умолчанию
    default_limits = {
        "gemini-2.5-flash": 15,
        "gemini-2.5-flash-latest": 15,
        "gemini-2.5-flash-lite": 15,
        "gemini-flash-lite-latest": 15,
    }
    
    if not value:
        return default_limits
    
    try:
        cleaned = value.strip().strip('"').strip("'")
        
        # Пробуем JSON формат
        try:
            return json.loads(cleaned)
        except json.JSONDecodeError:
            # Если не JSON, пробуем компактный формат: "model1:limit1,model2:limit2"
            result = {}
            for item in cleaned.split(','):
                if ':' in item:
                    model, limit = item.split(':', 1)
                    result[model.strip()] = int(limit.strip())
            if result:
                return result
            else:
                raise ValueError("No valid limits found")
    except (ValueError, AttributeError, json.JSONDecodeError) as e:
        logging.warning(f"Failed to parse DAILY_LIMITS from env: {e}. Using defaults.")
        return default_limits

def get_model_hash(model_name: str) -> str:
    """
    Генерирует короткий хэш модели (8 символов) для использования в callback_data.
    
    Args:
        model_name: Полное имя модели
        
    Returns:
        str: 8-символьный хэш модели
    """
    return hashlib.md5(model_name.encode()).hexdigest()[:8]

# We use BaseModel, NOT BaseSettings. We are not auto-loading from the environment.
class Settings(BaseModel):
    """
    Defines the shape and types of our settings for validation.
    Data is loaded manually and then passed here to be validated.
    """
    # --- CORE ---
    TELEGRAM_BOT_TOKEN: str
    GEMINI_API_KEYS: List[str]
    TAVILY_API_KEYS: List[str]
    OPENROUTER_API_KEYS: List[str] = []  # Опционально, по умолчанию пустой список
    DATABASE_URL: str
    ADMIN_ID: int
    PORT: int

    # --- CHAT ---
    CHAT_TOKEN_LIMIT: int = 384000
    TELEGRAM_MESSAGE_LIMIT: int = 4096

    # --- MODELS ---
    # Модели загружаются из env переменных, значения по умолчанию используются если не указаны
    AVAILABLE_MODELS: List[str] = ["gemini-2.5-flash", "gemini-2.5-flash-lite", "gemini-flash-latest", "gemini-flash-lite-latest"]
    DEFAULT_MODEL: str = "gemini-flash-latest"
    QNA_MODEL: str = "gemini-2.5-flash-lite"
    RESEARCH_MODEL: str = "gemini-2.5-flash"
    URL_SELECTION_MODEL: str = "gemini-flash-latest"
    
    # --- OPENROUTER MODELS ---
    # Модели загружаются из env переменных, значения по умолчанию используются если не указаны
    OPENROUTER_AVAILABLE_MODELS: List[str] = [
        "tngtech/tng-r1t-chimera:free",
        "upstage/solar-pro-3:free",
        "tngtech/deepseek-r1t-chimera:free",
        "nvidia/nemotron-3-nano-30b-a3b:free",
        "qwen/qwen3-4b:free",
        "qwen/qwen-2.5-vl-7b-instruct:free",
        "alibaba/tongyi-deepresearch-30b-a3b:free",
        "nvidia/nemotron-nano-9b-v2:free",
        "mistralai/mistral-small-3.1-24b-instruct:free",
        "nvidia/nemotron-nano-12b-v2-vl:free",
        "mistralai/devstral-2512:free",
        "deepseek/deepseek-r1-0528:free",
        "meta-llama/llama-3.3-70b-instruct:free",
        "z-ai/glm-4.5-air:free",
        "qwen/qwen3-coder:free",
        "openai/gpt-oss-120b:free",
        "nex-agi/deepseek-v3.1-nex-n1:free",
        "google/gemma-3-27b-it:free",
        "cognitivecomputations/dolphin-mistral-24b-venice-edition:free",
        "allenai/olmo-3.1-32b-think:free",
        "tngtech/deepseek-r1t2-chimera:free",
        "arcee-ai/trinity-mini:free"
    ]
    OPENROUTER_DEFAULT_MODEL: str = "upstage/solar-pro-3:free"
    OPENROUTER_QNA_MODEL: str = "upstage/solar-pro-3:free"
    OPENROUTER_RESEARCH_MODEL: str = "upstage/solar-pro-3:free"
    OPENROUTER_URL_SELECTION_MODEL: str = "upstage/solar-pro-3:free"
    
    # --- API PROVIDER SELECTION ---
    USE_OPENROUTER: bool = False  # По умолчанию используем Gemini, можно переключить на OpenRouter

    # --- LIMITS ---
    TAVILY_MONTHLY_CREDIT_LIMIT: int = 1000
    TAVILY_LIMIT_THRESHOLD_PERCENT: float = 0.97
    TAVILY_QNA_SEARCH_COST: int = 2
    TAVILY_ADVANCED_SEARCH_COST: int = 2
    LIMIT_THRESHOLD_PERCENT: float = 0.95
    # DAILY_LIMITS загружается из env переменной DAILY_LIMITS в формате JSON
    DAILY_LIMITS: Dict[str, int] = {
        "gemini-2.5-flash": 15,
        "gemini-flash-latest": 15,
        "gemini-2.5-flash-lite": 15,
        "gemini-flash-lite-latest": 15,
    }
    ALERT_COOLDOWN_SECONDS: int = 3600
    MAX_DOCUMENTS_PER_USER: int = 5

    # --- SAFETY ---
    SAFETY_SETTINGS: List[Dict[str, str]] = [
        {"category": "HARM_CATEGORY_HARASSMENT", "threshold": "BLOCK_NONE"},
        {"category": "HARM_CATEGORY_HATE_SPEECH", "threshold": "BLOCK_NONE"},
        {"category": "HARM_CATEGORY_SEXUALLY_EXPLICIT", "threshold": "BLOCK_NONE"},
        {"category": "HARM_CATEGORY_DANGEROUS_CONTENT", "threshold": "BLOCK_NONE"},
    ]

    # --- DEFAULT SYSTEM PROMPT ---
    DEFAULT_SYSTEM_PROMPT: str = """# РОЛЬ И ЗАДАЧА
Ты — полезный ИИ-ассистент для Telegram. Твоя задача — отвечать на вопросы пользователя, используя правильное форматирование и предоставляя точную, полезную информацию.

# КОНТЕКСТ
Ты работаешь в Telegram-боте, где форматирование должно соответствовать MarkdownV2 синтаксису. Твои ответы должны быть структурированными, информативными и дружелюбными.

# ПОШАГОВЫЕ ИНСТРУКЦИИ
1. **Проанализируй вопрос пользователя**
2. **Сформулируй четкий, структурированный ответ**
3. **Примени правильное MarkdownV2 форматирование**
4. **Проверь корректность математических выражений**
5. **Убедись в читаемости и логичности ответа**

# FEW-SHOT ПРИМЕРЫ
## Пример 1: Технический вопрос
**Вопрос:** "Что такое Python?"
**Правильный ответ:**
*Python* — это высокоуровневый язык программирования, который известен своей простотой и читаемостью.

_Основные особенности:_
- Простой и понятный синтаксис
- Большая стандартная библиотека
- Кроссплатформенность
- Поддержка множества парадигм программирования

[Подробнее в официальной документации](https://python\.org)

## Пример 2: Математический вопрос
**Вопрос:** "Как решить уравнение x² + 2x + 1 = 0?"
**Правильный ответ:**
*Решение квадратного уравнения:*

Дано: `x² + 2x + 1 = 0`

_Шаги решения:_
1. Определяем коэффициенты: `a = 1`, `b = 2`, `c = 1`
2. Вычисляем дискриминант: `D = b² - 4ac = 4 - 4 = 0`
3. Поскольку `D = 0`, уравнение имеет один корень: `x = -b/(2a) = -2/2 = -1`

**Ответ:** `x = -1`

## Пример 3: Объяснение концепции
**Вопрос:** "Объясни, что такое Docker"
**Правильный ответ:**
*Docker* — это платформа для разработки, доставки и запуска приложений в контейнерах.

_Ключевые преимущества:_
- Изоляция приложений
- Консистентность окружения
- Простота развертывания
- Масштабируемость

_Основные компоненты:_
- Docker Engine — ядро системы
- Docker Hub — репозиторий образов
- Docker Compose — оркестрация контейнеров

# ПРАВИЛА ФОРМАТИРОВАНИЯ
## ✅ РАЗРЕШЕНО
- `*жирный текст*` для ключевых терминов и заголовков
- `_курсив_` для вторичного акцента и определений
- `` `код` `` для технических терминов, команд и кода
- `[текст ссылки](URL)` для ссылок
- `- ` для списков
- Обычный текст для математики: `2 × 3 = 6`, `√2`, `1/2`

## ❌ ЗАПРЕЩЕНО
- HTML теги: `<b>`, `<i>`, `<code>`, `<a>`, `<strong>`, `<em>`
- Двойные символы: `**текст**`, `__текст__`
- LaTeX математические выражения: `$...$`, `$$...$$`
- Неэкранированные спецсимволы

# ФОРМАТИРОВАНИЕ МАТЕМАТИКИ
## ✅ ПРАВИЛЬНО
- `2 × 3 = 6` (НЕ `$2 × 3 = 6$`)
- `√2` (НЕ `$√2$`)
- `1/2` (НЕ `$\frac{1}{2}$`)
- `2^3 = 8` (НЕ `$2^3 = 8$`)
- `a \+ b = c` (НЕ `a+b=c`)
- `x = y / z` (НЕ `x=y/z`)
- `π ≈ 3\.14159` (НЕ `$\pi \approx 3\.14159$`)
- `x² + 2x + 1 = 0` (НЕ `$x^2 + 2x + 1 = 0$`)

## ❌ НЕПРАВИЛЬНО
- `$1 × 1 = 1$` - LaTeX синтаксис
- `$$√2$$` - LaTeX синтаксис
- `a+b` - без пробелов вокруг операторов
- `x=y` - без пробелов вокруг знака равенства

# СТРУКТУРИРОВАНИЕ ОТВЕТОВ
## Для технических вопросов:
1. *Краткое определение* - основное объяснение
2. _Ключевые особенности_ - список важных характеристик
3. - Конкретные примеры использования
4. [Ссылки на ресурсы](URL) - для дополнительной информации

## Для математических задач:
1. *Постановка задачи* - четкая формулировка
2. _Шаги решения_ - пошаговый алгоритм
3. **Финальный ответ** - выделенный результат

## Для объяснения концепций:
1. *Определение* - основное понятие
2. _Принципы работы_ - как это функционирует
3. - Практические применения
4. [Дополнительные источники](URL)

# СТИЛЬ ОБЩЕНИЯ
## ✅ ОБЯЗАТЕЛЬНО
- Будь полезным и точным
- Структурируй информацию логично
- Используй примеры для сложных концепций
- Объясняй технические термины простым языком
- Будь дружелюбным и терпеливым
- Следуй структуре примеров выше

## ❌ ЗАПРЕЩЕНО
- Использовать сложный технический жаргон без объяснений
- Давать неопределенные или расплывчатые ответы
- Игнорировать контекст вопроса пользователя
- Использовать неправильное форматирование
- Отклоняться от структуры примеров

# ЭКРАНИРОВАНИЕ СПЕЦСИМВОЛОВ
Если нужно использовать символы `.`, `!`, `-`, `[`, `]`, `(`, `)`, `*`, `_`, `` ` ``, `~`, `>`, `#`, `+`, `=`, `|`, `{`, `}`, экранируй их обратным слешем: `\.`, `\!`, `\-`, `\[`, `\]`, `\(`, `\)`, `\*`, `\_`, `` \` ``, `\~`, `\>`, `\#`, `\+`, `\=`, `\|`, `\{`, `\}`

# ФИНАЛЬНАЯ ПРОВЕРКА
Перед отправкой ответа убедись, что:
- [ ] Ответ полностью отвечает на вопрос пользователя
- [ ] Информация структурирована согласно примерам выше
- [ ] Использован правильный MarkdownV2 синтаксис
- [ ] Математические выражения отформатированы правильно
- [ ] Нет HTML тегов или LaTeX синтаксиса
- [ ] Тон общения дружелюбный и профессиональный
- [ ] Структура ответа соответствует типу вопроса
- [ ] Все спецсимволы правильно экранированы

# КЛЮЧЕВЫЕ ПРИНЦИПЫ
1. **Следуй примерам** - используй структуру из few-shot примеров
2. **Будь конкретным** - избегай общих фраз
3. **Структурируй логично** - используй заголовки, списки и акценты
4. **Проверяй форматирование** - убедись в корректности MarkdownV2
5. **Адаптируй стиль** - подбирай структуру под тип вопроса"""

    # --- COMPACT SYSTEM PROMPT (оптимизированная версия) ---
    # Компактная версия базового промпта для экономии токенов
    # Используется при наличии роли или для простых запросов
    COMPACT_SYSTEM_PROMPT: str = """# РОЛЬ
ИИ-ассистент для Telegram. Отвечай точно, структурированно, используя MarkdownV2.

# ФОРМАТИРОВАНИЕ
✅ Разрешено: `*жирный*`, `_курсив_`, `` `код` ``, `[ссылка](URL)`, `- списки`
❌ Запрещено: HTML теги, `**текст**`, LaTeX `$...$`, неэкранированные спецсимволы

# МАТЕМАТИКА
Формат: `2 × 3 = 6`, `√2`, `1/2`, `x² + 2x + 1 = 0` (НЕ `$...$`)

# СТРУКТУРА ОТВЕТА
1. *Краткое определение* - основное
2. _Детали_ - дополнительные сведения  
3. - Список ключевых элементов
4. [Ссылки](URL) - если есть

# ЭКРАНИРОВАНИЕ
Символы `.`, `!`, `-`, `[`, `]`, `(`, `)`, `*`, `_`, `` ` ``, `~`, `>`, `#`, `+`, `=`, `|`, `{`, `}` → экранировать: `\.`, `\!`, `\-`, и т.д.

# СТИЛЬ
Будь полезным, точным, структурируй логично, используй примеры, объясняй простым языком, будь дружелюбным.

# ПРОВЕРКА
- [ ] Ответ на вопрос
- [ ] MarkdownV2 синтаксис
- [ ] Математика без LaTeX
- [ ] Спецсимволы экранированы"""

def load_settings() -> Settings:
    """
    Manually loads all settings from the environment and validates them
    using the Pydantic model. This is the most robust method.
    """
    try:
        # Значения по умолчанию для моделей
        default_gemini_models = ["gemini-2.5-flash", "gemini-2.5-flash-lite", "gemini-flash-latest", "gemini-flash-lite-latest"]
        default_openrouter_models = [
            "tngtech/tng-r1t-chimera:free",
            "upstage/solar-pro-3:free",
            "tngtech/deepseek-r1t-chimera:free",
            "nvidia/nemotron-3-nano-30b-a3b:free",
            "qwen/qwen3-4b:free",
            "qwen/qwen-2.5-vl-7b-instruct:free",
            "alibaba/tongyi-deepresearch-30b-a3b:free",
            "nvidia/nemotron-nano-9b-v2:free",
            "mistralai/mistral-small-3.1-24b-instruct:free",
            "nvidia/nemotron-nano-12b-v2-vl:free",
            "mistralai/devstral-2512:free",
            "deepseek/deepseek-r1-0528:free",
            "meta-llama/llama-3.3-70b-instruct:free",
            "z-ai/glm-4.5-air:free",
            "qwen/qwen3-coder:free",
            "openai/gpt-oss-120b:free",
            "nex-agi/deepseek-v3.1-nex-n1:free",
            "google/gemma-3-27b-it:free",
            "cognitivecomputations/dolphin-mistral-24b-venice-edition:free",
            "allenai/olmo-3.1-32b-think:free",
            "tngtech/deepseek-r1t2-chimera:free",
            "arcee-ai/trinity-mini:free"
        ]
        
        # Manually load all values from the environment.
        raw_settings = {
            "TELEGRAM_BOT_TOKEN": os.getenv("TELEGRAM_BOT_TOKEN"),
            "DATABASE_URL": os.getenv("DATABASE_URL"),
            "ADMIN_ID": os.getenv("ADMIN_ID"),
            "PORT": os.getenv("PORT", "10000"), # Provide a default for PORT
            "GEMINI_API_KEYS": _load_and_clean_keys("GEMINI_API_KEYS"),
            "TAVILY_API_KEYS": _load_and_clean_keys("TAVILY_API_KEYS"),
            "OPENROUTER_API_KEYS": _load_and_clean_keys("OPENROUTER_API_KEYS", required=False),
            # Загружаем модели из env или используем значения по умолчанию
            "AVAILABLE_MODELS": _load_and_clean_keys("GEMINI_AVAILABLE_MODELS", required=False) or default_gemini_models,
            "OPENROUTER_AVAILABLE_MODELS": _load_and_clean_keys("OPENROUTER_AVAILABLE_MODELS", required=False) or default_openrouter_models,
            "DEFAULT_MODEL": os.getenv("DEFAULT_MODEL", "gemini-flash-latest"),
            "QNA_MODEL": os.getenv("QNA_MODEL", "gemini-2.5-flash-lite"),
            "RESEARCH_MODEL": os.getenv("RESEARCH_MODEL", "gemini-2.5-pro"),
            "URL_SELECTION_MODEL": os.getenv("URL_SELECTION_MODEL", "gemini-flash-latest"),
            "OPENROUTER_DEFAULT_MODEL": os.getenv("OPENROUTER_DEFAULT_MODEL", "upstage/solar-pro-3:free"),
            "OPENROUTER_QNA_MODEL": os.getenv("OPENROUTER_QNA_MODEL", "upstage/solar-pro-3:free"),
            "OPENROUTER_RESEARCH_MODEL": os.getenv("OPENROUTER_RESEARCH_MODEL", "upstage/solar-pro-3:free"),
            "OPENROUTER_URL_SELECTION_MODEL": os.getenv("OPENROUTER_URL_SELECTION_MODEL", "upstage/solar-pro-3:free"),
            "DAILY_LIMITS": _load_daily_limits(),
        }
        
        # Валидация: проверяем, что DEFAULT_MODEL и другие константы есть в списках моделей
        settings_obj = Settings(**raw_settings)
        
        # Проверяем Gemini модели
        if settings_obj.DEFAULT_MODEL not in settings_obj.AVAILABLE_MODELS:
            logging.warning(f"DEFAULT_MODEL '{settings_obj.DEFAULT_MODEL}' not in AVAILABLE_MODELS. Adding it.")
            settings_obj.AVAILABLE_MODELS.append(settings_obj.DEFAULT_MODEL)
        
        if settings_obj.QNA_MODEL not in settings_obj.AVAILABLE_MODELS:
            logging.warning(f"QNA_MODEL '{settings_obj.QNA_MODEL}' not in AVAILABLE_MODELS. Adding it.")
            settings_obj.AVAILABLE_MODELS.append(settings_obj.QNA_MODEL)
        
        if settings_obj.RESEARCH_MODEL not in settings_obj.AVAILABLE_MODELS:
            logging.warning(f"RESEARCH_MODEL '{settings_obj.RESEARCH_MODEL}' not in AVAILABLE_MODELS. Adding it.")
            settings_obj.AVAILABLE_MODELS.append(settings_obj.RESEARCH_MODEL)
        
        # Проверяем OpenRouter модели
        if settings_obj.OPENROUTER_DEFAULT_MODEL not in settings_obj.OPENROUTER_AVAILABLE_MODELS:
            logging.warning(f"OPENROUTER_DEFAULT_MODEL '{settings_obj.OPENROUTER_DEFAULT_MODEL}' not in OPENROUTER_AVAILABLE_MODELS. Adding it.")
            settings_obj.OPENROUTER_AVAILABLE_MODELS.append(settings_obj.OPENROUTER_DEFAULT_MODEL)
        
        return settings_obj
    except (ValidationError, ValueError) as e:
        # Catch errors from both Pydantic and our manual functions.
        print(f"FATAL: Could not load settings. Please check your environment variables. Error: {e}")
        exit(1)

# --- TIMEZONES ---
# Кэшируем временные зоны для предотвращения запросов к pg_timezone_names
PACIFIC_TZ = pytz.timezone('US/Pacific')
KYIV_TZ = pytz.timezone('Europe/Kyiv')
UTC_TZ = pytz.UTC  # Используем константу вместо pytz.utc

# --- LAZY LOADING SETTINGS ---
_settings_instance: Optional[Settings] = None

def get_settings() -> Settings:
    """
    Returns settings instance with lazy loading.
    This prevents initialization errors during import.
    """
    global _settings_instance
    if _settings_instance is None:
        try:
            _settings_instance = load_settings()
        except Exception as e:
            logging.error(f"Failed to load settings: {e}")
            raise
    return _settings_instance

# Backward compatibility - use lazy loading
def get_settings_safe() -> Optional[Settings]:
    """
    Safe version that returns None if settings cannot be loaded.
    Useful for testing and development.
    """
    try:
        return get_settings()
    except Exception:
        return None

# --- SINGLETON INSTANCE ---
# Create the one and only settings object for the app.
# Use lazy loading to prevent import errors
try:
    settings = get_settings()
except Exception:
    # During development/testing, allow None settings
    settings = None

class ConfigManager:
    """Manages configuration with hot reloading capability."""
    
    def __init__(self):
        self._settings = get_settings_safe()
        self._last_reload = time.time()
        self._reload_interval = 300  # 5 minutes
        self._watchers: List[Callable] = []
        self._lock = asyncio.Lock()
    
    @property
    def settings(self) -> Settings:
        """Returns current settings, reloading if necessary."""
        if self._settings is None:
            self._settings = get_settings()
        current_time = time.time()
        if current_time - self._last_reload > self._reload_interval:
            asyncio.create_task(self._reload_config())
        return self._settings
    
    async def _reload_config(self) -> None:
        """Reloads configuration from environment and migrates users with invalid models."""
        async with self._lock:
            try:
                new_settings = get_settings()
                
                # Check if any critical settings changed
                critical_changed = (
                    new_settings.TELEGRAM_BOT_TOKEN != self._settings.TELEGRAM_BOT_TOKEN or
                    new_settings.DATABASE_URL != self._settings.DATABASE_URL or
                    new_settings.ADMIN_ID != self._settings.ADMIN_ID
                )
                
                if critical_changed:
                    logging.warning("Critical configuration changed, restart may be required")
                
                # === ВАЛИДАЦИЯ И МИГРАЦИЯ АКТИВНЫХ ПОЛЬЗОВАТЕЛЕЙ ===
                migrated_count = 0
                try:
                    # Импортируем database только здесь, чтобы избежать циклических импортов
                    from app import database as db
                    
                    # Получаем все доступные модели (Gemini + OpenRouter)
                    all_available_models = set()
                    if new_settings.AVAILABLE_MODELS:
                        all_available_models.update(new_settings.AVAILABLE_MODELS)
                    if new_settings.OPENROUTER_AVAILABLE_MODELS:
                        all_available_models.update(new_settings.OPENROUTER_AVAILABLE_MODELS)
                    
                    if all_available_models and db.db_pool and not db.db_pool._closed:
                        # Находим пользователей с несуществующими моделями
                        # Используем параметризованный запрос для безопасности
                        placeholders = ','.join([f'${i+1}' for i in range(len(all_available_models))])
                        invalid_chats = await db.db_query(
                            f"""
                            SELECT user_id, model 
                            FROM chats 
                            WHERE model IS NOT NULL 
                            AND model NOT IN ({placeholders})
                            """,
                            tuple(all_available_models)
                        )
                        
                        # Мигрируем пользователей на DEFAULT_MODEL
                        for chat in invalid_chats:
                            user_id = chat['user_id']
                            old_model = chat['model']
                            
                            # Определяем правильный DEFAULT_MODEL
                            if "/" in old_model:  # OpenRouter модель
                                default_model = new_settings.OPENROUTER_DEFAULT_MODEL
                            else:  # Gemini модель
                                default_model = new_settings.DEFAULT_MODEL
                            
                            # Обновляем модель пользователя
                            await db.db_query("""
                                UPDATE chats 
                                SET model = $1 
                                WHERE user_id = $2
                            """, (default_model, user_id))
                            
                            migrated_count += 1
                            logging.info(f"Migrated user {user_id} from {old_model} to {default_model}")
                        
                        if migrated_count > 0:
                            logging.warning(f"Migrated {migrated_count} users to default models after config reload")
                except Exception as migration_error:
                    # Не прерываем перезагрузку при ошибке миграции
                    logging.error(f"Error during user migration: {migration_error}")
                
                # Проверяем, что DEFAULT_MODEL существует
                all_available_models_check = set()
                if new_settings.AVAILABLE_MODELS:
                    all_available_models_check.update(new_settings.AVAILABLE_MODELS)
                if new_settings.OPENROUTER_AVAILABLE_MODELS:
                    all_available_models_check.update(new_settings.OPENROUTER_AVAILABLE_MODELS)
                
                if new_settings.DEFAULT_MODEL not in all_available_models_check:
                    logging.error(f"DEFAULT_MODEL '{new_settings.DEFAULT_MODEL}' not in AVAILABLE_MODELS!")
                    raise ValueError(f"DEFAULT_MODEL must be in AVAILABLE_MODELS")
                
                # Update settings
                old_settings = self._settings
                self._settings = new_settings
                self._last_reload = time.time()
                
                # Notify watchers
                await self._notify_watchers(old_settings, new_settings)
                
                logging.info(f"Configuration reloaded successfully. Migrated {migrated_count} users.")
                
            except Exception as e:
                logging.error("Failed to reload configuration: %s", e)
                # Не прерываем работу при ошибке перезагрузки конфигурации
                # Система продолжит работать со старыми настройками
    
    def add_watcher(self, callback: Callable[[Settings, Settings], None]) -> None:
        """Adds a configuration change watcher."""
        self._watchers.append(callback)
    
    async def _notify_watchers(self, old_settings: Settings, new_settings: Settings) -> None:
        """Notifies all watchers of configuration changes."""
        for watcher in self._watchers:
            try:
                if asyncio.iscoroutinefunction(watcher):
                    await watcher(old_settings, new_settings)
                else:
                    watcher(old_settings, new_settings)
            except Exception as e:
                logging.error("Configuration watcher error: %s", e)
    
    async def force_reload(self) -> None:
        """Forces immediate configuration reload."""
        await self._reload_config()
    
    def get_setting(self, key: str, default: Any = None) -> Any:
        """Gets a specific setting value."""
        return getattr(self.settings, key, default)
    
    def update_setting(self, key: str, value: Any) -> None:
        """Updates a setting value (for testing/debugging)."""
        if hasattr(self.settings, key):
            setattr(self.settings, key, value)
            logging.info("Setting updated: %s = %s", key, value)
        else:
            logging.warning("Unknown setting: %s", key)


# Global config manager instance
config_manager = ConfigManager()


# Backward compatibility
def get_settings_compat() -> Settings:
    """Returns current settings (backward compatibility)."""
    return get_settings()


# Convenience functions for common settings
def get_bot_token() -> str:
    """Returns bot token."""
    return config_manager.get_setting('TELEGRAM_BOT_TOKEN')


def get_database_url() -> str:
    """Returns database URL."""
    return config_manager.get_setting('DATABASE_URL')


def get_admin_id() -> int:
    """Returns admin ID."""
    return config_manager.get_setting('ADMIN_ID')


def get_gemini_keys() -> List[str]:
    """Returns Gemini API keys."""
    return config_manager.get_setting('GEMINI_API_KEYS', [])


def get_tavily_keys() -> List[str]:
    """Returns Tavily API keys."""
    return config_manager.get_setting('TAVILY_API_KEYS', [])

def get_openrouter_keys() -> List[str]:
    """Returns OpenRouter API keys."""
    return config_manager.get_setting('OPENROUTER_API_KEYS', [])

def get_use_openrouter() -> bool:
    """Returns whether to use OpenRouter instead of Gemini."""
    return config_manager.get_setting('USE_OPENROUTER', False)
