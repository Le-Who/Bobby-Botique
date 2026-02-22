# GemAI Bot v2 – Technical Documentation

![Python](https://img.shields.io/badge/Python-3.11+-blue.svg)
![Telegram](https://img.shields.io/badge/Telegram-Bot-blue.svg)
![Gemini](https://img.shields.io/badge/AI-Google%20Gemini-orange.svg)
![OpenRouter](https://img.shields.io/badge/AI-OpenRouter-purple.svg)
![License](https://img.shields.io/badge/License-MIT-green.svg)

**GemAI Bot v2** is an advanced, asynchronous Telegram bot designed to serve as a comprehensive AI assistant. It orchestrates multiple AI providers (Google Gemini, OpenRouter), performs real-time web research, maintains long-term memory, and analyzes complex documents.

The project is built with a **monolithic asyncio architecture**, integrating a high-performance Telegram bot with a lightweight Flask-based monitoring dashboard.

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

The system runs as a single containerized application performing two parallel asyncio tasks:

1.  **Telegram Bot (`bot.py`)**:
    - Uses `python-telegram-bot` for long-polling.
    - Manages user interactions, message queues, and AI responses.
    - Handles "Agentic" workflows (Research, Q&A).
2.  **Web Server (`app/web.py`)**:
    - A lightweight Flask + Hypercorn server.
    - Exposes Health Check endpoints for cloud platforms (Render/Northflank).
    - Serves a secure Monitoring Dashboard.

**Data Persistence**:

- **PostgreSQL**: Stores user preferences, chat history (short-term & long-term), and API key usage statistics.
- **Redis** (Optional): Used for high-speed caching and temporary state management.

---

## 🧠 Backend Capabilities

### Core Logic & Agentic Workflow

Located in `app/handlers/agent.py`, the intelligent core enables:

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
- **Group Chat Mode**: Specialized handlers for admin-only or reply-only interactions in groups.

### AI Provider Routing & Key Rotation

The bot implements a sophisticated "Smart Router" for AI requests:

- **Multi-Provider Support**: Seamlessly switches between **Google Gemini** (Flash, Pro) and **OpenRouter** (GPT-4, Claude 3, etc.).
- **Key Rotation System**:
  - Rotates through a pool of API keys to avoid rate limits.
  - Tracks usage stats (requests/tokens) per key.
  - **Auto-Fallback**: If a key fails (Quota Exceeded) or a provider is down, it automatically tries the next key or switches to a backup model (e.g., Gemini Pro -> Gemini Flash -> OpenRouter).

### Document Processing

- **Formats**: Supports PDF (`pypdf`), DOCX (`python-docx`), and txt/md.
- **Multimodal Analysis**: Can "see" images via Gemini's vision capabilities.
- **RAG-lite**: Uploaded documents are parsed, truncated to fits context limits (~30k chars), and injected into the conversation for Q&A.

### Performance Optimizations (v2.1+)

- **Non-Blocking Document I/O**: Asynchronous file processing and streaming chunked hashing algorithms completely avoid Event Loop blocking and prevent RAM starvation (OOM) on memory-constrained 256-512MB hosting environments.
- **Batched Metrics DB Inserts**: Background batching via `asyncio.Queue` of monitoring metrics into PostgreSQL, replacing expensive synchronous tracking and dictionary iterations.
- **Scoped DB Transactions**: Optimized database pooling (`max_size=10`) with `asyncio.Semaphore` and scope-limited transactions to prevent connection starvation without hitting provider DB connection limits.
- **GIL-Free Image Processing**: Progressive image down-scaling and offloading Pillow JPEG compression into an isolated `ProcessPoolExecutor`.
- **TTLCache & Lazy Eviction**: O(1) in-memory lookups utilizing `cachetools` and lazy cache eviction for web search and states, bypassing CPU-blocking dictionary iteration loops.
- **Micro-GC Pauses**: Fine-tuned `gc.collect(1)` macro-invocations preventing full stop-the-world application pauses during heavy traffic spikes.
- **Robust TCP Pooling**: Scaled (yet strictly constrained) HTTPX connection pools (50 concurrent external HTTP connections) with Circuit Breaker tracking for external AI Providers to defend against socket exhaustion.

---

## 🖥 Frontend (Monitoring Dashboard)

While primarily a Telegram bot, the project includes a web frontend for administration and monitoring.

- **Technology**: Flask, Jinja2 Templates (`app/templates`), Vanilla CSS (`app/static`).
- **Endpoints**:
  - `/`: Visual dashboard showing generic system status (CPU, RAM, Uptime).
  - `/health`: JSON endpoint for docker healthchecks.
  - `/keys`: **(Secured)** Detailed view of API key usage, active keys, and remaining quotas per model.
- **Security**: Protected by a shared secret (`ADMIN_SECRET` or Bot Token) to prevent unauthorized access to sensitive metrics.

---

## 🛠 Technical Stack

| Category           | Technology                      | Purpose                          |
| :----------------- | :------------------------------ | :------------------------------- |
| **Language**       | Python 3.11+                    | Core runtime                     |
| **Bot Framework**  | `python-telegram-bot` (v20+)    | Async Telegram API wrapper       |
| **Web Server**     | Flask + Hypercorn               | Async-compatible web server      |
| **Database**       | `asyncpg` (PostgreSQL)          | High-performance async DB driver |
| **AI SDKs**        | `google-genai`, OpenAI (compat) | Interaction with LLMs            |
| **Search**         | `tavily-python`                 | AI-optimized web search          |
| **Doc Processing** | `pypdf`, `python-docx`          | Text extraction from files       |
| **Container**      | Docker                          | Standardization and deployment   |

---

## 🚀 Deployment & Infrastructure

The project is "Cloud Native" ready, specifically optimized for PaaS providers like **Northflank** and **Render**.

### Docker

- **Base Image**: `python:3.11-slim` (Lightweight, secure).
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
OPENROUTER_API_KEYS=sk-or-v1-...,sk-or-v1-...
TAVILY_API_KEY=tvly-xxxx
```

### System

```bash
PORT=10000              # Web server port
ENABLE_WEB_SERVER=true  # Enable/Disable dashboard
LOG_LEVEL=INFO          # DEBUG/INFO/WARNING
```

---

## 🧪 Testing

The project has a comprehensive test suite covering unit, integration, and performance validation.

### Running Tests

```bash
# Full suite
python -m pytest tests/

# Single file
python -m pytest tests/test_keyboards.py --tb=short

# Verbose with traceback
python -m pytest tests/ -v --tb=long
```

### Suite Structure (194 tests)

| Category           | Files                                                                                       | What They Cover                                      |
| :----------------- | :------------------------------------------------------------------------------------------ | :--------------------------------------------------- |
| **Core Logic**     | `test_ai_provider`, `test_agent_optimization`, `test_errors`                                | AI routing, fallback chains, error handling          |
| **Handlers**       | `test_callbacks`, `test_menus`, `test_io_handlers`                                          | Telegram callback dispatch, menu rendering, file I/O |
| **Database**       | `test_database_tavily`, `test_perf_db_messages`, `test_document_cleanup_optimization`       | Tavily key management, query optimization, cleanup   |
| **Infrastructure** | `test_circuit_breaker`, `test_cache_ttl`, `test_concurrency_hardening`                      | Circuit breaker, TTL cache, race conditions          |
| **Security**       | `test_auth_headers`, `test_security_headers`, `test_web_security`, `test_document_security` | Header enforcement, auth bypass prevention           |
| **Metrics**        | `test_metrics_integration`, `test_system_status`                                            | Batched metric saves, system status data             |
| **Utilities**      | `test_formatting`, `test_keyboards`, `test_time_utils`, `test_image_utils`                  | Text formatting, keyboard builders, timezone math    |

### Mock Isolation Rule

> **Critical**: Never assign `sys.modules["X"] = MagicMock()` at module top-level in test files. Always use `setup_module()` / `teardown_module()` with save/restore. See [CHANGELOG.md](CHANGELOG.md) §2.2.0 for detailed anti-pattern reference.

---

## 📝 Changelog

See [CHANGELOG.md](CHANGELOG.md) for a detailed history of changes.
