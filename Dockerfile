# Используем официальный образ Python
FROM python:3.11-slim

# Устанавливаем рабочую директорию
WORKDIR /app

# Копируем файл с зависимостями
COPY requirements.txt .

# Устанавливаем системные зависимости для сборки
RUN apt-get update && apt-get install -y --no-install-recommends build-essential && apt-get clean

# Устанавливаем зависимости
RUN pip install --no-cache-dir -r requirements.txt

# Копируем весь исходный код (включая новую папку app)
COPY . .

# Команда для запуска бота
CMD ["python", "bot.py"]
