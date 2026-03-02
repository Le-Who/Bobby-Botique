# Changelog

All notable changes to this project will be documented in this file.
Format is optimized for agent-parseable context.

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
