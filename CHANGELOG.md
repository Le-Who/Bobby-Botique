# Changelog

All notable changes to this project will be documented in this file.
Format is optimized for agent-parseable context.

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

### 🧪 Tests (+31 → 322 total)

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
- `test_provider_router.py`: 13 tests (KeyHealth scoring, ProviderRouter retry/skip/detection).

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
