# 🔍 **ДЕТАЛЬНОЕ ЛОГИРОВАНИЕ API ЗАПРОСОВ**

## **Обзор системы**

Реализована комплексная система детального логирования для всех API запросов в боте:

- **🤖 Gemini API** - генерация контента и анализ изображений
- **🔍 Tavily API** - веб-поиск и анализ
- **📱 Telegram Bot API** - обработка сообщений и команд
- **🗄️ Database API** - запросы к PostgreSQL
- **💾 Cache API** - операции с Redis

## **🚀 Возможности системы**

### **1. Детальное логирование запросов**
- Время начала и завершения каждого API вызова
- Длительность выполнения в миллисекундах
- Тип запроса и параметры
- Идентификация пользователя и чата

### **2. Мониторинг производительности**
- Время ответа для каждого API
- Статистика успешных/неуспешных запросов
- Анализ медленных запросов

### **3. Безопасность**
- Автоматическое скрытие API ключей
- Маскирование чувствительных данных
- Безопасное логирование в production

### **4. Трассировка ошибок**
- Полные стектрейсы для ошибок
- Контекст выполнения
- Детализация HTTP ошибок

## **📋 Примеры логов**

### **Gemini API - успешный запрос:**
```
🤖 GEMINI REQUEST STARTED: {
  "timestamp": "2025-01-20T10:30:15.123456",
  "api": "gemini",
  "model": "gemini-1.5-pro",
  "prompt_length": 245,
  "has_images": false,
  "user_id": 123456789,
  "chat_id": 987654321,
  "status": "STARTED"
}

✅ GEMINI RESPONSE COMPLETED: {
  "timestamp": "2025-01-20T10:30:18.456789",
  "api": "gemini",
  "model": "gemini-1.5-pro",
  "duration_ms": 3333.67,
  "response_length": 1247,
  "token_count": 89,
  "success": true,
  "user_id": 123456789,
  "chat_id": 987654321,
  "status": "COMPLETED"
}
```

### **Tavily API - поисковый запрос:**
```
🔍 TAVILY REQUEST STARTED: {
  "timestamp": "2025-01-20T10:30:20.123456",
  "api": "tavily",
  "search_type": "search",
  "query_length": 67,
  "query_preview": "Как работает искусственный интеллект в современном мире...",
  "user_id": 123456789,
  "chat_id": 987654321,
  "status": "STARTED"
}

✅ TAVILY RESPONSE COMPLETED: {
  "timestamp": "2025-01-20T10:30:22.456789",
  "api": "tavily",
  "search_type": "search",
  "duration_ms": 2333.45,
  "results_count": 7,
  "success": true,
  "user_id": 123456789,
  "chat_id": 987654321,
  "status": "COMPLETED"
}
```

### **Telegram API - обработка сообщения:**
```
📱 TELEGRAM REQUEST STARTED: {
  "timestamp": "2025-01-20T10:30:25.123456",
  "api": "telegram",
  "method": "handle_message",
  "chat_id": 987654321,
  "user_id": 123456789,
  "message_type": "text",
  "status": "STARTED"
}

✅ TELEGRAM RESPONSE COMPLETED: {
  "timestamp": "2025-01-20T10:30:28.456789",
  "api": "telegram",
  "method": "handle_message",
  "duration_ms": 3333.34,
  "success": true,
  "chat_id": 987654321,
  "user_id": 123456789,
  "status": "COMPLETED"
}
```

### **Ошибка API с полным стектрейсом:**
```
💥 API ERROR: {
  "timestamp": "2025-01-20T10:30:30.123456",
  "api": "gemini",
  "error_type": "APIError",
  "error_message": "400 INVALID_ARGUMENT: Invalid request parameters",
  "traceback": "Traceback (most recent call last):\n  File \"app/services.py\", line 89...",
  "context": {"function": "get_gemini_response"},
  "user_id": 123456789,
  "chat_id": 987654321,
  "status": "ERROR"
}
```

## **⚙️ Конфигурация**

### **Переменные окружения:**
```bash
# Уровень логирования
LOG_LEVEL=INFO  # DEBUG, INFO, WARNING, ERROR, CRITICAL

# Логирование в файл
LOG_TO_FILE=true
LOG_FILE_PATH=/tmp/bot_detailed.log
```

### **Настройка в коде:**
```python
from app.utils.logging_config import setup_detailed_logging

# Базовая настройка
setup_detailed_logging()

# Расширенная настройка
setup_detailed_logging(
    log_level="DEBUG",
    log_to_file=True,
    log_file_path="/tmp/custom.log"
)
```

## **🔧 Использование в коде**

### **Автоматическое логирование через декоратор:**
```python
from app.utils.api_logger import log_api_call

@log_api_call("gemini", "generate_content")
async def my_gemini_function():
    # Ваш код здесь
    pass
```

### **Ручное логирование:**
```python
from app.utils.api_logger import api_logger

# Логирование запроса
start_time = api_logger.log_gemini_request(
    model="gemini-1.5-pro",
    prompt_length=100,
    has_images=False,
    user_id=123456789,
    chat_id=987654321
)

# Логирование ответа
api_logger.log_gemini_response(
    start_time=start_time,
    model="gemini-1.5-pro",
    response_length=500,
    token_count=45,
    success=True,
    user_id=123456789,
    chat_id=987654321
)
```

## **📊 Анализ логов**

### **Поиск медленных запросов:**
```bash
# Запросы Gemini дольше 5 секунд
grep "duration_ms" /tmp/bot_detailed.log | grep "gemini" | awk -F'"' '$8 > 5000'

# Запросы Tavily дольше 3 секунд
grep "duration_ms" /tmp/bot_detailed.log | grep "tavily" | awk -F'"' '$8 > 3000'
```

### **Статистика ошибок:**
```bash
# Подсчет ошибок по API
grep "API ERROR" /tmp/bot_detailed.log | awk -F'"' '{print $6}' | sort | uniq -c

# Ошибки конкретного пользователя
grep "user_id.*123456789" /tmp/bot_detailed.log | grep "ERROR"
```

### **Анализ производительности:**
```bash
# Среднее время ответа Gemini
grep "GEMINI RESPONSE COMPLETED" /tmp/bot_detailed.log | grep "success.*true" | awk -F'"' '{sum+=$8; count++} END {print "Average:", sum/count, "ms"}'

# Топ-10 самых медленных запросов
grep "duration_ms" /tmp/bot_detailed.log | sort -t'"' -k8 -nr | head -10
```

## **🚨 Мониторинг и алерты**

### **Автоматические алерты:**
- Запросы дольше 10 секунд
- Ошибки API более 5% от общего числа
- Превышение лимитов токенов
- Проблемы с подключением к базам данных

### **Метрики для Grafana/Prometheus:**
- Время ответа API (p50, p95, p99)
- Количество запросов в секунду
- Процент ошибок
- Использование токенов

## **🔒 Безопасность**

### **Автоматическое скрытие:**
- API ключи: `AIza...abcd` → `AIza...abcd`
- Токены: `1234567890:ABC...XYZ` → `1234...XYZ`
- Пароли: `mypassword123` → `***`

### **Логирование в production:**
- Только INFO и выше
- Без DEBUG информации
- Ротация логов
- Архивирование старых логов

## **📈 Преимущества новой системы**

1. **🔍 Полная видимость** - каждый API вызов отслеживается
2. **⚡ Производительность** - выявление узких мест
3. **🐛 Отладка** - быстрое решение проблем
4. **📊 Аналитика** - статистика использования API
5. **🛡️ Безопасность** - защита чувствительных данных
6. **🚀 Масштабируемость** - готовность к production нагрузкам

## **🎯 Следующие шаги**

1. **Мониторинг в реальном времени** - интеграция с Grafana
2. **Автоматические алерты** - уведомления о проблемах
3. **Анализ трендов** - долгосрочная статистика
4. **Оптимизация** - выявление и устранение узких мест
5. **Документация** - создание runbooks для операторов

---

**Система готова к использованию в production! 🚀**
