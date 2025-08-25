# 🚀 Миграция с Render.com на Northflank.com

## 📋 Обзор изменений

### ✅ Что изменилось
- **Конфигурация**: `render.yaml` → `northflank.yaml`
- **Dockerfile**: Оптимизирован для Northflank.com
- **Docker Compose**: Обновлен для production deployment
- **Health Checks**: Упрощены (убраны keep-alive механизмы)

### ❌ Что убрано
- **Keep-alive механизмы**: Не нужны для Northflank.com
- **Render-специфичные настройки**: Автоматический деплой, регион
- **Избыточные health check endpoints**: Оставлен только `/health`

## 🔧 Шаги миграции

### 1. Подготовка Northflank.com
```bash
# Создать аккаунт на northflank.com
# Создать новый проект
# Настроить Git integration
```

### 2. Обновление переменных окружения
```bash
# В Northflank Dashboard добавить следующие переменные:
    PORT=10000
    LOG_JSON=true
    ENABLE_PERSISTENT_QUEUE=true
    REDIS_URL=<your-upstash-redis-url>
    TAVILY_API_KEYS=<your-tavily-keys>
    TELEGRAM_BOT_TOKEN=<your-bot-token>
    GEMINI_API_KEYS=<your-gemini-keys>
    DATABASE_URL=<your-supabase-url>
    ADMIN_ID=<your-admin-id>
```

### 3. Деплой
```bash
# Northflank автоматически соберет и развернет приложение
# Используя Dockerfile.northflank
```

## 🏗️ Архитектурные изменения

### Health Checks
- **Было**: 3 endpoints (`/`, `/status`, `/health`) для Render keep-alive
- **Стало**: 1 endpoint (`/health`) для стандартного мониторинга

### Ресурсы
- **CPU**: 0.5 cores (оптимизировано для free tier)
- **Memory**: 512MB (достаточно для Telegram бота)
- **Scaling**: 1 replica (фиксированное количество)

### Networking
- **Порт**: 10000 (сохранен для совместимости)
- **Протокол**: HTTP
- **Domains**: `gemaibotv2.northflank.app`

## 📊 Мониторинг и логирование

### Health Check Endpoint
```http
GET /health
Response: {"status": "healthy", "timestamp": "...", "services": {...}}
```

### Логирование
- **LOG_JSON**: true (структурированные логи)
- **PYTHONUNBUFFERED**: 1 (немедленный вывод)
- **Container logs**: Доступны в Northflank Dashboard

## 🔍 Тестирование

### Локальное тестирование
```bash
# Использовать docker-compose.northflank.yml
docker-compose -f docker-compose.northflank.yml up --build

# Проверить health check
curl http://localhost:10000/health
```

### Production проверки
1. **Health Check**: `/health` возвращает 200
2. **Bot Status**: Telegram бот отвечает на команды
3. **Database**: Подключение к Supabase работает
4. **Redis**: Подключение к Upstash работает

## 🚨 Troubleshooting

### Частые проблемы

#### 1. Port binding error
```bash
# Убедиться, что порт 10000 не занят
netstat -tulpn | grep :10000
```

#### 2. Environment variables
```bash
# Проверить все переменные в Northflank Dashboard
# Особенно DATABASE_URL и REDIS_URL
```

#### 3. Health check failures
```bash
# Проверить логи контейнера
# Убедиться, что Flask сервер запустился
```

## 📈 Преимущества Northflank.com

### ✅ Что улучшилось
- **Производительность**: Лучшая инфраструктура
- **Мониторинг**: Встроенные метрики и логи
- **Scaling**: Автоматическое масштабирование при необходимости
- **Security**: Встроенная защита и SSL

### 💰 Стоимость
- **Free Tier**: Доступен для небольших проектов
- **Pay-as-you-go**: Оплата только за использованные ресурсы
- **Predictable**: Прозрачное ценообразование

## 🔄 Rollback Plan

### В случае проблем
1. **Остановить** Northflank deployment
2. **Восстановить** Render.com deployment
3. **Проанализировать** логи и исправить проблемы
4. **Повторить** миграцию

### Критические моменты
- **Backup**: Всегда иметь backup данных
- **Testing**: Тестировать на staging перед production
- **Monitoring**: Внимательно следить за метриками после миграции

## 📞 Поддержка

### Northflank.com
- **Documentation**: https://docs.northflank.com
- **Community**: Discord, GitHub Discussions
- **Support**: Email support для платных планов

### Проект
- **Issues**: GitHub Issues
- **Documentation**: README.md, API_LOGGING_README.md
- **Configuration**: app/config.py
