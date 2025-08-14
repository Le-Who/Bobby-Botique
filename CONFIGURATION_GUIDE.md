# 🎛️ РУКОВОДСТВО ПО УПРАВЛЕНИЮ КОНФИГУРАЦИЕЙ GEMAI BOT

## 📋 СОДЕРЖАНИЕ

1. [Быстрый старт](#быстрый-старт)
2. [Режимы безопасности](#режимы-безопасности)
3. [Переменные окружения](#переменные-окружения)
4. [Примеры конфигураций](#примеры-конфигураций)
5. [Динамическое управление](#динамическое-управление)
6. [Отладка проблем](#отладка-проблем)

## 🚀 БЫСТРЫЙ СТАРТ

### 1. Создайте файл `.env` в корне проекта:
```bash
# Основные настройки
TELEGRAM_BOT_TOKEN=your_token_here
GEMINI_API_KEYS=key1,key2,key3
TAVILY_API_KEYS=key1,key2
DATABASE_URL=your_database_url
ADMIN_ID=123456789

# Режим безопасности
SAFETY_MODE=relaxed

# Отладка
DEBUG_MODE=true
LOG_LEVEL=DEBUG
```

### 2. Перезапустите бота:
```bash
# Бот автоматически загрузит новую конфигурацию
python bot.py
```

## 🛡️ РЕЖИМЫ БЕЗОПАСНОСТИ

### 🔒 **standard** (рекомендуется для продакшена)
```bash
SAFETY_MODE=standard
```
- Блокирует контент среднего и высокого уровня вреда
- Баланс между безопасностью и функциональностью
- Автоматический fallback при проблемах

### 🟡 **relaxed** (рекомендуется для тестирования)
```bash
SAFETY_MODE=relaxed
```
- Блокирует только контент высокого уровня вреда
- Меньше ложных срабатываний
- Подходит для большинства пользователей

### 🟢 **disabled** (только для отладки)
```bash
SAFETY_MODE=disabled
```
- Не блокирует контент
- **ВНИМАНИЕ:** Только для тестирования!
- Используйте временно для диагностики проблем

### 🔴 **aggressive** (максимальная безопасность)
```bash
SAFETY_MODE=aggressive
```
- Блокирует контент низкого, среднего и высокого уровня
- Максимальная защита
- Может блокировать безобидный контент

### 🔄 **auto** (умное переключение)
```bash
SAFETY_MODE=auto
```
- Автоматически переключается между режимами
- Адаптируется к проблемам в реальном времени
- Рекомендуется для нестабильных API

## 🔧 ПЕРЕМЕННЫЕ ОКРУЖЕНИЯ

### 🛡️ **Безопасность**
| Переменная | Значения | Описание |
|------------|----------|----------|
| `SAFETY_MODE` | `standard`, `relaxed`, `disabled`, `aggressive`, `auto` | Режим безопасности |
| `ENABLE_SAFETY_FALLBACK` | `true`, `false` | Автоматическое переключение настроек |
| `ENABLE_PROMPT_SIMPLIFICATION` | `true`, `false` | Упрощение проблемных промптов |
| `ENABLE_SYSTEM_INSTRUCTION_FALLBACK` | `true`, `false` | Отключение system_instruction при проблемах |

### 🐛 **Отладка и логирование**
| Переменная | Значения | Описание |
|------------|----------|----------|
| `DEBUG_MODE` | `true`, `false` | Режим отладки |
| `LOG_LEVEL` | `DEBUG`, `INFO`, `WARNING`, `ERROR` | Уровень логирования |
| `LOG_SAFETY_DECISIONS` | `true`, `false` | Логировать решения по безопасности |

### ⚡ **Производительность**
| Переменная | Значения | Описание |
|------------|----------|----------|
| `ENABLE_CACHE` | `true`, `false` | Включить кэширование |
| `CACHE_TTL_HOURS` | `1-168` | Время жизни кэша (часы) |
| `MAX_RETRIES` | `1-10` | Максимальное количество попыток |
| `REQUEST_TIMEOUT_SECONDS` | `30-300` | Таймаут запросов (секунды) |

## 📝 ПРИМЕРЫ КОНФИГУРАЦИЙ

### 🚀 **Продакшен (рекомендуется)**
```bash
# Безопасность
SAFETY_MODE=standard
ENABLE_SAFETY_FALLBACK=true

# Отладка
DEBUG_MODE=false
LOG_LEVEL=INFO

# Производительность
ENABLE_CACHE=true
CACHE_TTL_HOURS=72
MAX_RETRIES=3
```

### 🧪 **Тестирование**
```bash
# Безопасность
SAFETY_MODE=relaxed
ENABLE_SAFETY_FALLBACK=true

# Отладка
DEBUG_MODE=true
LOG_LEVEL=DEBUG
LOG_SAFETY_DECISIONS=true

# Производительность
ENABLE_CACHE=true
CACHE_TTL_HOURS=24
```

### 🔧 **Отладка проблем**
```bash
# Безопасность
SAFETY_MODE=disabled
ENABLE_SAFETY_FALLBACK=false
ENABLE_PROMPT_SIMPLIFICATION=false

# Отладка
DEBUG_MODE=true
LOG_LEVEL=DEBUG
LOG_SAFETY_DECISIONS=true

# Производительность
ENABLE_CACHE=false
MAX_RETRIES=1
```

### 🛡️ **Максимальная безопасность**
```bash
# Безопасность
SAFETY_MODE=aggressive
ENABLE_SAFETY_FALLBACK=false

# Отладка
DEBUG_MODE=false
LOG_LEVEL=WARNING

# Производительность
ENABLE_CACHE=true
CACHE_TTL_HOURS=168  # 7 дней
```

## 🔄 ДИНАМИЧЕСКОЕ УПРАВЛЕНИЕ

### 1. **Изменение без перезапуска**
```python
# В коде можно изменить настройки на лету
from app.config import settings

# Временно изменить режим безопасности
settings.SAFETY_MODE = "relaxed"

# Включить отладку
settings.DEBUG_MODE = True
```

### 2. **Программное управление**
```python
from app.config import get_safety_settings

# Получить настройки для конкретного режима
relaxed_settings = get_safety_settings("relaxed")
aggressive_settings = get_safety_settings("aggressive")
```

## 🐛 ОТЛАДКА ПРОБЛЕМ

### 1. **Проблема: Частые safety блокировки**
```bash
# Решение: Расслабить настройки
SAFETY_MODE=relaxed
DEBUG_MODE=true
LOG_LEVEL=DEBUG
```

### 2. **Проблема: Медленная работа**
```bash
# Решение: Оптимизировать производительность
ENABLE_CACHE=true
CACHE_TTL_HOURS=168
MAX_RETRIES=2
REQUEST_TIMEOUT_SECONDS=30
```

### 3. **Проблема: Непонятные ошибки**
```bash
# Решение: Включить детальное логирование
DEBUG_MODE=true
LOG_LEVEL=DEBUG
LOG_SAFETY_DECISIONS=true
```

### 4. **Проблема: API недоступен**
```bash
# Решение: Увеличить таймауты и попытки
MAX_RETRIES=5
REQUEST_TIMEOUT_SECONDS=120
ENABLE_SAFETY_FALLBACK=true
```

## 📊 МОНИТОРИНГ КОНФИГУРАЦИИ

### 1. **Проверка текущих настроек**
```python
from app.config import settings

print(f"Safety Mode: {settings.SAFETY_MODE}")
print(f"Debug Mode: {settings.DEBUG_MODE}")
print(f"Cache Enabled: {settings.ENABLE_CACHE}")
```

### 2. **Логи конфигурации**
При запуске бот автоматически логирует все настройки:
```
2024-01-01 12:00:00 - root - INFO - === CONFIGURATION LOADED ===
2024-01-01 12:00:00 - root - INFO - Safety Mode: relaxed
2024-01-01 12:00:00 - root - INFO - Debug Mode: true
2024-01-01 12:00:00 - root - INFO - Cache Enabled: true
```

## ⚠️ ВАЖНЫЕ ЗАМЕЧАНИЯ

1. **Безопасность**: Режим `disabled` используйте только для отладки
2. **Производительность**: Не устанавливайте `MAX_RETRIES` больше 5
3. **Кэш**: `CACHE_TTL_HOURS` не должен превышать 168 (7 дней)
4. **Таймауты**: `REQUEST_TIMEOUT_SECONDS` не должен быть меньше 30

## 🆘 ПОЛУЧЕНИЕ ПОМОЩИ

### 1. **Проверьте логи**
```bash
# Ищите строки с "CONFIGURATION LOADED"
grep "CONFIGURATION LOADED" logs/bot.log
```

### 2. **Используйте отладочный режим**
```bash
DEBUG_MODE=true
LOG_LEVEL=DEBUG
```

### 3. **Проверьте переменные окружения**
```bash
# Linux/Mac
env | grep -i safety
env | grep -i debug

# Windows
set | findstr /i safety
set | findstr /i debug
```

---

**🎯 Цель**: Сделать управление конфигурацией максимально простым и гибким!

**💡 Совет**: Начните с `SAFETY_MODE=relaxed` и `DEBUG_MODE=true`, затем настройте под свои нужды.
