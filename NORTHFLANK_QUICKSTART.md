# 🚀 Быстрый старт на Northflank.com

## ⚡ Экспресс-миграция

### 1. Автоматическая подготовка
```bash
python migrate_to_northflank.py
```

### 2. Деплой
```bash
./deploy_to_northflank.sh
```

### 3. Настройка в Northflank Dashboard
- Создать проект
- Подключить Git репозиторий
- Добавить переменные окружения
- Дождаться сборки

## 📁 Ключевые файлы

| Файл | Назначение |
|------|------------|
| `northflank.yaml` | Основная конфигурация |
| `Dockerfile.northflank` | Оптимизированный образ |
| `docker-compose.northflank.yml` | Локальное тестирование |
| `NORTHFLANK_MIGRATION.md` | Подробная документация |

## 🔧 Основные изменения

- ❌ Убраны keep-alive механизмы Render.com
- ✅ Оптимизированы health checks
- ✅ Упрощена конфигурация
- ✅ Улучшена производительность

## 📊 Мониторинг

- **Health Check**: `/health` endpoint
- **Логи**: Northflank Dashboard
- **Метрики**: Встроенные в платформу

## 🆘 Поддержка

- 📖 `NORTHFLANK_MIGRATION.md` - полная документация
- 🐛 GitHub Issues для багов
- 💬 Discord сообщество Northflank

---

**Время миграции**: ~15 минут  
**Сложность**: Низкая  
**Риск**: Минимальный
