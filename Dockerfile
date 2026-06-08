# Production Dockerfile
FROM python:3.14-slim

# Install minimal system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    ffmpeg \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python dependencies first (layer caching)
COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY . .
RUN test -f /app/docs/natal-reference-fixture.moira-jpl.json

# Prepare startup script + create non-root user with access to tg-api shared volume
# GID 101 matches the telegram-bot-api group inside the aiogram/telegram-bot-api container.
# Files on the shared Docker volume are owned by 101:101 — adding 'app' to GID 101
# allows the bot to read voice/photo/document files written by the Local Bot API Server.
RUN chmod +x /app/start.sh \
    && groupadd --gid 101 telegram-bot-api \
    && useradd --create-home --shell /bin/bash --groups telegram-bot-api app \
    && chown -R app:app /app

ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONPATH=/app

USER app

EXPOSE 10000

HEALTHCHECK --interval=60s --timeout=10s --start-period=40s --retries=3 \
    CMD curl -f http://localhost:10000/health || exit 1

CMD ["/app/start.sh"]
