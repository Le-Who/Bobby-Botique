# GemAI Bot v2

GemAI Bot v2 is an open-source Telegram AI assistant framework focused on resilient real-world bot operations: multi-provider AI routing, streaming recovery, agentic web research, multimodal processing, long-term graph memory, Telegram Mini App surfaces, Docker deployment, and operator tooling.

## Maintainer / OSS Status

- **Primary maintainer:** [Le-Who](https://github.com/Le-Who)
- **Repository:** <https://github.com/Le-Who/gemaibotv2>
- **Production branch (checked-in workflows):** `vps_testai`
- **License:** MIT, see [LICENSE](LICENSE).
- **Security policy:** see [SECURITY.md](SECURITY.md).
- **Contributing guide:** see [CONTRIBUTING.md](CONTRIBUTING.md).
- **Maintainer roadmap:** see [ROADMAP.md](ROADMAP.md).
- **Maintainer queue:** see [docs/MAINTAINER_QUEUE.md](docs/MAINTAINER_QUEUE.md).

This is a production-oriented Telegram bot codebase with VPS deployment automation. It includes provider failover, user-facing recovery from model/API failures, authenticated Mini App surfaces, persistent memory and background-task ownership. Current deployment health must be verified separately.

## Documentation status

Reviewed against checkout `8fc19516` on 2026-09-08. This is a maintained
application with deployment automation; a checkout alone does not establish
current production health, traffic, provider availability or an open-PR count.

Start with the [documentation index](docs/README.md), [agent agreements](AGENTS.md),
[architecture](docs/ARCHITECTURE.md) and [audit findings](docs/documentation-audit-2026-09-08.md).
The [changelog](CHANGELOG.md) records history, not a current runtime specification.

## Feature Overview

- Chat with Gemini, Opencode, OpenRouter and FreeTheAI routing, model selection,
  key rotation, provider fallback and recovery from partial responses.
- Quick search (`?`), bounded agentic research (`??`), URL/document/photo
  understanding and deterministic weather/currency/crypto shortcuts.
- Typed streamed/completed response delivery, preserving final actions; long
  responses use Redis Reader, optional public Telegraph, or Telegram splitting.
- Inline answers, response tabs, collaborative boards and image generation.
  Inline text and image paths have their own routing; they are not a fixed
  promise that a particular model will win a race or meet a latency target.
- `/draw` and natural-language image requests with canvas controls. Gemini native
  image generation accepts legacy Imagen selections as aliases. Pollinations
  requires a server-side key and Pollen budget; FreeTheAI has a separate image path.
- Voice transcription, intent-aware chat/search routing, queued TTS and Mini App
  Live Audio. Confirmation depends on the voice flow; it is not universal.
- Consent-gated private long-term memory, hybrid vector/text recall, provenance-aware
  graph writes and account-data controls. Group messages are not implicitly saved
  into private LTM.
- Daily Crocodile, 2048 and trivia; scheduled briefings, reminders, tarot and
  horoscopes. Horoscope settings provide on-demand today/tomorrow readings.
- Feature-gated natal reports with local city lookup, PyEphem planet calculations,
  local equal-house/angle math and optional LLM interpretation.
- Admin dashboard and daily-content controls, metrics, dependency validation and
  exact-SHA VPS deployment automation.

The public command/help catalog is [app/bot_commands.py](app/bot_commands.py).
Model defaults below describe code configuration, not independently verified
provider entitlements. See [Crocodile operations](docs/pollinations-daily-croc.md)
and [natal readiness](docs/natal-chart-product-readiness.md) for their boundaries.

## Non-Goals / Limitations

- There is no dedicated direct OpenAI provider module; OpenAI-compatible gateways
  are separate integrations. Updating Codex guidance does not add that provider.
- No ORM: persistence uses asyncpg and numbered SQL.
- Redis absence selects local fallbacks only where implemented; it does not make
  Reader, deferred delivery or every multi-replica feature equivalent.
- Provider quotas, response time, quality and model access are deployment-specific.
- Private-data erasure cannot retract copies already exported, shared or published
  to third-party services. Telegraph is disabled by default.
- Natal calculations support equal houses, not Placidus/Koch or a claim of
  Swiss Ephemeris parity; rollout still needs environment-specific verification.

## Architecture

- **Bot Container (`tg-bot`)**: A single async event loop runs both the Telegram webhook updater and the Quart web server via Hypercorn. Webhook mode: Quart receives updates at a derived-hash webhook path, validates the optional secret header, deduplicates updates and enqueues them for `UserScopedUpdateProcessor(50)`. Rejected or backpressured requests do not receive an unconditional success response. Falls back to long-polling if `WEBHOOK_URL` is unset.
- **Local Bot API Server (`tg-api`)**: Self-hosted `telegram-bot-api` container (`aiogram/telegram-bot-api:latest`) communicates with Telegram via MTProto. The bot sends HTTP requests to `http://tg-api:8081/bot` instead of `api.telegram.org`. Shared Docker volume (`tg-api-data`) enables zero-copy file access for voice/photo/document processing. File limit: 2 GB (vs 50 MB cloud API). Timezone: `Europe/Kyiv`.
- **Media Cleanup Cron (`tg-media-cleanup`)**: Alpine-based sidecar container that runs a 60s-tick loop: (1) `chmod -R g+rX` on the shared volume to fix permission conflicts between `telegram-bot-api` (UID 101) and the bot container (GID 101), and (2) deletes cached media files older than 7 days every 24 hours to prevent disk exhaustion.
- **Database (PostgreSQL)**: Source of truth for users, chats, messages, metrics, roles, and pgvector embeddings.
- **Cache (Redis)**: Optional high-speed layer for caching rate limits and transient states.
- **Third-Party APIs**: Google Gemini (native SDK), Opencode Go (HTTPX), OpenRouter (HTTPX), JINA AI (HTTPX), Tavily (HTTPX).

> **Docker networking:** All three containers share a `tg-net` bridge network. The shared volume `tg-api-data` is mounted at `/var/lib/telegram-bot-api` in both `tg-api` and `tg-bot`. The bot's web server binds to `127.0.0.1:$PORT` on the host (not `0.0.0.0`) — Caddy/Nginx reverse proxy is expected in front.

```mermaid
graph TD;
    User-->TelegramCloud[Telegram Cloud];
    TelegramCloud-->LocalAPI["tg-api<br/>Local Bot API Server<br/>MTProto ↔ REST"];
    LocalAPI-->BotHandler["tg-bot<br/>Python Bot + Quart"];
    Admin-->QuartServer["Quart Web Server<br/>/metrics, /health, /webapp"];

    BotHandler-->ProviderRouter;
    ProviderRouter-->Gemini[Google Gemini];
    ProviderRouter-->OpencodeGo[Opencode Go];
    ProviderRouter-->OpenRouter[OpenRouter];

    BotHandler-->Tavily[Tavily Search];

    BotHandler-->Cache[(Redis)];
    BotHandler-->DB[(PostgreSQL/pgvector)];
    QuartServer-->DB;

    LocalAPI-.->SharedVolume["tg-api-data<br/>/var/lib/telegram-bot-api<br/>Shared Docker Volume"];
    BotHandler-.->SharedVolume;
    Cleanup["tg-media-cleanup<br/>Alpine cron<br/>chmod + 7d prune"]-.->SharedVolume;
```

## Repository Structure

| Path                  | Purpose                                                                        |
| --------------------- | ------------------------------------------------------------------------------ |
| `app/`                | Core application logic (bot, web server, DB layer, handlers).                  |
| `app/handlers/`       | Telegram command and message processors (`ai_chat`, `ai_search`, `commands`, `inline`). |
| `app/repos/`          | Database repository pattern implementations (queries for chats, memory, keys). |
| `app/providers/`      | AI provider abstraction layer (base, Gemini, Opencode, OpenRouter, FreeTheAI, Pollinations, Imagen, TTS, router). |
| `app/response_delivery/` | Typed provider-event coordination and the single owner of Telegram response rendering/finalization. |
| `app/core/`           | Agentic research engine — multi-step query decomposition and tool use.         |
| `app/context/`        | Context assembly subsystem (assembler, summarizer, compression, token budget). |
| `app/documents/`      | Document processing: chunking strategies, parsers, document repository.        |
| `app/middleware/`     | Request pipeline middleware (debounce aggregation, dedup prevention).           |
| `app/adapters/`       | Distributed/local concurrency primitives.                                |
| `app/db/`             | Database bootstrap: schema validation, migrations runner, RLS, seed.           |
| `app/utils/`          | Shared utilities (formatting, keyboards, background tasks, image utils, reader SSR, etc.). |
| `app/templates/`      | HTML Jinja2 templates for admin dashboard and Telegram Mini App.               |
| `app/games/`          | Crocodile, daily 2048/trivia, shared AI authoring and Telegram delivery. |
| `app/bot_commands.py` | Canonical public command catalog used by `/help` and Telegram's slash-command menu. |
| `app/bot_instance.py` | PTB Bot singleton — allows non-PTB code (WebSocket handlers) to call Bot API methods. |
| `app/web_miniapp.py`  | Quart Blueprint for Telegram Mini App (initData auth, memory/settings/game API). |
| `app/deferred_response.py` | Redis-backed deferred AI generation worker for background retry after total outage. |
| `app/intent_router.py`| Lightweight LLM-bypass for weather/currency/crypto queries via WeatherAPI.com, ExchangeRate-API & CoinGecko. |
| `app/state.py`        | Process-local user state, local locks and debounced PostgreSQL persistence. |
| `docs/`               | Extended architectural documentation.                                          |
| `scripts/migrations/` | Numbered SQL migration files — single source of truth for all DDL.             |
| `tests/`              | Comprehensive test suite (Unit and Integration).                               |
| `bot.py`              | Main application entry point uniting Quart and the Telegram updater.           |

## Tech Stack

| Layer           | Technology            | Purpose                                        |
| --------------- | --------------------- | ---------------------------------------------- |
| Runtime         | Python 3.14-slim      | Execution environment                          |
| Bot Framework   | `python-telegram-bot` | Async interaction with Telegram APIs           |
| Web Server      | Quart + Hypercorn     | Lightweight dashboard & Prometheus `/metrics`  |
| Database        | `asyncpg`             | High-performance Async PostgreSQL driver       |
| Vector DB       | `pgvector`            | Storing and querying semantic memories         |
| Data Validation | `pydantic`            | Configuration and strictly-typed object models |
| Observability   | `structlog`           | Structured JSON logging & context correlation  |

## Setup

1. Clone the repository.
2. Use Python `>=3.14,<3.15`; the Docker image uses the slim variant. Supply PostgreSQL with pgvector available. CI uses PostgreSQL 17 with pgvector.
3. Install the pinned package manager and synchronize the committed dependency graph:
   ```bash
   python -m pip install "uv==0.12.6"
   uv sync --locked
   ```
4. Create an untracked UTF-8 `.env` using the configuration reference below; this checkout does not ship `.env.example`. The application reads process environment; local `uv run --env-file .env` or Docker `--env-file` loads the file explicitly.
5. Create PostgreSQL database with `pgvector` extension. Pending numbered SQL migrations from `scripts/migrations/` are applied automatically on startup; migration files are kept idempotent so fresh deploys and repeated bootstrap runs remain safe.

## Configuration

Configuration is explicitly read from environment variables in `app/config.py` and infrastructure modules. The following is an operator reference; `load_settings()` defines effective defaults (which may differ from bare `Settings` field defaults). Deployment Secrets and persisted admin overrides can change them.

> [!IMPORTANT]
> Variables marked ✅ are **required** — the application will refuse to start if they are absent. Variables marked ⚙️ are optional and will use the listed defaults.

---

### 📔  Core / Authentication

| Variable | Required | Format / Example | Default | Notes |
|---|---|---|---|---|
| `TELEGRAM_BOT_TOKEN` | ✅ | `123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11` | — | Obtained from [@BotFather](https://t.me/BotFather). Must match the running bot. |
| `ADMIN_ID` | ⚙️ | `123456789` | `0` | Set a real administrator ID for admin features, seeding and natal smoke. The loader permits omission; this does not configure a usable administrator. |
| `ADMIN_SECRET` | ⚙️ | Any secure random string, e.g. `openssl rand -hex 24` | — | Password for the admin web dashboard login form. Also used as encryption seed for stored API keys in the DB. **Keep stable across restarts** — changing it breaks decryption of stored keys. |

---

### 🗄️  Database

| Variable | Required | Format / Example | Default | Notes |
|---|---|---|---|---|
| `DATABASE_URL` | ✅ | `postgresql://user:pass@localhost:5432/gemaibotv2` | — | Standard asyncpg/libpq DSN. **Must** have the `pgvector` extension available — the bot won't start without it. Use a DSN reachable from the bot container; `localhost` inside a container is that container, not the VPS host. |
| `DB_POOL_MIN_SIZE` | ⚙️ | `2`—`10` | `2` | Minimum open asyncpg connections. Increase on high-traffic deployments to avoid connection storms. |
| `DB_POOL_MAX_SIZE` | ⚙️ | `10`—`50` | `10` | Maximum open asyncpg connections. Keep below the PostgreSQL `max_connections` limit (default 100). Tune from observed connection pressure and the database connection budget. |
| `TEST_DATABASE_URL` | ⚙️ | Same DSN format, pointing to a test DB | — | Used **only** during integration test execution (`pytest -m integration`). Completely isolated from production data. |

> **RLS deployment boundary:** the current startup path uses `DATABASE_URL` for
> both schema migrations and runtime queries. A PostgreSQL superuser,
> `BYPASSRLS` role, or table owner can bypass ordinary `ENABLE ROW LEVEL
> SECURITY` policies, so the built-in RLS layer is defense-in-depth rather than
> a hard tenant boundary when such a DSN is used. Application queries still set
> a transaction-local tenant context and scope by `user_id`. For hard database
> isolation, first split the current single-DSN bootstrap/runtime path: run
> migrations and RLS provisioning through a separate owner/migrator, then run
> the bot through a `NOSUPERUSER NOBYPASSRLS` non-owner role with only required
> DML, sequence, and function privileges. Validate cross-tenant denial against
> that role before enabling `FORCE ROW LEVEL SECURITY`; the current code does
> not yet provide that two-role bootstrap automatically.

> **Migrating from Supabase to a local DB:**
> ```bash
> # 1. Dump from Supabase
> pg_dump "postgresql://postgres.xxx:PASS@aws-1-eu-north-1.pooler.supabase.com:5432/postgres" \
>   --no-owner --no-acl -Fc -f backup.dump
>
> # 2. Create local DB with pgvector
> createdb -U postgres gemaibotv2
> psql -U postgres gemaibotv2 -c "CREATE EXTENSION IF NOT EXISTS vector;"
>
> # 3. Restore data
> pg_restore -h localhost -U postgres -d gemaibotv2 --no-owner --no-acl -Fc backup.dump
> ```
> After restore, update `DATABASE_URL` to `postgresql://postgres:pass@localhost:5432/gemaibotv2` and redeploy. Validate restore completeness, extensions, grants and application behavior before switching traffic; keep the backup for recovery.

---

### 📦 Cache & Queue (Redis)

| Variable | Required | Format / Example | Default | Notes |
|---|---|---|---|---|
| `REDIS_URL` | ⚙️ | `redis://localhost:6379/0` or `rediss://user:pass@host:6379` | — | Used for: distributed LLM semaphores (multi-replica safety), Telegram Mini App Reader page cache (24h TTL), Gemini image per-key counters. Some locks/counters fall back locally if absent; Redis-backed Reader/deferred features and cross-process coordination need separate validation. Use `rediss://` scheme for TLS connections. |

---

### 🌐  Network & Web Server

| Variable | Required | Format / Example | Default | Notes |
|---|---|---|---|---|
| `PORT` | ⚙️ | `10000` | `10000` | Port the Quart web server and admin dashboard binds to. On VPS, expose via Caddy/Nginx reverse proxy — do **not** bind directly to `0.0.0.0` in production. |
| `ENABLE_WEB_SERVER` | ⚙️ | `true` / `false` | `true` | Disabling this skips starting the Quart server entirely. Set `false` only for local dev without the dashboard. |
| `WEBHOOK_URL` | ⚙️ | `https://bot.example.com` | — | If set, the bot registers itself as a Telegram Webhook at this URL and stops long-polling. **Must be HTTPS.** Required for production webhook deployments. If absent, the bot uses long-polling (simpler for single-server setups). |
| `WEBAPP_BASE_URL` | ⚙️ | `https://bot.example.com` | `""` | Public URL from which the Telegram Mini App settings panel and reader are served. Must equal `WEBHOOK_URL` in most deployments. If empty, oversized responses use the optional Telegraph fallback or safe Telegram splitting. |
| `TELEGRAPH_PUBLICATION_ENABLED` | ⚙️ | `true` / `false` | `false` | Explicit privacy opt-in for publishing long responses and natal mirrors as public `telegra.ph` pages. Keep disabled unless users are informed that anyone with the link can read the page. |
| `MINIAPP_SHORT_NAME` | No | `gemaibotv2` | `""` | Short name for the Telegram Mini App deep links (`t.me/<bot>/<short_name>`). Configure the matching Mini App short name in BotFather; do not confuse it with a menu button URL. |

---

### Natal Reports

| Variable | Required | Format / Example | Default | Notes |
|---|---|---|---|---|
| `NATAL_REPORTS_ENABLED` | ⚙️ | `true` / `false` | `false` | Enables `/natal` and hosted natal report generation. Keep disabled until live VPS smoke tests pass, then enable explicitly. |
| `NATAL_REPORT_TTL_DAYS` | ⚙️ | `365` | `365` | Retention window for hosted natal reports. PostgreSQL storage keeps shared links stable by default. |
| `NATAL_GEOCODER_PROVIDER` | ⚙️ | `local` / `nominatim` | `local` | Birth-place resolution uses the local GeoNames-backed city catalog. Set `nominatim` explicitly to allow network fallback for unresolved free text. |
| `NATAL_CITY_OVERRIDES_PATH` | ⚙️ | `/srv/bot/natal-city-overrides.json` | `""` | Optional UTF-8 JSON file for admin-reviewed city records missing from GeoNames. Records are loaded into the local autocomplete/geocoding path; see `docs/natal-city-overrides.example.json`. |
| `NATAL_SEND_RAW_BIRTH_DATA_TO_LLM` | ⚙️ | `false` | `false` | Privacy guard. Keep `false`: LLM prompts should receive derived chart data, not raw birth date/place. |

Natal city autocomplete uses GeoNames data via the `geonamescache` Python package. GeoNames data is licensed under CC BY 4.0 and should be credited in public product materials.

Before enabling natal reports publicly, run the live smoke check on the host with real environment variables:

```bash
python scripts/natal_smoke.py --webhook-url "$WEBHOOK_URL"
```

---

### 🖥️  Local Bot API Server (Optional)

When configured, the bot communicates with a self-hosted Local Bot API Server instead of `api.telegram.org`. This supports shared-volume media access and larger-file workflows; it does not remove all network or processing latency.

| Variable | Required | Format / Example | Default | Notes |
|---|---|---|---|---|
| `TELEGRAM_API_ID` | ⚠️  | `12345678` | — | From [my.telegram.org](https://my.telegram.org). Required only when running the Local Bot API Server container. |
| `TELEGRAM_API_HASH` | ⚠️  | `0123456789abcdef...` | — | From [my.telegram.org](https://my.telegram.org). Required only when running the Local Bot API Server container. |
| `TELEGRAM_LOCAL_SERVER_URL` | ⚙️ | `http://tg-api:8081/bot` | `""` | URL of the Local Bot API Server. When set, enables `local_mode=True` in PTB. When empty (default), the bot uses the official Telegram cloud API. |

---

### 🤖 Gemini Models (Primary LLM Provider)

| Variable | Required | Format / Example | Default | Notes |
|---|---|---|---|---|
| `GEMINI_API_KEYS` | ✅ | `key1,key2,key3` | — | Comma-separated Google AI Studio API keys. The system rotates through them automatically on quota exhaustion or 503 errors for Gemini chat/judge/fallback paths. Each key has an independent daily request budget tracked in the DB. Minimum 1 key required. Live Audio no longer consumes this pool. |
| `GEMINI_AVAILABLE_MODELS` | ⚙️ | `gemini-3.7-flash,gemini-3.6-flash,gemini-3.5-flash-lite` | `gemini-3.6-flash,gemini-3.5-flash-lite` | Exact, ordered Gemini list shown in `/model`. Future syntactically valid `gemini-*` IDs are accepted without a code release. Role models such as `DEFAULT_MODEL` are not added to this selector automatically. Use the single token `none` for an intentionally empty list. |
| `DEFAULT_MODEL` | ⚙️ | `gemini-3.6-flash` | `gemini-3.6-flash` | Internal model used for standard conversational messages. It may be intentionally hidden from the user-selectable list. |
| `QNA_MODEL` | ⚙️ | `gemini-3.5-flash-lite` | `gemini-3.5-flash-lite` | Internal model used for quick Q&A web search queries (`?` prefix). |
| `RESEARCH_MODEL` | ⚙️ | `gemini-3.6-flash` | `gemini-3.6-flash` | Internal model used for synthesizing Tavily search results into a final research answer. |
| `URL_SELECTION_MODEL` | ⚙️ | `gemini-3.5-flash-lite` | `gemini-3.5-flash-lite` | Lightweight internal model that scores and filters candidate URLs during agentic web research before full content extraction. |
| `TAXONOMY_MODEL` | ⚙️ | `gemini-3.5-flash-lite` | `gemini-3.5-flash-lite` | Internal model used by MemPalace to classify memories into the Wing/Room taxonomy and to judge temporal contradictions (LLM-as-Judge). |

---


---

### Vertex AI (Primary Inline Driver + Optional Live Internet Route)

When `VERTEX_AI_KEY` and `VERTEX_AI_PROJECT` are set, Vertex AI Express is used by specialized inline/game paths; explicit Crocodile text-model selection follows the shared Gemini setting described in the operations notes. **Live Audio remains separate by default**: the default Mini App voice path in the code runs through the Gemini GenAI Live API path (`gemini-3.1-flash-live-preview`). For testing, the Live Mini App now also exposes an **opt-in Vertex Live route** on `gemini-live-2.5-flash-native-audio` with Google Search grounding, selectable as `Vertex Live · с доступом в интернет`.

For that experimental live route, `VERTEX_AI_PROJECT` and `VERTEX_AI_LOCATION` are not enough by themselves. The bot container must also receive readable ADC credentials, typically by mounting a service-account JSON and exporting `GOOGLE_APPLICATION_CREDENTIALS` to that in-container path. The included GitHub Actions deploy flow supports this via the `VERTEX_LIVE_SERVICE_ACCOUNT_JSON` secret and mounts it read-only into the bot container.

| Variable | Required | Format / Example | Default | Notes |
|---|---|---|---|---|
| `VERTEX_AI_KEY` | No | GCP API key string | `""` | Used by configured Vertex Express paths; selection order depends on the caller. This API key is distinct from ADC service-account credentials for the optional Live route. |
| `VERTEX_AI_PROJECT` | No | `my-gcp-project-123` | `""` | GCP project ID where Vertex AI API is enabled. Required together with `VERTEX_AI_KEY`. |
| `VERTEX_AI_LOCATION` | No | `us-central1` | `us-central1` | Vertex AI region. Must match where your models are available. |

### Opencode Go Models

| Variable | Required | Format / Example | Default | Notes |
|---|---|---|---|---|
| `OPENCODE_API_KEYS` | ⚙️ | `sk-abc,sk-xyz` | `[]` | Comma-separated Opencode Go API keys. When set and `PRIMARY_PROVIDER=opencode`, routes standard chat/search/inline through `opencode.ai/zen/go/v1` using Bearer auth. Key rotation works identically to Gemini keys. |
| `OPENCODE_AVAILABLE_MODELS` | ⚙️ | `opencode-go/minimax-m2.7,opencode-go/qwen3.5-plus` | Role-model defaults | Exact, ordered Opencode list shown in `/model`. Use `none` for an intentionally empty list. |
| `PRIMARY_PROVIDER` | ⚙️ | `opencode` / `gemini` / `openrouter` / `freetheai` | `opencode` | Default provider selection; persisted `/set_provider` overrides take precedence. Availability and per-path fallback still depend on keys and model capabilities. |
| `OPENCODE_DEFAULT_MODEL` | ⚙️ | `opencode-go/qwen3.5-plus` | `opencode-go/qwen3.5-plus` | Default Opencode chat role. |
| `OPENCODE_QNA_MODEL` | ⚙️ | `opencode-go/qwen3.6-plus` | `opencode-go/qwen3.6-plus` | Model for quick Q&A search synthesis (`?` prefix). |
| `OPENCODE_RESEARCH_MODEL` | ⚙️ | `opencode-go/glm-5.1` | `opencode-go/glm-5.1` | Model for deep research synthesis (`??`). |
| `OPENCODE_VISION_MODEL` | ⚙️ | `opencode-go/mimo-v2-omni` | `opencode-go/mimo-v2-omni` | Vision-capable model — supports `image_url` natively. Exempt from Gemini vision redirect. |
| `OPENCODE_INLINE_MODEL` | ⚙️ | `opencode-go/minimax-m2.5` | `opencode-go/minimax-m2.5` | Lighter model for inline mode generation. |
| `JINA_API_KEY` | ⚙️ | `jina_xxx...` | `""` | API key for [JINA AI Search](https://jina.ai). Used as grounding backend for `?` quick search when Opencode is active. Also used by agentic research engine for page reading via `r.jina.ai`. Without this key, Opencode search queries run without web context. |

Model IDs are configurable; this table is not a provider-side availability catalog.

---
### OpenRouter Models

| Variable | Required | Format / Example | Default | Notes |
|---|---|---|---|---|
| `OPENROUTER_API_KEYS` | ⚙️ | `sk-or-v1-abc,sk-or-v1-xyz` | `[]` | Comma-separated OpenRouter API keys. Required for OpenRouter generation. Rotated same as Gemini keys. Note: OpenRouter is **disabled for multimodal (image) requests** — Gemini is always used for vision. |
| `OPENROUTER_AVAILABLE_MODELS` | ⚙️ | `stepfun/step-3.5-flash:free,qwen/qwen3-4b:free` | `OPENROUTER_DEFAULT_MODEL` | Exact, ordered OpenRouter list shown in `/model` when at least one OpenRouter key is configured. Use `none` for an intentionally empty list. |
| `OPENROUTER_DEFAULT_MODEL` | ⚙️ | `stepfun/step-3.5-flash:free` | `stepfun/step-3.5-flash:free` | Default model for standard chat on OpenRouter. |
| `OPENROUTER_QNA_MODEL` | ⚙️ | `stepfun/step-3.5-flash:free` | `stepfun/step-3.5-flash:free` | OpenRouter model for quick Q&A search synthesis. |
| `OPENROUTER_RESEARCH_MODEL` | ⚙️ | `stepfun/step-3.5-flash:free` | `stepfun/step-3.5-flash:free` | OpenRouter model for agentic research synthesis. |
| `OPENROUTER_URL_SELECTION_MODEL` | ⚙️ | `stepfun/step-3.5-flash:free` | `stepfun/step-3.5-flash:free` | OpenRouter model for URL scoring during agentic research. |

---

### 🚀 FreeTheAI (Multimodal Router)

| Variable | Required | Format / Example | Default | Notes |
|---|---|---|---|---|
| `FREETHEAI_API_KEYS` | ⚙️ | `sk-freetheai-1,sk-freetheai-2` | `[]` | Comma-separated API keys for FreeTheAI. Provides advanced multimodal image generation and Lyria-based audio music models. |
| `FREETHEAI_AVAILABLE_MODELS` | ⚙️ | `cat/claude-4-6-sonnet,yng/deepseek-r1` | `FREETHEAI_DEFAULT_MODEL` | Exact, ordered FreeTheAI chat-model list shown in `/model`. Image/audio IDs are not selectable here. Use `none` for an intentionally empty list. |
| `FREETHEAI_DEFAULT_MODEL` | ⚙️ | `cat/claude-4-6-sonnet` | `cat/claude-4-6-sonnet` | Default chat model via FreeTheAI. |

For every `*_AVAILABLE_MODELS` variable, an unset or whitespace-only value uses
the provider defaults; a space is therefore **not** a way to clear the list.
Set the value to the single case-insensitive token `none` to make that
provider's user-selectable list empty. `/models` changes create an explicit
database override, which takes precedence over env/Secrets across restarts.
The provider view shows the active source. **Reset to .env** deletes that
override and immediately reloads the current env/Secret value. Legacy
automatically seeded DB lists are discarded so they cannot mask a newly
deployed Secret.

---

### 📔  Search & Web Research

> **Note**: `JINA_API_KEY` is documented in the **Opencode Go Models** section above — it serves dual purpose as both Opencode search grounding and agentic page reader.

| Variable | Required | Format / Example | Default | Notes |
|---|---|---|---|---|
| `TAVILY_API_KEYS` | ✅ | `tvly-key1,tvly-key2` | — | Comma-separated Tavily Search API keys. Used by search/research services; quick-search grounding depends on the active provider. Credit usage is tracked by the application. |
| `WEATHER_API_KEY` | No | `abc123...` | `""` | API key for WeatherAPI.com. Powers intent-direct weather query handler — weather queries intercepted before reaching the LLM. Without this key they fall through to the LLM. |
| `EXCHANGE_RATE_API_KEY` | No | `abc123...` | `""` | API key for ExchangeRate-API.com. Powers intent-direct currency conversion queries. Without this key currency queries fall through to the LLM. |

---

### 🧠 Agentic Research Engine

| Variable | Required | Format / Example | Default | Notes |
|---|---|---|---|---|
| `AGENTIC_MODEL` | ⚙️ | `gemini-2.5-flash` | `""` (uses `RESEARCH_MODEL`) | Overrides the LLM used inside the agentic research loop. Set to a more capable model (e.g. `gemini-2.5-flash`) for better research quality at higher cost. If empty, falls back to `RESEARCH_MODEL`. |
| `AGENTIC_MAX_ITERATIONS` | ⚙️ | `5`—`15` | `5` | Maximum research loop cycles before the agent is forced to synthesize an answer. Each iteration = one round of query → search → read → reflect. Higher = deeper research, higher API cost. |
| `AGENTIC_MAX_PAGES` | ⚙️ | `3`—`10` | `3` | Maximum web pages the agent reads per iteration. Each page consumes Jina/Tavily credits and LLM tokens. |
| `AGENTIC_MAX_TOKENS` | ⚙️ | `100000`—`500000` | `100000` | Hard token budget cap for the entire agentic session. The loop terminates if accumulated prompt + completion tokens exceed this value. Prevents runaway sessions on complex queries. |
| `AGENTIC_TIMEOUT_SECONDS` | ⚙️ | `90`—`300` | `90` | Wall-clock time limit for the entire agentic session. If the loop doesn't finish within this window, a partial result is returned. Increase to `180`+ on powerful VPS for deeper research. |
| `AGENTIC_PAGE_CONTENT_LIMIT` | ⚙️ | `4096`—`16384` | `8192` | Maximum characters extracted from each web page before truncation. Higher = more context per page, more LLM tokens consumed. |
| `ADAPTIVE_THINKING_ENABLED` | ⚙️ | `true` / `false` | `true` | Enables the automatic `thinking_level` selector (14-rule heuristic). When `true`, simple greetings get `low` depth and complex code/research queries get `high`. User's manual `/thinking` setting always overrides this. |

---

### 📊 Rate Limits & Concurrency

| Variable | Required | Format / Example | Default | Notes |
|---|---|---|---|---|
| `DAILY_LIMITS` | ⚙️ | JSON model-to-limit map or comma-separated `model:limit` entries | Model-specific map in `app/config.py`; generic fallback `19` | Per-user daily request caps by model name. Requests beyond the limit receive a "quota exceeded" reply. Tracked in the DB with reset at midnight UTC. The TTS model `gemini-2.5-flash-preview-tts` can be included with a separate limit. |
| `MAX_CONCURRENT_HEAVY_REQUESTS` | ⚙️ | `4`—`32` | `4` | Global asyncio semaphore limiting simultaneously active LLM/TTS requests. Backed by Redis for multi-replica safety. Tune against measured load, provider quota and memory limits. |
| `MAX_CONCURRENT_ULTRA_HEAVY_REQUESTS` | ⚙️ | `1`—`8` | `1` | Separate semaphore for agentic research (`??`) sessions. These are memory-intensive due to iterative context accumulation. Tune against measured resource use. |
| `LRU_STATE_CACHE_SIZE` | ⚙️ | `1000`—`50000` | `1000` | Maximum number of `UserState` objects held in the in-process LRU cache. Tune from measured state size and cache misses. |
| `DB_POOL_MIN_SIZE` | ⚙️ | `2`—`10` | `2` | See Database section. |
| `DB_POOL_MAX_SIZE` | ⚙️ | `10`—`50` | `10` | See Database section. |

---

### 🎨 Image Generation

| Variable | Required | Format / Example | Default | Notes |
|---|---|---|---|---|
| `IMAGE_MODELS` | ⚙️ | Comma-separated IDs/aliases | `flux,zimage,gptimage-1-5,gptimage,qwen-image,wan-image,klein` | Configured canvas/inline selections. Daily admin additionally loads the live image catalog; saved selections are retained if unavailable. |
| `DEFAULT_IMAGE_MODEL` | ⚙️ | `zimage` | `zimage` | Which Pollinations model is pre-selected by default in the Canvas keyboard. |
| `POLLINATIONS_API_KEY` | ⚙️ | `sk_...` | — | Required for all Pollinations generation. Keep server-side; manage keys and Pollen budget at [enter.pollinations.ai](https://enter.pollinations.ai/keys). Runtime `/keys` overrides remain supported. |
| `IMAGE_GEN_DAILY_LIMIT` | ⚙️ | `10`—`100` | `10` | Per-user daily cap for Gemini native image generations via Google API. Counted separately from Pollinations. |
| `IMAGE_GEN_RPD_PER_KEY` | ⚙️ | `25` | `25` | Requests-per-day budget per Gemini API key for native image generation. This is a local budget, not a provider entitlement. Tracked in Redis with in-memory fallback. Prevents image quota from consuming keys needed for LLM chat. |
| `IMAGE_GEN_TIMEOUT` | ⚙️ | `30.0`—`120.0` | `60.0` | Max seconds to wait for a Gemini native image API response before timing out and rotating to the next key. |
| `IMAGE_GEN_MAX_RETRIES` | ⚙️ | `1`—`5` | `3` | Number of Gemini key rotation attempts on quota/error before failing image generation entirely. |

---

### 🔊 Voice / TTS (ElevenLabs & Gemini)

| Variable | Required | Format / Example | Default | Notes |
|---|---|---|---|---|
| `ELEVENLABS_API_KEYS` | ⚙️ | `sk_abc,sk_xyz` | `[]` | Comma-separated ElevenLabs API keys used for outbound voice synthesis. Load-balanced with round-robin rotation. If empty, the system falls back exclusively to Gemini REST TTS (`gemini-2.5-flash-preview-tts`). Free ElevenLabs tier gives ~10k chars/month per key. |
| `ELEVENLABS_VOICE_ID` | ⚙️ | `XB0fDUnXU5powFXDhCwa` | `XB0fDUnXU5powFXDhCwa` | ElevenLabs Voice ID to use for synthesis. Default is Charlotte (conversational, English/Russian). Browse voices at [elevenlabs.io/voice-library](https://elevenlabs.io/voice-library). |

---

### Logging & Observability

The default is one correlated UTF-8 NDJSON stream with structured exceptions,
request/trace/job IDs, release metadata and bounded non-blocking output. Every
actually selected provider key is represented by its last four characters plus
a fingerprint on both request and terminal/error events; full credentials are
always scrubbed. Protected operational logs default to bounded scrubbed message
text (`LOG_CONTENT_MODE=full`, up to 2048 characters) plus size/fingerprint
metadata. Operators can explicitly switch to `metadata`, while `preview` remains
a selector-scoped diagnostic mode. Message-bearing rows are restricted evidence,
not CI/public/alert payloads; `DEBUG` never relaxes credential rules.

Use canonical `LOG_FORMAT=json|text`; `STRUCTURED_LOGGING` and `LOG_PRETTY` are
compatibility aliases. Production deploys private Grafana/Loki/Alloy search with
100-row/15-minute browser defaults and a 500-row server ceiling; access is through
an SSH tunnel. See the [operator/agent runbook](docs/logging.md),
[log viewer runbook](ops/observability/README.md), [event catalog](docs/log-events.md),
and [dated audit](docs/logging-audit-2026-09-10.md).

### 🧪 Testing Only

| Variable | Required | Format / Example | Default | Notes |
|---|---|---|---|---|
| `TEST_DATABASE_URL` | ⚙️ | `postgresql://user:pass@localhost:5432/test_db` | — | Postgres connection string used **exclusively** by `pytest -m integration`. Must point to a clean, separate database — integration tests run destructive DDL and DML. Never set this to the production DB. |

## Run

**Local Python (dev, long-polling):**

```bash
uv run --locked --env-file .env python bot.py
```

**Docker Compose (legacy local, single container):**

```bash
docker compose -f docker-compose.yml up -d
```

**Production VPS (application containers plus private log-search stack via GitHub Actions CI/CD):**

The canonical deployment is automated by `.github/workflows/deploy.yml`. It starts only after a successful completed `CI` run for `vps_testai`, checks out `workflow_run.head_sha`, builds that exact SHA-tagged image in GHCR, and then SSH-deploys 3 containers:

1. **`tg-api`** — Local Telegram Bot API Server (`aiogram/telegram-bot-api:latest`). Requires `TELEGRAM_API_ID` and `TELEGRAM_API_HASH` from [my.telegram.org](https://my.telegram.org). Before starting or reusing the local API server, each deployment runs `scripts/release_cloud_bot_api.py`; the script safely releases any cloud Bot API session without relying on a stale host marker.
2. **`tg-bot`** — The Python bot container. Connected to `tg-api` via `TELEGRAM_LOCAL_SERVER_URL=http://tg-api:8081/bot`. Mounts the shared volume `tg-api-data` at `/var/lib/telegram-bot-api` for zero-copy file access.
3. **`tg-media-cleanup`** — Alpine cron sidecar. Runs `chmod -R g+rX` every 60s (fixes UID 101 permission conflicts) and `find -mtime +7 -delete` every 24h (prevents disk exhaustion from cached media).

The application containers share the `tg-net` Docker bridge network and
`TZ=Europe/Kyiv`. The same successful deployment starts an independent,
resource-bounded Grafana/Loki/Alloy/Docker-proxy project before replacing
`tg-bot`; Grafana listens only on VPS `127.0.0.1:3000`.

If `VERTEX_LIVE_SERVICE_ACCOUNT_JSON` is configured in GitHub Actions secrets, the deploy script also writes that service-account JSON to the VPS, mounts it into `tg-bot` as `/run/secrets/vertex-live-sa.json`, and exports `GOOGLE_APPLICATION_CREDENTIALS=/run/secrets/vertex-live-sa.json`. The mounted file must remain readable by the non-root bot process; the workflow now prepares it with read permissions suitable for the container user instead of root-only mode.

For a manual deployment, adapt the checked-in workflow for the target host and
verified image SHA. The workflow is the complete reference for credentials,
migration ordering, health gates, media retention and eligible rollback; a short
`docker run` example is not equivalent.

## Schema Management

All database DDL is managed via **numbered SQL migration files** in `scripts/migrations/`.

| Component | Role |
|---|---|
| `scripts/migrations/000_init_schema.sql` | Initial bootstrap only; later migrations are required for the current schema |
| `scripts/migrations/001-019_*.sql` | Incremental schema changes (ALTER, indexes, RLS, triggers, cleanup) |
| `scripts/migrations/020_add_trgm_hybrid_search.sql` | Enables `pg_trgm` extension + GIN index for hybrid keyword+semantic memory search |
| `scripts/migrations/024_upgrade_gemini_v2_768.sql` | Migrates embeddings to `halfvec(768)` (gemini-embedding-2-preview) |
| `scripts/migrations/025_add_temporal_graph_edges.sql` | Adds `updated_at` + unique constraint to `memory_edges` for temporal upserts |
| `scripts/migrations/026_add_core_persona_edges.sql` | Adds `is_core BOOLEAN` + partial index to `memory_edges` for Core Persona Protection |
| `scripts/migrations/026b_add_predicate_embedding.sql` | Adds `predicate_embedding halfvec(768)` + HNSW index for Semantic Edge Deduplication |
| `scripts/migrations/027_add_edge_provenance.sql` | Adds `source_memory_ids BIGINT[]` + GIN index to `memory_edges` for HippoRAG 2 Dual-Node provenance |
| `scripts/migrations/032_add_wing_room_taxonomy.sql` | Adds `wing`, `room`, `hall_type` to `long_term_memory` and `wing`, `room` to `memory_nodes` with B-tree + partial HNSW indexes |
| `scripts/migrations/033_add_role_diaries.sql` | Adds `role_diaries JSONB DEFAULT '{}'` to `user_state` for MemPalace persistent role diaries |
| `scripts/migrations/034_global_settings.sql` | Creates `global_settings` key-value table for runtime configuration; seeds `inline_thinking_level` default |
| `scripts/migrations/018_add_missing_table_definitions.sql` | Backfill migration for databases that applied `000` without all tables |
| `scripts/migrate.py` | Standalone runner with strict manifest validation; deployment applies pending versions before starting the candidate. |
| `app/db/migrations.py` | Internal migration framework. Stops before legacy DDL when any numbered migration fails. |
| `app/db/schema.py` | Fail-closed startup validation — verifies all expected tables and critical runtime columns after migrations |
| `app/db/rls.py` | Row Level Security policy management |
| `app/db/seed.py` | Initial data seeding (admin user, API keys, indexes) |

**Workflow:**
- **Deploy-time:** `scripts/migrate.py` runs in an ephemeral container. If any migration fails, deployment aborts.
- **CI:** apply the complete migration chain to clean pgvector PostgreSQL, rerun it as a no-op, then require `--check` to report no pending migration versions. This is not a checksum or full-schema drift audit.
- **Startup-time:** `init_db()` -> `run_migrations()` -> `validate_schema()` -> `setup_row_level_security()` -> seed. Any numbered migration, schema, or RLS failure aborts startup.

**Diagnostic caveat:** even `scripts/migrate.py --check` and `--status` may create the migration-tracking table. They use `DATABASE_URL`, not `TEST_DATABASE_URL`; never run them against production as casual read-only checks.

**Adding new tables:** Create a new numbered `.sql` file in `scripts/migrations/`, add the table name to `EXPECTED_TABLES` in `app/db/schema.py`, and add RLS configuration to `app/db/rls.py` if needed.

## Long-Term Memory Architecture

LTM is implemented in `app/repos/memory*.py`, not `app/memory_manager.py`.
It combines pgvector `halfvec(768)`, hybrid text/vector retrieval, graph traversal
and tiered context. Current model names, thresholds and budgets are defined in
`app/repos/memory_config.py` and the relevant retrieval/handler code, not fixed
latency or recall-quality guarantees.

Private-memory work carries a durable consent epoch and renewable provider lease.
Disabling LTM or erasing an account invalidates stale work. Group messages are not
implicitly captured into private LTM. Retrieval treats remembered content as data,
not trusted instructions.

Extraction and consolidation prepare provider results outside the write transaction,
then share a caller-owned, tenant-bound graph writer. Durable source memories,
nodes/edges, normalized `memory_edge_sources` and compatibility snapshots are
committed consistently. Cleanup follows live provenance so unsupported edges do
not remain usable. See [ADR 0002](docs/adr/0002-provenance-safe-memory-graph-writes.md).

Use `/memory`, `/clearmemory`, `/settings`, `/mydata` and the confirmed private-chat
`/deleteme` flow for user controls. Do not substitute ad-hoc SQL deletion for the
repository workflows or claim these controls establish legal compliance.
Exports and third-party published copies are outside automatic local erasure.

## Scripts

| Command | Purpose |
| --- | --- |
| `uv run --locked ruff check .` | Run the repository-wide lint rules. |
| `uv run --locked ruff format --check .` | Enforce the same formatting gate used by CI. |
| `uv run --locked mypy app bot.py` | Type-check the production Python surface. |
| `uv run --locked python scripts/check_encoding.py` | Detect UTF-8 corruption in protected documentation. |
| `uv export --locked --no-dev --output-file production-requirements.txt` | Export the exact production graph for external audit tools. |

## Dependency maintenance

`pyproject.toml` is the only manually edited dependency manifest and `uv.lock` is the committed exact graph. Use the pinned tool version for every lock operation:

```bash
python -m pip install "uv==0.12.6"
uv lock --check
uv sync --locked
uv run --locked python scripts/dependency_frontier.py audit
```

The `Dependency Frontier Audit` workflow wakes weekly but a fixed UTC epoch gate permits scheduled resolution only every 14 days; a manual dispatch runs immediately. It resolves the policy-constrained and unconstrained stable frontiers twice for Linux production and Windows development with a seven-day release cooldown. Its report is discovery evidence, not proof of compatibility. Dependabot groups post-1.0 patch/minor updates, while major and current `0.x` updates remain separate and are never auto-merged.

The manual `Dependency Live Canary` workflow accepts a same-repository dependency PR that changes only `pyproject.toml` and `uv.lock`. Before the protected job is entered, an unprivileged job rejects non-PyPI/direct sources, disables source builds, installs both locked graphs, and verifies exact environment parity. Both checkouts discard persisted Git credentials. The protected job repeats the wheel-only locked installation without secrets; dedicated credentials exist only in the probe step. The `dependency-canary` GitHub Environment must require a reviewer and provide dedicated, low-quota credentials:

| Environment entry | Purpose |
| --- | --- |
| `CANARY_TELEGRAM_BOT_TOKEN` | Dedicated bot that can access only the canary chat. |
| `CANARY_TELEGRAM_CHAT_ID` | Dedicated chat where one silent tagged message is sent and deleted. |
| `CANARY_GEMINI_API_KEY` | Revocable, low-quota Gemini canary key. |
| `CANARY_TAVILY_API_KEY` | Revocable, low-quota Tavily canary key. |
| `CANARY_GEMINI_MODEL` | Optional environment variable; defaults to `gemini-2.5-flash`. |

Baseline and candidate probes run in one approved window. Missing credentials, cleanup failure, baseline failure, transient provider ambiguity, cancellation, or any skipped comparison cannot publish a passing candidate status.

The dedicated canary is optional infrastructure, not a substitute for production verification. If no separate canary bot exists, do not treat that gate as passed or reuse the production bot token in it. For a deliberate production dependency trial, keep the dependency changes in one reviewed PR so CI and the running bot exercise the exact combined graph. The deploy workflow then:

1. serializes production transitions without cancelling an in-flight deploy;
2. proves that the successful CI commit belongs to exactly one merged same-repository PR;
3. allows automatic rollback only when both dependency manifests and an explicit set of non-runtime review files changed;
4. preserves the current `tg-bot` container and independently verifies that its image SHA equals the PR base SHA;
5. starts the candidate, checks `/health`, runs enabled natal maintenance, and only then deletes the preserved container;
6. restores and health-checks the previous container after an eligible candidate failure while leaving the deployment failed for investigation.

This rollback protects startup and health-gated failures. It cannot detect a semantically wrong provider response after `/health` succeeds, and it deliberately refuses automatic rollback when code or migrations changed. After deploying a frontier trial, exercise the real bot during a controlled low-traffic window:

- ordinary text in both streaming and non-streaming paths;
- provider fallback plus agentic/web search;
- photo and document understanding;
- voice transcription and TTS;
- `/draw` image generation and `/live` audio when enabled;
- memory write/read, a Redis-backed callback or game action, and an admin/background test send;
- structured logs, provider error rates, duplicate replies, stuck streams, and dropped updates.

Any functional regression, schema/type error from a provider, unexplained error-rate increase, duplicate delivery, or stalled response rejects the candidate even when all automated checks passed. Keep the PR under observation for 24–48 hours; a post-health functional failure requires an explicit redeploy of the recorded base SHA because the workflow has no reliable signal for automatic application-level rollback.

## API / Events / Contracts

**Telegram Commands:**

- **Public command catalog** (the same catalog powers Telegram's menu and `/help`):
  - **Start and chat:** `/start`, `/help`, `/newchat`.
  - **Personalization:** `/model`, `/roles`, `/setprompt`, `/thinking`, `/settings`.
  - **Search and briefings:** `/res` toggles web search; `/subscribe` and `/unsubscribe` manage the daily intelligence briefing checked by the hourly scheduler.
  - **Documents, images, and voice:** `/documents`, `/draw`, `/live`. Image-generation aliases `/img`, `/image`, and `/generate` remain accepted but are not duplicated in the public menu.
  - **Conversation history:** `/save`, `/conversations`, `/switch`, `/rename`, `/delete`, `/export`, `/stats`.
  - **Memory and privacy:** `/memory`, `/clearmemory`, `/mydata`, `/deleteme`. Private-data commands require a direct chat; irreversible deletion requires explicit confirmation.
  - **Games and reminders:** `/games`, `/dailycroc`, `/daily2048`, `/trivia`, `/remind`.
  - **Tarot and astrology:** `/tarot`, `/natal`, `/horoscope_settings`, `/horoscope_stop`.

Administrative and developer commands are deliberately excluded from `app/bot_commands.py` and Telegram's public slash-command menu.

- **Admin Commands (Requires `ADMIN_ID`):**
  - `/admin` — Central administration hub.
  - `/listmodels`, `/listusers` — List configured models and registered users.
  - `/adduser`, `/deluser` — Manual user management.
  - `/metrics`, `/rolemetrics` — Detailed system and role-based usage telemetry.
  - `/cachestats`, `/queuestats`, `/docstats`, `/groupstats` — Performance monitoring for different subsystems.
  - `/clearcache`, `/clearoldmetrics`, `/clearolddocs` — System maintenance and cleanup.
  - `/updatetavilykeys`, `/checktavilykeys` — Hot-swap and verify search API keys.
  - `/checkgeminikeys` — Async parallel health check of all Gemini API keys against the Google API.
  - `/set_inline_thinking <level>` — Set inline generation `thinking_level` at runtime (stored in `global_settings` DB table, no restart required). Valid: `minimal`, `low`, `medium`, `high`.
  - `/set_provider <name>` — Switch the primary LLM provider at runtime without restart. Valid: `opencode`, `gemini`, `openrouter`, `freetheai`. Stored in `global_settings`; individual generation paths still apply their capability and key checks.
  - `/models` — Runtime model management wizard for Gemini, Opencode, OpenRouter, and FreeTheAI. Add or remove individual selectable models, inspect whether env or an admin override is active, and reset by deleting the override and restoring the current env/Secret list. New Gemini additions are checked for `generateContent` support before persistence.
  - `/registergroup` — Authorize the bot for use in a specific Telegram group.
  - `/reloadconfig` — Trigger an immediate hot-reload of the environment configuration.

**Web Dashboard (Quart HTTP Routes):**

- `GET /`, `GET /login`, `POST /login`, `GET /logout` — UI interface (requires `ADMIN_SECRET` authentication and uses Cookie Sessions).
- `GET /health` — Robust unauthenticated API health check.
- `GET /metrics` — Exposes Prometheus telemetry text (uptime, errors, usage).
- `GET /api/dashboard` — Aggregated batch endpoint (replaces 8 individual fetches with 1 RTT). Auth required.
- `GET /api/overview`, `/api/keys`, `/api/errors`, `/api/cache`, `/api/queue`, `/api/database`, `/api/circuit-breakers`, `/api/memory` — Individual JSON data endpoints for dashboard charts (requires auth cookie or `X-Auth-Token` header).
- `GET /api/key-health` — Per-key health diagnostics: status, failure count, suspension info. Auth required.
- `GET /api/events` — Server-Sent Events stream (5s interval) for real-time CPU, memory, DB, queue, and request metrics.

## Main User Flows

- Start with `/help` for the current categorized RU/EN public catalog.
- Standard chat combines selected model, history and consent-gated memory.
- `?` requests quick search; `??` requests bounded agentic research.
- Inline mode requires BotFather inline mode and chosen-result feedback; use the
  deployed bot's username, not a hard-coded example bot identity.
- `/horoscope_settings` supports subscriptions and on-demand readings;
  `/horoscope_stop` stops scheduled delivery.
- The admin web dashboard requires configured authentication; `/admin_daily#croc`
  manages daily Crocodile and its shared ordinary/daily text-model setting.

## Troubleshooting

- **Conflict Error on Startup**: Usually signifies another bot instance is currently polling the Telegram API using the same Token. Requires closing duplicate instances if not using Webhooks.
- **`decryption_error` traces**: Usually caused by attempting to load the database on a new host without providing the exact prior `ADMIN_SECRET` (the code derives the Fernet key; the secret itself need not be base64).
- **Search features hanging**: Check `TAVILY_API_KEYS` exhaustively or verify the `circuit_breaker` state at `/api/circuit-breakers`.

## Operational Documentation Caveats

- The Dockerfile health start period is 40 seconds. Legacy Compose overrides it
  to 120 seconds only when that Compose file is used; it does not control the VPS workflow.
- The legacy Compose file exposes its published port differently from the
  loopback-only VPS workflow. Review network exposure before using it outside local development.
- The VPS deployment state, configured credentials and public provider catalogs
  must be verified separately; the repository does not prove current health.

## Architecture Decisions

Use the [architecture reference](docs/ARCHITECTURE.md) for current boundaries,
[ADR 0001](docs/adr/0001-single-owner-ai-response-delivery.md) for single-owner
delivery, and [ADR 0002](docs/adr/0002-provenance-safe-memory-graph-writes.md) for
transactional graph writes. [ROADMAP.md](ROADMAP.md) is future work, not implemented
behavior; dated specs and plans retain their original design context.

## Testing

See [CONTRIBUTING.md](CONTRIBUTING.md) for exact locked commands, test isolation and
CI gates. Unit/E2E checks exclude both the integration directory and integration
marker; integration tests also exist outside that directory. Use an explicitly
disposable PostgreSQL/Redis target for DB tests. Rollback fixtures do not make
arbitrary migration tests safe for production.

`pytest.ini` enables async auto mode, xdist and per-test timeout by default.
Historical test counts are not fresh validation evidence. For documentation-only
changes, check encoding, links, source accuracy and `git diff --check`.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for setup, validation, pull-request, and secret-handling guidance.

Minimum maintainer checks for behavior changes:

1. Create a descriptive PR.
2. Verify the relevant `pytest` checks pass.
3. Run `python -m ruff check .` and `python -m ruff format --check .`.
4. Run `python -m mypy app bot.py` for production-code changes.
5. Update user-facing docs when public behavior or deployment changes.

## License

MIT, see [LICENSE](LICENSE).
