# Codebase Hardening and LTM Deepening Implementation Plan

**Design:** `docs/superpowers/specs/2026-08-27-codebase-hardening-and-ltm-deepening-design.md`  
**Method:** staged convergence, red-green-refactor, isolated mechanical formatting

## Task 1: Lock the Verification Contract in Tests

**Files**

- Create: `tests/test_ci_workflow_config.py`
- Extend: `tests/test_deploy_workflow_config.py`

**Red tests**

1. Assert CI includes `vps_testai` for push and pull requests.
2. Assert no workflow references `load_test.py`.
3. Assert unit tests explicitly exclude integration tests.
4. Assert integration setup includes a PostgreSQL/pgvector service, migrations, and `TEST_DATABASE_URL`.
5. Assert CI runs an audit of `requirements.txt`.
6. Assert deploy consumes a successful CI result for the same head SHA.
7. Assert deploy has branch concurrency, correct retry semantics, immutable image tagging, and a bounded health check.

**Command**

```powershell
python -m pytest tests/test_ci_workflow_config.py tests/test_deploy_workflow_config.py -q --override-ini="addopts="
```

## Task 2: Repair Dependencies and Verification Workflows

**Files**

- Modify: `requirements.txt`
- Modify: `requirements-dev.txt`
- Modify: `pyproject.toml`
- Modify: `.github/workflows/ci.yml`

**Implementation**

1. Move `cryptography` to a safe supported 50.x range.
2. Pin the Ruff formatter version used by CI and local development.
3. Add the dependency-audit tool to development verification.
4. Align Ruff and mypy with Python 3.14.
5. Repair CI triggers and remove stale file references.
6. Separate unit and integration test commands.
7. Start PostgreSQL with pgvector, run migrations, and fail if integration tests are skipped because the test DSN is absent.
8. Add dependency audit; defer the global format gate until Task 11.

**Green checks**

```powershell
python -m pytest tests/test_ci_workflow_config.py -q --override-ini="addopts="
python -m ruff check .
uvx --from pip-audit pip-audit -r requirements.txt --progress-spinner off
```

## Task 3: Repair Deploy Ordering and Health Detection

**Files**

- Modify: `.github/workflows/deploy.yml`
- Extend: `tests/test_deploy_workflow_config.py`

**Implementation**

1. Trigger deployment from a successful CI workflow run for `vps_testai`.
2. Checkout and tag the exact `workflow_run.head_sha`.
3. Add deploy concurrency.
4. Make the initial Docker build eligible for retry without hiding retry failure.
5. Pull and run the immutable SHA tag on the VPS.
6. Fail when the Telegram API readiness loop expires.
7. Poll Docker health and `/health`; print logs and fail on timeout.

**Green check**

```powershell
python -m pytest tests/test_ci_workflow_config.py tests/test_deploy_workflow_config.py -q --override-ini="addopts="
```

## Task 4: Make TaskManager Shutdown Honest

**Files**

- Modify: `app/utils/background_tasks.py`
- Modify: `bot.py` to log an incomplete drain result
- Extend: `tests/test_background_tasks.py`
- Extend: `tests/test_taskmanager_bounded.py`

**Red tests**

1. A cancelled task with asynchronous `finally` cleanup must finish cleanup before successful drain returns.
2. A cancellation-resistant task must make drain return failure within the configured total bound.
3. Finished/cancelled tasks must leave the manager's tracked set.

**Implementation**

Replace the fixed sleep with a bounded second wait over the cancelled task snapshot. Return a boolean status and log outstanding task names.

**Green check**

```powershell
python -m pytest tests/test_background_tasks.py tests/test_taskmanager_bounded.py -q --override-ini="addopts="
```

## Task 5: Make Tavily Key Replacement Atomic

**Files**

- Modify: `app/repos/keys.py`
- Extend: `tests/test_database_tavily.py`

**Red tests**

1. All destructive statements use one acquired connection inside one transaction.
2. Insert failure rolls back and leaves the active cache untouched.
3. Success clears the cache once after the transaction exits successfully.

**Implementation**

Prepare encrypted rows before acquiring the connection, then pass `conn` to all repository helpers inside `conn.transaction()`.

**Green check**

```powershell
python -m pytest tests/test_database_tavily.py -q --override-ini="addopts="
```

## Task 6: Remove Unsafe Cleanup and Resolve Type Drift

**Files**

- Delete: `clear_db.py`
- Modify: `pyproject.toml`
- Modify: `app/database.py`
- Modify: `app/cache.py`
- Modify: `app/repos/memory.py`
- Modify: `app/repos/chats.py` and affected handlers as needed
- Modify: `app/repos/memory_extraction.py`
- Modify: `app/repos/memory_consolidation.py`
- Create or extend focused type-contract tests

**Red checks**

1. Add a static safety assertion that the unsafe script and its per-file ignore are absent.
2. Capture `mypy app bot.py` failures.

**Implementation**

Add the persisted-chat state to `ChatState`, type Redis TLS options accurately, add explicit returns, and remove same-scope variable reuse that confuses mypy. Do not silence errors globally.

**Green checks**

```powershell
python -m mypy app bot.py --cache-dir=NUL
python -m ruff check .
```

## Task 7: Define the Shared LTM Graph Contract with Tests

**Files**

- Create: `app/repos/memory_graph_writer.py`
- Create: `tests/test_memory_graph_writer.py`
- Extend: `tests/test_memory_extraction_provenance.py`
- Extend: `tests/test_memory_consolidation_safety.py`

**Red tests**

1. Typed plans reject nodes or edges without durable support IDs.
2. The writer requires a caller-provided connection and never acquires a pool connection.
3. Node-source writes precede node projection.
4. Edge-source writes precede edge projection.
5. Conflict decisions close only matching current tenant edges.
6. SQL failures propagate instead of becoming a zero result inside the writer.

## Task 8: Move Real-Time Extraction onto the Shared Writer

**Files**

- Modify: `app/repos/memory_extraction.py`
- Modify: `app/repos/memory_graph_writer.py`
- Extend: `tests/test_memory_extraction_provenance.py`

**Implementation sequence**

1. Keep extraction, embeddings, optimistic conflict read, privacy recheck, and external ambiguity resolution in `memory_extraction.py`.
2. Normalize entities, relations, source IDs, and decisions into the shared plan.
3. Keep RLS context, advisory lock, and final consent/source recheck in the caller transaction.
4. Delegate only mutation SQL.
5. Preserve rollback-to-logged-zero behavior at the outer extraction boundary.

**Green check**

```powershell
python -m pytest tests/test_memory_graph_writer.py tests/test_memory_extraction_provenance.py tests/test_memory_consent.py -q --override-ini="addopts="
```

## Task 9: Move Consolidation onto the Shared Writer

**Files**

- Modify: `app/repos/memory_consolidation.py`
- Modify: `app/repos/memory_graph_writer.py`
- Extend: `tests/test_memory_consolidation_safety.py`

**Implementation sequence**

1. Keep privacy lease, snapshot validation, extraction, embeddings, fact insertion, and derivation links in consolidation.
2. Convert inserted fact IDs and relation support indexes into node/edge provenance plans.
3. Delegate graph mutation inside the existing fact transaction.
4. Mark raw sources consolidated only after the writer succeeds.
5. Let writer failures roll back facts, graph data, and source marks together.

**Green check**

```powershell
python -m pytest tests/test_memory_graph_writer.py tests/test_memory_consolidation_safety.py tests/test_ltm_schema_graph_baseline.py -q --override-ini="addopts="
```

## Task 10: Verify LTM Against PostgreSQL When Available

**Files**

- Extend integration tests only when a missing contract is not already covered.

**Commands**

```powershell
python -m pytest -m integration tests/integration -q --override-ini="addopts="
```

If `TEST_DATABASE_URL` is unavailable, record the skip as a verification limitation and ensure CI contains the real database gate.

## Task 11: Centralize and Rewrite Public Bot Help

**Files**

- Create: `app/bot_commands.py`
- Modify: `app/handlers/commands.py`
- Modify: `app/handlers/cb_navigation.py`
- Modify: `app/handlers/menus.py` where wording is stale
- Modify: `app/i18n.py`
- Create: `tests/test_bot_help_catalog.py`
- Extend existing menu/help tests

**Red tests**

1. Public catalog commands are registered.
2. Admin commands are not in the public catalog.
3. Every catalog and help key has Russian and English text.
4. Telegram command descriptions fit platform limits.
5. Generated help is parse-mode safe and categorized.
6. Help does not mention nonexistent commands, stale limits, or internal implementation terms.

**Implementation**

Generate the Telegram command menu and `/help` overview from the catalog. Keep detailed localized topics in i18n. Rewrite copy by user journey using concise, kind, actionable language.

## Task 12: Normalize Formatting and Enable the Gate

**Files**

- Mechanical changes across Python files
- Modify: `.github/workflows/ci.yml`

**Sequence**

1. Confirm all functional Python changes and full non-integration tests are green.
2. Run the pinned `python -m ruff format .` as a formatting-only change.
3. Run `git diff --check`, encoding, Ruff lint, mypy, and tests.
4. Enable `python -m ruff format --check .` in CI.
5. Make no logical edits inside the formatting change.

## Task 13: Update Engineering Documentation

**Files**

- Modify: `README.md`
- Modify: `docs/ARCHITECTURE.md`
- Modify: `CHANGELOG.md`
- Update docstrings in changed modules

**Sequence**

1. Run `python scripts/check_encoding.py` before writes.
2. Update commands, branch names, counts, architecture boundaries, CI/deploy guarantees, help behavior, and known integration limitation.
3. Run the encoding check again.
4. Search for stale claims such as `load_test.py`, old branch triggers, old test counts, and unconditional CI stability claims.

## Task 14: Final Verification

```powershell
python scripts/check_encoding.py
python -m ruff check .
python -m ruff format --check .
python -m mypy app bot.py --cache-dir=NUL
python -m pytest tests --ignore=tests/integration -m "not integration" --override-ini="addopts="
python -m pytest -m integration tests/integration --override-ini="addopts="
uvx --from pip-audit pip-audit -r requirements.txt --progress-spinner off
git diff --check
git status --short --branch
```

Report exact pass/skip counts and explicitly identify any verification that could not run locally.
