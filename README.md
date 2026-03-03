# GemAI Bot v2 – Technical Documentation

![Python](https://img.shields.io/badge/Python-3.14+-blue.svg)
![Telegram](https://img.shields.io/badge/Telegram-Bot-blue.svg)
![Gemini](https://img.shields.io/badge/AI-Google%20Gemini-orange.svg)
![OpenRouter](https://img.shields.io/badge/AI-OpenRouter-purple.svg)
![License](https://img.shields.io/badge/License-MIT-green.svg)

**GemAI Bot v2** is an advanced, asynchronous Telegram bot designed to serve as a comprehensive AI assistant. It orchestrates multiple AI providers (Google Gemini, OpenRouter), performs real-time web research, maintains long-term memory, and analyzes complex documents.

The project is built with a **monolithic asyncio architecture**, integrating a high-performance Telegram bot with a lightweight Quart-based monitoring dashboard.

---

## 📑 Table of Contents

- [🎯 Project Goals](#-project-goals)
- [🏗 Architecture Overview](#-architecture-overview)
- [🧠 Backend Capabilities](#-backend-capabilities)
  - [Core Logic & Agentic Workflow](#core-logic--agentic-workflow)
  - [AI Provider Routing & Key Rotation](#ai-provider-routing--key-rotation)
  - [Document Processing](#document-processing)
- [🖥 Frontend (Monitoring Dashboard)](#-frontend-monitoring-dashboard)
- [🛠 Technical Stack](#-technical-stack)
- [🚀 Deployment & Infrastructure](#-deployment--infrastructure)
- [⚙️ Configuration](#%EF%B8%8F-configuration)
- [🧪 Testing](#-testing)
- [📝 Changelog](#-changelog)

---

## 🎯 Project Goals

1.  **Resilience**: Ensure 24/7 availability with robust error handling, self-healing database connections, and graceful shutdowns.
2.  **Scalability**: Bypass API rate limits through intelligent key rotation and multi-provider fallback strategies.
3.  **Versatility**: Go beyond text generation by integrating web search (Tavily), document understanding, and group chat management.
4.  **Observability**: Provide real-time insights into system health and resource usage via a web dashboard.

---

## 🏗 Architecture Overview

> 📖 **For the full module map, design patterns, and dependency graph, see [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md).**

The system runs as a single containerized application performing two parallel asyncio tasks:

1.  **Telegram Bot (`bot.py`)**:
    - Uses `python-telegram-bot` with **dual mode**: long-polling (default) or **webhook** (set `WEBHOOK_URL` env var).
    - Manages user interactions, message queues, and AI responses.
    - Handles "Agentic" workflows (Research, Q&A).
    - **Streaming responses** (`streaming.py`): Gemini `generate_content_stream` + debounced `edit_message_text` (1.2s, 80-char minimum). **Multi-message overflow**: auto-splits at natural break points when exceeding 4096 chars. **Retry with key rotation** (up to 3 keys) before non-streaming fallback. Falls back to non-streaming for OpenRouter.
    - **Smart model suggestions**: Regex heuristics classify message type (code/reasoning) and suggest upgrade to a more capable model. **No-downgrade policy** (v2.8.2+): only suggests models with higher capability tier.
2.  **Web Server (`app/web.py`)**:
    - A lightweight Quart + Hypercorn server (fully async-native).
    - Exposes Health Check (`/health`) and **Prometheus metrics** (`/metrics`) endpoints.
    - Serves a secure Monitoring Dashboard with cookie-session auth.

**Data Persistence**:

- **PostgreSQL**: Stores user preferences, chat history (short-term & long-term), API key usage statistics, and **long-term memory** (pgvector embeddings for semantic recall).
- **Redis** (Optional): Async `redis.asyncio.Redis` client for high-speed caching and temporary state management. Properly closed via `aclose()` on shutdown.

**Database Package** (`app/db/`):

- `schema.py` — All `CREATE TABLE` DDL statements.
- `migrations.py` — SQL file runner + legacy inline migrations.
- `rls.py` — Row Level Security configuration and policy templates.
- `seed.py` — Initial data seeding (admin user, API keys, indexes).
- `database.py` acts as a backward-compatible facade, re-exporting all public functions.

---

## 🧠 Backend Capabilities

### Core Logic & Agentic Workflow

Located in `app/handlers/` as modular sub-handlers (`ai_core.py`, `ai_chat.py`, `ai_search.py`, `ai_photo.py`, `ai_document.py`), with `agent.py` as a thin re-export facade. Capabilities:

- **Deep Dive Research**:
  1.  Analyzes user query.
  2.  Uses **Tavily API** to search the web.
  3.  **URL Selection Agent**: Uses AI to score and select the most relevant sources.
  4.  **Content Scraper**: Fetches content from selected URLs.
  5.  **Synthesis Agent**: Generates a comprehensive answer with citations based on the scraped context (up to 30k+ tokens).
- **Context-Aware Chat**:
  - Maintains conversation history in PostgreSQL.
  - Injects system instructions and user preferences into every prompt.
  - Supports "New Topic" to reset context while keeping long-term memory.
  - **Context Summarization** (v2.6.9+): Two-tier summarization system for long conversations:
    - **Local tier**: Snippet-based truncation for conversations under 30K dropped tokens.
    - **LLM tier**: Asynchronous refine-chain summarization (chunked 10K × 6 max) for conversations above 30K dropped tokens.
    - 128K token budget with 12K response reserve and 4K summary budget.
    - Summaries persisted in `chats.context_summary` column across sessions.
    - Tier-specific metrics (triggered, LLM/local counts, tokens saved) surfaced in the dashboard.
- **Thinking Level Control** (v2.7.2+): Per-user configurable reasoning depth via `/thinking` command (`off`/`low`/`medium`/`high`/`auto`). Auto-detects model family: `thinkingBudget` for Gemini 2.5, `thinkingLevel` for Gemini 3. OpenRouter models unaffected.
- **pgvector long-term memory** (`repos/memory.py`): `gemini-embedding-001` (3072-dim `halfvec`), HNSW index, cosine similarity search, `task_type`-aware asymmetric embedding, 500/user limit, 90-day TTL. Semantically recalled during context assembly, stored after each exchange.
- **Smart model selection** (`model_selector.py`): Regex heuristics classify messages (code/reasoning) → non-intrusive inline button suggestions for upgrades only. **No-downgrade policy**: `_get_tier()` ranks models (`lite=1 < flash=2 < 2.5-flash=3 < pro=4`); suggestions only when `tier(suggested) > tier(current)`. `switch_model:` callback handler for one-tap switching.
- **Group Chat Mode**: Specialized handlers for admin-only or reply-only interactions in groups.
- **Customizable Roles**: Browse system role catalog, generate AI roles, or write custom roles manually — all manageable via an AIDA-structured roles hub.

### AI Provider Routing & Key Rotation

The bot implements a sophisticated "Smart Router" for AI requests:

- **Multi-Provider Support**: Seamlessly switches between **Google Gemini** (Flash, Pro) and **OpenRouter** (GPT-4, Claude 3, etc.).
- **ProviderRouter** (v2.6+):
  - Unified AI call path — both `ProviderRouter` and `AgentRequestUseCase` route through Provider classes (`GeminiProvider` / `OpenRouterProvider`).
  - **DB-backed `KeyStatusManager`** (v2.7.0+): Persistent per-model key health tracking with error-category-aware cooldowns. Replaces the in-memory `KeyHealth` class.
  - **Two-tier key selection**: SQL prioritizes active keys first, then probes cooldown-expired keys for recovery.
  - **Error-aware suspension**: `API_KEY_INVALID` → 24h, `quota` → midnight PT, `rate_limit` → 60s, transient errors → no suspension. Exponential backoff on repeated failures (capped at 7 days).
  - **Multimodal auto-detection**: Detects PIL Image / bytes in history and forces Gemini automatically.
  - **Per-user rate limiting**: Consolidated `RateLimiter` (async) + `SyncRateLimiter` (sync, login) with periodic cleanup, stats, and admin reset.
  - **`DailyKeyManager`**: Generic key rotation engine shared by Gemini and OpenRouter, parameterized by table names.
  - **OpenRouter exclusion fix** (v2.7.1): `get_available_openrouter_key()` now properly forwards `excluded_hashes` to the two-tier SQL query, ensuring failed keys are rotated out.
- **Key Rotation System**:
  - Rotates through a pool of API keys to avoid rate limits.
  - Tracks usage stats (requests/tokens) per key.
  - **Auto-Fallback**: If a key fails (Quota Exceeded) or a provider is down, it automatically tries the next key or switches to a backup model.
- **Stage Indicators** (v2.5+): Animated processing stages (🤔→💭→✅) keep users informed during multi-step AI operations.
- **Heartbeat Feedback** (v2.6.4+): 3-stage progressive updates (15s → 30s → 50s) reassure users during long-running requests.
- **Manual Role Persistence** (v2.6.4+): In-progress manual role creation survives bot restarts via DB-backed `user_state` columns.
- **Origin-Aware Role Buttons** (v2.7.0+): "🎭 Выбрать роль ИИ" under AI responses sends the roles menu as a **new message** (preserving the response); from menus it edits in-place. Controlled via `callback_data` suffix (`open_roles:from_response` vs `open_roles`).

### Document Processing

- **Formats**: Supports PDF (`pypdf`), DOCX (`python-docx`), and txt/md.
- **Multimodal Analysis**: Can "see" images via Gemini's vision capabilities.
- **RAG-lite**: Uploaded documents are parsed, truncated to fits context limits (~30k chars), and injected into the conversation for Q&A.

### Performance Optimizations (v2.1+)

- **Non-Blocking Document I/O**: Asynchronous file processing and streaming chunked hashing algorithms completely avoid Event Loop blocking and prevent RAM starvation (OOM) on memory-constrained 256-512MB hosting environments.
- **Batched Metrics DB Inserts**: Background batching via `asyncio.Queue` of monitoring metrics into PostgreSQL, replacing expensive synchronous tracking and dictionary iterations.
- **Hybrid Database Schema** (PostgreSQL/Supabase)
  - `users`, `api_keys`, `conversations`, `model_configuration` tables
  - `active_chat_messages` table for O(1) history insert performance
  - Database-side JSON ETL handling to bypass Python GIL limits
  - Prepared statements for high-throughput concurrency
  - RLS Denormalization indexing (`owner_user_id`) to optimize security policies
  - Transaction-local RLS context (`set_config(..., true)`) preventing context leaks
- **Repository Layer** (`app/repos/`): Canonical location for domain logic:
  - `keys.py` — API key rotation (DailyKeyManager, MonthlyKeyManager, KeyStatusManager)
  - `users.py` — Auth, user state, feedback
  - `chats.py` — Chat state management
  - `conversations.py` — Saved conversations CRUD
  - `roles.py` — Custom user roles CRUD (7 functions)
  - `user_stats.py` — Per-user daily/weekly statistics (3 functions)
  - `admin.py` — Admin-only operations: user management, metrics cleanup, key inspection (6 functions)
  - `metrics_repo.py`, `analytics.py` — System metrics and analytics queries
- **In-Memory Caching** (TTLCache / Redis)
  - Key status, user authorization, and dynamic AI model limits are aggressively cached to reduce DB I/O.
- **Scoped DB Transactions**: Optimized database pooling (`max_size=10`) with `asyncio.Semaphore` and scope-limited transactions to prevent connection starvation without hitting provider DB connection limits.
- **Micro-GC Pauses**: Fine-tuned `gc.collect(1)` macro-invocations preventing full stop-the-world application pauses during heavy traffic spikes.
- **Robust TCP Pooling**: Scaled (yet strictly constrained) HTTPX connection pools (50 concurrent external HTTP connections) with Circuit Breaker tracking for external AI Providers to defend against socket exhaustion.

### Resilience & Circuit Breakers (v2.6.6+)

- **Gemini/OpenRouter**: DB-backed per-model key health tracking (`key_model_status` table) with error-category-aware cooldowns and automatic recovery probing.
- **Tavily API**: Circuit breaker in `search_services.py` — trips after consecutive failures, auto-recovers.
- **Telegram API**: Lazy circuit breaker in `messaging.py` — prevents flooding Telegram servers during outages.
- **Response time tracking**: `MetricsMiddleware` wired into `handle_request` and `@track_metrics` decorator on all search handlers for per-operation latency dashboards.

### Security Hardening (v2.6.5+)

- **Nonce-based CSP**: Per-request `secrets.token_urlsafe(16)` nonce replaces `'unsafe-inline'` in `script-src` and `style-src` directives.
- **Brute-force protection**: `SyncRateLimiter` on `/login` (5 attempts per 5 min → 429), with periodic eviction of stale IPs.
- **`DecryptionError` handling**: API key decryption failures produce user-friendly messages instead of raw Python tracebacks.
- **Error response sanitization**: API endpoints return generic `"internal_error"` instead of exception class names.
- **SQL injection prevention**: Regex validation for dynamic table names in `DailyKeyManager`.

### Planned Improvements

- _(Planned)_ **Database Architecture V3**: Migration from Monolithic JSON TEXT arrays to normalized JSONB/Relational models, removal of block AST caching constraints, and RLS constraint denormalization to eliminate high-concurrency CPU limits.

---

## 🖥 Frontend (Monitoring Dashboard)

While primarily a Telegram bot, the project includes a web frontend for administration and monitoring.

- **Technology**: Quart (async Flask-compatible), Jinja2 Templates (`app/templates`), Vanilla CSS (`app/static`).
- **Endpoints**:
  - `/`: Visual dashboard showing system status (CPU, RAM, Uptime), performance metrics, and context summarization stats.
  - `/health`: JSON endpoint for docker healthchecks.
  - `/api/overview`: **(Secured)** System health, performance metrics, and summarization tier stats.
  - `/api/keys`: **(Secured)** Detailed view of API key usage, active keys, and remaining quotas per model.
- **Security**: Protected by `ADMIN_SECRET` with cookie-session auth, CSRF tokens, IP-based brute-force protection (5 attempts → 429), and nonce-based Content-Security-Policy.

---

## 🛠 Technical Stack

| Category           | Technology                      | Purpose                          |
| :----------------- | :------------------------------ | :------------------------------- |
| **Language**       | Python 3.14+                    | Core runtime                     |
| **Bot Framework**  | `python-telegram-bot` (v22+)    | Async Telegram API wrapper       |
| **Web Server**     | Quart + Hypercorn               | Async-native web server          |
| **Database**       | `asyncpg` (PostgreSQL)          | High-performance async DB driver |
| **AI SDKs**        | `google-genai`, OpenAI (compat) | Interaction with LLMs            |
| **Search**         | `tavily-python`                 | AI-optimized web search          |
| **Doc Processing** | `pypdf`, `python-docx`          | Text extraction from files       |
| **Linting**        | Ruff (`pyproject.toml`)         | F, B, I, UP, RUF rules enforced  |
| **Container**      | Docker                          | Standardization and deployment   |

---

## 🚀 Deployment & Infrastructure

The project is "Cloud Native" ready, specifically optimized for PaaS providers like **Northflank** and **Render**.

### Docker

- **Base Image**: `python:3.14-slim` (Lightweight, secure, fast).
- **Security**: Runs as a non-root `app` user.
- **Entrypoint**: Custom `start.sh` script to handle environment setup.
- **Healthcheck**: Built-in curl command pinging `localhost:10000/status`.

### Services (`docker-compose.yml`)

- **telegram-gemini-bot**: The main application service.
- Configured with `restart: unless-stopped` for resilience.
- Mounts `./data` for persistent storage (if not using a managed DB).

### Signal Handling

Implements graceful shutdown handling (SIGINT/SIGTERM) to ensure:

- Database connections are closed properly.
- Pending Telegram updates are dropped or processed.
- Web server unbinds ports immediately.

---

## ⚙️ Configuration

Configuration is managed via environment variables (supports `.env` file).

### Essential

```bash
TELEGRAM_BOT_TOKEN=123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11
DATABASE_URL=postgresql://user:pass@host:5432/dbname
ADMIN_ID=123456789
```

### AI Providers

```bash
# Comma-separated keys for rotation
GEMINI_API_KEYS=AIzaSy...,AIzaSy...
TAVILY_API_KEYS=tvly-xxxx
OPENROUTER_API_KEYS=sk-or-v1-...,sk-or-v1-...  # Optional
```

### System

```bash
PORT=10000              # Web server port (default: 10000)
ENABLE_WEB_SERVER=true  # Enable/Disable dashboard (default: true)
ADMIN_SECRET=...        # Secret for dashboard login & API key encryption
REDIS_URL=redis://...   # Optional — enables Redis caching layer
```

### Complete Environment Variable Reference

| Variable                         | Required | Default                       | Description                                                 |
| -------------------------------- | -------- | ----------------------------- | ----------------------------------------------------------- |
| `TELEGRAM_BOT_TOKEN`             | ✅       | —                             | Telegram Bot API token                                      |
| `DATABASE_URL`                   | ✅       | —                             | PostgreSQL connection string                                |
| `ADMIN_ID`                       | ✅       | —                             | Telegram user ID of the admin                               |
| `GEMINI_API_KEYS`                | ✅       | —                             | Comma-separated Google Gemini API keys                      |
| `TAVILY_API_KEYS`                | ✅       | —                             | Comma-separated Tavily search API keys                      |
| `OPENROUTER_API_KEYS`            | ❌       | `[]`                          | Comma-separated OpenRouter API keys                         |
| `ADMIN_SECRET`                   | ❌       | `None`                        | Dashboard login secret & API key encryption key             |
| `PORT`                           | ❌       | `10000`                       | Web server listening port                                   |
| `ENABLE_WEB_SERVER`              | ❌       | `true`                        | Enable/disable the monitoring dashboard                     |
| `REDIS_URL`                      | ❌       | `None`                        | Redis connection URL (enables multi-layer cache)            |
| `DEFAULT_MODEL`                  | ❌       | `gemini-flash-latest`         | Default Gemini model for chat                               |
| `QNA_MODEL`                      | ❌       | `gemini-2.5-flash-lite`       | Model for Q&A tasks                                         |
| `RESEARCH_MODEL`                 | ❌       | `gemini-2.5-pro`              | Model for deep research                                     |
| `URL_SELECTION_MODEL`            | ❌       | `gemini-flash-latest`         | Model for URL relevance scoring                             |
| `GEMINI_AVAILABLE_MODELS`        | ❌       | 4 default models              | Comma-separated list of available Gemini models             |
| `OPENROUTER_DEFAULT_MODEL`       | ❌       | `stepfun/step-3.5-flash:free` | Default OpenRouter model                                    |
| `OPENROUTER_QNA_MODEL`           | ❌       | `stepfun/step-3.5-flash:free` | OpenRouter Q&A model                                        |
| `OPENROUTER_RESEARCH_MODEL`      | ❌       | `stepfun/step-3.5-flash:free` | OpenRouter research model                                   |
| `OPENROUTER_URL_SELECTION_MODEL` | ❌       | `stepfun/step-3.5-flash:free` | OpenRouter URL selection model                              |
| `OPENROUTER_AVAILABLE_MODELS`    | ❌       | `[]`                          | Comma-separated available OpenRouter models                 |
| `DAILY_LIMITS`                   | ❌       | See `config.py`               | JSON or `model:limit,...` format for per-model daily limits |
| `USE_OPENROUTER`                 | ❌       | `false`                       | Force OpenRouter as default provider                        |

---

## 🧪 Testing

The project has a comprehensive test suite covering unit, integration, and performance validation.

### Running Tests

```bash
# Setup (install dev dependencies)
pip install -r requirements-dev.txt

# Full suite
python -m pytest tests/

# Single file
python -m pytest tests/test_keyboards.py --tb=short

# Verbose with traceback
python -m pytest tests/ -v --tb=long
```

### Suite Structure (619 tests, 1 skipped)

| Category           | Files                                                                                                                                                                                                         | What They Cover                                                                                                      |
| :----------------- | :------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ | :------------------------------------------------------------------------------------------------------------------- |
| **Core Logic**     | `test_ai_provider`, `test_provider_router`, `test_agent_optimization`, `test_errors`, `test_ai_chat`, `test_ai_search`, `test_ai_document`, `test_ai_photo`, `test_context_assembler`, `test_prompt_registry` | AI routing, key status management, fallback chains, error classification, AI handler coverage, context summarization |
| **Handlers**       | `test_callbacks`, `test_messages`, `test_commands`, `test_cmd_admin`, `test_cmd_conversations`, `test_menus`, `test_roles_menu`, `test_io_handlers`, `test_stage_indicators`                                  | Callback dispatch, request flow, commands, admin commands, conversation CRUD, menu rendering, role UI                |
| **Integration**    | `test_integration_flow`, `test_callback_responsiveness_scenario`                                                                                                                                              | End-to-end request flow (auth, rate limit, agent, error recovery), callback responsiveness                           |
| **Database**       | `test_database_tavily`, `test_perf_db_messages`, `test_document_cleanup_optimization`                                                                                                                         | Tavily key management, query optimization, cleanup                                                                   |
| **Infrastructure** | `test_circuit_breaker`, `test_cache_ttl`, `test_concurrency_hardening`                                                                                                                                        | Circuit breaker, TTL cache, race conditions                                                                          |
| **Security**       | `test_auth_headers`, `test_security_headers`, `test_web_security`, `test_document_security`, `test_decryption_error_handling`                                                                                 | Header enforcement, auth bypass prevention, CSP nonce, DecryptionError handling                                      |
| **Metrics**        | `test_metrics_integration`, `test_system_status`                                                                                                                                                              | Batched metric saves, system status data                                                                             |
| **Utilities**      | `test_formatting`, `test_keyboards`, `test_time_utils`, `test_image_utils`, `test_audit_fixes`                                                                                                                | Text formatting, keyboard builders, timezone math, audit regression tests                                            |

### Mock Isolation Rule

> **Critical**: Never assign `sys.modules["X"] = MagicMock()` at module top-level in test files. Always use `setup_module()` / `teardown_module()` with save/restore. See [CHANGELOG.md](CHANGELOG.md) §2.2.0 for detailed anti-pattern reference.

---

## 📝 Changelog

See [CHANGELOG.md](CHANGELOG.md) for a detailed history of changes.
