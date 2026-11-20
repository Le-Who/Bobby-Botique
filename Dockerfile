FROM python:3.15-slim@sha256:65a65fc7b30b06cb6df4930fd4650697cb23f4c2698ab81fc87e4b031bc36928

RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    ffmpeg \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

RUN python -m pip install --no-cache-dir "uv==0.13.5"
COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-dev --no-install-project --link-mode copy

COPY . .
RUN test -f /app/docs/natal-reference-fixture.moira-jpl.json

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
