# Next priority coverage — 2026-10-15

This dated follow-up continues the [message ownership round](message-ownership-followup-2026-10-15.md).
It records uncommitted working bytes based on `68489459ecf7660eb2a23da474b52c430cb9be2f`;
it is verification evidence, not deployed state or authority for further changes.

## Priority and result

Current-source audits selected two demonstrated local lifecycle failures:
overlapping cancellation owners interrupting memory-task cleanup, and an obsolete
long-wait edit overwriting answer/recovery content. These precede lower-priority
coverage of the existing network-stall recheck and document failure presentation.
Earlier coverage maps were used as pointers; current source and tests established
the remaining gaps. Diagnostic probes asserted bad behavior and are kept distinct
from regression RED/GREEN evidence.

Added **72 test cases**: **34 memory** and **38 notice**
cases. The final serial offline suite passed **4714 cases**,
including **118 browser-marked cases**, with
**7 deselected** integration-marked cases. Setup, call and
teardown all passed for every executed case. The final result has no skipped or
failed phases, collection errors, runtime/unraisable warnings or pending-task
output. Duration: **467.911 seconds**.

## Concurrent memory cancellation owners

[`memory_autosave.py`](../app/repos/memory_autosave.py) now initiates cancellation
only for an unfinished child without a pending cancellation request. Every owner
still independently awaits its own shielded join. Two per-user callers or a
per-user caller overlapping zero-timeout shutdown drain therefore wait for the
same child's asynchronous finalizer rather than cancelling it a second time.

The [new memory tests](../tests/test_memory_concurrent_cancellation_ownership.py)
cover direct registration and real retryable TaskManager submission, successful
and failing cleanup, and cancellation of neither/first/second/both owners. Events
hold the finalizer while assertions require the child and both owners to remain
pending, cleanup to start once, the registry to retain ownership and each caller
to preserve its own first cancellation reason. Completed and already-cancelling
children are positive controls. Per-user operations preserve other-user work;
global shutdown intentionally has a broader scope.

The source correction preserves the previous helper fix, durable consent epochs,
barrier ordering, existing exception/retry policy and the TaskManager implementation.
No finalizer deadline or detached cleanup was added. A positive `Task.cancelling()`
count is not a hard-stop guarantee for work suppressing cancellation without
`uncancel()`. Independent direct cancellation senders remain outside the common
helper's coordination guarantee. Generic finally tests do not establish actual
SQL rollback or durable lease release.

## Long-wait notice handoff

The [notice tests](../tests/test_long_wait_notice_ownership.py) exercise the real
handler, canonical state ownership, heartbeat, TaskManager and delivery boundary.
Synthetic provider/Telegram operations are held by explicit events. A separately
gated notice finalizer makes it observable whether answer/recovery writes wait for
notice retirement. Assertions inspect displayed text and final actions, along with
task/registry cleanup; a stopped heartbeat alone is insufficient.

The correction retains ownership of notice work and joins its local cooperative
cleanup at the response handoff. First text and terminal output without text are
covered separately. Stop/retry, normal completion and relevant replacement/startup
exits retain the preceding round's ownership guarantees. Domain/recovery actions
remain owned by delivery. The report does not claim that local cancellation can
recall a Telegram edit already accepted remotely.

Stop/retry callbacks revalidate the captured owner after awaiting worker cleanup
and again after the stop toast, before publishing recovery UI. The new notice
registry distinguishes chats sharing the same numeric Telegram message ID.
Two existing manually driven execution fixtures now provide real Task completion
and callbacks; their behavioral assertions remain in place.
The agent's exception recovery fallback awaits that same scoped retirement before
its direct recovery edit; independent review demonstrated why worker-finally
cleanup was too late on that path.
The broad source review also reproduced the ordinary-chat preflight refusal
bypass and grouped the reachable direct-output siblings into one correction.
Necessary chat/search/QnA and music terminal handoffs await notice retirement;
existing progress and long-wait timing, provider selection and privacy decisions
remain the policy. Retirement at handler startup would remove useful wait UI and
is not used as a shortcut.

Final runtime/test files and exact new nodeids are listed by SHA-256 and case ID
in the [evidence JSON](test-coverage/2026-10-15-next-priority/evidence.json).
The [appendix](test-coverage/2026-10-15-next-priority/README.md) records reproduction
and failure-history boundaries.

## Verification and review

Final focused cohorts contain **217 distinct
passing cases**, including all 72 new cases; every one joins the full result.
The full suite ran serially without global coverage instrumentation or concurrent
heavy verification. All **932 selected verification inputs**
had identical before/after full-run hashes. Previous uncommitted work and dated
artifacts were preserved; the JSON records every changed existing entry file.

Task reviews and final broad review use `gpt-6.1-sol`, with `xhigh` for concurrent
ownership implementation/review and `high` for the initial bounded audits. Final review and
documentation status: **approved; zero open findings; documentation checks passed**. Actual global gate results and
review resolutions are recorded in the JSON; no passing result is inferred from
a progress log or a previous report.

## Scoped coverage and limits

These are selected-cohort module measurements, not whole-project coverage.
Baseline and final selected test sets differ, so their counters do not isolate
the percentage gain attributable to the new tests.
Memory's added interleavings exercise previously covered helper lines, so its
87/105 statements and 19/28 branches remain unchanged. Behavioral protection is
broader even when these counters do not increase. Notice source denominators can
change with lifecycle ownership code. Raw metadata/counters are retained; no
per-test context attribution is inferred from the JSON summaries.

| Cohort | Module | Statements | Branches | Statement coverage | Branch coverage |
| --- | --- | ---: | ---: | ---: | ---: |
| Memory | `app/repos/memory_autosave.py` | 87/105 | 19/28 | 82.86% | 67.86% |
| Notice | `app/handlers/agent.py` | 42/65 | 12/24 | 64.62% | 50.00% |
| Notice | `app/handlers/ai_chat.py` | 330/398 | 78/98 | 82.91% | 79.59% |
| Notice | `app/handlers/ai_search.py` | 220/290 | 36/64 | 75.86% | 56.25% |
| Notice | `app/handlers/cb_ai_actions.py` | 61/261 | 8/60 | 23.37% | 13.33% |
| Notice | `app/handlers/messages.py` | 359/503 | 81/136 | 71.37% | 59.56% |
| Notice | `app/response_delivery/coordinator.py` | 149/159 | 50/62 | 93.71% | 80.65% |
| Notice | `app/response_delivery/delivery.py` | 86/104 | 5/16 | 82.69% | 31.25% |
| Notice | `app/utils/heartbeat.py` | 87/88 | 38/40 | 98.86% | 95.00% |

Runs used synthetic credentials and the existing offline recorder, with
repository `.env` reads denied and integration targets unset. Every run with
complete recorder JSON records zero blocked Python socket attempts; incomplete
hard-timeout outputs have no inferred counters. This is a Python guard, not an OS sandbox. PostgreSQL,
Redis, live Telegram/providers, deployment and multi-process behavior were not
exercised. Integration directories were excluded and integration-marked cases
deselected. No commits, pushes, PRs, dependencies or migrations were performed.

## Remaining candidates

The bounded audits recorded three separate follow-ups: existing network-stall
admission (quick/delayed cleanup under the current busy recheck policy); real
document delete-all database failure being collapsed into an empty result; and
cross-channel voice admission during a text reservation before lock acquisition.
They were not implemented here. Non-finishing cleanup deadlines and forced-stop
behavior require lifecycle policy; none was invented by these corrections.

The [implementation plan](superpowers/plans/2026-10-15-next-priority-coverage.md)
records the scoped acceptance checks and final gates.
