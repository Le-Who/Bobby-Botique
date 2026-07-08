# gemaibotv2 — Agent Context (updated 2026-06-25)

## Project Overview

Python Telegram bot with Gemini AI backend.

| Layer | Technology |
|---|---|
| Language | Python 3.12+ (pyproject targets 3.11 for compat) |
| Bot framework | python-telegram-bot ≥ 20.7 (PTB) |
| LLM providers | Gemini (`google-genai`), OpenRouter, Opencode, Pollinations, FreeTheAI |
| Image generation | Imagen 4 (fast / base / ultra), Pollinations (flux, zimage, gptimage…) |
| Voice / TTS | Gemini Live API (real-time audio), ElevenLabs, FreeTheAI Audio |
| Database | PostgreSQL via `asyncpg` + pgvector |
| Caching / state | Redis (distributed `UserState`) |
| Web server | Quart + Hypercorn (health checks, Mini App SSR, Natal Chart web) |
| Observability | `structlog` (structured), `rich` (console), Prometheus (`prometheus_client`) |
| Serialization | `orjson` + `msgspec` (Rust-backed, 2-6× faster than stdlib `json`) |
| Event loop | `uvloop` on Linux/Docker; asyncio on Windows |

---

## ⚠️ CRITICAL: UTF-8 Encoding — NEVER VIOLATE

All source files are UTF-8. **Violations have caused production incidents (commit 4556976 corrupted 60+ emoji).**

```python
# ✅ ALWAYS specify encoding explicitly
content = open("README.md", encoding="utf-8").read()
open("README.md", "w", encoding="utf-8").write(content)

# ❌ NEVER rely on system default (cp1251 on this Windows machine)
content = open("README.md").read()
```

- Windows terminal showing garbled emoji is a **display bug** — the file on disk is correct.
- **Never** replace emoji with `?`, `???`, or `\uXXXX` escape sequences.
- **Never** "convert" or "fix" encoding unless you have verified actual corruption with:
  ```python
  python -c "open('README.md', encoding='utf-8').read(); print('OK')"
  ```
- Pre-commit hook (`scripts/check_encoding.py`) **blocks commits** with mojibake patterns.

---

## Code Rules

### Async-first
- **Everything is async.** Use `asyncio`, `await`, `async with` throughout.
- Never call blocking I/O without `asyncio.to_thread` or an equivalent executor.
- Background fire-and-forget tasks **must** use `app.utils.background_tasks` (tracked `TaskManager`) — never raw `asyncio.create_task` for long-lived work. `RUF006` (dangling task lint) is enabled.

### Error handling
- Raise from the typed hierarchy in `app/errors.py` (`GemaibotBaseException` → domain subclasses).
- Use the `handle_api_errors` async context manager for provider calls.
- Never swallow exceptions silently — log with `structlog` and re-raise or convert to a typed error.

### LLM / Provider routing
- All LLM calls go through `app/providers/router.py` (`ProviderRouter`). Never call a provider SDK directly from a handler.
- `CommandHandler` **cannot** accept Cyrillic command names — PTB enforces `^[\da-z_]{1,32}$` and raises `ValueError`. Use `MessageHandler(filters.Regex(...))` for Russian-language commands.
- Model constants live in `app/config.py` (`GEMINI_PRIMARY_MODEL`, `GEMINI_ECONOMY_MODEL`, etc.). Never hard-code model names in handlers.

### State & Concurrency
- `UserState` in `app/state.py` is Redis-backed and distributed. Always acquire its lock before mutating.
- Debounce middleware in `app/middleware/debounce.py` — respect its semantics when adding new message handlers.
- Dedup middleware in `app/middleware/dedup.py` prevents webhook duplicate delivery.

### Telegram specifics
- Streaming responses live in `app/streaming.py` — use the existing helpers, never re-implement chunked edit loops.
- `app/utils/text_format.py` owns all MarkdownV2 escaping. Never call `telegram.helpers.escape_markdown` directly.
- `app/utils/keyboards.py` is the single source of truth for `InlineKeyboardMarkup` builders.
- `app/utils/messaging.py` wraps `send_message` / `edit_message` with retry and flood-wait logic.

### Serialization
- Use `app/utils/json_compat.py` (re-exports `orjson`/`msgspec` with stdlib fallback) — **not** `import json`.
- `msgspec.Struct` is preferred over `dataclasses` for hot-path deserialization.

### Security
- All admin-only handlers must call `security.py` guards before acting.
- Provider API keys are encrypted at rest (`app/crypto.py`). Never log raw keys.
- Telegram `initData` for Mini App requests is validated in `app/web_miniapp.py`.

---

## Module Map

```
bot.py                      # Application entrypoint, PTB dispatcher, handler registration
app/
  config.py                 # All settings (Pydantic), model constants, env loading
  errors.py                 # Typed exception hierarchy + handle_api_errors ctx manager
  state.py                  # UserState — Redis-backed distributed per-user state + locks
  streaming.py              # Streaming response writer (chunked edits, finish detection)
  i18n.py                   # Internationalization strings (Russian primary)
  intent_router.py          # Top-level intent routing (text → handler dispatch)
  prompt_registry.py        # Centralized prompt templates (do not inline prompts in handlers)
  security.py               # Admin guards, initData validation, rate limiting
  crypto.py                 # AES-GCM encryption for stored provider keys
  metrics.py                # Business metrics (structlog + Prometheus counters/histograms)
  prometheus.py             # Prometheus HTTP exposition endpoint
  circuit_breaker.py        # Per-provider circuit breaker (CLOSED/OPEN/HALF-OPEN)
  degradation.py            # Graceful degradation policy when providers are unhealthy
  queue.py                  # Redis-backed async task queue
  memory_manager.py         # Long-term memory retrieval / injection into context
  cache.py                  # In-process + Redis two-tier cache helpers
  database.py               # asyncpg pool management, migration runner
  tracing.py                # OpenTelemetry-compatible request tracing stubs

  providers/
    router.py               # ProviderRouter — all LLM calls enter here
    gemini.py               # Gemini (google-genai) provider
    openrouter.py           # OpenRouter provider
    opencode.py             # Opencode (OpenAI-compat) provider
    pollinations.py         # Pollinations image generation
    imagen_provider.py      # Google Imagen 4 (fast/base/ultra)
    elevenlabs_tts.py       # ElevenLabs TTS provider
    freetheai*.py           # FreeTheAI text / audio / image providers
    tts.py                  # TTS provider abstraction / selector
    base.py                 # BaseProvider ABC + shared helpers

  handlers/
    messages.py             # Main message dispatch (text, media, voice routing)
    commands.py             # /start, /help, /reset and other user commands
    cmd_admin.py            # Admin-only commands (/admin, /stats, /broadcast…)
    cmd_image.py            # /draw image generation commands
    cmd_reminders.py        # /remind reminder commands
    inline.py               # Inline query handler (all inline modes)
    callbacks.py            # Callback query dispatcher
    cb_*.py                 # Domain-specific callback handlers (image, voice, roles…)
    ai_chat.py              # AI chat pipeline orchestration
    ai_photo.py             # Photo/vision analysis pipeline
    ai_search.py            # Web search pipeline (Jina + Tavily)
    natal_chart.py          # Natal chart / astrology handler
    daily_crocodile.py      # Daily Crocodile game handler
    daily_2048.py           # Daily 2048 game handler
    scheduled_*.py          # Scheduled job handlers (horoscopes, briefs)

  context/
    assembler.py            # Builds the LLM message context (history + memory + system)
    compression.py          # Context compression / summarization
    token_budget.py         # Token budget enforcement

  repos/
    memory.py               # Memory CRUD + vector search (pgvector)
    keys.py                 # Provider API key management
    chats.py                # Chat / conversation persistence
    users.py                # User profile & settings
    *.py                    # Domain-specific repositories (analytics, games, etc.)

  db/
    migrations.py           # Schema migrations (asyncpg, run at startup)
    rls.py                  # PostgreSQL Row-Level Security policies
    schema.py               # Table DDL source of truth

  middleware/
    debounce.py             # Request debouncing (suppress rapid duplicate updates)
    dedup.py                # Webhook deduplication (idempotency key check)

  games/
    crocodile.py            # Crocodile game core logic
    judge.py                # LLM-based answer judge
    word_bank.py            # Word bank management
    daily_2048.py           # 2048 game logic
    hinting.py              # Hint generation

  utils/
    text_format.py          # MarkdownV2 escaping, message splitting, formatting
    keyboards.py            # InlineKeyboardMarkup builders (single source of truth)
    messaging.py            # send/edit helpers with retry + flood-wait
    json_compat.py          # orjson/msgspec with stdlib fallback — USE THIS, not `import json`
    background_tasks.py     # Tracked TaskManager for fire-and-forget async tasks
    decorators.py           # @admin_only, @rate_limit, @log_call decorators
    logging_config.py       # structlog + rich configuration
    multimodal_processor.py # Image/audio/doc preprocessing for LLM input

  web.py                    # Quart app (health, admin API, webhook endpoint)
  web_miniapp.py            # Telegram Mini App SSR + WebSocket (games, boards)
  web_natal.py              # Natal chart web rendering
```

---

## Project Commands

```bash
# Run full test suite (parallel, timeout 30s)
pytest tests/ -n auto -q

# Run only unit tests
pytest tests/ -m unit -n auto -q

# Run integration tests (require live DB/Redis)
pytest tests/ -m integration -q

# Lint (must be clean before any commit)
ruff check .

# Auto-fix lint issues
ruff check . --fix

# Format
ruff format .

# Type check
python -m mypy app/ --ignore-missing-imports

# Check encoding integrity (blocks commit if mojibake found)
python scripts/check_encoding.py

# Run pre-commit hooks on all files
pre-commit run --all-files
```

---

## Key Invariants (Do Not Break)

1. **One JSON import** — always `from app.utils.json_compat import json`, never `import json`.
2. **One keyboard builder** — always `app/utils/keyboards.py`, never construct `InlineKeyboardMarkup` inline in handlers.
3. **One Markdown escaper** — always `app/utils/text_format.py`.
4. **No raw LLM calls in handlers** — all LLM calls go through `ProviderRouter`.
5. **No Cyrillic `CommandHandler`** — use `MessageHandler(filters.Regex(...))` for Russian commands.
6. **No hard-coded model names** — use constants from `app/config.py`.
7. **No bare `asyncio.create_task`** for long-lived work — use `background_tasks.TaskManager`.
8. **Encoding always explicit** — `encoding="utf-8"` on every `open()` call.

---

## Testing Conventions

- `asyncio_mode = auto` — all `async def test_*` functions run natively without `@pytest.mark.asyncio`.
- Default `addopts`: `-n auto --dist=loadgroup --timeout=30` (parallel, 30-second per-test timeout).
- Markers: `unit`, `integration`, `slow`.
- Factories in `tests/factories.py` — use them to construct test objects, never raw dicts.
- `tests/conftest.py` — shared fixtures (mock bot, mock DB pool, mock Redis, mock provider).
- Never mock `app/utils/text_format.py` — its output is behavior, test it directly.
- Integration tests must be isolated: use test-only DB schema / prefixed Redis keys, never production data.

---

## Observability

- **Structured logging**: `structlog` with `contextvars`-bound `request_id`. Always bind context at the top of a handler, not inline.
- **Metrics**: `app/metrics.py` owns all business counters/histograms. Prometheus exposition at `/metrics`. Add new metrics there, not in handlers.
- **Circuit breaker**: `app/circuit_breaker.py` — each provider has an independent breaker. Check `is_open()` before routing; `app/degradation.py` decides the fallback.
- **Tracing**: `app/tracing.py` stubs — spans are propagated via `contextvars`.

---

## Common Gotchas

| Symptom | Root cause | Fix |
|---|---|---|
| `ValueError: Invalid command` on registration | Cyrillic command name in `CommandHandler` | Use `MessageHandler(filters.Regex(...))` |
| Garbled emoji in terminal | Windows cp1251 terminal display | Not a bug — file is correct UTF-8 |
| Emoji replaced with `?` after file write | `open()` without `encoding="utf-8"` | Always specify `encoding="utf-8"` |
| Duplicate webhook updates processed | `dedup` middleware bypassed | Never skip `app/middleware/dedup.py` |
| Dangling asyncio task (RUF006) | `asyncio.create_task` without reference | Store task in `TaskManager` |
| Provider call in handler | Direct SDK call bypassing router | Route through `ProviderRouter` |
