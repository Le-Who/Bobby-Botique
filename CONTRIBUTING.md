# Contributing

Read [AGENTS.md](AGENTS.md) for authority, workspace and sensitive-data agreements.
Select implementation/safety contracts through the [standards task index](CODING_STANDARDS.md#task-index).
This guide owns reproducible setup, checks and service isolation; the
[documentation index](docs/README.md) distinguishes current and historical references.

## Reproducible setup

The project requires Python `>=3.14,<3.15` and uv `==0.12.6`:

```bash
python -m pip install "uv==0.12.6"
uv --version
uv sync --locked
```

`pyproject.toml` is the editable manifest; `uv.lock` fixes exact versions.
Do not update the dependency graph as a side effect of unrelated work.
The bot reads process environment; for local execution with an untracked `.env`:

```bash
uv run --locked --env-file .env python bot.py
```

This starts the real application, can apply migrations and contact Telegram/providers.
It is not an offline verification command. Use dedicated development resources.

## Validation by change type

For a focused Python change, start with the relevant existing test file:

```bash
uv run --locked pytest tests/test_bot_help_catalog.py --override-ini="addopts=" --timeout=30
```

Replace that example with the affected area. For broader Python/runtime changes,
the CI-aligned local gates are:

```bash
uv run --locked ruff check .
uv run --locked ruff format --check .
uv run --locked mypy app bot.py
uv run --locked pytest tests/ --ignore=tests/integration -m "not integration" --override-ini="addopts=" --timeout=30
```

`pytest.ini` defaults to `-n auto --dist=loadgroup --basetemp=.pytest_tmp --timeout=30`.
The commands above clear `addopts` to run serially without reusing that fixed
temporary directory and retain the per-test timeout explicitly. Do not use
`-m unit` as a synonym for all non-integration tests: marker coverage differs.

Runtime warnings and unraisable exceptions fail tests. Fix the owning task or
fixture cleanup instead of suppressing async driver errors globally. Offline E2E
cases run without a database; only fixtures that actually acquire a test
connection skip when their explicitly isolated service is missing.

## Browser regressions

The five natal report browser cases require Node.js 24 and the exact Playwright
version in `tests/browser/package-lock.json`. Install and run them from the
repository root:

```bash
npm --prefix tests/browser ci --ignore-scripts
node tests/browser/node_modules/playwright/cli.js install --with-deps chromium
GEMAIBOT_REQUIRE_BROWSER_TESTS=1 uv run --locked pytest tests/test_natal_web_report.py -m browser -n 0 --override-ini="addopts=" --timeout=30
```

In PowerShell, set `$env:GEMAIBOT_REQUIRE_BROWSER_TESTS = "1"` before the pytest
command. On Windows the browser installer downloads Chromium; system dependency
installation applies to Linux. Missing Node.js or the package is an optional skip
in a minimal local checkout. The dedicated CI browser job sets the required flag,
installs Chromium and fails if the prerequisites or checks are missing.

## Documentation checks

Read [Text editing and UTF-8](CODING_STANDARDS.md#text-editing-and-utf-8) before editing.
Run the encoding scan before and after documentation edits; after edits, also review
source facts and links and check the diff:

```bash
python -X utf8 scripts/check_encoding.py
python -X utf8 scripts/check_docs_links.py
git diff --check
```

The encoding script strictly
decodes tracked and non-ignored new Markdown, rejects unexpected C0/C1 controls
and known mojibake signatures, and never modifies files. Explicit filenames limit
the check to those inputs. It cannot detect every semantically damaged word.
Do not run unrelated runtime/provider tests to validate prose.

CI also runs `python scripts/check_docs_links.py` (current guides; add
`--include-historical` for an archival audit) and `python scripts/check_env_registry.py`.
The latter compares literal readers and explicit Docker forwarding with
`docs/config-registry.json`; after reviewing an intentional source change, refresh
with `python scripts/check_env_registry.py --write`. It does not validate live
environment values, dynamic readers or deployment success. See the
[revision review](docs/revisions-review-2026-09-29.md) for exact coverage.
Supplied `revisions/` bundles are external review inputs, excluded from Ruff and
the current-guide link check; integrated files remain subject to normal gates.

## Database and integration safety

Integration tests appear both in `tests/integration/` and among top-level tests.
They require an explicitly disposable PostgreSQL target with pgvector; some paths
also need isolated Redis. CI uses PostgreSQL 17 and Redis 7 service containers.

```bash
uv run --locked pytest tests/ -m integration -n 0 --override-ini="addopts=" --timeout=30
```

Set `TEST_DATABASE_URL` only to that test database. Production-target guards compare
host, port and database, not just credentials. `GEMAIBOT_TEST_DATABASE_IS_EPHEMERAL`
is a narrowly checked CI/loopback exception, not a local bypass switch.
Tests may run destructive DDL/DML; transactional fixtures do not protect every
test path. Do not report skipped integration tests as passed.

The real Redis queue-script tests additionally require `TEST_REDIS_URL` pointing
to disposable Redis. They execute the production Lua constants, use UUID-scoped
keys and remove only their own keys. CI uses database 15 of its temporary Redis
service for these cases. The integration command above selects both PostgreSQL
and Redis cases; absent service URLs are reported as skips locally.

CI applies the migration chain twice and checks pending versions before tests.
Before preparing a disposable database manually, read [Database and migrations](CODING_STANDARDS.md#database-and-migrations)
for the migration command's target and write side effects, then set that target
deliberately.

## Tooling and hooks

Pre-commit and locked/CI Ruff are aligned at 0.15.2. Install hooks for each clone:

```bash
uv run --locked pre-commit install
uv run --locked pre-commit run documentation-encoding --all-files
```

The local encoding hook checks Markdown filenames supplied by pre-commit; CI runs
the default repository-wide scan. Ruff hooks can auto-fix Python files, so use
the locked `ruff check` and `ruff format --check` commands for read-only review.
Local hook installation is not shared by cloning.

CI also builds/smokes the production image and produces dependency audit,
SBOM and license inventory evidence. These are distinct from local unit checks.
The observability job validates pinned Compose/Alloy/Loki/HAProxy configuration
and starts the private stack to check readiness and Grafana authentication.
It also verifies end-to-end ingestion of synthetic bot events through the stdout
collector pipeline, including filtering, privacy and query-limit checks.
Live canary/deployment actions need dedicated credentials and explicit operational
scope; see [dependency maintenance](README.md#dependency-maintenance).

## Review evidence

- Add meaningful regression tests for changed behavior; do not create tests that
  merely duplicate low-impact prose or configuration text.
- Inspect fixtures/targets before execution: a test name does not establish offline
  scope. Start with the smallest meaningful check; broaden for cross-cutting behavior,
  schema, auth, provider or lifecycle changes, and fix regressions caused by the change.
- Run additional checks only when the scope, a failure or an unresolved concern
  warrants them. Report pre-existing failures separately without masking them.
- A review summary should state the change, exact checks/results, and remaining
  gaps. Use [Deployment and readiness](CODING_STANDARDS.md#deployment-and-readiness)
  for the limits of local/service evidence.

For environment changes, apply [Providers, models and configuration](CODING_STANDARDS.md#providers-models-and-configuration);
for async or response changes, apply [Telegram delivery and async work](CODING_STANDARDS.md#telegram-delivery-and-async-work).
Test data and sensitive findings follow [Credentials, authentication and webhooks](CODING_STANDARDS.md#credentials-authentication-and-webhooks);
message-bearing logs follow [Logging and private observability](CODING_STANDARDS.md#logging-and-private-observability).
