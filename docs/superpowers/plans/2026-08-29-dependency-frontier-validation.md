# Dependency Frontier Validation Implementation Plan

**Design:** `docs/superpowers/specs/2026-08-29-dependency-frontier-validation-design.md`  
**Execution style:** test-driven, incremental commits, no automatic merge

## Task 1: Establish dependency metadata contracts

**Create:** `tests/test_dependency_metadata.py`

1. Add failing tests that require:
   - `[project]` metadata with Python 3.14 support;
   - runtime dependencies in `[project.dependencies]`;
   - development dependencies in `[dependency-groups].dev`;
   - `[tool.uv] package = false`;
   - explicit `[tool.dependency-frontier]` policy;
   - no duplicate canonical dependency names;
   - a non-empty `uv.lock` matching project metadata.
2. Migrate the two requirements files into `pyproject.toml` without intentionally changing constraints.
3. Generate the first real universal lock with the pinned repository `uv` version.
4. Remove `requirements.txt` and `requirements-dev.txt` as independent manifests.
5. Run the metadata tests, `uv lock --check`, `uv sync --locked`, and `uv pip check`.

## Task 2: Implement the pure frontier model

**Create:** `scripts/dependency_frontier.py`  
**Create:** `tests/test_dependency_frontier.py`

1. Add failing tests for:
   - loading policy from TOML;
   - PEP 508 parsing and canonical names;
   - preserving extras and markers while removing specifiers;
   - rejecting URLs, duplicate names, malformed constraints, and unsupported policy;
   - version/change classification including pre-1.0 releases;
   - exact 14-day cadence across year boundaries;
   - normalized lock comparisons;
   - terminal status transitions.
2. Implement immutable dataclasses and pure functions until these tests pass.
3. Keep subprocess, filesystem, registry, and GitHub concerns outside the pure model.

## Task 3: Implement resolver orchestration and reports

**Modify:** `scripts/dependency_frontier.py`  
**Modify:** `tests/test_dependency_frontier.py`

1. Add failing tests around a mocked command runner for baseline checks, policy/frontier compilation, repeat-resolution comparison, and failures.
2. Generate policy and frontier `.in` inputs into a temporary directory.
3. Call pinned `uv pip compile` with Python/platform/cutoff inputs and a second determinism pass.
4. Parse direct and transitive versions from compiled locks.
5. Write deterministic `dependency-frontier.json` and `dependency-frontier.md` reports.
6. Ensure the default mode is read-only and never edits project manifests or locks.
7. Add `schedule-due` and `audit` CLI commands with meaningful exit codes.

## Task 4: Convert local, CI, and Docker installs to the lock

**Modify:** `Dockerfile`  
**Modify:** `.github/workflows/ci.yml`  
**Modify:** `tests/test_ci_workflow_config.py`  
**Modify:** `tests/test_deploy_workflow_config.py`  
**Modify:** `README.md`

1. Update contract tests first so they require locked `uv` installs and reject direct range installs.
2. Pin the `uv` installer/version consistently in CI and Docker.
3. Replace `pip install -r ...` with `uv sync --locked` or a frozen export derived from `uv.lock`.
4. Export an exact production requirements artifact only for `pip-audit`.
5. Make Docker install exactly the production subset from the lock.
6. Update developer commands and architecture documentation.

## Task 5: Add scheduled discovery and update PR policy

**Create:** `.github/workflows/dependency-frontier.yml`  
**Create:** `.github/dependabot.yml`  
**Create:** `tests/test_dependency_frontier_workflow.py`

1. Add failing workflow contract tests for:
   - weekly wakeup plus fixed-epoch 14-day gate;
   - manual dispatch;
   - read-only discovery permissions;
   - pinned uv;
   - seven-day cooldown;
   - grouped patch/minor updates and separate major updates;
   - no automerge;
   - report artifacts and job summary;
   - no secrets in resolver/test jobs.
2. Implement the workflow and Dependabot configuration.
3. Run resolver/report tests and validate YAML parsing.

## Task 6: Strengthen dependency boundary and service tests

**Create:** `tests/test_dependency_boundaries.py`  
**Modify:** `.github/workflows/ci.yml`

1. Add deterministic contract tests for the high-risk dependency APIs used by the application.
2. Add a real ephemeral Redis service to the integration job.
3. Retain serial PostgreSQL/pgvector integration and repeated migrations.
4. Add a candidate-only container smoke/health job and exact graph checks.
5. Generate an SBOM and audit the exact installed production graph.

## Task 7: Implement the protected live canary

**Create:** `scripts/dependency_live_canary.py`  
**Create:** `tests/test_dependency_live_canary.py`  
**Create:** `.github/workflows/dependency-live-canary.yml`

1. Add failing tests for missing configuration, redaction, transient classification, deterministic failure classification, cleanup, and baseline/candidate comparison.
2. Implement minimal Telegram, Gemini, and Tavily probes using dedicated canary environment variables.
3. Ensure Telegram cleanup runs even after partial failure.
4. Implement the trusted manual-dispatch workflow:
   - accept a dependency PR number;
   - reject fork PRs and any changed file outside the dependency allowlist;
   - use trusted base code and only candidate manifest/lock files;
   - require the protected `dependency-canary` environment;
   - set a pending commit status before execution;
   - run baseline and candidate with identical fixtures;
   - publish success only for a fully passing comparison;
   - leave failure, cancellation, missing secrets, or inconclusive provider state non-green.
5. Document the dedicated secret names and least-privilege requirements.

## Task 8: Add durable status/reporting contracts

**Modify:** `scripts/dependency_frontier.py`  
**Modify:** `.github/workflows/dependency-frontier.yml`  
**Modify:** `tests/test_dependency_frontier.py`

1. Test the exact status vocabulary and forbid implicit success for skipped gates.
2. Produce Markdown/JSON summaries containing tool, cutoff, input hash, platform, direct and transitive changes, and validation state.
3. Create or update one tracking issue only when an actionable frontier change is not represented by a PR; close it on a clean run.
4. Keep GitHub mutation in a separate least-privilege job after read-only discovery finishes.

## Task 9: Repository-wide verification

1. Run encoding validation before and after documentation writes.
2. Run focused new tests without xdist.
3. Run Ruff check and format check.
4. Run mypy.
5. Run the full non-integration suite.
6. Run PostgreSQL/Redis integration tests if local services are available; otherwise validate the CI service contract and report the local limitation.
7. Run `uv lock --check`, locked sync, dependency consistency, and production export audit.
8. Build the Docker image and run its offline smoke check.
9. Run a manual read-only frontier audit and inspect its Markdown/JSON outputs.
10. Review `git diff --check`, encoding, secrets, workflow permissions, and changed-file scope.

## Task 10: Handoff

1. Summarize the canonical dependency workflow and commands.
2. Identify repository changes and verification evidence.
3. List the external GitHub Environment configuration that cannot be committed:
   - `CANARY_TELEGRAM_BOT_TOKEN`;
   - `CANARY_TELEGRAM_CHAT_ID`;
   - `CANARY_GEMINI_API_KEY`;
   - `CANARY_TAVILY_API_KEY`;
   - required reviewer protection for `dependency-canary`.
4. State residual limitations without describing the system as proof of compatibility.
