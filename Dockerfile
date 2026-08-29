# Production Dockerfile
FROM python:3.14-slim@sha256:cae66f2ef0ec51a9891263eeee7f987dacf0a9879e8aa9353d5606e0530619a5

# Install minimal system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    ffmpeg \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install the exact production graph from the committed lockfile.
RUN python -m pip install --no-cache-dir "uv==0.12.6"
COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-dev --no-install-project --link-mode copy

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
ENV PATH="/app/.venv/bin:$PATH"

USER app

EXPOSE 10000

HEALTHCHECK --interval=60s --timeout=10s --start-period=40s --retries=3 \
    CMD curl -f http://localhost:10000/health || exit 1

CMD ["/app/start.sh"]
