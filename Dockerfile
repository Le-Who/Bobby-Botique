# Используем официальный образ Python (optimized for free tier)
FROM python:3.11-slim

# Устанавливаем системные зависимости и очищаем кэш
RUN apt-get update && apt-get install -y --no-install-recommends \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# Устанавливаем рабочую директорию
WORKDIR /app

# Создаем non-root пользователя для безопасности
RUN useradd --create-home --shell /bin/bash app \
    && chown -R app:app /app

# Копируем файл с зависимостями
COPY requirements.txt .

# Устанавливаем зависимости
RUN pip install --no-cache-dir -r requirements.txt

# Копируем исходный код
COPY . .

# Создаем скрипт для запуска бота
RUN echo '#!/bin/bash' > /app/start.sh && \
    echo 'echo "=== BOT STARTUP SCRIPT ==="' >> /app/start.sh && \
    echo 'echo "Container ID: $HOSTNAME"' >> /app/start.sh && \
    echo 'echo "Process ID: $$"' >> /app/start.sh && \
    echo 'echo "Starting bot..."' >> /app/start.sh && \
    echo 'exec python /app/bot.py' >> /app/start.sh && \
    chmod +x /app/start.sh

# Настраиваем переменные окружения для логирования
ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONPATH=/app

# Передаем права пользователю app
RUN chown -R app:app /app
USER app

# Команда для запуска бота
CMD ["/app/start.sh"]
