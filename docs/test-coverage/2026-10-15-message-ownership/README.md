# Edited-request and direct-intent ownership evidence — 2026-10-15

This appendix supports the [dated report](../../message-ownership-followup-2026-10-15.md).
The [machine-readable evidence](evidence.json) reduces synthetic recorder phase
records; it is a dated observation of uncommitted working bytes.

## Final results

- Full serial offline run: **4642 passed**, including
  **118 browser-marked cases**;
  **7 deselected**, **0 failed/skipped/errors**.
- Setup/call/teardown: each **4642 passed**. Exit **0**;
  duration **426.745s**.
- Current scoped branch run: **44 passed**; neighbors **46**. These are
  **90 distinct cases**, including **30 new cases** (19 edited + 11 intent).
- Final focused/full outputs: no runtime/unraisable warnings or pending-task
  messages. Every recorder JSON reports zero blocked socket connections.
- All **813** selected source/test/doc/config
  inputs had identical before/after full-run snapshots. The seven changed Python
  files' SHA-256 values are included in JSON.
- Global Ruff, formatting (736 files), Mypy (288 files) and environment registry
  (111 names, 0 changes) passed after the fix.
- Independent task finding: addressed; scoped fix review clean. Final broad
  review: **approved**, no open findings. UTF-8, current/dedicated dated links
  and whitespace gates: **passed**.

## Reproduction

From the repository root, with locked Python 3.14.3 / uv 0.12.6:

```powershell
$env:GEMAIBOT_AUDIT_OUTPUT_DIR = '.superpowers/sdd/2026-10-15-message-ownership-followup/<fresh-run-directory>'
uv run --locked python -X utf8 docs/test-coverage/2026-10-09/offline_audit.py focus tests/ --ignore=tests/integration -m 'not integration'
```

The recorder overrides default parallel addopts, sets `--timeout=30`, uses a unique
basetemp, captures setup/call/teardown and adds `-x`. Its browser-required flag is
cleared; the actual 118 passing browser-marked
cases establish their execution. Exact effective args/versions and deselected
nodeids are in JSON. Full coverage instrumentation was not enabled.

The current scoped run adds `--cov=app.handlers.messages --cov=app.state
--cov=app.utils.heartbeat --cov=app.handlers.agent --cov-branch --cov-context=test`
with a unique `COVERAGE_FILE`, `COVERAGE_CORE=ctrace`, and JSON output. Scope is the
five test modules named in JSON. Coverage counters/metadata and raw JSON hash are
retained; no whole-project or per-test context percentage is inferred.

## Failure history and interpretation

Seven initial regression calls failed before runtime corrections: superseded
ordinary/edit cleanup, old error UI cleanup, cancelled intent reservation,
repeated cancellation during cleanup, stale edited input and unstarted-worker
reservation. Two further review-fix REDs using real TaskManager submission failed
`CORO_CREATED != CORO_CLOSED` for normal/edit workers before the coroutine-lifetime
correction. Their JSON/log hashes and exact cases are retained.

Final source review exposed a combined done-task/queued-callback/edit race. The
additional RED evidence and the final fix are recorded separately; replacement
heartbeat ownership no longer depends on placeholder I/O yielding.

The initial review probe had a **passing test call but process exit 1**: an unawaited
inner coroutine raised an unraisable warning at pytest unconfiguration. The
recorder aborted before producing JSON. Evidence explicitly records this limit;
the synthetic log and review finding remain local. The subsequent RED fixture
closes leftovers only after its closure assertion fails, preserving clean failing
call evidence without suppressing warnings.

Intermediate exploration is retained separately, including the initial
callback-only cleanup regression, test timing/spy assumptions, and the asynchronous
task-double warning. These are not final passing runs. Final scoped evidence uses
production TaskManager submission for all new cases.

The baseline's coverage `no-sysmon-context` warning is instrumentation-only;
final ctrace coverage is warning-free. Source denominators changed. The prior
round's coverage/CI-discovery timeout is historical and was not rerun/relabelled as
this round's result.

This follow-up's initial full run recorded **2391 passed, 1 failed**, exit1, due to
the old integration-flow submit fixture returning `None`. All 813 selected inputs
were unchanged during that run. This evidence is preserved as `initial_full_run`,
separately from final success; no timeout was relaxed.

## Preservation and evidence boundary

JSON lists all existing entry files changed this turn and the count of unchanged
entry files; previous priority artifacts, memory changes/tests and unrelated work
were preserved. Raw synthetic recorder logs/JSON, baseline bytes, before/after
snapshots and independent review reports remain under the local ignored
`.superpowers/sdd/2026-10-15-message-ownership-followup/` workspace. They are not
committed payload. JSON supplies hashes and compact records for review.

No live service, integration database/Redis, migration, deployment or multi-process
verification was performed. The Python socket guard and synthetic environment do
not constitute an OS network sandbox. No commit/push/PR was made.
