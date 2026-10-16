# Next priority coverage evidence — 2026-10-15

This appendix supports the [dated report](../../test-coverage-next-priority-2026-10-15.md)
and [machine-readable evidence](evidence.json). It describes uncommitted local
working bytes, not a deployed service.

## Final evidence

- Full offline suite: **4714 passed**, with
  **118 passing browser-marked cases** and
  **7 deselected**; exit0, no skipped/failed phases or
  collection errors. All setup/call/teardown phases passed.
- New cases: **72 = 34 memory + 38 notice**. Final
  focused cohorts contain **217 distinct**
  passing cases, all present in the full run.
- Full duration **467.911s**; no runtime/unraisable,
  unawaited-coroutine or pending-task output. Blocked Python socket attempts:0.
- Before/after snapshots: **932 identical entries**.
  Changed Python file hashes match frozen inputs and current files.
- Prior work preservation, raw scoped counters, exact args/versions, all new
  nodeids and every retained recorder outcome are in JSON.
- Final review/documentation status: **approved; zero open findings; documentation checks passed**.

## Reproduction

From repository root with locked uv0.12.6/Python3.14.3:

```powershell
$env:GEMAIBOT_AUDIT_OUTPUT_DIR = '.superpowers/sdd/2026-10-15-next-priority-coverage/<fresh-output-directory>'
uv run --locked python -X utf8 docs/test-coverage/2026-10-09/offline_audit.py focus tests/ --ignore=tests/integration -m 'not integration'
```

The recorder clears default parallel addopts, keeps `--timeout=30`, adds `-x`
and a unique basetemp. Full coverage instrumentation is not enabled. The recorder
clears its browser-required flag; actual passing browser-marked cases establish
execution. Exact effective arguments and deselected nodeids are in JSON.

Scoped baselines/finals use explicit modules, branch coverage, unique
`COVERAGE_FILE` and `COVERAGE_CORE=ctrace`. The memory schedule fix does not raise
its measured counters. Notice source denominators can change. No global or
per-test-context coverage percentage is claimed.

## Interpreting retained runs

`run_purposes` distinguishes diagnostic probes of bad behavior, baselines, genuine
pre-source REDs, exploratory iterations and current final GREEN runs. A passing
diagnostic probe asserts the defect and is not a regression fix. RED failures
must have the intended failing behavioral call; setup/collection mistakes are
reported separately. Focused cohorts overlap history and must not be added to
the full count. Raw JSON and synthetic-log SHA-256 joins are published for every
retained recorder result.

Memory REDs cover a second user owner, shutdown-drain overlap and actual retryable
TaskManager submission. The original child's event-held finally was interrupted
before release. The corrected final matrix preserves independent caller reasons,
one cancellation initiation, owned cleanup and registry lifetime. Notice REDs
cover an obsolete cooperative edit overtaking successor output; current GREEN
results assert final text/actions and cleanup rather than a timer flag alone.

The memory owned-check helper initially misread a clean Git no-index difference
exit1. Its clean/bad whitespace probes and corrected final checks retain that
tooling history separately; no test assertion or warning policy was weakened.

Notice baselines initially included two incorrect test selections (exit4), and
exploratory test construction/markup corrections are labeled separately in JSON.
One neighboring run hit the existing hard timeout because an old Task-spec mock
never invoked completion callbacks. Its synthetic focus.log is retained, but no
recorder JSON was emitted, so no test total or successful result is inferred for
that run. The two affected capture fixtures were narrowly repaired to provide
real Task/callback semantics before final verification. The existing timeout and
warning policy remain unchanged.

Independent task review found an additional real-agent exception fallback that
bypassed typed terminal delivery and started recovery output before retirement.
A corrected reviewer probe and a permanent real-handler regression both failed
before the minimal `agent.py` handoff correction. The reviewed scope extension
and subsequent re-review are recorded separately from the original typed-path
result. Earlier reviewer probe attempts included an overbroad edit gate (one
hard timeout without JSON and one bounded checkpoint failure) and a diagnostic
keyboard serialization error; they are retained as harness failures, not defect
proof. Final verification uses the corrected permanent regression.

The subsequent broad source review reproduced a real regular-chat generation
preflight refusal being overwritten after worker completion. Its one grouped P1
also identified reachable search/QnA stop/refusal and music terminal handoffs for
the original author to repair together. That source report, bounded rework,
permanent regression evidence and same-seat re-review remain distinct from the
earlier task-level agent-fallback finding. The final cohort supersedes the
earlier typed-path and agent-only focused runs; their historical results remain
available without being presented as verification of the later correction.
The first expanded direct-output cohort passed all cases but misspelled the
coordinator coverage target and emitted a coverage warning. It is retained as
an intermediate result; the current final cohort repeats the same case set with
all eight intended module targets and unchanged formatted source/test bytes.

## Boundaries and local artifacts

Raw recorder outputs, entry/runtime baselines, snapshots and independent reviews
remain under ignored `.superpowers/sdd/2026-10-15-next-priority-coverage/`.
Previous round artifacts were not rewritten. Public evidence lists changed
existing files and the unchanged-entry count.

The guard is not an OS network sandbox. No PostgreSQL/Redis integration target,
live Telegram/provider, migration, deployment or multi-process check was used.
Memory tests do not establish SQL rollback/durable privacy, and the common helper
does not control every external cancellation sender or a never-finishing finalizer.
Notice ordering is local and cooperative; cancellation cannot retract a remotely
accepted edit. No commit/push/PR was performed.
