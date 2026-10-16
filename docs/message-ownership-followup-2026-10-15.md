# Message ownership follow-up — 2026-10-15

This dated report records the requested continuation of the [priority coverage
round](test-coverage-priority-2026-10-15.md): old-task completion when a Telegram
message is edited, and direct-intent cancellation before placeholder creation.
It describes the uncommitted working files based on checkout
`68489459ecf7660eb2a23da474b52c430cb9be2f`; it is historical verification evidence, not deployed
state or authority for further work.

## Result and behavioral protection

Added **30 parameterized cases**: 19 in
[`test_edited_request_ownership.py`](../tests/test_edited_request_ownership.py)
and 11 in
[`test_direct_intent_admission_ownership.py`](../tests/test_direct_intent_admission_ownership.py).
The final full offline run passed **4642 cases**, including
**118 browser-marked cases**;
7 integration-marked cases were deselected. Setup,
call and teardown all passed for every executed case. No failed or skipped phase,
collection error, runtime warning, unraisable exception or pending-task output
appeared in the final run.

The regressions enter the real handlers, task registry, canonical user lock,
heartbeat implementation and production `TaskManager.submit`. External repository,
Telegram and provider boundaries return synthetic responses. Explicit events
suspend asynchronous cleanup and error UI; elapsed time is not the test oracle.

| Gap | Protected behavior |
| --- | --- |
| Old ordinary/edit worker is still cleaning up | An edited admission cancels and drains its predecessor before reusing the placeholder; late old cleanup cannot erase the replacement's registry/busy ownership or disable its heartbeat |
| Old worker exits through error UI | Its asynchronous error edit finishes before replacement work can update the same placeholder |
| Rapid edits or cancellation while admission drains | The latest owner remains reachable; repeated cancellation does not interrupt predecessor cleanup; the canonical lock serializes workload and another user continues |
| Edited text after success, cancellation or error | The real agent gets request-scoped `text_override`; subsequent normal input is unaffected and legacy agent override calls remain compatible |
| Direct intent is cancelled before placeholder | Owned child cleanup finishes, original cancellation arguments propagate, no placeholder/chat workload starts and a later request is admitted |
| Intent completion/caller cancellation race | Both completion orderings, repeated caller cancellation and a self-cancelled child retain cleanup ownership |
| Healthy intent outcomes | Exact handled Markdown card/retry input, context enrichment, no-intent/unhandled fallthrough and ordinary-exception fallback remain compatible |
| Worker cancelled before its first step | State/heartbeat resources are released and the input coroutine passed to the real task manager is closed |

## Narrow production corrections

[`messages.py`](../app/handlers/messages.py) now registers an admission owner while
direct intent or edited-request handoff is in progress. An edit drains the previous
owner before placeholder/heartbeat reuse. Identity-fenced release prevents an old
owner from clearing a newer reservation. A callback marker also joins queued
resource cleanup when the predecessor is already done, so old completion cannot
remove a replacement heartbeat. Started workers clean up synchronously in
their `finally`; an idempotent done callback also handles cancellation before the
first step. That callback closes the exact input coroutine, accounting for the
production task manager's outer wrapper. The task manager itself is unchanged.

Direct-intent work runs in an owned child. Shielded cancellation drain finishes its
cleanup despite repeated caller cancellation and preserves the original
`CancelledError`. Handled-card early busy release and voice handoff ordering remain
unchanged. Edited input uses the existing explicit agent argument instead of new
writes to shared `_edited_text_override`.

[`state.py`](../app/state.py) retains a cancelling owner until cleanup releases it,
exposes that owner to the edit handoff, and avoids delivering repeated cancellation
into an already-cancelling task. Existing callback consumers remain compatible.
No provider retry, delivery, schema or dependency policy changed.

Three existing test modules received boundary-double adjustments with behavioral
assertions retained: the concurrency spy still performs every real registration
but counts AI workers separately from the current admission task; four local task
doubles in `test_messages.py` now use `MagicMock(spec=asyncio.Task)` because
`Task.add_done_callback` is synchronous. The integration-flow capture fixture now
returns a Task-spec double instead of `None`, retaining its explicit coroutine
execution and database-history/delivery assertions.

## Verification and review

The final focused branch-coverage selection passed **44 cases**
(30 new plus 14 existing); message, state/heartbeat and integration-flow
neighbors passed **46**, giving **90 distinct focused cases**, all included in the full
run. The full suite ran serially, without coverage instrumentation or concurrent
heavy verification; duration was **426.745 seconds**. Its
exact arguments, all new nodeids, phase counts, source hashes and exclusions are in
the [evidence appendix](test-coverage/2026-10-15-message-ownership/README.md) and
[JSON](test-coverage/2026-10-15-message-ownership/evidence.json).

Locked global checks passed: `ruff check .`, `ruff format --check .` (736 files),
`mypy app bot.py` (288 source files), and environment registry (111 names, 0 changes).
Final UTF-8, current and dated-document links, and whitespace checks passed.
The final broad review approved the source, regressions and evidence joins with
no open Critical, Important or Minor findings.

All subagents used `gpt-6.1-sol`: `xhigh` for implementation/task review and final
concurrency review, `high` for bounded contract investigation and scoped fix
review. The task review found one Important gap hidden by direct `create_task`
submission in the initial fixture: real task-manager cancellation left the inner
request coroutine unclosed. Both ordinary/edit paths were reproduced before the
correction; all new cases now use the real submission boundary. Scoped re-review
confirmed that finding addressed. Final source review then reproduced a combined
pre-start-cancellation/edit race: a completed predecessor's queued callback could
remove the replacement heartbeat. Its correction and added regression are checked
in the final scoped review; final evidence sign-off is recorded in the appendix.

## Scoped measurement and limits

These measurements cover four modules under the focused selection, after the
review fix. They are not whole-project coverage percentages.

| Module | Covered statements | Covered branches | Statement coverage | Branch coverage |
| --- | ---: | ---: | ---: | ---: |
| `app/handlers/agent.py` | 34/65 | 10/24 | 52.31% | 41.67% |
| `app/handlers/messages.py` | 333/498 | 76/136 | 66.87% | 55.88% |
| `app/state.py` | 165/312 | 12/40 | 52.88% | 30.00% |
| `app/utils/heartbeat.py` | 35/38 | 8/10 | 92.11% | 80.00% |

The 14-case baseline, seven initial RED runs, review-fix RED runs and exploratory
failures remain separate from final passing evidence. Baseline coverage emitted
`no-sysmon-context`; final scoped coverage used `COVERAGE_CORE=ctrace` without that
warning. Source denominators changed, and these JSON reports do not establish
per-test context attribution. No current whole-project coverage increase is
claimed. The previous round's instrumented CI-discovery timeout remains in its
original dated report; it was not relabeled or used to relax a timeout.

The first full run in this follow-up stopped with **2391 passed and 1 failed**
because the old integration-flow submit fixture returned `None`. Its unchanged
813-input snapshot, actual failure and logs remain separate from final success.

Synthetic credentials, denied repository `.env` reads, unset integration targets
and the recorder's Python socket guard were used; all recorded runs attempted zero
blocked Python socket connections. This is not an OS network sandbox or live
Telegram/provider/PostgreSQL/Redis/multi-process evidence. Integration directories
were excluded and integration-marked cases deselected. Existing network-stall
retry timing and unrelated voice/draw lifecycle policies were not redesigned.

No commits, pushes, PRs, deployment, migrations, dependency updates or live service
calls were performed. Previous uncommitted work and dated artifacts were preserved.
The implementation [plan](superpowers/plans/2026-10-15-message-ownership-followup.md)
records acceptance and verification steps.
