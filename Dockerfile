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
USER app

# Копируем файл с зависимостями
COPY --chown=app:app requirements.txt .

# Устанавливаем зависимости с оптимизацией для Render
RUN pip install --no-cache-dir --user -r requirements.txt

# Копируем исходный код
COPY --chown=app:app . .

# Настраиваем переменные окружения для оптимизации
ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1
ENV PATH="/home/app/.local/bin:${PATH}"

# Expose порт для Render health check
EXPOSE 10000

# Команда для запуска бота
CMD ["python", "bot.py"]
