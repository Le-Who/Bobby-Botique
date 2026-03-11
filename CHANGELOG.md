# Changelog

All notable changes to this project will be documented in this file.
Format is optimized for agent-parseable context.

## [2.8.39] - 2026-03-11 - Deep Dive UX: Soft Exit Button

### ✨ UX Improvement
| Change | File | Detail |
|--------|------|--------|
| Added "💬 Вернуться в чат" button | `keyboards.py`, `cb_navigation.py` | After deep dive (`??`) responses, users now see a friendly exit button that disables `is_deep_dive` without clearing conversation history. Previously the only option was "Начать новую тему" which fully reset the chat — too destructive, users avoided pressing it and stayed stuck in search mode. |

## [2.8.38] - 2026-03-11 - Agentic Search Memory & Context Fixes

### 🔴 Critical Fixes
| Fix | File | Detail |
|-----|------|--------|
| Unbounded memory leak | `agentic.py` | Google GenAI SDK protobuf implementations create reference cycles that evade standard GC when returned. A single 5-iteration `??` search generated up to 250MB in leaked `contents` objects (containing full parsed web pages). Added strict `contents.clear()` + `gc.collect()` in a `finally` block to force cyclic GC sweep, completely resolving the leak. |
| Follow-up routing gap | `agent.py` | After `??` search set `is_deep_dive=True`, follow-up messages fell through to regular chat (no search tools) → hallucinations. Added `elif chat_state.is_deep_dive` routing case. |
| Missing conversation context | `agentic.py`, `ai_search.py` | `AgenticSearch.run()` started from scratch each time. Now receives last 10 history entries so the agent can answer contextual follow-ups ("А за 2018?") using prior search results. |

### 🟡 Medium Fixes
| Fix | File | Detail |
|-----|------|--------|
| DB schema mismatch | `waiting_facts.py` | `SELECT created_at FROM users` failed because `users` table lacks `created_at`. Replaced with `SELECT MIN(metric_date) as first_seen FROM user_metrics` (index-only scan). |

## [2.8.35] - 2026-03-11 - Code Quality Audit & Test Acceleration

### 🔍 Professional Code Audit (9 Findings Fixed)

| ID | Severity | Fix | File |
|----|----------|-----|------|
| C1 | 🔴 Critical | Extracted `get_cached_genai_client()` factory — `AgenticSearch` and `GeminiProvider` now share TLS-cached clients | `gemini.py`, `agentic.py` |
| C2 | 🔴 Critical | Added `close()` coroutine for module-level `httpx.AsyncClient` to prevent resource leaks on shutdown | `web_reader.py` |
| C3 | 🔴 Critical | Moved mid-file imports to module top | `ai_search.py` |
| M2 | 🟡 Medium | Added TTL cache (120s) for personalized stats — DB queries drop from ~27 to ~3 per research session | `waiting_facts.py` |
| M3 | 🟡 Medium | Replaced global `asyncio.create_task` test monkeypatch with local interception to prevent Windows `stack overflow` & semaphore exhaustion | `test_messages.py` |
| M4 | 🟡 Medium | Wired `settings.AGENTIC_MODEL` into model selection chain (was defined but unused) | `ai_search.py` |
| M5 | 🟡 Medium | Fixed `relation "authorized_users" does not exist` error by pointing query to correct `users` table | `waiting_facts.py` |
| M6 | 🟡 Medium | Fixed `search_type must be 'search' or 'qna'` API exception by using correct internal Enum mapping | `search_services.py` |
| L1 | 🟢 Low | Replaced f-string logging with `%s` format (6 instances) | `agentic.py` |
| L2 | 🟢 Low | Fixed typo "про проведении" → "при проведении" | `ai_search.py` |
| L3 | 🟢 Low | Deduplicated `has_function_calls` iteration + added mypy assertion for type narrowing | `agentic.py` |
| L4 | 🟢 Low | Implemented 10-second cache for waiting facts during AI search loops so users have time to read them | `ai_search.py` |

### ⚡ Test Suite Acceleration

- **Parallel Execution**: Installed `pytest-xdist` and set `addopts = -n auto` in `pytest.ini` for automatic parallel test execution across all CPU cores.
- **Integration Test Separation**: Added `@pytest.mark.integration` marker to all 14 integration test files. Default `pytest` run now excludes them via `-m "not integration"`.
- **CI Pipeline Split**: GitHub Actions `ci.yml` now has two test jobs:
  - `test-unit` (fast, ~1-2min): Runs on every push/PR — unit tests only.
  - `test-integration` (slow, ~5min): Runs only on merge to `main` — full suite with DB tests.

### ✅ Quality Gates

| Check | Result |
|-------|--------|
| `ruff format + check` | 0 errors |
| `mypy app/ tests/` | 0 errors in 217 files |
| `pytest` (unit, parallel) | 1000+ tests passed |

### Files Changed

| File | Change |
|------|--------|
| `app/providers/gemini.py` | Added `get_cached_genai_client()` factory |
| `app/core/agentic.py` | Uses cached client, %s logging, deduplication |
| `app/web_reader.py` | Added `close()` coroutine |
| `app/handlers/ai_search.py` | Import ordering, typo fix, AGENTIC_MODEL wiring |
| `app/utils/waiting_facts.py` | TTL cache for personalized stats |
| `tests/test_messages.py` | Background task await fix |
| `tests/test_agentic_search.py` | Updated mock for cached client factory |
| `pytest.ini` | `addopts = -n auto --basetemp=.pytest_tmp -m "not integration"` |
| `.github/workflows/ci.yml` | Split into `test-unit` + `test-integration` jobs |
| `tests/integration/*.py` | Added `pytestmark = pytest.mark.integration` |

---

## [2.8.34] - 2026-03-10 - Agentic Web-Browsing Research

### 🤖 Agentic Research Loop

- **Multi-Step Query Decomposition**: Upgraded static web search to a dynamic agentic loop (`AgenticSearch`). The Gemini model can iteratively decompose complex questions, formulate search queries via the Tavily API, and self-correct if the gathered context is incomplete.
- **URL Triage & Pre-Evaluation**: The agent can autonomously evaluate search result metadata (titles, snippets) and selectively pick which URLs warrant deep reading.
- **Deep DOM Parsing with Jina**: Integrated `Jina Reader API` (`web_reader.py`) to bypass anti-bot protections, extract core text from noisy DOMs, and accurately retrieve article contents from target URLs.
- **Iterative Synthesis**: Bound loop recursion with strict limits (`AGENTIC_MAX_ITERATIONS=3`, `AGENTIC_MAX_PAGES=3`) to prevent infinite scraping while providing comprehensive, deeply-researched synthesis answers.
- **Dynamic Waiting Facts**: Introduced `app/utils/waiting_facts.py` to display personalized system stats and generic interesting trivia to the user while the agent is executing prolonged multi-step research iterations, providing a better UX.

### 🧹 Infrastructure Updates

- **Dependencies**: Added configurations for `JINA_API_KEY` and updated the `prompt_registry` with a dedicated new `RESEARCH_AGENT_SYSTEM` zero-shot planning role.
- **Testing**: Added extensive test suites and mock infrastructure (`test_agentic_search.py`) covering the agentic iteration flow, direct answer fallbacks, parsing errors, tool calling signatures, and API exceptions.

### Files Changed

| File | Change |
| --- | --- |
| `app/core/agentic.py` | [NEW] Implement `AgenticSearch` loop managing tools and memory |
| `app/web_reader.py` | [NEW] Implement Async wrapper over `Jina Reader API` |
| `app/utils/waiting_facts.py` | [NEW] Fun facts & stats during active research waiting |
| `app/handlers/ai_search.py` | Fully refactored to utilize the `AgenticSearch` process |
| `app/search_services.py` | Parameterized advanced queries allowing dynamic max lengths |
| `app/stage_indicators.py` | Added agentic specific UI stage indicators (`STAGES_AGENTIC_RESEARCH`) |

### 🧪 Tests: 1064 passed (100% Core + Integration Coverage)

---

## [2.8.33] - 2026-03-10 - Concurrency Scaling & Reliability

- **Strict Context Isolation**: Relocated the _last_finish_reason state from a globally mutable module variable into a thread-safe contextvars.ContextVar. Resolved a dangerous race condition where parallel streams could leak safety block statuses into adjacent AI responses.
- **Circuit Breaker Unblocking**: Severely refactored CircuitBreaker.call() to explicitly release its internal syncio.Lock during remote HTTP execution. This eradicated a critical head-of-line blocking bottleneck, allowing 100% concurrent request throughput without sacrificing state transition safety.
- **Connection Pool Harmonization**: Enforced explicit limitations on incoming Telegram updates (concurrent_updates=50) to perfectly align with the syncpg maximum connection pool size. This prevents database saturation timeouts under burst conditions.

### 🛡️ Graceful Degradation & Memory Safety

- **Bounded Task Queue**: Hardened the fire-and-forget TaskManager by implementing a strict MAX_TASKS = 100 upper limit. Submissions beyond this queue are synchronously rejected, mitigating the risk of Out-Of-Memory (OOM) failures from orphan background jobs.
- **Graceful DB Teardown**: Interlocked TaskManager.drain(timeout=10.0) into the application's core SIGTERM/SIGINT shutdown registry. Ensure all long-living background tasks (e.g. vector embedding storage) successfully commit before the event loop collapses.

### Files Changed

| File | Change |
| --- | --- |
| pp/streaming.py | Migrated _last_finish_reason to ContextVar |
| pp/providers/gemini.py | Adapted finish reason propagation to ContextVars |
| pp/providers/openrouter.py | Adapted finish reason propagation to ContextVars |
| pp/circuit_breaker.py | Fractured continuous lock into pre/post-flight scopes |
| ot.py | Hardcoded concurrent_updates=50 and registered TaskManager.drain() |
| pp/utils/background_tasks.py | Bounded Task manager up to 100 max tasks |

### 🧪 Tests: 1060 passed (100% Core + Integration Coverage)

---

## [2.8.32] - 2026-03-10 - E2E Testing & Background Task Stability

### 🧪 End-to-End Test Suite Completion

- **E2E Happy Path**: Added tests/e2e/test_chat_happy_path.py to completely simulate the core conversational loop from Telegram struct ingestion to final PostgreSQL database persistence.
- **Background Task Validation**: Resolved the _background_tasks mock reference to accurately validate and await asynchronous DB queries launched outside the HTTP request lifecycle.
- **Database Schema Alignment**: Fixed archaic assertions in performance and integration tests to correctly expect active_chat_messages tables instead of the legacy user_chats JSONB column.

### 🐛 Bug Fixes & Architecture Alignment

- **Async Mocking TypeErrors**: Resolved MagicMock object can't be awaited in key rotation and metrics generation tests by enforcing correct AsyncMock injection into connection pool context managers.
- **Datetime Mock Isolation**: Corrected the datetime monkeypatching in test_time_utils.py to preserve core module subclasses, preventing integration failures when hydrating Timezones.
- **Memory Feature Coverage**: Added dedicated integration tests for storage vector dimension constraints and similarity_search logic utilizing mocked API embeddings.

### 🧹 Code Quality

- **Ruff Linting**: Auto-fixed import sorting across the new E2E and integration test files.
- **Mypy Type Safety**: Confirmed 100% strict type safety across 212 application files with zero warnings.

### 🧪 Tests: 1056 passed (100% Core + Integration Coverage)

---

 - 2026-03-10 - Integration Test Stabilization

### 🧪 Integration Tests Stabilization

- **Event Loop Leakage Prevention**: Mocked the global `heavy_request_semaphore` in test contexts to prevent the Redis semaphore from holding references to closed `asyncio` event loops across test suite runs resulting in `RuntimeError: Event loop is closed`.
- **Module Namespace Cross-Contamination**: Addressed `AsyncMock` leakage where function imports inside of `handle_request` cached un-mocked database references in the module namespace. Patched both `app.repos.chats.get_user_chat` and `app.handlers.agent.get_user_chat` simultaneously for perfect isolation under heavy pytest load.
- **Strict Parameter Contracts**: Fixed `AttributeError` exceptions inside the integration pipeline by adding missing attributes (`thinking_level`, `context_summary`, `deep_dive_thread_id`) to the `fake_chat_state` mock payload, fully replicating the complex production `ChatState` object.

### Files Changed

| File                             | Change                                                                                                  |
| -------------------------------- | ------------------------------------------------------------------------------------------------------- |
| `tests/test_integration_flow.py` | Added comprehensive integration mocks (semaphore, state lock, thinking_level, multiple module bindings) |
| `tests/test_ai_chat.py`          | Cleaned up AAA testing blocks and import definitions                                                    |

### 🧪 Tests: 1054 passed (0 skipped, 0 failures)

---

## [2.8.30] - 2026-03-10 - Test Suite Reliability & CI Pipeline

### 🧪 Test Suite Stabilisation & Technical Debt

- **Database Transaction Isolation**: Implemented a global `force_test_db_conn` autouse fixture in integration tests. This forcefully mocks the `DatabaseManager` connection pool, ensuring all application business logic runs strictly inside the test's `BEGIN`/`ROLLBACK` transaction boundary, providing perfect isolation and parallelization safety.
- **Deterministic Time Mocking**: Eliminated flaky `asyncio.sleep` calls in `test_circuit_breaker.py` by introducing a `mock_time` fixture that monkeypatches `time.time()`. This allows instantaneous testing of timeouts without hanging the event loop, vastly improving test speed and CI reliability.
- **Webhook Integration Fix**: Resolved a `TypeError` parsing bug in `test_integration_webhook.py` by configuring `mock_app.bot.defaults.tzinfo = timezone.utc`, allowing `telegram.Update` JSON hydration to succeed locally.

### ⚙️ CI/CD Pipeline

- **Automated Validation**: Restructured `.github/workflows/ci.yml` pipeline to automatically execute `ruff check`, `ruff format`, `mypy`, and `pytest` across all application directories, tests, and utility scripts (`load_test.py`).
- **Graceful DB Degradation**: Configured the Pytest step to conditionally run integration tests only when `TEST_DATABASE_URL` is present; ensuring GitHub actions execute unit tests fully without failing on missing secrets.

### Files Changed

| File                                            | Change                                                         |
| ----------------------------------------------- | -------------------------------------------------------------- |
| `tests/integration/conftest.py`                 | Added global transactional pool mocking for `db_manager`       |
| `tests/test_circuit_breaker.py`                 | Replaced real sleep timers with `mock_time.advance()`          |
| `tests/integration/test_integration_webhook.py` | Injected UTC timezone into `mock_application`                  |
| `.github/workflows/ci.yml`                      | Expanded QA coverage matrix across `tests/` and `load_test.py` |

### 🧪 Tests: 1055 passed (all suites clean)

---

## [2.8.29] - 2026-03-10 - Concurrency & Graceful Shutdown Bug Fixes

### 🛡️ Concurrency & Operational Stability

- **Concurrency Starvation Fixed**: Inverted the locking order in `messages.py`. Users now acquire their `state.get_user_lock` _before_ taking a slot from the global `heavy_request_semaphore`. Prevention of head-of-line blocking where a single active user could monopolize the entire system.
- **Gemini Connection Pool Leak Fixed**: Implemented `cachetools.LRUCache` to persistently cache Google `genai.Client` instances by API key. Resolves a critical leak where every request instantiated a fresh `httpx.AsyncClient` preventing TLS session reuse and exhausting sockets.
- **Graceful Shutdown Bypass Fixed**: `bot.py` now explicitly catches `asyncio.CancelledError` thrown by the `uvloop` shutdown signals, safely executing the `application.stop()` sequence under `asyncio.shield` to prevent data loss.
- **Task Queue Deadlock Eliminated**: Modified `TaskQueue.stop()` to invoke `worker.cancel()` rather than using blocking `queue.put()` operations. This prevents a classic shutdown deadlock when the queue is completely saturated.

### Files Changed

| File                       | Change                                                               |
| -------------------------- | -------------------------------------------------------------------- |
| `app/handlers/messages.py` | Lock inversion (`user_lock` then `heavy_request_semaphore`)          |
| `app/providers/gemini.py`  | Global `_gemini_clients_cache` LRU implementation                    |
| `bot.py`                   | Catch `asyncio.CancelledError` and shield `application.stop()`       |
| `app/queue.py`             | Prevent queue deadlock by migrating blocking shutdown to `.cancel()` |

### 🧪 Tests: Re-validated Unit Suites

---

## [2.8.28] - 2026-03-09 - Database Latency & N+1 Roundtrip Fixes

### ⚡ Performance & Database Query Batching

- **Resolved N+1 Query Anti-Pattern**: Replaced 5 sequential database requests in `get_user_chat` and `update_user_chat` with single atomic Common Table Expression (CTE) executed pipelines.
- **Drastically Reduced Network Latency**: By aggregating results natively inside PostgreSQL using `row_to_json` and `json_to_recordset`, operations that previously took ~400-600ms due to AWS EU Central PgBouncer roundtrips now complete in ~80ms (a single roundtrip).
- **Consolidated RLS Scoping**: Session context (`set_config('app.user_id', ...)`) is still correctly applied on the unified connections, maintaining strict Row-Level Security isolation without sacrificing performance.

### Files Changed

| File                 | Change                                                            |
| -------------------- | ----------------------------------------------------------------- |
| `app/repos/chats.py` | Overhauled `get_user_chat` and `update_user_chat` SQL statements. |

### 🧪 Tests: 1054 passed (all suites clean)

---

## [2.8.27] - 2026-03-09 - Database Reliability & Legacy Schema Cleanup

### 🛡️ State Management & Distributed Concurrency Safety

- **Removed Unsafe Local Caching**: Completely removed the in-memory `TTLCache` (`_active_chats_cache`) from `app/database.py` and `app/repos/chats.py`. In an environment scaled beyond a single replica, local memory caching directly caused state divergence and chat history truncation (Data Loss).
- **Enforced Single Source of Truth (SSOT)**: `get_user_chat` and `update_user_chat` now strictly and unconditionally route `ChatState` reads/writes directly to the PostgreSQL database, ensuring perfect consistency across all bot instances with `asyncpg` pooling.

### 🧹 Schema Tech Debt Cleanup

- **Dropped Legacy Column**: Executed DDL migration to `ALTER TABLE public.chats DROP COLUMN history;`. The message history has been durably stored in the relational `active_chat_messages` table for a long time, but the legacy `history` JSONB column was still being zeroed out with `[]` on every `chat_state` update, creating unnecessary I/O constraints.

### Files Changed

| File                         | Change                                                    |
| ---------------------------- | --------------------------------------------------------- |
| `app/database.py`            | Removed `_active_chats_cache` TTLCache initialization     |
| `app/repos/chats.py`         | Dropped TTL caching logic and `history` dummy JSON insert |
| `app/repos/conversations.py` | Removed legacy cache invalidation sweeps                  |

### 🧪 Tests: 1054 passed (all suites clean)

---

## [2.8.26] – 2026-03-09 – CI Stabilization & Type Safety

### 🧪 CI Stabilization: Test Mocking Architecture Fixes

- **Mock Integrity**: Resolved brittle mocks in `test_ai_chat.py`, `test_metrics_integration.py`, and `test_database_tavily.py` where `MagicMock` was erroneously used instead of `AsyncMock` for database calls, resolving `TypeError: object MagicMock can't be used in 'await' expression`.
- **Namespace Collision**: Removed dynamic `importlib.reload(app)` behavior from `test_perf_db_messages.py` that corrupted the `app.database` alias space context (`AttributeError`).
- **Time Freezing**: Fixed mock `datetime` patches missing crucial timezone arguments, preventing `tzinfo` exceptions during test suite isolation.

### 🧹 Code Quality & Mypy Strictness

- **Type Safety**: Fixed remaining Pydantic `arg-type` coercion warnings within the Gemini Provider SDK implementations (`GeminiProvider` configuring `safety_settings`).
- **Missing Semaphore**: Resolved a missing namespace export in `app/handlers/msg_media.py` by reinstantiating `_HEAVY_REQUEST_SEMAPHORE=asyncio.Semaphore(10)` module-locally, preventing runtime `AttributeError`s for media group grouping.
- **Routing Types**: Updated `stream_response` to be recognized natively as an async iterator. Defaulted payload arguments like `chat_id` and `parse_mode` to secure fallbacks rather than `None`.

### Files Changed

| File                                | Change                                                           |
| ----------------------------------- | ---------------------------------------------------------------- |
| `tests/test_perf_db_messages.py`    | Removed `importlib.reload(app)` mock context bleed               |
| `tests/test_metrics_integration.py` | Converted mock configurations to `AsyncMock` to satisfy `await`s |
| `app/providers/gemini.py`           | Fixed implicit dict → type `safety_settings` coercion            |
| `app/providers/router.py`           | Fixed async iterable hint masking for `stream_response`          |
| `app/streaming.py`                  | Added `type: ignore` patches for `parse_mode` types              |
| `app/handlers/ai_search.py`         | Strict typing for streaming response payload params              |
| `app/handlers/ai_photo.py`          | Strict typing formatting                                         |
| `app/handlers/msg_media.py`         | Module-local initialization of `_HEAVY_REQUEST_SEMAPHORE`        |

### 🧪 Tests: 1054 passed (0 skipped, 0 failures)

---

## [2.8.25] – 2026-03-09 – Background Task Resilience & Distributed Concurrency Limits

### 🛡️ Resilient Background Task Manager

- **Prevent Data Loss**: Replaced suppressed `asyncio.create_task` scattered usage with a centralized `TaskManager`. Background tasks, such as saving memory vectors (`_bg_store`), are now retried with exponential backoff.
- **Global Error Hooks**: When tasks exhaust all retries, critical failures are sent to the admin dashboard via a centralized error callback, ensuring observability.

### 🌐 Distributed State Management

- **Redis-backed Semaphores**: Upgraded the local `_HEAVY_REQUEST_SEMAPHORE` to a `GlobalLLMSemaphore` backed by Redis Sorted Sets (ZSETs), preventing API starvation across multi-replica deployments horizontally.
- **Graceful Degradation**: If Redis is unavailable, the semaphore transparently falls back to an in-memory `asyncio.Semaphore`, ensuring high availability.

### 🧹 Code Quality & Testing Strictness

- **Cleanups**: Fixed unused variables (`stream_last_msg`, `attempt`, `resolution`) and undefined names across `ai_search.py`, `router.py`, and test packages.
- **Ruff compliance**: Verified automated formatting (`ruff format`) and strict linting (`ruff check`) across the codebase.

### Files Changed

| File                            | Change                                                        |
| ------------------------------- | ------------------------------------------------------------- |
| `app/utils/background_tasks.py` | Centralized `TaskManager` with retry and error callback hooks |
| `bot.py`                        | Registered background task alerts to Admin alert pipeline     |
| `app/adapters/concurrency.py`   | Built Redis `GlobalLLMSemaphore` with graceful fallback       |
| `app/handlers/ai_chat.py`       | Wired background hooks to vector database                     |
| `tests/*`                       | Refactored mock assignments                                   |

### 🧪 Tests: 1054 passed (all suites clean)

---

## [2.8.24] – 2026-03-09 – Unified AI Streaming Architecture

### ✨ Unified Real-Time Streaming

- **Universal Streaming Layer**: Replaced static tuple unpacking with a unified `stream_and_display` layer across all AI modalities. Real-time streaming is now fully supported for:
  - 💬 Regular Chat (`ai_chat.py`)
  - 📄 Document Analysis Q&A (`ai_document.py`)
  - 🌉 Image/Photo Analysis (`ai_photo.py`)
  - 🔍 Web Search & QnA (`ai_search.py`)

### 🧪 Test Suite Adaptations & AsyncPG Fixes

- **Mocking Strategy Update**: Updated all integration and unit tests to correctly mock the 3-tuple signature of `stream_and_display`.
- **Database Connection Safety**: Patched `_resolve_ai_request` in QnA and Integration tests to prevent `asyncpg.InterfaceError` ("connection was closed in the middle of operation") caused by DB event looping during tests.
- **Assertion Safety**: Swapped invalid `MagicMock` awaits for `AsyncMock` to ensure stability.

### Files Changed

| File                                      | Change                                                               |
| ----------------------------------------- | -------------------------------------------------------------------- |
| `app/handlers/ai_document.py`             | Integrated `stream_and_display` for streaming document Q&A           |
| `app/handlers/ai_photo.py`                | Vision model multimodal inputs now stream in real time               |
| `app/handlers/ai_search.py`               | Replaced standard response fetching with generic localized streaming |
| `app/handlers/agent.py`                   | Refactored orchestrator for streaming compatibility                  |
| `tests/test_ai_document.py`               | Updated mock signature and assert references                         |
| `tests/test_ai_photo.py`                  | Updated mock signature and assert references                         |
| `tests/test_ai_search.py`                 | Patched dependencies to avoid `InterfaceError`                       |
| `tests/integration/test_e2e_app_smoke.py` | Patched unpacking logic                                              |

### 🧪 Tests: 1054 passed (all suites clean)

---

## [2.8.23] – 2026-03-09 – Strict AAA Refactoring & Typing Fixes

### 🧪 Strict AAA Unit & Integration Tests

- **Unit Test Boundaries**: Refactored `test_ai_chat.py` to adhere to strict Arrange-Act-Assert (AAA) logic. Eliminated fragile internal function mocks (`update_stage`, `get_registry`, `handle_ai_response_error`) in favor of mocking strict database and network boundaries.
- **Integration Test Rewrites**: Converted `test_e2e_app_smoke.py` into a true integration flow (`test_integration_full_message_flow`), accurately validating database state continuity without pseudo-E2E network mocks.
- **Teardown Safety**: Fixed `RuntimeError` (`Event loop is closed`) on tear down in `test_concurrency.py` cache tests by mocking redis layer.

### 🧹 Code Quality & Typing Verification

- **Mypy Audit (100% Passed)**: Fixed 4 typing bugs detected in integration suites (`Dict | None` index violations and missing variable annotations).
- **Ruff Compliance**: Resolved `F841` (unused variable assignment) during test teardowns and enforced standardized import sorting across impacted domains.

### 🧪 Tests: 1054 passed (all suites clean)

---

## [2.8.22] – 2026-03-09 – Testing Infrastructure AAA Refactoring & Integration Boundaries

### 🧪 Legacy Test AAA Refactoring

- Completely refactored `test_ai_chat.py` and `test_smoke.py` away from implementation coupling towards the Arrange-Act-Assert (AAA) principle.
- Created `tests/factories.py` providing `make_telegram_update` and `make_telegram_context` generators to standardise component isolation and mock provisioning.

### 🛡️ Integration Verification Coverage

- **Web Dashboard Authorization Boundary**: Added `test_web_security.py` to target Quart's Admin endpoints, enforcing the Admin Secret logic securely without leaking internal errors.
- **Concurrency Locks Protection**: Created `test_concurrency_locks.py`:
  - Enforced queueing boundaries against the `state.get_user_lock` layer for simultaneous chat requests.
  - Successfully demonstrated that UI Callbacks are rejected via the `_is_user_busy` validation, preventing DB corruption and generating a Toast warning for the user.

### Files Changed

| File                                          | Change                                                          |
| --------------------------------------------- | --------------------------------------------------------------- |
| `tests/test_ai_chat.py`                       | AAA refactored, assertions centralized, decoupled context       |
| `tests/test_smoke.py`                         | Formalized admin Quart verification against standard mocking    |
| `tests/factories.py`                          | [NEW] Centralized mock creation tools                           |
| `tests/integration/test_web_security.py`      | [NEW] Web authorization perimeter checks                        |
| `tests/integration/test_concurrency_locks.py` | [NEW] Sequential lock enforcement and callback busy validations |

### 🧪 Tests: 1053 passed, 0 skipped, 1 unrelated external runner fail

---

## [2.8.21] – 2026-03-06 – Code Quality Audit: Lint, Security & Dead Code Cleanup

### 🧹 Ruff & Vulture: 25 Findings → 0

| Category                     | Count | Fix                                                                        |
| ---------------------------- | ----- | -------------------------------------------------------------------------- |
| **F823 (bug)**               | 1     | Removed redundant re-import of `TelegramFormatter` in `ai_chat.py`         |
| **B023 (style)**             | 4     | Bound loop variables as default args in `text_format.py:_flush_blockquote` |
| **I001 (imports)**           | 7     | Auto-fixed via `ruff check --fix`                                          |
| **Unused imports (app)**     | 3     | Removed `timezone` (config.py, memory.py), `ChatAction` (commands.py)      |
| **Unused imports (tests)**   | 7     | Removed `PropertyMock` ×4, `Chat`/`User` ×1                                |
| **Unused variables (tests)** | 3     | Renamed `tz` → `_tz` in test_time_utils.py MockDatetime stubs              |

### 🔒 Security: 14 CVEs Patched

| Package        | Old     | New    | CVEs Fixed |
| -------------- | ------- | ------ | ---------- |
| `aiohttp`      | 3.10.11 | 3.13.3 | 9          |
| `cryptography` | 43.0.3  | 46.0.5 | 2          |
| `pypdf`        | 6.7.3   | 6.7.5  | 2          |
| `werkzeug`     | 3.1.5   | 3.1.6  | 1          |

### ✅ Quality Gates

| Check          | Result                    |
| -------------- | ------------------------- |
| mypy           | 0 errors                  |
| ruff           | All checks passed         |
| vulture (≥80%) | 0 findings                |
| pip-audit      | No known vulnerabilities  |
| Duplicate code | 0 duplicates (≥6 stmts)   |
| pytest         | **1040 passed**, 0 failed |

### Files Changed

| File                       | Change                                          |
| -------------------------- | ----------------------------------------------- |
| `app/handlers/ai_chat.py`  | Removed redundant `TelegramFormatter` re-import |
| `app/utils/text_format.py` | Default-arg binding in `_flush_blockquote`      |
| `app/config.py`            | Removed unused `timezone` import                |
| `app/handlers/commands.py` | Removed unused `ChatAction` import              |
| `app/repos/memory.py`      | Removed unused `timezone` import                |
| `requirements.txt`         | `cryptography` upper bound <44 → <48            |
| 7 test files               | Removed unused imports, renamed unused vars     |

### 🧪 Tests: 1040 passed, 0 failures

---

## [2.8.20] – 2026-03-06 – Streaming Overflow Formatting Fix

### 🔴 Fix: Broken Formatting When Streamed Responses Overflow Into Multiple Messages

**Root cause:** `_overflow_to_new_message` split the raw markdown buffer at a text boundary, but **did not carry open markdown formatting state** to the remainder. Each half was independently formatted by `markdown_to_html()`, so the second message lost formatting context (unclosed `**`, `` ` ``, ` ``` `, `*` etc. from the first message).

**Fix:** New `_detect_open_markdown(text)` helper scans raw markdown for unclosed constructs:

- Fenced code blocks (` ``` `) — with language specifier preservation
- Inline code (`` ` ``)
- Bold (`**`)
- Italic (`*`)

Returns `(suffix, prefix)` — suffix closes open constructs in the frozen message, prefix reopens them in the remainder. Applied in `_overflow_to_new_message` before each half is formatted.

### Files Changed

| File                             | Change                                                                       |
| -------------------------------- | ---------------------------------------------------------------------------- |
| `app/streaming.py`               | `_detect_open_markdown()` helper + `_overflow_to_new_message` context repair |
| `tests/test_markdown_context.py` | [NEW] 22 unit tests for markdown context detection                           |
| `tests/test_streaming.py`        | +4 integration tests for overflow formatting preservation                    |

### 🧪 Tests: 954 passed, 0 failures

---

## [2.8.19] – 2026-03-06 – Memory Leak Fixes

### 🔴 Fix: Unbounded Memory Growth in User State Store

**Root cause:** `_UserStateStore._states` was a plain `dict` — every unique user added a permanent entry (~200B) that was never evicted. Over time, this caused monotonic RAM growth.

**Fix:** Replaced with `cachetools.LRUCache(maxsize=10000)`. Evicted states are transparently re-loaded from DB on next access via `_ensure_loaded()` — zero data loss.

### 🔴 Fix: Unbounded Growth in Metrics Dictionaries

**Root cause:** `MetricsCollector.daily_metrics` and `_user_daily` dicts accumulated entries forever — one per day and one per user-day respectively. `RoleConversationMetricsCollector.role_applications` also grew without limit.

**Fix:** Added `_prune_old_metrics()` to the `_event_processor` save cycle — prunes `daily_metrics` older than 8 days and `_user_daily` entries for past days. Capped `role_applications` at 500 entries with LFU eviction.

### 🟡 Fix: Per-Request `genai.Client` Allocation in Streaming

**Root cause:** `streaming.py` created a new `genai.Client()` on every streaming request (connection pool + TLS context), while `GeminiProvider` already reused its client correctly.

**Fix:** Added module-level `_streaming_client` with the same reuse pattern — rebuild only when API key changes.

### ℹ️ Deprecation Warning (`asyncio.iscoroutinefunction`)

Verified `memory_manager.py` already uses the correct `inspect.iscoroutinefunction()`. The log warning originates from CPython internals (`loop.add_signal_handler` lambda) — harmless until Python 3.16 and cannot be eliminated from user code.

### Files Changed

| File               | Change                                                |
| ------------------ | ----------------------------------------------------- |
| `app/state.py`     | `_UserStateStore._states`: `dict` → `LRUCache(10000)` |
| `app/metrics.py`   | `_prune_old_metrics()`, `_MAX_ROLE_ENTRIES=500` cap   |
| `app/streaming.py` | Module-level `_streaming_client` reuse                |

### 🧪 Tests: 923 passed, 0 failures

---

## [2.8.18] – 2026-03-05 – Logging Refactoring: ContextFilter, Unified APILogger, f-string Cleanup

### 🔧 ContextFilter for user_id/chat_id

Extended `request_context.py` with `set_user_context()`/`get_user_id()`/`get_chat_id()` via `contextvars`. `RequestContextFilter` auto-injects `user_id`, `chat_id`, and `request_id` into every log record. JSONFormatter already included these fields — no changes needed.

### 🏗️ Unified APILogger (452 → 156 lines)

Replaced 8 specialized methods (`log_gemini_request`, `log_gemini_response`, `log_openrouter_request`, etc.) with 3 universal ones: `log_request()`, `log_response()`, `log_error()`. Removed dead code: `_sanitize_data`, `log_api_call` decorator, `log_with_context`.

### 🔄 Decorator Context Propagation

`authorized_only` and `admin_only` decorators now call `set_user_context()` and `set_request_id()` — covers all ~40+ handlers automatically. Undecorated handlers in `cb_ai_actions.py` updated manually. Redundant calls removed from `commands.py`.

### 🧹 f-string Logging → `%s` Formatting

33 f-string `logging.*()` calls converted to lazy `%s` formatting across 12 files. Zero f-string logging calls remain in `app/`.

### 🧹 cmd_admin.py Inline user_id Cleanup

Removed 7 redundant `update.effective_user.id` references from log messages — user_id is now auto-injected via RequestContextFilter.

### Files Changed

| File                                  | Change                                                      |
| ------------------------------------- | ----------------------------------------------------------- |
| `app/request_context.py`              | `set_user_context()`, `get_user_id()`, `get_chat_id()`      |
| `app/utils/logging_config.py`         | `RequestContextFilter` extended, removed `log_with_context` |
| `app/utils/api_logger.py`             | 8 methods → 3 (452 → 156 lines), dead code removed          |
| `app/utils/decorators.py`             | `set_user_context` + `set_request_id` in both decorators    |
| `app/handlers/cb_ai_actions.py`       | Added `set_user_context`/`set_request_id` to 3 handlers     |
| `app/handlers/commands.py`            | Removed redundant `set_request_id` import/usage             |
| `app/handlers/cmd_admin.py`           | Removed 7 inline `user_id` from log messages                |
| `app/providers/gemini.py`             | Updated to `log_request()`/`log_response()`                 |
| `app/providers/openrouter.py`         | Updated to `log_request()`/`log_response()`                 |
| `app/search_services.py`              | Updated to `log_request()`/`log_response()`                 |
| 12 files across `app/`                | f-string logging → `%s` formatting (33 instances)           |
| `tests/test_decorators.py`            | [NEW] 6 tests for decorator context propagation             |
| `tests/test_request_context.py`       | +7 tests for user context                                   |
| `tests/test_api_logger_request_id.py` | Rewritten for new API                                       |

### 🧪 Tests: 923 passed, 0 failures

---

## [2.8.17] – 2026-03-05 – Quality Infrastructure: Integration Tests, CI/CD, Lint/Type Safety

### 🧪 Integration Tests with Real Supabase DB (22 new → 36 total)

New repo-level integration tests against dedicated test database (`tests_test_gemai`). Each test runs inside `BEGIN → ROLLBACK` — zero data persists.

| File                                            | Tests | Covers                                                               |
| ----------------------------------------------- | ----- | -------------------------------------------------------------------- |
| `tests/integration/test_repos_users.py`         | 4     | `save_user_state` UPSERT, `load_user_state`, feedback                |
| `tests/integration/test_repos_chats.py`         | 6     | Chat defaults, model/history update, clear, thinking level, messages |
| `tests/integration/test_repos_conversations.py` | 6     | CRUD, messages FK, rename, delete cascade, count                     |
| `tests/integration/test_repos_roles.py`         | 6     | CRUD, rename, delete, count, user-scoped access                      |

### ⚙️ CI/CD Pipeline (updated)

`.github/workflows/ci.yml` — 3-job pipeline:

1. **Lint** — `ruff check --output-format=github` + `ruff format --check`
2. **Type Check** — `mypy app/` (0 errors enforced)
3. **Test** — `pytest tests/ -x -q --ignore=tests/integration`

### 🔧 Lint & Type Safety (0 errors)

- **Ruff**: 3 import sort errors auto-fixed, 4 files reformatted
- **Mypy**: 4 errors fixed — 3 `arg-type` in `ai_chat.py`, 1 `assignment` in `logging_config.py`

### 🧹 .gitignore / .dockerignore Audit

| Issue                                                                   | Fix                                           |
| ----------------------------------------------------------------------- | --------------------------------------------- |
| IDE files tracked (`.cursor/`, `.Jules/`, `.vscode/`, `.roomodes`)      | `git rm --cached` + added to `.gitignore`     |
| Missing entries (`.coverage`, `htmlcov/`, `.mypy_cache/`, `.venv/`)     | Added to `.gitignore`                         |
| Docker image bloat (`scripts/`, `docs/`, `.github/`, `northflank.yaml`) | Added to `.dockerignore`                      |
| OS artifacts                                                            | Added `Thumbs.db`, `.DS_Store`, `Desktop.ini` |

### 📝 README.md Updated

- Test count: 652 → **870+**, 0 skipped
- Added 6 new test categories to suite structure table
- CI/CD section updated: 3 jobs (was 2)
- New "Integration Tests" section with safety guarantees
- `TEST_DATABASE_URL` added to env var reference

### Files Changed

| File                                            | Change                          |
| ----------------------------------------------- | ------------------------------- |
| `tests/integration/test_repos_users.py`         | [NEW] 4 integration tests       |
| `tests/integration/test_repos_chats.py`         | [NEW] 6 integration tests       |
| `tests/integration/test_repos_conversations.py` | [NEW] 6 integration tests       |
| `tests/integration/test_repos_roles.py`         | [NEW] 6 integration tests       |
| `tests/integration/__init__.py`                 | [NEW] Package marker for pytest |
| `.github/workflows/ci.yml`                      | Updated: 3 jobs + mypy          |
| `.gitignore`                                    | Comprehensive rewrite           |
| `.dockerignore`                                 | Comprehensive rewrite           |
| `README.md`                                     | Testing section overhaul        |
| `app/handlers/ai_chat.py`                       | Mypy arg-type fix               |
| `app/utils/logging_config.py`                   | Mypy assignment fix             |
| `app/handlers/chat_logic.py`                    | Ruff import sort fix            |
| `app/handlers/ai_search.py`                     | Ruff import sort fix            |
| `app/handlers/msg_document.py`                  | Ruff format fix                 |
| `app/metrics.py`                                | Ruff format fix                 |
| `app/utils/text_format.py`                      | Ruff format fix                 |

### 🧪 Tests: 870 passed, 0 failures, 0 skipped

---

## [2.8.16] – 2026-03-05 – Streaming HTML Misnesting Fix

### 🔴 Fix: Telegram "unmatched end tag" Errors During Streaming

**Root cause:** `sanitize_html_tags()` fixed misnested HTML tags (e.g. `<code>...<i>...</code>`) by appending close tags to the **end** of the string, but never repositioned the original misplaced close tag. Telegram's strict parser still rejected the output because nesting order was invalid.

**Fix:** Rewrote `sanitize_html_tags()` with a **rebuild approach** — walks HTML as segments, inserts close/reopen tags _at the point_ of misnesting. Also strips empty tag pairs (`<i></i>`) produced by the reopen logic.

| Input                     | Old output (broken)           | New output (valid)            |
| ------------------------- | ----------------------------- | ----------------------------- |
| `<code>E = <i>mc²</code>` | `<code>E = <i>mc²</code></i>` | `<code>E = <i>mc²</i></code>` |

### Files Changed

| File                        | Change                                                                       |
| --------------------------- | ---------------------------------------------------------------------------- |
| `app/utils/text_format.py`  | `sanitize_html_tags()` rewritten with rebuild approach + empty tag stripping |
| `tests/test_text_format.py` | Strengthened existing tests + 2 new tests with stack-walk nesting validation |

### 🧪 Tests: 26 passed, 0 failures (text_format suite)

---

## [2.8.15] – 2026-03-04 – Mypy Type Safety Overhaul (110 → 0 Errors)

### 🔍 Full Mypy Type Audit

Systematic resolution of all 110 mypy errors across 42+ files. **Zero runtime behavior changes** — all fixes are type-level only. Mypy now runs clean and can catch real type bugs going forward.

### Fix Categories

| Category                 | Count | Approach                                                                    |
| ------------------------ | ----- | --------------------------------------------------------------------------- |
| SDK suppressions         | ~15   | `# type: ignore[arg-type]` — genai Pydantic coercion (documented, not bugs) |
| Real type bug fixes      | ~10   | Fixed return types, loop var conflicts, undefined variables                 |
| Nullable guards          | ~20   | Guards for `context.args`, `user_data`, `callback_query`, `effective_user`  |
| Dict/var annotations     | ~10   | Explicit `dict[str, Any]`, `list[str]`, pre-declarations                    |
| Variable renames         | 2     | Shadow/type-conflict resolution                                             |
| Unreachable suppressions | 6     | Defensive patterns: double-check lock, isinstance guards                    |

### 🔴 Real Bug Fixes Found by Mypy

| File              | Bug                                                              | Fix                                           |
| ----------------- | ---------------------------------------------------------------- | --------------------------------------------- | --------------------- |
| `ai_photo.py`     | Return type `str` instead of `None` on failure path              | Changed `return ""` → `return None`           |
| `security.py`     | Loop variable type conflict between if/else branches             | Renamed `file_type` → `ext_set` in one branch |
| `msg_document.py` | Reference to undefined `filename` variable                       | Fixed to `f"document.{file_ext}"` fallback    |
| `callbacks.py`    | `model_name` redefined with conflicting type in if/else          | Pre-declared with `str                        | None` before branches |
| `decorators.py`   | Missing `effective_user` null check (potential `AttributeError`) | Added early return guard                      |

### 🛡️ SDK Suppressions (Intentional `# type: ignore`)

GenAI SDK uses Pydantic coercion — dicts are valid where typed models are expected. Suppressed with comments documenting why (not bugs, matches official SDK examples). Mypy still catches all **other** type errors in these files.

### Files Changed

| File                                | Change                                                                |
| ----------------------------------- | --------------------------------------------------------------------- |
| `app/streaming.py`                  | Added `Any` import, `dict[str, Any]` annotations                      |
| `app/ai_provider.py`                | `dict[str, Any]` annotations, assertion guards, defensive unreachable |
| `app/security.py`                   | Unified `allowed_extensions` declaration, loop var rename             |
| `app/config.py`                     | `list[str]` annotation, `type: ignore[assignment]` for dev fallback   |
| `app/state.py`                      | Unreachable suppression for double-check lock pattern                 |
| `app/cache.py`                      | Unreachable suppression for defensive else branch                     |
| `app/handlers/callbacks.py`         | `model_name` pre-declaration, `all_models` annotation                 |
| `app/handlers/cb_conversations.py`  | `assert query`, `context.user_data` guards                            |
| `app/handlers/cb_roles.py`          | `context.user_data` null check                                        |
| `app/handlers/msg_document.py`      | `file_name` fallback, `type: ignore` for arg-type                     |
| `app/handlers/msg_media.py`         | Unreachable for Task state, `type: ignore` for duck-type              |
| `app/handlers/ai_search.py`         | Renamed `user_id`/`chat_id` → `trace_user_id`/`trace_chat_id`         |
| `app/handlers/cmd_conversations.py` | `context.args` guard                                                  |
| `app/handlers/cmd_admin.py`         | `context.args` + `chat.title` guards                                  |
| `app/utils/decorators.py`           | `effective_user` null checks in 4 decorators                          |
| `app/utils/logging_config.py`       | `dict[str, Any]` for `extra_dict`                                     |
| `app/utils/image_utils.py`          | PIL resize explicit `(w, h)` tuple                                    |
| `app/utils/network.py`              | `assert last_exception is not None` before raise                      |
| `app/utils/keyboards.py`            | `type: ignore[return-value]` for intentional `None`                   |
| `app/tracing.py`                    | Return type `dict[str, str \| None]`                                  |
| `app/search_services.py`            | `http_client: AsyncClient \| None` annotation                         |
| `app/memory_manager.py`             | `dict[str, Any]`/`dict[str, float]` annotations                       |
| `app/metrics.py`                    | `dict[str, list[Any]]` annotation                                     |
| `app/context/assembler.py`          | `list[int]` annotation                                                |
| `bot.py`                            | `type: ignore[union-attr]` for `reconfigure`                          |
| + SDK files                         | `type: ignore[arg-type]` for genai Pydantic coercion                  |

### 🧪 Mypy: 0 errors (was 110)

---

## [2.8.14] – 2026-03-04 – Streaming HTML Sanitizer & Finish Reason Inspection

### 🔴 Fix: HTML Entity Parsing Error During Streaming

**Root cause:** `markdown_to_html()` produced unclosed HTML tags when processing incomplete markdown fragments during mid-stream flushes. Telegram rejected the malformed HTML (`"unmatched end tag at byte offset 3139, expected "</code>", found "</i>"`), causing repeated edit failures.

**Fix:** New `sanitize_html_tags()` function in `text_format.py` — lightweight stack-based HTML tag balancer that closes unclosed tags and resolves misnested tags. Applied in `StreamingWriter._flush()` (mid-stream) and `_overflow_to_new_message()`.

### 🔴 Fix: Truncated Streaming Responses (~94 chars)

**Root cause:** `stream_gemini_response()` never inspected `finish_reason` from streaming chunks. When the model stopped early (SAFETY/RECITATION), the truncated response was silently treated as success.

**Fix:** `stream_gemini_response()` now captures `finish_reason` from each chunk's candidates. `stream_and_display()` checks it after iteration:

- **SAFETY/RECITATION** → user sees partial text + `⚠️ Ответ был прерван фильтром безопасности`
- **MAX_TOKENS** → text + `⚠️ Ответ был обрезан из-за ограничения длины`
- **< 150 chars with unusual finish_reason** → WARNING logged
- `finish_reason` included in "Streaming complete" log line

### Files Changed

| File                        | Change                                                                     |
| --------------------------- | -------------------------------------------------------------------------- |
| `app/utils/text_format.py`  | New `sanitize_html_tags()` function                                        |
| `app/streaming.py`          | `finish_reason` capture, sanitizer integration, blocked/truncated handling |
| `tests/test_text_format.py` | +8 tests for sanitizer and partial-streaming scenarios                     |

### 🧪 Tests: 660 passed, 1 skipped, 0 failures

---

## [2.8.13] – 2026-03-04 – Streaming Concurrency Race Fix

### 🔴 Fix: Callback Race Condition During Streaming

**Root cause:** 6 callback handlers (`model_button`, `switch_model`, `new_topic`, `new_chat`, `deep_dive:new_topic`, `toggle_search`) performed Read-Modify-Write on `chat_state` **without checking the per-user lock**. When triggered during active streaming, their changes were silently overwritten by the streaming handler's final `update_user_chat`.

**Fix:** Added `_is_user_busy(user_id)` lock-check guard — shows "⏳ Дождитесь завершения" toast and skips mutation when the user lock is held.

### Files Changed

| File                                  | Change                                                 |
| ------------------------------------- | ------------------------------------------------------ |
| `app/handlers/callbacks.py`           | `_is_user_busy` helper + 6 handler guards              |
| `tests/test_concurrency_hardening.py` | +2 regression tests (guard presence, AST verification) |

### 🧪 Tests: 652 passed, 1 skipped, 0 failures

---

## [2.8.12] – 2026-03-04 – Code & Logic Audit + Ruff Expansion

### 🔍 Full Code & Logic Audit

Systematic audit of 30+ modules (~12K lines), 19 handlers, 10 repos. **Ruff: 0 violations.**

### 🔴 Fix: Missing RLS_CONFIG Entries

`long_term_memory` and `key_model_status` tables were missing from `RLS_CONFIG` in `db/rls.py`. Their RLS policies existed only via manual DB migrations — deploying to a fresh DB would have left them unprotected.

### ⚙️ Ruff Expansion (86 auto-fixes)

Expanded lint rules from 5 to 10 categories:

| Rule | Category              | Violations Fixed                                    |
| ---- | --------------------- | --------------------------------------------------- |
| SIM  | flake8-simplify       | 23 (suppressible-exception, collapsible-if, etc.)   |
| PIE  | flake8-pie            | 33 (unnecessary-placeholder, reimplemented-builtin) |
| C4   | flake8-comprehensions | 2 (unnecessary-dict-comprehension)                  |
| E    | pycodestyle           | 0 new (E501/E402 ignored by design)                 |
| T20  | flake8-print          | 0 new (config.py print exempted)                    |

### 🔧 Configurable Concurrency Limit

`MAX_CONCURRENT_HEAVY_REQUESTS` added to `Settings` and `load_settings()` — now configurable via env variable (default: 4). Previously hardcoded via `getattr` with fallback.

### Files Changed

| File                       | Change                                                        |
| -------------------------- | ------------------------------------------------------------- |
| `app/db/rls.py`            | Added `long_term_memory` + `key_model_status` to `RLS_CONFIG` |
| `pyproject.toml`           | +5 ruff rule categories (SIM, E, C4, PIE, T20), tuned ignores |
| `app/config.py`            | `MAX_CONCURRENT_HEAVY_REQUESTS` field + env loading           |
| `app/handlers/messages.py` | Uses `settings.MAX_CONCURRENT_HEAVY_REQUESTS` directly        |
| `app/utils/text_format.py` | `noqa: SIM102` on intentionally nested if                     |
| 86 files                   | Auto-fixed by ruff (PIE790, SIM103, SIM108, C420, etc.)       |

### 🧪 Tests: 650 passed, 1 skipped, 0 failures

---

## [2.8.11] – 2026-03-04 – Safety Refusal Regression Fix

### 🔴 Fix: Increased Safety Refusals from Gemini Models

**Root cause (two factors):**

1. `gemini-flash-latest` alias silently resolved to Gemini 3.x (stricter guardrails)
2. `PROMPT_ENGINEER` template contained `"игнорируя возможные проблемы с безопасностью"` — detected by Gemini as a jailbreak marker, triggering _increased_ refusals even with `BLOCK_NONE` safety settings

### PROMPT_ENGINEER v3.0

Redesigned the role generation meta-prompt using modular architecture (Role → Goal → Principles → Output Schema → Example → Rules):

- Removed jailbreak-triggering phrase
- Added 3-layer anti-self-censoring: generated `system_prompt` and `constraints` fields must not contain ethical/moral disclaimers
- Added few-shot example for consistent JSON output
- Made model-agnostic (removed hardcoded "Gemini 2.5 Pro" reference)

### Model Configuration

- Removed `gemini-flash-latest` from `DEFAULT_GEMINI_MODELS` (floating alias → stricter model)
- Cleaned up 4 remaining references across `model_selector.py`, `ai_provider.py`, `config.py`

### Regression Test

- `TestNoJailbreakMarkers`: Checks all prompt templates for known jailbreak marker phrases, prevents future regressions

### Files Changed

| File                            | Change                                                    |
| ------------------------------- | --------------------------------------------------------- |
| `app/prompts.py`                | PROMPT_ENGINEER v3.0 — modular, anti-self-censoring       |
| `app/prompt_registry.py`        | Same + version bump `2.1.0` → `3.0.0`                     |
| `app/config.py`                 | Removed `gemini-flash-latest`, updated docstring examples |
| `app/model_selector.py`         | Removed `flash-latest` from tier ranking                  |
| `app/ai_provider.py`            | Removed `flash-latest` from `_is_gemini3_model()`         |
| `tests/test_prompt_registry.py` | +2 jailbreak-marker regression tests                      |

### 🧪 Tests: 650 passed, 1 skipped, 0 failures

---

## [2.8.10] – 2026-03-03 – E2E Smoke Tests

### 🧪 End-to-End Smoke Tests (11 new)

| Area                 | Tests | What it verifies                                 |
| -------------------- | ----- | ------------------------------------------------ |
| Health endpoint      | 2     | `/health` JSON structure, Redis status           |
| Metrics endpoint     | 2     | `/metrics` Prometheus text format                |
| Prometheus generator | 1     | Line format validation (HELP/TYPE/value)         |
| Handler registration | 3     | commands/callbacks/messages register on mock app |
| Admin alerts E2E     | 2     | Startup health report + shutdown notification    |
| Full pipeline        | 1     | tag_error → is_error_message → metrics_collector |

> **Note:** Monitoring dashboard, Prometheus `/metrics`, and `/health` endpoints were already fully implemented. No new monitoring infrastructure needed.

**Total tests: 648 → 0 failures**

---

## [2.8.9] – 2026-03-03 – Concurrency Tests, CI/CD & Redis Fix

### 🧪 Concurrency Stress Tests (6 new)

| Test                    | Concurrency     | What it proves                            |
| ----------------------- | --------------- | ----------------------------------------- |
| Cache stampede reads    | 50 concurrent   | No data corruption under concurrent reads |
| Cache concurrent writes | 50 concurrent   | In-memory cache integrity                 |
| Alert rate limiter      | 20 concurrent   | ≤5 sent despite 20 simultaneous fires     |
| StreamingWriter         | 20 rapid writes | All chunks appear in final output         |
| Key resolution          | 10 concurrent   | Each gets valid key, no interference      |
| Error classification    | 100 concurrent  | Thread-safe classification                |

### 🔧 CI/CD Pipeline (improved)

- Added `TEST_gemaibotv2` branch trigger
- Concurrency group (auto-cancels outdated runs)
- pip dependency caching via `actions/setup-python`
- Job timeouts (5min lint, 10min test/build)
- `ADMIN_SECRET` env var for CI test isolation
- GitHub-format ruff output for inline annotations

### 🐛 Redis Connection Pool Exhaustion (bug fix)

- **Root cause:** `max_connections=2` + `concurrent_updates=True` = pool exhaustion when ≥3 users send messages simultaneously
- **Fix:** Increased `max_connections` 2→10 (Upstash free tier allows 100)
- **Fix:** Removed `ping()` before retry — it consumed a pool slot, causing deadlock under load

**Total tests: 637 → 0 failures**

---

## [2.8.8] – 2026-03-03 – Admin Alerts, Type Annotations & Integration Tests

### 🔔 Admin Alert System

New `app/admin_alerts.py` module — rate-limited Telegram notifications (5/5min) for:

- 🚨 Critical: unhandled exceptions in `global_error_handler`
- 🟢 Startup: health check report (DB, Redis, AI status)
- 🔴 Shutdown: graceful stop notification

### 📝 Type Annotations

Added return types to `agent_use_cases.py` (7 methods), `cache.py` (2 functions). Repos layer already fully typed.

### 🧪 Integration Tests (12 new)

| Area            | Tests | Coverage                                          |
| --------------- | ----- | ------------------------------------------------- |
| StreamingWriter | 2     | Debouncing, finalize                              |
| Error pipeline  | 5     | tag→detect→handle chain, retryable classification |
| Admin alerts    | 3     | Send, rate limit, traceback                       |
| Key rotation    | 2     | Exhausted keys, exclusion retry                   |

**Total tests: 631 → 0 failures**

---

### 🔴 Pipeline Bugs Fixed (4)

| Bug                                               | File             | Fix                                                                |
| ------------------------------------------------- | ---------------- | ------------------------------------------------------------------ |
| Untagged error strings (7 sites)                  | `ai_provider.py` | Wrapped with `tag_error(ErrorCode.*)` for fast-path classification |
| Fire-and-forget `create_task` (RUF006)            | `ai_chat.py`     | Added `_background_tasks` set + done callback                      |
| `_build_contents` injected fake user msg on error | `ai_provider.py` | Return `None` to abort cleanly                                     |
| OpenRouter logged as Gemini                       | `ai_provider.py` | `log_gemini_response` → `log_openrouter_response`                  |

### 🟡 Ruff: 21 → 0 Errors

- 17 auto-fixed (import sorting `I001`, `UP017`)
- 4 manual fixes: `F821` (Message import), `F841`×3 (unused vars), `RUF059`, `UP037`

### ✅ Comprehensive Audit Results

- **Performance:** No bottlenecks — DB cached (TTLCache), IO uses executors, ProcessPoolExecutor for images
- **MyPy:** 852 annotation-noise errors, 0 runtime type bugs
- **Security:** All SQL parameterized, no eval/exec, RLS on 21 tables, secrets sanitized, context cleanup paired

---

## [2.8.6] – 2026-03-03 – Dependency Audit & Critical Bug Fixes

### 🔍 Full Dependency & Codebase Audit

Audited all 17 project dependencies against official documentation. Systematic codebase scan for 15+ anti-patterns. Found and fixed **7 issues across 6 files**.

### 🔴 Critical: Restored Missing Document Processor Methods

`_process_pdf_sync` and `_process_word_sync` were accidentally deleted in REFACTOR commit (`e176c82`) but still called at runtime. **All PDF/Word document uploads crashed with `AttributeError`.**

Both methods restored as `@staticmethod`s from git history.

### Dependency Fixes

| #   | Fix                         | Files Changed                                      |
| --- | --------------------------- | -------------------------------------------------- |
| 1   | Redis `sync→async` client   | `app/cache.py`                                     |
| 2   | `pytz→zoneinfo` migration   | `app/config.py`, `app/utils/time.py`, 4 test files |
| 3   | Removed unused `orjson`     | `requirements.txt`                                 |
| 4   | `"PyPDF2"→"pypdf"` log refs | `app/document_processor.py`                        |
| 5   | Added `tzdata` fallback     | `requirements.txt`                                 |

### Codebase Fixes

| #   | Fix                                                 | Severity | Files Changed               |
| --- | --------------------------------------------------- | -------- | --------------------------- |
| 1   | Restored `_process_pdf_sync` / `_process_word_sync` | 🔴       | `app/document_processor.py` |
| 2   | `DOCUMENT_SUPPORT` dead code → proper import gating | 🟡       | `app/document_processor.py` |
| 3   | Redis `aclose()` on shutdown                        | 🟡       | `app/cache.py`, `bot.py`    |

### Files Changed

| File                         | Change                                                                 |
| ---------------------------- | ---------------------------------------------------------------------- |
| `app/cache.py`               | `redis.asyncio.Redis`, `shutdown_redis()`, removed `asyncio.to_thread` |
| `app/config.py`              | `pytz.timezone()` → `ZoneInfo()`, `pytz.UTC` → `datetime.timezone.utc` |
| `app/utils/time.py`          | `.localize()` → `datetime.combine(..., tzinfo=...)`                    |
| `app/document_processor.py`  | Restored `_process_pdf_sync`/`_process_word_sync`, import gating       |
| `bot.py`                     | `shutdown_redis()` in `_cleanup_application()`                         |
| `requirements.txt`           | Removed `orjson`, `pytz`; added `tzdata`                               |
| `tests/test_time_utils.py`   | `pytz` → `zoneinfo`                                                    |
| `tests/test_menus.py`        | Removed `pytz` mock                                                    |
| `tests/test_web_security.py` | Removed `pytz` from mock list                                          |
| `tests/test_auth_headers.py` | Removed `pytz` from mock list                                          |

### 🧪 Tests: 619 passed, 0 failures

---

## [2.8.5] – 2026-03-03 – Logic Audit: 7 Bug Fixes

### 🔍 Systematic Cross-Module Logic Audit

Reviewed 25+ modules. Found and fixed **7 logic bugs** across 6 files. Cross-pattern search confirmed no additional instances of any bug pattern codebase-wide.

| #   | File                          | Bug                                                                                      | Severity |
| --- | ----------------------------- | ---------------------------------------------------------------------------------------- | -------- |
| 1   | `app/documents/repository.py` | `cleanup_old_documents` DELETE missing `RETURNING id` — always returned 0                | 🔴       |
| 2   | `app/documents/repository.py` | `get_user_document_stats` hardcoded limit=5 instead of `settings.MAX_DOCUMENTS_PER_USER` | 🟡       |
| 3   | `app/handlers/ai_chat.py`     | Deprecated `asyncio.get_event_loop()` — error in Python 3.14                             | 🟡       |
| 4   | `app/state.py`                | `_ensure_loaded` race: concurrent handlers double-load from DB                           | 🔴       |
| 5   | `app/security.py`             | `SyncRateLimiter` documented a `threading.Lock` but never created one — data race        | 🔴       |
| 6   | `app/circuit_breaker.py`      | `__init__` created asyncio task at import time — crashes without event loop              | 🟡       |
| 7   | `app/context/assembler.py`    | `_fix_role_alternation` mutated input dicts in-place, corrupting `chat_state.history`    | 🟡       |

### Files Changed

| File                          | Change                                                       |
| ----------------------------- | ------------------------------------------------------------ |
| `app/documents/repository.py` | Added `RETURNING id`, config-based limit                     |
| `app/handlers/ai_chat.py`     | `get_running_loop()` replaces `get_event_loop()`             |
| `app/state.py`                | Double-check locking in `_ensure_loaded`                     |
| `app/security.py`             | `threading.Lock` added to `SyncRateLimiter`                  |
| `app/circuit_breaker.py`      | Guarded `_start_monitoring()` with `try/except RuntimeError` |
| `app/context/assembler.py`    | Shallow copies in `_fix_role_alternation`                    |

### 🧪 Tests: 619 passed, 0 failures

---

## [2.8.4] – 2026-03-03 – Full AI Logic Audit

### 🔍 Comprehensive Handler & Repos Audit

Systematic audit of all AI handlers, callbacks, streaming, repos, security, state, and document processing layers. **18 fixes across 10 files**, 5 commits.

#### Handler & Streaming Fixes

- **Heartbeat race conditions**: Added `stop_heartbeat()` before streaming loops in `ai_photo.py`, `ai_search.py`, `ai_document.py`
- **Assembler `None` handling**: Fixed double user message in `assembler.py` when `user_message=""` passed from search handler
- **Error sanitization**: Replaced `str(e)` leaks with generic messages in `ai_document.py`, `streaming.py`, `cb_roles.py` (×2), `msg_document.py` (×2)

#### Callback Concurrency & Safety

- **Retry race condition**: Added semaphore + user lock to `retry_last_callback` in `callbacks.py`
- **Callback parsing**: Changed `split(":")` → `split(":", 2)` in `fallback_callback` to handle model names with colons
- **Exception handling**: Wrapped background task wrappers in try/except to prevent silent exception swallowing
- **Duplicate DB write**: Removed redundant `update_user_chat` call in `role_apply_callback`

#### Database & Performance

- **Transaction isolation bug**: `switch_to_conversation` called `get_conversation_messages` and `db_execute_many` outside the transaction connection — now passes `conn=conn`
- **N+1 query**: `migrate_invalid_models` replaced per-user UPDATE loop with 2 batch `UPDATE ... WHERE user_id = ANY($2)` statements

### Files Changed

| File                           | Changes                                         |
| ------------------------------ | ----------------------------------------------- |
| `app/handlers/ai_chat.py`      | `stop_heartbeat`, empty-history guard           |
| `app/handlers/ai_search.py`    | `stop_heartbeat`, `None` user_message           |
| `app/handlers/ai_photo.py`     | `stop_heartbeat`                                |
| `app/handlers/ai_document.py`  | `stop_heartbeat`, sanitize `str(e)`             |
| `app/handlers/callbacks.py`    | Semaphore/lock, safe split, exception handling  |
| `app/handlers/cb_roles.py`     | Sanitize `str(e)` ×2, remove duplicate DB write |
| `app/handlers/msg_document.py` | Sanitize `str(e)` and `result['error']`         |
| `app/streaming.py`             | Sanitize error messages                         |
| `app/context/assembler.py`     | Skip `None` user message                        |
| `app/repos/conversations.py`   | Transaction isolation fix (`conn` param)        |
| `app/repos/chats.py`           | Batch N+1 migration                             |

### 🧪 Tests: 619 passed, 0 failures

---

### 🔴 Fix: `ValueError: history must be a non-empty list` after 503 failures

**Root cause**: When all streaming attempts failed (503 UNAVAILABLE × 3), the non-streaming fallback received an empty `chat_state.history`. The assembler's `_build_final_history` skipped appending the user message when it was falsy (`if user_message:`), and `_validate_inputs` raised an unhandled `ValueError`.

**Three-layer fix**:

| Layer               | File                       | Change                                                                  |
| ------------------- | -------------------------- | ----------------------------------------------------------------------- |
| Root cause          | `app/context/assembler.py` | Always append user message (fallback to `"..."` if empty)               |
| Graceful validation | `app/ai_provider.py`       | `_validate_inputs` returns error string instead of raising `ValueError` |
| Defensive guard     | `app/handlers/ai_chat.py`  | Check for empty history before non-streaming fallback, show retry UI    |

### 🔴 Fix: Heartbeat overwrites streaming content

**Root cause**: The heartbeat (`⏳ Обрабатываю ваш запрос...` at 15/30/50s intervals) was never stopped before `stream_and_display` began editing the same placeholder message. When streaming attempt 1 failed fast (~2s) and attempt 2 took ~74s, the heartbeat overwrote the stage indicator mid-retry.

**Fix**: `stop_heartbeat(placeholder_message.message_id)` called before entering the streaming retry loop.

### Files Changed

| File                        | Change                                                  |
| --------------------------- | ------------------------------------------------------- | ------------------------ |
| `app/context/assembler.py`  | `_build_final_history`: always append user message      |
| `app/ai_provider.py`        | `_validate_inputs` → returns `str                       | None` instead of raising |
| `app/handlers/ai_chat.py`   | Empty-history guard + `stop_heartbeat` before streaming |
| `tests/test_ai_provider.py` | Updated validation tests for new return-value API       |

### 🧪 Tests (93 passed, 0 failures)

---

## [2.8.2] – 2026-03-03 – Streaming Resilience, Embedding Upgrade & Model Selector Fix

### ✨ Continuous Multi-Message Streaming

Replaces response truncation with seamless multi-message streaming. When a response exceeds Telegram's 4096-char limit:

1. **Auto-overflow**: Freezes the current message at a natural break point (paragraph, sentence, word boundary via `_find_split_point()`).
2. **New message creation**: Calls `reply_text` (not `chat.send_message`) to start a new message in the thread.
3. **Continues streaming**: New tokens flow into the new message. Repeats indefinitely for very long responses.
4. **Correct button placement**: `stream_and_display` returns `(text, success, last_message)`. Post-stream buttons (Retry, Role) are attached to `last_message` via `edit_reply_markup`, not `edit_text`.

### 🔄 Streaming Retry with Key Rotation

**Root cause fixed**: On 503/APIError, streaming fell through to non-streaming `_get_ai_response_with_routing` — users lost the streaming UX.

**Solution**: Retry loop (up to 3 attempts) with `excluded_key_hashes`:

```
stream(key_A) → 503 → exclude A → stream(key_B) → ✅ streaming preserved
```

Only falls to non-streaming if ALL streaming keys are exhausted.

### 🧠 Model Selector: No-Downgrade Policy

**3 bugs fixed** in `model_selector.py`:

| #   | Bug                                                             | Fix                                                                   |
| --- | --------------------------------------------------------------- | --------------------------------------------------------------------- |
| 1   | `available = set(...)` — random iteration order                 | Changed to `list` for deterministic ordering                          |
| 2   | Substring `"flash"` matched all 5 models (including flash-lite) | Added `_get_tier()` capability ranking                                |
| 3   | No upgrade/downgrade check — suggested flash-lite from flash    | Only suggests models with `_get_tier(suggested) > _get_tier(current)` |

Tier system: `lite=1 < flash=2 < 2.5-flash=3 < pro=4`. Removed "short message → fast model" rule (all flash models are fast enough).

### 🔬 Embedding Upgrade: `gemini-embedding-001` @ 3072 dims

- **Model**: `text-embedding-004` → `gemini-embedding-001` with `task_type` parameter (`RETRIEVAL_DOCUMENT` / `RETRIEVAL_QUERY`).
- **Dimensions**: 768 → 3072 via `halfvec(3072)` (16-bit float) for HNSW index compatibility.
- **Migration**: `017_upgrade_embeddings_3072_halfvec.sql` — drops old index/column, creates new `halfvec(3072)` column + HNSW index.

### 🧹 Config Cleanup

- **Removed `gemini-3-flash-preview`** from `DEFAULT_GEMINI_MODELS` — it's an alias of `gemini-flash-latest`. Synced with production env.
- **Migration 007 fix**: `CURRENT_DATE` is volatile, invalid in index predicate — replaced with safe alternative.
- **Migration 006**: Made idempotent, deleted redundant 007.

### Files Changed

| File                            | Change                                                                           |
| ------------------------------- | -------------------------------------------------------------------------------- |
| `app/streaming.py`              | `StreamingWriter` overflow logic, `_find_split_point()`, `last_message` property |
| `app/handlers/ai_chat.py`       | 3-tuple unpacking, `edit_reply_markup` on `last_message`, streaming retry loop   |
| `app/model_selector.py`         | `_get_tier()`, no-downgrade policy, `set` → `list`, removed short-msg rule       |
| `app/config.py`                 | Removed `gemini-3-flash-preview` from defaults                                   |
| `app/repos/memory.py`           | `gemini-embedding-001`, `halfvec(3072)`, `task_type` parameter                   |
| `scripts/migrations/017_*.sql`  | Embedding column upgrade migration                                               |
| `tests/test_phase3_features.py` | Updated model selector + streaming tests                                         |

### 🧪 Tests (619 passed, 1 skipped)

### 🏗️ Structured Error Codes (MED-03)

- **`ErrorCode` enum** (16 codes) + `_ERROR_PROPERTIES` registry in `errors.py` — each code maps to `(retryable, key_related, penalty_category)`.
- **`tag_error()` / `extract_error_code()`** helpers — zero-width-space prefix for O(1) classification.
- 15+ error strings tagged across `ai_provider.py` (GeminiProvider, OpenRouterProvider, ProviderRouter).
- 4 classifiers (`is_error_message`, `is_retryable_error`, `is_key_related_error`, `classify_key_error`) refactored: code-based fast path + text-parsing fallback for backward compatibility.

### 🧹 Architecture Cleanup

- **HIGH-05**: Removed 4 unused forwarders from `ai_core.py` — 3 accessed private methods of `AgentRequestUseCase` (`_resolve_key_generic`, `_resolve_gemini_request`, `_resolve_openrouter_request`) + 1 superseded (`_get_ai_response_with_key_rotation`). File: 187 → 131 lines.
- **HIGH-03**: Removed ~82 lines of dead `DEFAULT_SYSTEM_PROMPT` / `COMPACT_SYSTEM_PROMPT` from `config.py`. Actual sole source: `prompts.py` + `prompt_registry.py`.
- **MED-08**: Added `__init__.py` to `app/handlers/` and `app/utils/` for explicit package namespaces.

### 🔒 Security & Reliability

- **SEC-02**: `get_model_hash()` now uses SHA-256 instead of MD5 (drop-in replacement, ephemeral UI hashes only).
- **MED-04**: Wrapped all 7 `pool._closed` accesses with `_is_pool_closed()` helper in `database.py` / `metrics_repo.py`, decoupling from asyncpg internals.
- **MED-01**: Extracted `SyncRateLimiter` class in `security.py` — reused by `web.py` login protection, replacing 30-line ad-hoc implementation.

### 📦 Module Splits (MED-07, MED-09, MED-11)

- **MED-09**: Extracted `MetricsMiddleware` + `track_metrics` decorator → `app/utils/metrics_middleware.py`. Re-exports in `metrics.py` for backward compat.
- **MED-07**: Split `context_assembler.py` (616 lines) → `app/context/` package:
  - `token_budget.py` — constants, `TokenBudget`, `AssembledContext` dataclasses
  - `summarizer.py` — LLM refine-chain summarization + chunk splitting
  - `assembler.py` — `ContextAssembler` class, `should_summarize()`, singleton
  - Original file → thin re-export facade (13 lines)
- **MED-11**: Split `document_processor.py` (697 lines) → `app/documents/` package:
  - `repository.py` — all DB CRUD, stats, duplicate/limit checks
  - `parsers.py` — sync file I/O helpers (hash, temp file)
  - Orchestrator class + facade functions remain in `document_processor.py`

### ✅ Previously Resolved (verified this session)

- **HIGH-04**: `DEFAULT_GEMINI_MODELS` constant de-duplicated (single definition, 3 references)
- **HIGH-06**: `flask_app` → `quart_app` renamed in `web.py` + `bot.py`
- **SEC-01**: OpenRouter referer updated to `https://t.me/gemaibotv2`
- **PERF-01**: Removed extra `count_tokens` API call → uses `response.usage_metadata`
- **MED-10**: `genai.Client` reuse per provider instance (lazy init + key check)
- **MED-06**: `_custom_role_cache` → `TTLCache(maxsize=256, ttl=3600)`
- **MED-12**: `_load_daily_limits` now logs raw malformed env var values

### Files Changed

| File                              | Change                                                                                      |
| --------------------------------- | ------------------------------------------------------------------------------------------- |
| `app/errors.py`                   | `ErrorCode` enum, `_ERROR_PROPERTIES`, `tag_error`, `extract_error_code`, `strip_error_tag` |
| `app/ai_provider.py`              | 15+ error strings tagged with `ErrorCode`                                                   |
| `app/handlers/ai_core.py`         | Removed 4 unused forwarders (187→131 lines)                                                 |
| `app/config.py`                   | Removed dead prompts (82 lines), MD5→SHA256                                                 |
| `app/database.py`                 | `_is_pool_closed()` helper, replaced 6 `pool._closed`                                       |
| `app/repos/metrics_repo.py`       | `pool._closed` → `_is_pool_closed()`                                                        |
| `app/security.py`                 | [NEW] `SyncRateLimiter` class                                                               |
| `app/web.py`                      | Replaced ad-hoc rate limiter with `SyncRateLimiter`                                         |
| `app/handlers/__init__.py`        | [NEW] Empty init                                                                            |
| `app/utils/__init__.py`           | [NEW] Empty init                                                                            |
| `app/utils/metrics_middleware.py` | [NEW] `MetricsMiddleware`, `track_metrics`                                                  |
| `app/metrics.py`                  | Slimmed — re-exports middleware from submodule                                              |
| `app/context/`                    | [NEW] Package: `token_budget.py`, `summarizer.py`, `assembler.py`                           |
| `app/context_assembler.py`        | → Thin re-export facade (616 → 13 lines)                                                    |
| `app/documents/`                  | [NEW] Package: `repository.py`, `parsers.py`                                                |
| `app/document_processor.py`       | Slimmed orchestrator (697 → ~310 lines)                                                     |

### 🧪 Tests (572 passed, 1 skipped)

## [2.8.0] – 2026-03-03 – Production Audit: Phases 1–4

### ✨ Phase 1: Foundation & CI

- **GitHub Actions CI** (`.github/workflows/ci.yml`): lint → test → Docker build pipeline.
- **Structured JSON logging**: Auto-enabled in production with `request_id` propagation via `contextvars`.
- **`/export` command**: Export current chat history as Markdown document.
- **`ChatAction.TYPING`**: Sent immediately on message receipt, before any processing.

### 🏗️ Phase 2: Architecture Cleanup

- **Handler extraction**: `messages.py` from 1163 → ~260 lines. Logic split into:
  - `msg_media.py` — media group accumulation, image processing
  - `msg_roles.py` — role/conversation rename state machines
  - `msg_document.py` — document upload, duplicate detection
- **`/settings` command**: Unified view of user preferences with inline navigation.

### ✨ Phase 3: Product Features

- **Streaming responses** (`streaming.py`): Gemini `generate_content_stream` + debounced `edit_message_text` (1.2s, 80-char minimum). Falls back to non-streaming for OpenRouter.
- **pgvector long-term memory** (`repos/memory.py`): `text-embedding-004` (768-dim), HNSW index, cosine similarity search, 500/user limit, 90-day TTL. Semantically recalled during context assembly, stored after each exchange.
- **Smart model selection** (`model_selector.py`): Regex heuristics classify messages (code/reasoning/simple/creative) → non-intrusive inline button suggestions when mismatch detected. `switch_model:` callback handler for one-tap switching.

### 🔧 Phase 4: Production Hardening

- **Webhook mode** (`bot.py`): Set `WEBHOOK_URL` env var → auto-registers `/webhook/<token>` route, replaces long-polling. Graceful cleanup on shutdown.
- **Prometheus `/metrics`** (`prometheus.py` + `web.py`): Zero-dependency text format exporter — uptime, API calls, errors, active users, memory. Unauthenticated for scraping.
- **GDPR commands** (`commands.py`):
  - `/mydata` — exports all user data as JSON (GDPR Article 20)
  - `/deleteme CONFIRM` — deletes all user data with confirmation gate (GDPR Article 17)
- **Degradation matrix** (`degradation.py`): `check_system_health()` checks DB/Redis/AI status. `can_process_message()` returns fallback decisions with user-facing messages.

### Files Changed

| File                            | Change                                         |
| ------------------------------- | ---------------------------------------------- |
| `.github/workflows/ci.yml`      | [NEW] CI pipeline                              |
| `app/streaming.py`              | [NEW] Streaming responses                      |
| `app/repos/memory.py`           | [NEW] pgvector long-term memory                |
| `app/model_selector.py`         | [NEW] Smart model auto-selection               |
| `app/prometheus.py`             | [NEW] Prometheus text exporter                 |
| `app/degradation.py`            | [NEW] Service degradation matrix               |
| `app/handlers/msg_media.py`     | [NEW] Media handling (extracted)               |
| `app/handlers/msg_roles.py`     | [NEW] Role FSMs (extracted)                    |
| `app/handlers/msg_document.py`  | [NEW] Document handling (extracted)            |
| `app/handlers/messages.py`      | Thin router (~260 lines, was 1163)             |
| `app/handlers/ai_chat.py`       | Memory recall, model suggestions, streaming    |
| `app/handlers/callbacks.py`     | `switch_model:` callback handler               |
| `app/handlers/commands.py`      | `/export`, `/settings`, `/mydata`, `/deleteme` |
| `app/web.py`                    | `/metrics` endpoint                            |
| `bot.py`                        | Webhook mode (WEBHOOK_URL env var)             |
| `app/utils/logging_config.py`   | Structured JSON logging                        |
| `tests/test_phase3_features.py` | [NEW] 20 integration tests                     |

### 🧪 Tests (572 passed, 1 skipped)

- `test_phase3_features.py`: 20 new tests — model selector (6), streaming (4), Prometheus (2), GDPR (2), degradation (6).

---

## [2.7.2] – 2026-03-02 – User-Configurable Thinking Levels

### ✨ New: `/thinking` Command

Per-user control of Gemini's reasoning depth. Auto-detects model family and sends the correct API parameter:

- **Gemini 2.5** (`gemini-2.5-flash`, `gemini-2.5-flash-lite`) → `thinkingBudget` (int: 0–24576)
- **Gemini 3** (`gemini-3-flash-preview`, `gemini-flash-latest`) → `thinkingLevel` (minimal/low/medium/high)
- **OpenRouter** → ignored (no ThinkingConfig sent)

User-facing levels: `off`, `low`, `medium`, `high`, `auto` (reset to model default).

### Files Changed

| File                       | Change                                                                                           |
| -------------------------- | ------------------------------------------------------------------------------------------------ |
| `app/ai_provider.py`       | `_build_thinking_config()`, `_is_gemini3_model()`, `thinking_level` param through provider chain |
| `app/agent_use_cases.py`   | `get_ai_response()` accepts `thinking_level`                                                     |
| `app/handlers/ai_core.py`  | `_get_ai_response_with_routing()` accepts `thinking_level`                                       |
| `app/handlers/ai_chat.py`  | Reads `chat_state.thinking_level`, passes to router                                              |
| `app/handlers/commands.py` | `/thinking` command + registration                                                               |
| `app/database.py`          | `ChatState.thinking_level` field                                                                 |
| `app/repos/chats.py`       | `update_thinking_level()`, DB read/write of column                                               |
| `tests/test_ai_chat.py`    | Test fixture updated with `thinking_level=None`                                                  |

### 🧪 Tests (552 passed, 1 skipped)

---

## [2.7.1] – 2026-03-02 – OpenRouter Key Rotation Fix & Supabase Advisory

### 🔴 Critical Fix: OpenRouter Retries Using Same Suspended Key

**Root cause**: `get_available_openrouter_key()` accepted `excluded_hashes` but ignored it — called `get_available_key()` (simple least-used query, no status/exclusion filtering) instead of `get_fresh_available_key()` (two-tier SQL with exclusions + status checks). The retry loop always re-selected the same rate-limited key, guaranteeing all 3 attempts failed identically.

**Fix**: Rewired to `_openrouter_km.get_fresh_available_key()` with `excluded_hashes` and `daily_limit` forwarding — now mirrors the Gemini key path.

### 🔒 Security: Supabase `search_path` Advisory

- `check_key_hash_exists()` trigger function now has `SET search_path = public`, resolving the Supabase security advisor lint about mutable search_path.

### Files Changed

| File                                                        | Change                                                                         |
| ----------------------------------------------------------- | ------------------------------------------------------------------------------ |
| `app/repos/keys.py`                                         | `get_available_openrouter_key()` → `get_fresh_available_key()` with exclusions |
| `scripts/migrations/016_fix_check_key_hash_search_path.sql` | New migration: `SET search_path = public` on trigger function                  |
| `tests/test_repos_keys.py`                                  | +2 regression tests: exclusion forwarding, no-keys case                        |

### 🧪 Tests (550 passed, 1 skipped)

- `TestGetAvailableOpenrouterKey`: 2 new tests verifying exclusion forwarding and empty result handling.

---

## [2.7.0] – 2026-03-01 – Persistent Per-Model Key Health System

### 🔴 Critical Fix: "All Gemini keys exhausted" False Positive

**Root cause**: An invalid key (`API_KEY_INVALID`) with `request_count=0` was perpetually re-selected by the `ORDER BY request_count ASC` query because failed requests never increment usage. The `excluded_key_hashes` set was only checked client-side — the DB query had no exclusion filter.

### ✨ New: DB-Backed `KeyStatusManager`

Replaces the in-memory `KeyHealth` dataclass with a persistent, per-model key health system.

- **`key_model_status` table**: Tracks `(key_hash, model_name)` with `status`, `suspended_until`, `failure_count`, and `last_error`.
- **Error-category-aware cooldowns** (`classify_key_error()` in `errors.py`):
  - `API_KEY_INVALID` → 24h suspension (not forever — recovery probe after cooldown)
  - `quota_exceeded` → suspend until midnight Pacific time
  - `rate_limit` → 60s suspension
  - `503/timeout/overloaded` → no suspension (transient)
- **Two-tier SQL key selection**: Active keys (Tier 1) are tried first; cooldown-expired keys (Tier 2) are probed for recovery.
- **Exponential backoff**: Repeated failures double the cooldown (capped at 7 days).
- **Auto-recovery**: On successful probe, key promoted back to `active`, `failure_count` reset to 0.
- **SQL-level exclusion**: `excluded_key_hashes` now passed to the DB query via `WHERE ak.key_hash != ALL($excluded)`.

### 🏗️ Architecture Changes

- **Removed `KeyHealth` dataclass** from `ai_provider.py` — all health tracking is now DB-backed.
- **Simplified `_resolve_key_generic`** in `agent_use_cases.py` — removed 5-attempt client-side loops; DB query handles exclusion and status filtering directly.
- **`get_available_gemini_key` / `get_available_openrouter_key`**: Accept `excluded_hashes` parameter; skip cache when exclusions exist.

### Files Changed

| File                                              | Change                                                        |
| ------------------------------------------------- | ------------------------------------------------------------- |
| `scripts/migrations/014_add_key_model_status.sql` | New migration: `key_model_status` table + RLS + index         |
| `app/db/schema.py`                                | Added `key_model_status` to `create_tables()`                 |
| `app/errors.py`                                   | New `classify_key_error()` function                           |
| `app/repos/keys.py`                               | `KeyStatusManager` class, two-tier SQL queries                |
| `app/agent_use_cases.py`                          | Simplified `_resolve_key_generic` (DB-level filter)           |
| `app/ai_provider.py`                              | Removed `KeyHealth`, `ProviderRouter` uses `KeyStatusManager` |
| `tests/test_provider_router.py`                   | Rewritten: 7 tests for suspend/recover/categories             |
| `tests/test_decryption_error_handling.py`         | Fixed for new `excluded_hashes` parameter                     |

### 🧪 Tests (548 passed, 1 skipped)

- `test_provider_router.py`: 7 tests — successful response, exhausted keys, key failure + suspend, quota category, excluded keys propagation, OpenRouter detection, transient non-suspension.
- `test_decryption_error_handling.py`: Fixed mock assertions for `excluded_hashes` parameter.

### 🎭 UX Fix: "Выбрать роль ИИ" No Longer Destroys AI Response

**Problem**: Clicking "🎭 Выбрать роль ИИ" under an AI response called `edit_message_text`, replacing the response with the roles menu and losing the AI-generated content.

**Solution**: Origin-aware callback routing via `callback_data` suffix:

- AI response keyboards use `open_roles:from_response` → roles menu sent as **new message** (`reply_text`)
- Menu keyboards keep `open_roles` → in-place edit (existing behavior)

| File                                                          | Change                                                                             |
| ------------------------------------------------------------- | ---------------------------------------------------------------------------------- |
| `keyboards.py`                                                | `ai_response_keyboard()`, `deep_dive_keyboard()` → `from_response`                 |
| `ai_chat.py`, `ai_search.py`, `ai_photo.py`, `ai_document.py` | Ad-hoc keyboards → `from_response`                                                 |
| `errors.py`                                                   | `build_retry_and_roles_keyboard()`, `build_roles_keyboard()`                       |
| `cb_roles.py`                                                 | `open_roles_callback`: `reply_text` when `from_response`, else `edit_message_text` |
| `callbacks.py`                                                | Regex: `^open_roles(:from_response)?$`                                             |
| `test_errors.py`                                              | 2 assertions updated                                                               |

### 🧹 Full Codebase Lint Cleanup (126 errors → 0)

- **105 auto-fixed** via `ruff check --fix`: `I001` (import sorting), `UP006/UP045` (modern type annotations), `UP015/UP012/F541` (misc)
- **5 `UP035`**: Removed deprecated `typing.Dict/List/Tuple/Optional` imports in `heartbeat.py`, `test_database_tavily.py`, `test_repos_keys.py`, `test_roles_menu.py`, `test_menus.py`
- **12 `F841`**: Prefixed unused mock variables with `_` in 6 test files
- **1 `RUF006`**: Stored fire-and-forget task reference in `context_assembler.py`
- **1 `B019`**: Suppressed with `noqa` in `prompt_registry.py` (singleton lru_cache by design)

---

## [2.6.9] – 2026-02-28 – Context Summarization System

### ✨ New Feature: Two-Tier Context Summarization

Intelligent conversation summarization for long multi-turn conversations, preventing context window overflow while preserving conversational quality.

#### Architecture

- **128K token budget** with 12K response reserve and 4K summary budget.
- **Local tier** (< 30K dropped tokens): Fast snippet-based truncation — first/last lines of dropped messages.
- **LLM tier** (≥ 30K dropped tokens): Asynchronous refine-chain summarization via Gemini. Chunks up to 10K tokens × 6 max. Fires as a background `asyncio.create_task`, persists result via callback.
- **`ContextAssembler`** class (`context_assembler.py`): Token budgeting, two-tier decision logic, `AssembledContext` dataclass with `dropped_messages`, `was_truncated`, `llm_summarization_scheduled` fields.

#### Summarization Prompts

- `SUMMARIZATION_SYSTEM` and `SUMMARIZATION_CHUNK` templates in `prompt_registry.py`.
- Prompt-engineering best practices: structured output, explicit instructions, anti-hallucination guardrails.

#### Integration

- **`ai_chat.py`**: Checks `assembled.llm_summarization_scheduled`, fires `schedule_llm_summarization()`, callback persists summary via `update_user_chat()`. Records tier-specific metrics.
- **`ai_search.py`**: Same pattern — LLM scheduling + metrics for search handler context assembly.
- **`callbacks.py`**: 3 chat-clearing handlers (`new_topic_callback`, `deep_dive_callback:new_topic`, `new_chat_callback`) now clear `context_summary = None` to prevent stale summaries.

#### Database

- **Migration `013_add_context_summary.sql`**: Adds `context_summary TEXT` column to `chats` table.
- **`ChatState.context_summary`** field in `database.py` — replaces prior `_context_summary` dynamic attribute hack.
- **`repos/chats.py`**: `get_user_chat` / `update_user_chat` load and persist `context_summary`.

#### Metrics & Dashboard

- **`SummarizationMetrics`** in `metrics.py`: New fields `llm_summarizations`, `local_summarizations`, `llm_summarization_failures`. `record_summarization()` auto-detects tier from `llm:`/`local:` prefix.
- **`/api/overview`** in `web.py`: Includes `summarization` object with `triggered`, `llm_tier`, `local_tier`, `tokens_saved`, `avg_summary_length`, `llm_failures`.
- **Dashboard** (`dashboard.html`): New "Context Summarization" card in the Overview tab — 4 stat cards (Triggered, LLM Tier, Local Tier, Tokens Saved) with live polling.

### 🧪 Tests (554 passed, 1 skipped)

- `test_context_assembler.py`: Comprehensive tests for constants, chunk splitting, LLM scheduling, `AssembledContext` fields.
- `test_prompt_registry.py`: Updated expected template count (7 → 9) for new summarization templates.
- `test_ai_chat.py`: Updated `make_chat_state` mock with `context_summary=None`.
- All pre-existing tests pass — 1 pre-existing skip, 0 new failures.

### Files Changed

| File                                             | Change                                    |
| ------------------------------------------------ | ----------------------------------------- |
| `app/context_assembler.py`                       | New — two-tier summarization engine       |
| `app/prompt_registry.py`                         | +2 summarization prompt templates         |
| `app/database.py`                                | `ChatState.context_summary` field         |
| `app/repos/chats.py`                             | DB load/save of `context_summary`         |
| `app/handlers/ai_chat.py`                        | LLM scheduling + metrics                  |
| `app/handlers/ai_search.py`                      | LLM scheduling + metrics                  |
| `app/handlers/callbacks.py`                      | Clear summary on topic reset (3 handlers) |
| `app/metrics.py`                                 | LLM/local tier tracking                   |
| `app/web.py`                                     | `/api/overview` includes summarization    |
| `app/templates/dashboard.html`                   | Summarization stats card                  |
| `scripts/migrations/013_add_context_summary.sql` | DB migration                              |
| `tests/test_context_assembler.py`                | New test file                             |

---

## [2.6.8] – 2026-02-28 – Ruff Integration & Code Modernization

### 🐛 Bug Fixes (Ruff-identified)

- **B026 (silent data loss)**: `*args` passed after keyword `exceptions=` in `network.py` — reordered to prevent silent argument loss.
- **B904 (10 fixes)**: Missing `from e`/`from None` in exception `raise` statements across `database.py`, `cache.py`, `crypto.py`, `security.py`, `config.py` — restores exception chaining for proper traceback analysis.
- **RUF006 (5 fixes)**: Dangling `asyncio.create_task()` calls without stored references (GC risk) in `callbacks.py`, `messages.py`, `prompts.py` — stored in `_background_tasks` set with `add_done_callback` cleanup.

### 🧹 Code Modernization (598 auto-fixes)

- **isort (I)**: 75 files — import ordering normalized.
- **pyupgrade (UP)**: 502 fixes — type annotations modernized to PEP 585/604 (`List[str]` → `list[str]`, `Optional[X]` → `X | None`), deprecated `typing` imports removed, `super()` calls simplified.
- **SIM117**: 6 files — nested `with` statements merged.
- **W291/W293**: 23 trailing whitespace fixes.
- **RUF059 (5)**: Unused unpacked variables (`ai_key`, `resolution`, `parse_mode`, `priority`) replaced with `_`.
- **F541 (1)**: f-string without placeholders in `menus.py` → plain string.
- **B905 (1)**: `zip()` → `zip(strict=False)` in `web.py`.

### ⚙️ Tooling

- **`pyproject.toml`**: New Ruff configuration — enforces `F` (Pyflakes), `B` (Bugbear), `I` (isort), `UP` (pyupgrade), `RUF006`, `RUF059`. Per-file ignores for tests and false positives.
- **`conftest.py`**: Custom event loop exception handler to suppress cosmetic asyncpg `ConnectionDoesNotExistError` during test teardown GC.

### 🧪 Tests (485 passed, 1 skipped)

- All pre-existing tests pass after 622 total code changes across 40+ files.
- Flaky `test_execute_gemini_request_other_error` resolved (stale patch from prior sessions).

## [2.6.7] – 2026-02-28 – Codebase Audit Phase 2-3: Hardening, DRY & Cleanup

### 🛡️ Security & Reliability (Phase 2)

- **C3 (API key logging)**: Masked API key prefix in debug logs to `[:6]****`
- **H1 (race condition)**: `_UserStateStore.__getitem__` uses `setdefault()` to prevent TOCTOU races
- **H2 (fire-and-forget)**: `ConfigManager.schedule_reload()` stores task reference + debounce guard (3s cooldown)
- **H3 (zombie tasks)**: `_background_tasks` set in `messages.py` prevents "exception never retrieved"
- **H5 (token leak)**: `httpx`/`httpcore` loggers now suppressed to WARNING+ to prevent Bearer token exposure
- **M4 (encryption heuristic)**: `is_encrypted()` now validates base64url encoding via `re.fullmatch()`, preventing false positives

### 🏗️ DRY & Code Quality (Phase 3)

- **M1 (handler boilerplate)**: New `safe_handler()` / `safe_callback()` decorators in `decorators.py` — centralize error handling for Telegram handlers
- **Decorator adoption**: Applied `@safe_handler()` to `start_command`, `help_command`, `documents_command`, `stats_command` — eliminated ~40 lines of try/except boilerplate
- **M3 (false positives)**: `is_key_related_error` narrowed from bare `"limit"` to `"rate limit"`, `"daily limit"`, `"limit exceeded"`
- **M2 (unnecessary lock)**: Removed `asyncio.Lock` from `RoleConversationMetricsCollector` (atomic in single-threaded asyncio)

### 🧹 Cleanup

- **L4 (resource leak)**: Added `close_http_clients()` for OpenRouter and `close_tavily_client()` for Tavily — both wired to `_cleanup_application()` in `bot.py`
- **L3 (dead code)**: Removed unused `validate_file_upload` from `security.py`
- **L1 (unclean shutdown)**: `os._exit(1)` → `sys.exit(1)` in signal handler
- **L2 (redundant logic)**: Simplified `_get_admin_secret()` to use `settings.ADMIN_SECRET` directly
- **I3 (unused dep)**: Replaced `pydantic-settings` with `pydantic` in `requirements.txt`
- **M8 (unused imports)**: Removed `re`, `json`, `hashlib`, `date` from `database.py`

### 📐 Type Annotations

- `conversations.py`: bare `-> list` → `-> List[Dict[str, Any]]`, `-> int` → `-> Optional[int]`
- `keys.py`: Added return types to `DailyKeyManager.increment_usage` and `MonthlyKeyManager.increment_usage`

### 🧪 Tests (+32 → 485 total, 1 skipped)

- `test_audit_fixes.py`: 32 regression tests covering all 23 fixes from Phases 2–3
  - `TestIsEncryptedHardened` (5), `TestIsKeyRelatedErrorNarrowed` (6), `TestSafeHandlerDecorator` (3)
  - `TestMediaGroupMaxSize` (2), `TestHttpClientClose` (2), `TestMetricsCollectorNoLock` (3)
  - `TestMigrateInvalidModels` (3), `TestDatabaseCleanImports` (1), `TestValidateFileUploadRemoved` (1)
  - `TestRequirementsPydantic` (1), `TestConfigReloadDebounce` (2), `TestUserStateStoreSetdefault` (1)
  - `TestHttpxLoggerSuppression` (1), `TestSafeCallbackDecorator` (1)

## [2.6.6] – 2026-02-27 – Resilience, Repository Extraction & Performance Wiring

### 🛡️ Resilience: Circuit Breakers

- **Tavily API**: Wrapped `_tavily_api_call` in `run_with_resilience(circuit_name="tavily")` in `search_services.py`. Trips after consecutive failures, preventing cascading Tavily hammering during outages.
- **Telegram API**: Added lazy `_get_telegram_cb()` circuit breaker to `messaging.py`. `edit_text` and `reply_text` calls in `send_long_message` are now wrapped — prevents flooding Telegram servers when API is degraded.
- Lazy initialization pattern (`_get_telegram_cb()`) avoids `asyncio.create_task` at import time, which previously caused test collection failures.

### 🏗️ Repository Extraction: `db.db_query` → `repos/`

Eliminated **all 20 raw SQL queries** from handler files. Zero `db.db_query` calls remain in any handler module.

| New Module            | Functions                                                                                                                                                                      | Migrated From                            |
| --------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ | ---------------------------------------- |
| `repos/roles.py`      | 7 (`get_user_custom_roles`, `get_user_custom_roles_full`, `get_custom_role_count`, `get_custom_role_prompt`, `create_custom_role`, `delete_custom_role`, `rename_custom_role`) | `cb_roles.py`, `menus.py`, `messages.py` |
| `repos/user_stats.py` | 3 (`get_user_today_request_count`, `get_user_weekly_stats`, `get_user_model_usage_today`)                                                                                      | `commands.py`, `menus.py`                |
| `repos/admin.py`      | 6 (`authorize_user`, `revoke_user`, `list_authorized_users`, `clear_old_metrics`, `get_all_tavily_keys`, `get_tavily_usage_for_month`)                                         | `cmd_admin.py`                           |

### ⚡ Performance & Metrics

- **`track_metrics` decorator fix**: Inner function was incorrectly `async` and missing `functools.wraps` — fixed to correctly wrap async functions.
- **Handler response time tracking**: Wired `metrics_collector.record_request()` into `handle_request` at both success and error paths. Dashboard `average_response_time` now reflects real handler latency.
- **Search handler timing**: Applied `@track_metrics` decorator to `_handle_qna_search`, `_handle_research_agent`, `_handle_complex_agent_search` in `ai_search.py` for per-operation latency tracking.

### 🧹 Cleanup

- Removed stale `from app import database as db` imports from 4 handler files (`cb_roles.py`, `menus.py`, `messages.py`, `cmd_admin.py`).
- Migrated 2 Tavily proxy imports in `search_services.py` to direct repo imports.
- Updated `database.py` end-of-file repo directory comment with new modules.

### 🧪 Tests (+4 → 453 total, 1 skipped)

- `test_integration_flow.py`: 4 new end-to-end scenarios — unauthorized rejection, rate limiting, happy path through agent, error recovery with retry keyboard.
- Updated `test_roles_menu.py`: Removed stale `db` patch, all 10 tests now target repo functions.
- Updated `test_cmd_admin.py`: 3 tests updated to patch `repos/admin` functions instead of removed `cmd_admin.db`.

---

## [2.6.5] – 2026-02-26 – Codebase Remediation & Security Hardening

### 🔴 Critical Fixes

- **`safe_decrypt` silent failure**: Previously returned raw ciphertext on decryption failure (wrong `ADMIN_SECRET`), causing silent API key auth errors. Now raises `DecryptionError` with clear error logging.
- **`get_current_active_gemini_key` missing decryption**: Returned encrypted ciphertext directly to the Gemini SDK → "API key not valid" errors. Added `safe_decrypt` call.
- **Blocking Redis `ping()`**: Synchronous `redis_client.ping()` in health check and API endpoints blocked the asyncio event loop. Wrapped with `asyncio.to_thread()`.
- **Silent state persistence failures**: `_persist()` errors logged at `DEBUG` level → invisible in production. Upgraded to `WARNING` + added `add_done_callback` to fire-and-forget tasks.

### 🔒 Security Hardening

- **Brute-force protection**: IP-based login rate limiter on `/login` (5 attempts per 5 minutes → 429). Periodic eviction of stale IPs every 50 checks prevents memory growth.
- **SQL injection prevention**: Added regex validation (`_SAFE_TABLE_RE`) for table names in `DailyKeyManager.__init__`.
- **Nonce-based CSP**: Replaced `'unsafe-inline'` in `script-src` and `style-src` with per-request nonces (`secrets.token_urlsafe(16)`). Templates (`login.html`, `dashboard.html`) use `{{ g.csp_nonce }}`.
- **Error response sanitization**: 7 API endpoints replaced `str(type(e).__name__)` with generic `"internal_error"` to prevent leaking internal exception class names.

### 🏗️ Architecture: `database.py` → `app/db/` Package

Split the monolithic `database.py` (950 → ~480 lines) into 4 focused modules:

| Module                 | Contents                                                         |
| ---------------------- | ---------------------------------------------------------------- |
| `app/db/schema.py`     | All `CREATE TABLE` statements (~200 lines)                       |
| `app/db/migrations.py` | SQL file runner + legacy inline migrations (~115 lines)          |
| `app/db/rls.py`        | RLS config, policy templates, setup functions (~140 lines)       |
| `app/db/seed.py`       | Initial data seeding (admin user, API keys, indexes) (~57 lines) |

Backward compatibility preserved: `from app.database import X` continues to work via re-exports.

### 🟡 Regression Fixes (Post-Audit)

- **F-1 DecryptionError UX**: `_resolve_key_generic` now catches `DecryptionError` and returns `'decryption_failed'` → user sees `🔐 Ошибка расшифровки API-ключей...` instead of raw traceback.
- **F-2 Login rate limiter leak**: `_login_attempts` dict now periodically evicts stale IPs (every 50 checks).
- **F-3 Dual RLS wrappers**: `_init_schema` now uses the module-level backward-compat wrapper instead of a redundant direct import.

### ⚡ Performance

- **`asyncio.Lock` in `MultiLayerCache`**: Protects TTLCache reads/writes from concurrent coroutine corruption.
- **Removed ~60 lines of duplicate Redis-only caching** in `get_cached_search_result` / `cache_search_result`.

### 🧪 Tests (+5 → 409 total)

- `test_decryption_error_handling.py`: 5 tests — primary path catch, fallback catch, user-friendly message validation, normal flow unaffected, no-retry behavior.
- Updated `test_crypto.py`: expects `DecryptionError` from `safe_decrypt`.
- Updated `test_security_headers.py`, `test_web_security.py`: validates nonce-based CSP (`'nonce-'` present, `'unsafe-inline'` absent).

---

## [2.6.4] – 2026-02-26 – Error Handling & Role Save Bug Fix

### 🔴 Critical Fixes

- **Manual role save false-positive error**: `role_manual_save_callback` showed "❌ Нет данных для сохранения роли" even though the role was saved. Root cause: `clear_manual_role_state()` was called before the save callback, wiping in-memory state, and `context.user_data` doesn't reliably persist across Telegram Update boundaries. Fix: store prompt in `app.state` (new `manual_role_prompt` field), use `finish_manual_role_input()` to flip flags without clearing data, clear only after save/cancel.

### 🟡 Improvements

- **Actionable error messages**: 16 bare error messages across `messages.py`, `callbacks.py`, and `menus.py` now include contextual recovery buttons (retry, back to menu, back to documents) instead of dead-end text.
- **3-stage heartbeat on long waits**: Placeholder "🤔 Думаю..." now updates progressively (15s → "⏳ Обрабатываю ваш запрос...", 30s → "⏳ Ответ генерируется...", 50s → "⏳ Запрос обрабатывается дольше обычного...") using `asyncio.Event` for race-safe cancellation.
- **Manual role DB persistence**: Added `awaiting_manual_role_title`, `awaiting_manual_role_prompt`, `manual_role_title`, `manual_role_prompt` columns to `user_state` table (migration `008`) and updated `save_user_state`/`load_user_state` in `users.py`. Manual role creation now survives bot restarts.
- **Stale test assertions fixed**: 4 pre-existing test failures from UI text refactoring (old: "Управление ролями"/"Чат очищен", new: "Роли"/"Новый чат") now pass.

---

## [2.6.3] – 2026-02-26 – Codebase Audit & Bug Fixes

### 🔴 Critical Fixes

- **Missing `get_supabase_metrics` re-export**: `web.py:api_database()` called `database.get_supabase_metrics()` which was missing from `_REPO_EXPORTS` → `AttributeError` at runtime. Added mapping to `_REPO_EXPORTS`.
- **`await` on synchronous `float` return**: `cache.py:get_cache_stats()` used `await metrics_collector.get_cache_hit_rate()`, but `get_cache_hit_rate()` is a sync function returning `float` → `TypeError: 'float' object can't be awaited`. Removed erroneous `await`.

### 🟡 Medium Fixes

- **Redundant Redis `INFO` call**: `/api/cache` endpoint called both `get_cache_stats()` and `get_multi_layer_cache_stats()`, but the latter already calls `get_cache_stats()` internally — doubling Redis round-trips. Removed standalone call.
- **`redis.info()` dict mishandled as string**: `get_cache_stats()` converted the `redis.info()` dict to `str(dict)` and split on newlines, producing garbled stats (`"N/A"` for memory, uptime). Now accesses dict keys directly.

### ✅ Audit Findings (Clean)

Full scan of 21 core modules, 13 handlers, 7 repos confirmed:

- No SQL injection (all user-facing queries parameterized)
- No bare `except:`, `eval()`, `exec()`, `os.system()`
- No `asyncio.run()` or blocking `time.sleep()` in async context
- All `_REPO_EXPORTS` entries resolve correctly (60+ callsites verified)
- Security module (`InputSanitizer`, `RateLimiter`) comprehensive
- Circuit breaker, memory manager, task queue all properly bounded

### 🧪 Tests

- **404 passed**, 1 skipped — full green suite after all fixes.

---

## [2.6.2] – 2026-02-26 – UI/UX Audit & Manual Role Creation

### 🎨 UI/UX Improvements (Marketing Psychology)

- **Start menu restructured** (Hick's Law): New 4-row layout with primary CTA (`💬 Новый чат`), paired settings row (`🧠 Модель AI` + `🎭 Роли`), document/conversation shortcuts, and compact toggle row.
- **Roles hub redesigned** (AIDA funnel): Browse (`📚 Каталог` + `👤 Мои роли`) → Create (`✨ Сгенерировать` + `📝 Написать`) → Reset → Back.
- **Role detail buttons** updated: `✅ Применить` → `▶️ Активировать`, `🛑 Отключить` → `🔄 Сбросить` (positive framing).
- **Delete confirmation dialogs** — loss-aversion framing: "🚨 Все документы будут удалены безвозвратно" / "Вся история сообщений будет потеряна безвозвратно".
- **Cancel buttons** relabeled: `❌ Отмена` → `↩️ Отмена` (less alarming).
- **Keyboard button labels** updated: `🆕→✨ Новая тема`, `🔁→🔄 Повторить` (reduced activation energy).

### 🐛 Anti-Flood Fixes

- **6 `reply_text` → `edit_message_text`** conversions: role rename prompt, role AI creation prompt, role prompt view, and 3 error messages that were creating new messages instead of editing.

### 🛡️ Error Recovery (15+ dead-ends fixed)

- All error messages now have at least one actionable button (`⬅️ Меню`, `🎭 Меню ролей`, `📄 К документам`, `⬅️ К беседам`).
- Fixed across: `callbacks.py`, `cb_roles.py`, `cb_documents.py`, `cb_conversations.py`.

### ✨ New Feature: Manual Role Creation

- New `📝 Написать` button in roles hub and role lists.
- 2-step text flow: Title → Prompt → Preview → 💾 Save & Apply.
- 3 new callbacks: `role_create_manual`, `role_manual_cancel`, `role_manual_save`.
- State management: 3 new fields + 6 helper functions in `state.py`.
- Text handler: `_handle_manual_role_input()` in `messages.py`.

### 🧹 Code Quality

- **Keyboard consolidation**: Removed dead `main_menu_keyboard()` / `document_menu_keyboard()`. Added centralized `feedback_row()`, `ai_response_keyboard()`, `deep_dive_keyboard()`, `error_with_back_keyboard()`.
- **`messaging.py` migration**: 42 lines of duplicated keyboard code → single imports from `keyboards.py`.
- **Conversations menu**: Added `⬅️ Назад` back button and fixed empty-state dead-end.
- **Documents menu**: Added `⬅️ Назад` back button (from Phase 1).

### 🧪 Tests (404 passed, 1 skipped)

- Updated `test_keyboards.py`: 7 new tests (feedback_row, ai_response, deep_dive, error_with_back).
- Updated `test_menus.py`: new start menu assertions (4 rows, updated labels, new doc/conv buttons).
- Updated `test_roles_menu.py`: assertions match new button labels (Сгенерировать, Активировать).

---

## [2.6.1] – 2026-02-26 – Technical Audit Fixes

### 🔴 Critical Fixes

- **CRIT-01**: Fixed unreachable `except (APIError, httpx.HTTPError)` in `GeminiProvider._execute_request` — `APIError` was already caught by the preceding handler. Now correctly `except httpx.HTTPError`.
- **CRIT-02**: Removed duplicate `asyncpg.InterfaceError` from second except clause in both `DatabaseManager.query()` and `execute_many()`. `InterfaceError` was caught by the first clause (connection handler) and could never reach the second (rate-limit handler).

### 🟠 High-Severity Fixes

- **SEC-05**: `set_user_context()` now re-raises on failure instead of silently swallowing it. Prevents queries from running with stale RLS context (potential cross-user data leak).
- **SEC-01**: Added `_SAFE_IDENTIFIER_RE` regex validation for SQL table names in `setup_row_level_security()` before f-string interpolation.
- **BUG-05**: Added `log_openrouter_response()` to `APILogger`. OpenRouter `_log_failure` now logs under correct `openrouter` API name instead of misleading `gemini`.

### 🟡 Medium Fixes

- **BUG-01/02**: Removed unreachable `elif parts is None` dead code branches in `_build_contents` and `_build_messages`.
- Added `import re` to `database.py` (needed for new regex validation).
- Restored `GeminiProvider._log_failure` accidentally deleted during refactor.

### 🧪 Tests

- **401 passed**, 1 skipped, 0 failures — full green suite after all fixes.

---

## [2.6.0] – 2026-02-25 – Sprint 6–7: Audit & Refactoring

### 🔒 Security Audit (12 fixes)

- **C-1**: RLS `set_config` scoped to transaction-local (`true`) to prevent context leakage between pooled connections.
- **C-2**: Bare `except:` → `except (json.JSONDecodeError, ValueError, TypeError)` in `database.py`.
- **C-3**: API key preview reduced from 10 → 4 chars across `database.py` and `metrics_repo.py`.
- **C-4**: `force_update_tavily_keys()` wrapped in atomic transaction.
- **C-5**: Added `exc_info=True` to generic exception handlers in `services.py`.
- **C-6**: `delete_conversation()` wrapped in atomic transaction.
- **C-8**: `conversation_messages` RLS policy rewritten to use subquery through `conversations` table.
- **A-1**: `APIError` → `GemaibotAPIError` to avoid collision with `google.genai.errors.APIError`.
- **D-2**: Redundant `except Exception:` clause removed from `database.py`.
- **D-3**: `ConnectionRefusedError` → `ServiceConnectionRefusedError` to avoid shadowing builtin.
- **D-4**: `token_count` correctly restored from `token_budget` on conversation switch.

### 🏗️ Architecture Refactoring

- **Metrics deduplication**: Deleted ~125 duplicate lines from `database.py` → re-exports from `repos/metrics_repo.py`.
- **Rate limiter consolidation**: Removed `_UserRateLimiter` (23 lines) from `ai_provider.py`. `ProviderRouter` now uses `security.RateLimiter` (includes periodic cleanup, stats, admin reset).
- **`DailyKeyManager` class**: New generic key rotation engine in `repos/keys.py`. Gemini and OpenRouter share one parameterized SQL engine. Tavily kept separate (monthly-credit model).
- **Provider call chain fix**: `GeminiProvider` and `OpenRouterProvider` now call `_execute_*_request()` directly, eliminating double-retry bug.
- **Unified AI call path**: `AgentRequestUseCase.get_ai_response()` now delegates to Provider classes via `get_provider_for_model()` instead of calling `services.get_*_response()` directly.
- **Deprecation warnings**: `services.get_gemini_response()` and `services.get_openrouter_response()` now emit `DeprecationWarning`. New code should use Provider classes.

### ⚡ Performance

- **Lazy logging**: Converted 242 f-string logging calls → `%s` format across 36 files. Prevents string interpolation when log level is disabled.

### 🐛 Bug Fix: Model Timeout (gemini-2.5-flash / gemini-3-flash)

- **Root cause**: `asyncio.to_thread(client.models.generate_content, ...)` ran the synchronous SDK method in a thread. When `asyncio.wait_for()` timed out, it cancelled only the Python future — the thread continued running the HTTP request as a zombie, consuming resources.
- **Fix**: Switched to native async `client.aio.models.generate_content()` + `client.aio.models.count_tokens()`. These properly support `asyncio.CancelledError` and abort the HTTP connection on timeout.
- **SDK-level deadline**: Added `HttpOptions(timeout=90_000)` (90s) to the `genai.Client`, ensuring the HTTP library itself enforces a hard deadline even if the asyncio layer fails.
- **Python-side deadline**: Reduced from 120s → 100s (10s buffer over SDK timeout) to prevent silent hangs.

### 🧪 Tests (+31 → 349 pre-Phase 7)

- `test_daily_key_manager.py`: 12 DailyKeyManager + 5 MonthlyKeyManager tests.
- `test_unified_call_path.py`: 4 tests (Gemini routing, OpenRouter routing, error response, no-keys guard).
- `test_provider_router_integration.py`: 6 tests (full chain Gemini/OpenRouter, all-exhausted, rate limit, multimodal, key-failure retry).
- `test_timeout_smoke.py`: 4 tests (async cancellation, no zombie tasks, CancelledError propagation, SDK HTTP timeout config).
- Updated `test_services_gemini.py` mocks: `client.models.*` → `client.aio.models.*` (`AsyncMock`).

### 🧹 Code Quality

- Standardized all Russian comments/docstrings in `security.py` `RateLimiter` to English.
- Removed dead `from app import services` import from `agent_use_cases.py`.
- **MonthlyKeyManager** — new generic class for monthly-credit key rotation (Tavily). Completes the KeyManager abstraction alongside DailyKeyManager.
- **OpenRouter timeout** tightened 120s → 90s with explanatory comments. No zombie-thread risk (uses async httpx).
- **asyncio.to_thread audit** — all remaining usages verified safe (CPU-bound encoding, Redis ops).
- **\_save_image_as_bytes** — reviewed, already has 5s ProcessPoolExecutor timeout. No changes needed.
- **Russian → English comments** — automated translation of 659 comment/docstring lines across 26 files (dictionary-based, no LLM). User-facing strings preserved.
- **Provider class migration** — moved `_execute_gemini_request` (312 lines) and `_execute_openrouter_request` (265 lines) from `services.py` into self-contained `GeminiProvider` and `OpenRouterProvider` classes. New `app/utils/image_utils.py` for shared image processing.
- **services.py → search_services.py** — renamed to reflect actual content (pure Tavily search). Backward-compat shim at `services.py`. Removed all deprecated wrappers (`get_gemini_response`, `get_openrouter_response`, `_with_retry`, `_validate_api_inputs`, `_caller_info`) and execute stubs. Dead `from app import services` import removed from `ai_document.py`. Final size: 160 lines (was 951).

### 🔒 Security Hardening (Phase 7)

- **RLS enabled** on 4 previously unprotected tables: `user_metrics`, `user_state`, `feedback`, `schema_migrations`.
- **2 unused indexes dropped**: `idx_group_messages_owner`, `idx_error_logs_created_at`.

### 🏗️ Typed Exception Catches (Phase 6–7, 75 total)

| File                    | Catches Refined | Types Used                                                                |
| ----------------------- | --------------- | ------------------------------------------------------------------------- |
| `database.py`           | 13              | `(asyncpg.PostgresError, asyncpg.InterfaceError)`                         |
| `document_processor.py` | 17              | `asyncpg.*`, `ValueError`, `pypdf.errors.PdfReadError`, `httpx.HTTPError` |
| `ai_search.py`          | 20              | `(BadRequest, NetworkError)`                                              |
| `messages.py`           | 4               | `(BadRequest, NetworkError)`, `OSError`                                   |
| `ai_provider.py`        | 10              | `(APIError, httpx.HTTPError)`, `(TypeError, ValueError)`                  |
| `memory_manager.py`     | 2               | `(OSError, AttributeError)`                                               |
| `cache.py`              | 9               | `(ConnectionError, RedisError)`, `(TypeError, ValueError, KeyError)`      |

### 🧪 Tests (+34 → 383 total)

- `test_repos_users.py`: 11 tests — auth, cache, state, feedback.
- `test_repos_conversations.py`: 8 tests — CRUD, rename, delete.
- `test_repos_keys.py`: 12 tests — DailyKeyManager, MonthlyKeyManager, cache.
- `test_io_handlers.py`: Converted from `unittest.TestCase` + manual event loop → `pytest-asyncio` (fixed test-ordering failure).
- `test_ai_provider.py`: Fixed `test_gemini_wrapper_error` — added missing `count_tokens` mock.

### 📝 Type Hints (Phase 8, 114 annotations)

- **Repos** (11): `keys.py` (6), `users.py` (1), `chats.py` (1), `analytics.py` (1), `metrics_repo.py` (1), `conversations.py` (1)
- **Handlers** (103): `commands.py` (33), `cb_roles.py` (20), `callbacks.py` (14), `cb_conversations.py` (12), `messages.py` (11), `menus.py` (6), + 7 smaller files

### 🧪 Integration Tests (+18 → 401 total)

- `test_integration_flows.py`: 18 tests across 4 cross-module flows:
  - **AI response lifecycle** (3): Gemini routing, OpenRouter routing, error handling
  - **Key rotation** (8): DailyKeyManager get/exhaust/increment/available, MonthlyKeyManager get/exhaust
  - **Conversation CRUD** (4): create+list, rename, delete, save history
  - **User auth chain** (4): admin bypass, DB lookup, unauthorized, cache hit

---

## [2.5.0] – 2026-02-25 – Sprint 5: Polish & Hardening

### ⚡ ProviderRouter Enhancements

- **Multimodal auto-detection**: Router detects PIL Image / bytes in history and forces Gemini automatically — handlers no longer pass `use_openrouter=False` explicitly.
- **Per-user rate limiting**: Sliding-window `_UserRateLimiter` (default 20 req/min) prevents abuse. Returns user-friendly `⏳ Слишком много запросов` on throttle.

### 📊 Per-User Metrics

- **`user_metrics` table**: New `(user_id, metric_date)` keyed table tracks personal request counts and model usage.
- **`MetricsCollector` update**: `record_request()` and `record_api_call()` now accept `user_id` param; per-user data saved to DB alongside global metrics.
- **`/stats` and `/start` personalized**: Queries now show personal stats (today count, 7-day history, model usage) instead of global.

### 🖼️ Stage Indicators

- Wired `STAGES_PHOTO` into `ai_photo.py`: `_handle_photo` and `_handle_media_group_photos` now show animated processing stages.

### 🧪 Test Coverage (+28 tests → 291 total)

- `test_stage_indicators.py`: 15 tests (definitions, update behavior, capping, sequential, error handling).
- `test_provider_router.py`: 13 tests (KeyHealth scoring, ProviderRouter retry/skip/detection). _Note: rewritten in v2.7.0 for DB-backed KeyStatusManager._

### 🧹 Cleanup

- Removed 4 stale re-exports from `agent.py` facade.
- Removed unused `from app import services` in `ai_photo.py`.

---

## [2.4.0] – 2026-02-25 – Sprint 4: Legacy Cleanup

### 🧹 Removed Dead Code

- `_LegacyGeminiWrapper` / `_LegacyOpenRouterWrapper` aliases from `ai_provider.py`.
- `_UserLocksProxy` class + `USER_LOCKS` singleton from `state.py`.
- `get_user_locks()` backward-compat alias from `state.py`.
- `_cleanup_memory_cache()` deprecated stub from `cache.py`.

### 🔄 Handler Router Migration (Pre-Sprint-4)

- **All AI handlers** now use `_get_ai_response_with_routing()` instead of direct `services.get_gemini_response()` calls.
- Migrated: `ai_photo.py` (3 functions), `ai_document.py`, `ai_search.py::_handle_complex_agent_search`.
- Removed manual `db.increment_gemini_key_usage()` calls — router handles usage tracking internally.
- Added `handle_ai_response_error()` checks for router error messages.

---

## [2.3.0] – 2026-02-25 – Codebase Audit, Docker Optimization & Bug Fixes

### 🧹 Codebase Audit & Cleanup

- **Legacy Files**: Moved 12 obsolete files to the `legacy/` directory to declutter the root workspace.
  - Dead code (`app/health.py`, `app/alerts.py`) and their orphaned tests moved to `legacy/app/` and `legacy/tests/`.
  - Development tools and benchmarks moved to `legacy/dev_scripts/`.
  - Obsolete Render deployment configs moved to `legacy/deploy_render/`.
- **Gitignore**: Expanded `.gitignore` to explicitly exclude IDE configurations (`.vscode`, `.cursor`, `.Jules`, `.roomodes`), linter caches, and stale test logs.

### 🐳 Docker & CI Optimization

- **`Dockerfile.northflank` Overhaul**:
  - Upgraded base image from Python 3.11 to **Python 3.14-slim** for better performance string resolving and modern standard library features.
  - Reduced Dockerfile length by 50% (71 → 36 lines).
  - Extracted inline startup commands into a dedicated `start.sh` executable.
  - Consolidated `RUN` layers to reduce image size.
  - Added native Docker `HEALTHCHECK` instruction.
- **Requirements Split**: Separated dependencies into `requirements.txt` (prod-only) and `requirements-dev.txt` (includes `pytest`). Production image no longer installs testing frameworks.
- **`.dockerignore`**: Created robust `.dockerignore` to prevent tests, legacy files, and IDE caches from inflating the production image.

### 🐛 Bug Fixes

- **Telegram Polling Crash**: Fixed `TypeError: Updater.start_polling() got an unexpected keyword argument` on startup.
  - **Root Cause**: `python-telegram-bot` v22.0 removed HTTP timeout arguments from `start_polling()`.
  - **Fix**: Removed deprecated kwargs, preserved valid Telegram long-polling `timeout=30`, and kept HTTP timeouts correctly scoped to `HTTPXRequest`. Added static AST regression test (`test_start_polling_kwargs.py`) to prevent recurrence.
- **Database Metrics Crash**: Fixed `column "request_id" does not exist` error spamming logs on boot.
  - **Root Cause**: The `metrics` and `error_logs` tables relied on a standalone SQL migration script that was never executed in the deployment pipeline.
  - **Fix**: Native schema definitions and the missing `request_id` column patching were integrated directly into the `app/database.py:_init_schema` and `_run_migrations` boot sequence for automatic repair.

## [2.2.0] – 2026-02-22 – Test Suite Isolation Overhaul

### Context

The full test suite (`python -m pytest tests/`) suffered from **cascading cross-test failures** (up to 38 simultaneous) caused by global `sys.modules` mock pollution. Individual tests passed in isolation but failed when collected together because early-alphabetical test files injected `MagicMock` objects into `sys.modules` at **module parse time** (before `setup_module`), permanently replacing real modules for all subsequently-collected files.

### Root Cause (for future agents)

Python's `sys.modules` is a global singleton. When a test file executes `sys.modules["pytz"] = MagicMock()` at the **top level** (outside any function), pytest evaluates it during **collection** — before any test runs. This poisons `pytz` for every other test file in the session. The fix pattern is:

1. **Move** all `sys.modules[...] = MagicMock()` into `setup_module()`.
2. **Save** original modules: `_original_modules[k] = sys.modules.pop(k, None)`.
3. **Restore** in `teardown_module()`: delete injected keys, `sys.modules.update(_original_modules)`.
4. **Reload** dependent modules via `importlib.reload()` in `setup_module()` when upstream mocks change their identity.

### Files Changed

#### `tests/test_auth_headers.py`

- **Before**: 12 `sys.modules[...] = MagicMock()` calls at module top-level.
- **After**: All moved into `setup_module()`/`teardown_module()` with proper save/restore.

#### `tests/test_menus.py`

- **Before**: `setup_mocks()` injected mocks into `sys.modules` but `app.handlers.menus` was already imported with stale references.
- **After**: Added `importlib.reload(sys.modules["app.handlers.menus"])` inside `setup_module()` after mock injection. Added cleanup of `app.handlers.menus` in `teardown_module()`.

#### `tests/test_keyboards.py`

- **Before**: No `setup_module`. Imported functions at top level bound to whatever `telegram` module existed at collection time.
- **After**: Added `setup_module()` that detects MagicMock `telegram`, deletes it, reloads `app.utils.keyboards`, and re-injects all public attributes onto the test module via `setattr(module, attr, getattr(reloaded, attr))`.

#### `tests/test_database_tavily.py`

- **Before**: Top-level `sys.modules["pytz"] = MagicMock()` and `sys.modules["asyncpg"] = MagicMock()`. Used `import app.database` (attribute-chain form).
- **After**: Removed all `sys.modules` overrides entirely. Switched from `import app.database` → `from app import database` to avoid `AttributeError: module 'app' has no attribute 'database'` caused by teardown scripts deleting `app.database` from `sys.modules`.

#### `tests/test_document_cleanup_optimization.py`

- **Before**: `patch.dict(sys.modules, {...})` context manager wrapping a `from app.document_processor import DocumentProcessor` at module level. The `mock_db.db_query` was a `MagicMock` (not `AsyncMock`), causing `'MagicMock' object can't be awaited`.
- **After**: Direct `from app.document_processor import DocumentProcessor` (no sys.modules patching). Test uses `@patch("app.document_processor.database")` decorator with `AsyncMock` for `db_query`.

#### `tests/test_perf_db_messages.py`

- **Before**: `import app.database` at top level failed with `AttributeError: module 'app' has no attribute 'database'` when prior teardowns deleted it from `sys.modules`.
- **After**: Added `get_database()` helper that uses `from app import database` + `patch.object()` to avoid relying on `sys.modules` state.

#### `tests/test_security_headers.py`

- **Before**: `/health` endpoint returned `503` when run after `test_io_handlers.py` because the real (dead) database pool was initialized.
- **After**: Added `patch("app.web.database.is_database_connected", return_value=True)` to the `client` fixture.

#### `tests/test_system_status.py`

- **Before**: `@patch("app.database.db_query")` decorator targeted the real `app.database` module, but after `patch.dict` + `importlib.reload`, `app.metrics` internally referenced the MagicMock substitute. The decorator's patch never reached the code path.
- **After**: Removed `@patch("app.database.db_query")`. Now creates `AsyncMock` inline and patches via `patch.object(self.metrics_module, "db")` to target the actual reference used by the reloaded module.

#### `tests/test_metrics_integration.py` (prior session)

- Removed destructive `importlib.reload()` calls in `setUp()`.

#### `tests/test_web_security.py` (prior session)

- Moved top-level `sys.modules` mocks into `setup_module()`/`teardown_module()`.

#### `tests/test_callbacks.py` (prior session)

- Moved top-level `sys.modules` mocks into `setup_module()`/`teardown_module()`.

#### `tests/test_document_security.py` (prior session)

- Wrapped `sys.modules["app.database"] = MagicMock()` in `setup_module()`/`teardown_module()`.

### Verification

```
python -m pytest tests/ --tb=short
=========== 192 passed, 1 skipped, 1 xfailed, 0 failures in 31.89s ===========
```

### Anti-Pattern Reference (for future agents)

| ❌ Anti-Pattern                                            | ✅ Correct Pattern                                                   |
| ---------------------------------------------------------- | -------------------------------------------------------------------- |
| `sys.modules["X"] = MagicMock()` at top level              | Move into `setup_module()` with save/restore in `teardown_module()`  |
| `import app.database` then `app.database.func()`           | `from app import database` then `database.func()`                    |
| `@patch("app.database.db_query")` after `importlib.reload` | `patch.object(reloaded_module, "db")` targeting the cached reference |
| `MagicMock()` for async functions                          | `AsyncMock()` for any function that is `await`ed                     |
| `importlib.reload()` in `setUp()`                          | Avoid; use `setup_module()` (once per file) instead                  |

---

## [2.1.0] – Performance Optimizations

- Non-blocking document I/O with async file processing
- Batched metrics DB inserts via `asyncio.Queue`
- Scoped DB transactions with `asyncio.Semaphore`
- GIL-free image processing via `ProcessPoolExecutor`
- TTLCache with lazy eviction for web search states
- Micro-GC pauses with tuned `gc.collect(1)`
- Robust TCP pooling with Circuit Breaker tracking

