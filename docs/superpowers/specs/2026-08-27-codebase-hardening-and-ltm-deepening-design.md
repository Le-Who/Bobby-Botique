# Codebase Hardening and LTM Deepening Design

**Date:** 2026-08-27  
**Status:** Approved  
**Branch:** `vps_testai`

## Purpose

Restore truthful automated verification, fix confirmed correctness and security defects, concentrate duplicated long-term-memory graph persistence behind one deep transactional module, and bring both engineering documentation and in-bot help into line with actual behavior.

The work follows a staged-convergence strategy. Each functional wave must leave the repository in a testable state before the next wave starts. Mechanical formatting and documentation are deliberately separated from behavioral changes.

## Evidence and Baseline

The design is based on repository inspection, Git history, targeted reproductions, and local verification:

- application code: 233 Python files and roughly 85,000 lines;
- tests: 247 Python files and roughly 52,000 lines;
- baseline unit/E2E run: 2,641 passed, 28 skipped, 2 deselected;
- integration-only run: 63 skipped because `TEST_DATABASE_URL` is unavailable locally;
- `ruff check .`: passes;
- `ruff format --check .`: 233 files would be reformatted;
- `mypy app bot.py`: 43 errors in nine files;
- dependency audit: four known findings are selected through the current `cryptography>=41,<48` constraint;
- UTF-8 repository check: passes.

The PostgreSQL behavior of the LTM changes is not considered verified until the integration suite runs against an isolated database with pgvector and all migrations applied.

## Confirmed Defects

1. CI does not run for the active `vps_testai` branch and references the removed `load_test.py` file.
2. The integration job provides `DATABASE_URL`, while integration fixtures require `TEST_DATABASE_URL`; the job can therefore succeed with all database tests skipped.
3. The CI formatter would fail on 233 files, and the unpinned Ruff range makes formatting behavior non-deterministic over time.
4. Deploy runs independently of CI, has no concurrency guard, deploys the mutable `latest` tag, and implements the Docker build retry incorrectly.
5. `cryptography` is constrained below all versions required to resolve the current audit findings.
6. `TaskManager.drain()` returns before cancelled tasks finish their cleanup.
7. `force_update_tavily_keys()` performs destructive refresh steps across independent database operations despite documentation claiming atomicity.
8. `clear_db.py` executes immediately against `DATABASE_URL` and treats the normal Fernet `gAAAAA` prefix as corruption.
9. `ChatState._has_persisted_chat` is a dynamic, undocumented runtime field used across repositories and handlers.
10. Type-checking configuration targets Python 3.11 while runtime, Docker, and CI target Python 3.14.
11. Real-time extraction and consolidation independently implement node/edge upserts and provenance projection, creating a large locality and maintenance problem.
12. README, architecture documentation, CI claims, test counts, and in-bot help have drifted away from the code.

## Scope

### Included

- CI, dependency audit, deterministic lint/format/type/test gates;
- deploy ordering, concurrency, immutable image identity, retry semantics, and post-start health detection;
- safe dependency upgrade for `cryptography`;
- bounded and observable background-task shutdown;
- atomic Tavily key replacement;
- removal of unsafe `clear_db.py`;
- elimination of the current application mypy errors without behavioral changes;
- a shared transaction-scoped LTM graph persistence module;
- full repository formatting as an isolated mechanical wave followed by a global format gate;
- README, architecture documentation, changelog, relevant module docstrings, and user-facing bot help/menu text;
- Russian and English parity for the user-facing capability catalog and help topics.

### Excluded

- new database schema or migration solely for the refactor;
- changes to LTM extraction prompts, privacy policy, retention policy, semantic thresholds, or conflict policy;
- automatic application rollback after production migrations;
- replacement of `clear_db.py` with another production-wide destructive script;
- unrelated performance work, UI redesign, or feature development;
- claiming local PostgreSQL integration success when no test database is available.

## Delivery Waves

### Wave 1: Truthful Verification and Dependency Security

CI runs for pushes and pull requests involving the active `vps_testai` branch. Legacy branch triggers may remain only when they correspond to an intentionally supported branch.

The pipeline contains explicit, non-overlapping checks:

1. Ruff lint;
2. application mypy (`app` and `bot.py`);
3. unit/E2E tests with integration tests explicitly excluded;
4. integration tests against an ephemeral PostgreSQL/pgvector service after migrations;
5. dependency audit against production requirements;
6. global Ruff format check after the formatting wave is complete.

No command references `load_test.py`. Integration setup provides both application-safe test configuration and `TEST_DATABASE_URL`; collection must fail rather than silently pass if the database job lacks its test DSN.

`cryptography` moves to a supported 50.x range whose floor resolves all currently reported advisories. Crypto behavior is protected by the existing Fernet and key-encryption tests plus a fresh dependency audit.

### Wave 2: Deploy Correctness

Deploy is tied to the successful CI result for the exact `vps_testai` commit. The image is tagged with the commit SHA; `latest` may remain as a convenience tag but is never the deployment identity.

A deploy concurrency group allows only the newest active deployment for the branch. Docker build retry uses supported GitHub Actions failure semantics: the first attempt may continue to a retry, while failure of the retry fails the job.

Critical prerequisites fail closed:

- migrations must succeed;
- the local Telegram Bot API must become ready;
- the new bot container must reach its `/health` endpoint within a bounded interval.

An unhealthy deployment fails visibly and preserves container logs for diagnosis. Automatic rollback is excluded because a completed schema migration may not be backward compatible with the previous application image.

### Wave 3: Operational Correctness and Explicit Types

#### Background tasks

`TaskManager.drain()` uses two bounded phases:

1. wait for ordinary completion until the requested drain timeout;
2. cancel remaining tasks and wait for cancellation cleanup for a separate grace interval.

The method returns an observable success value. Stubborn task names and counts are logged at error level. Shutdown remains bounded; it never replaces cancellation cleanup with a fixed sleep.

#### Tavily refresh

Settings validation, hashing, and encryption happen before destructive database work. One acquired connection and one transaction perform:

1. delete old Tavily keys;
2. insert the prepared replacement set;
3. delete old usage records.

The in-memory key cache is cleared only after a successful commit. Any database error returns failure while preserving the previously committed key pool and cache.

#### Unsafe database cleanup

`clear_db.py` is removed, and its stale Ruff ignore is removed. Existing explicit administrative operations remain the supported maintenance surface.

#### Type contracts

`ChatState` gains an explicit persisted-chat field with semantics matching current call sites. Remaining mypy failures are resolved using accurate annotations, distinct local names, explicit returns, and typed options; broad `Any`, blanket ignores, and runtime behavior changes are avoided.

Ruff and mypy target Python 3.14, matching Docker and CI.

### Wave 4: Deep Transaction-Scoped LTM Graph Module

A new repository module owns the implementation details of graph persistence. Its visible contract consists of typed mutation plans and a typed result. It receives:

- an existing database connection already inside the caller's transaction;
- the current user ID;
- normalized node candidates with embeddings, attributes, and supporting memory IDs;
- normalized edge candidates with predicate embeddings, weights, core flags, and supporting memory IDs;
- caller-resolved conflict decisions when the real-time extraction path used an external ambiguity judge.

The module owns:

- semantic and exact node resolution under the mutation lock;
- node upsert and canonical-name mapping;
- node-source upsert and projection of current node attributes from provenance;
- exact-predicate edge upsert and caller-authorized closing of superseded edges;
- edge-source upsert and projection of current edge attributes from provenance;
- validation that every persisted graph object has a durable source;
- returning affected node/edge counts and mappings required by callers.

The module does not own:

- acquiring the connection or opening/committing the transaction;
- RLS user-context setup;
- advisory-lock acquisition;
- consent, epoch, or source-snapshot policy;
- LLM calls, embeddings, or ambiguity resolution;
- insertion of consolidated facts or marking raw memories consolidated.

This ownership preserves the existing atomicity boundaries:

#### Real-time extraction flow

1. validate durable source and consent;
2. call structured extraction and compute embeddings outside transactions;
3. read an optimistic conflict snapshot;
4. recheck privacy before any external ambiguity call;
5. open the write transaction, set RLS context, acquire the per-user advisory lock, and recheck source/epoch;
6. pass a normalized plan to the shared graph module;
7. commit or roll back as one graph mutation.

#### Consolidation flow

1. hold the existing privacy lease and validate the raw-memory snapshot;
2. call consolidation extraction and compute every required embedding outside the transaction;
3. open the transaction, set RLS context, acquire the per-user advisory lock, and recheck the exact source snapshot;
4. insert consolidated facts and derivation links;
5. pass normalized node/edge provenance referring to the inserted fact IDs to the shared graph module;
6. mark raw sources consolidated;
7. commit all facts, graph data, provenance, and source marks together.

Database failures from the shared module propagate to the transaction boundary. Real-time extraction may convert a completed rollback into a logged zero-result. Consolidation must leave raw memories unmarked so it can retry safely.

No schema or SQL-policy change is required. Existing tenant-scoped foreign keys, RLS, provenance tables, and advisory locking remain authoritative.

### Wave 5: Deterministic Repository Formatting

Formatting occurs only after functional changes and their focused tests are green.

1. Pin the Ruff version used by development and CI.
2. Align the formatter target with Python 3.14.
3. Run `ruff format .` as a standalone mechanical change with no behavioral edits mixed into it.
4. Re-run encoding, Ruff lint, mypy, and the complete available test suite.
5. Enable `ruff format --check .` as a required CI gate.

This sequence avoids a permanently red gate and makes the one-time 233-file normalization reviewable as mechanical work. The formatting change should be isolated to reduce conflicts with concurrent branches.

### Wave 6: Engineering and In-Bot Documentation

#### Engineering documentation

Update:

- `README.md` with truthful CI behavior, test commands, counts, integration requirements, and security gates;
- `docs/ARCHITECTURE.md` with current structure, counts, verification flow, and the LTM persistence boundary;
- `CHANGELOG.md` with all user-visible and operational changes;
- docstrings for the new LTM module and modified lifecycle/transaction helpers.

Documentation must explicitly distinguish locally passed tests from integration tests that require an external database.

#### User-facing capability documentation

Introduce one capability catalog for public bot commands and help categories. Each entry carries a stable command identity, category, localization keys, a short Telegram-safe description, and availability metadata where required.

The catalog becomes the shared source for:

- the Telegram command menu;
- the `/help` overview;
- categorized help navigation;
- tests that compare documented public commands with registered handlers.

Detailed scenario copy remains localized in `app/i18n.py`. Administrative commands remain separate and are never exposed in public help.

The audit covers the primary user journeys:

- start and main menu;
- chat, models, roles, thinking, and custom instructions;
- web search and research;
- documents;
- conversations and export;
- memory, personal-data export, and account deletion;
- settings;
- image generation and media understanding;
- Live Audio;
- games;
- reminders;
- Tarot, horoscope, and natal chart entry points.

Copy rules:

- explain the benefit or action before naming the command;
- use consistent terms across buttons, menus, and help;
- avoid internal terms such as RLS, provenance, graph, provider, or epoch;
- state what failed, whether data was preserved, and the next safe action;
- describe irreversible consequences before confirmation;
- keep the main help compact and reveal details by category;
- maintain Russian and English parity;
- remove stale promises and verify every limit or command against code.

## Error-Handling Invariants

- Privacy-sensitive LTM operations fail closed.
- Missing or stale provenance produces no graph write.
- No external network operation occurs inside an LTM write transaction.
- Database exceptions are not swallowed inside the shared graph module.
- Consolidation never marks raw sources processed unless fact and graph writes commit.
- Tavily cache state changes only after the replacement transaction commits.
- Shutdown is bounded and reports incomplete cancellation cleanup.
- CI cannot represent skipped integration tests as a successful database verification.
- Deploy cannot run for a commit whose required CI result failed.
- User-facing errors contain no secrets, stack traces, SQL, or provider internals.

## Test Strategy

All behavior changes follow red-green-refactor.

### Workflow contract tests

- active branch triggers;
- absence of deleted file references;
- explicit separation of unit and integration commands;
- PostgreSQL/pgvector service, migrations, and `TEST_DATABASE_URL` wiring;
- dependency audit invocation;
- CI-to-deploy success dependency and exact SHA checkout/tagging;
- deploy concurrency, retry semantics, and health gate.

### Operational tests

- cancellation cleanup completes before `drain()` reports success;
- stubborn cancellation returns failure within the bound and is logged;
- Tavily refresh uses one connection and transaction;
- failure after delete rolls back and does not clear cache;
- successful commit clears cache exactly once;
- unsafe cleanup script and stale configuration entry are absent.

### Type-contract tests

- current chat persistence behavior remains unchanged with the explicit dataclass field;
- `mypy app bot.py` is clean under Python 3.14 configuration.

### LTM module tests

- source-less node or edge plans fail closed;
- node and edge provenance is upserted before aggregate projection;
- current attributes are derived only from complete, tenant-owned provenance;
- exact predicate identity and semantic node threshold remain unchanged;
- conflict decisions close only authorized current edges;
- caller-owned transactions roll back all shared-module writes on failure;
- extraction retains consent/epoch rechecks around external ambiguity resolution;
- consolidation retains exact source locking, fact derivation, and mark-after-write ordering;
- integration tests exercise both callers against PostgreSQL with pgvector.

### Documentation and help tests

- every public catalog command is registered;
- administrative commands are not exposed publicly;
- every catalog/help key has Russian and English text;
- Telegram command descriptions stay within platform limits;
- generated HTML/Markdown is valid for the selected parse mode;
- help contains no stale or nonexistent commands and no internal terminology.

## Verification Sequence

For each wave, run the smallest relevant tests first. Before completion, run:

1. `python scripts/check_encoding.py`;
2. `python -m ruff check .`;
3. `python -m ruff format --check .`;
4. `python -m mypy app bot.py`;
5. unit/E2E suite with integration explicitly excluded;
6. integration suite when an isolated `TEST_DATABASE_URL` is available;
7. dependency audit against `requirements.txt`;
8. workflow contract tests and documentation/help tests.

If the integration database remains unavailable locally, completion reporting must say so plainly and rely only on the configured CI integration job for database verification.

## Acceptance Criteria

- Active-branch CI is truthful and all required gates are green.
- Production dependency audit reports no known vulnerability for the resolved requirements.
- Deploy consumes a successful CI result for the same immutable commit and detects an unhealthy start.
- Graceful shutdown awaits cancellation cleanup within a documented bound.
- Tavily refresh is atomic and preserves old state on failure.
- The unsafe cleanup script is gone.
- `mypy app bot.py` reports no errors.
- Both LTM callers use the shared transaction-scoped graph module without changing privacy, retention, provenance, or conflict semantics.
- Global Ruff format check passes and is enforced in CI.
- Engineering documentation matches the final code and verification evidence.
- Public bot help is centralized, bilingual, concise, kind, actionable, and consistent with registered commands.

