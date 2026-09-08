# Contributing

Read [AGENTS.md](AGENTS.md) for repository agreements and the
[documentation index](docs/README.md) for current versus historical references.
Keep changes focused, preserve user data and use redacted/fake test inputs.
Never commit credentials, service-account JSON, private logs or database dumps.

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

Documentation-only edits require source/link review, `git diff --check`,
`python scripts/check_encoding.py` before and after edits. The script strictly
decodes tracked and non-ignored new Markdown, rejects unexpected C0/C1 controls
and known mojibake signatures, and never modifies files. Explicit filenames limit
the check to those inputs. It cannot detect every semantically damaged word.
Do not run unrelated runtime/provider tests to validate prose.

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

CI applies the migration chain twice and checks pending versions before tests.
If preparing a disposable database manually, note that `scripts/migrate.py` reads
`DATABASE_URL`, not `TEST_DATABASE_URL`; set its target deliberately.
Even `--check`/`--status` can create `schema_migrations` and do not verify complete
schema drift. Never run these against production during a routine local check.

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
Live canary/deployment actions need dedicated credentials and explicit operational
scope; see [dependency maintenance](README.md#dependency-maintenance).

## Implementation and review

- Reuse existing adapters, repositories, formatting and fixtures where they fit.
- Preserve async boundaries, tracked background work and response ownership.
- Add meaningful regression tests for changed behavior; do not create tests that
  merely duplicate low-impact prose or configuration text.
- Run additional checks only when the scope, a failure or an unresolved concern
  warrants them. Report pre-existing failures separately without masking them.
- Update current reference docs with changed behavior; keep old plans and release
  evidence explicitly historical.
- A review summary should state the change, exact checks/results, and remaining
  gaps. Do not equate local tests with deployed/provider verification.
- Do not publish sensitive findings in PR text; follow [SECURITY.md](SECURITY.md).
