# Voice admission and document outcomes coverage — 2026-10-16

This dated round continues the [previous priority coverage work](test-coverage-next-priority-2026-10-15.md).
It verifies uncommitted working bytes based on `68489459ecf7660eb2a23da474b52c430cb9be2f`.
Earlier uncommitted changes are the entry baseline; this report is verification
evidence, not deployed state or authority for further changes. Audits began on
2026-10-15; the implementation plan and report are dated 2026-10-16.

## Priority and result

Current-source audits selected two demonstrated gaps: voice callbacks bypassing
an accepted text request's pre-lock busy reservation, and bulk document database
failure being presented as an empty result. Existing network-stall recheck probes
passed under their current policy and remain a separate coverage candidate.

Added **42 test cases**: **29 voice admission** and **13 document
outcome** cases. The final serial offline non-integration suite passed
**4756 cases**, including **118
browser-marked cases**, with **7 deselected** integration-marked
cases. All setup, call and teardown phases passed. There were no skipped or
failed phases, collection errors, runtime/unraisable warnings or pending-task
output. Recorder duration: **451.867 seconds**.

## Voice callback admission

The [voice gate](../app/handlers/cb_voice.py) now checks canonical
`UserState.is_processing` together with the existing lock check for `confirm`,
`deep_search`, `transcribe_only`, `retranscribe_flash` and `edit`. The
nonblocking text registration can release the outer update-processor lane while
intent or placeholder I/O is pending. During that interval, the text request's
busy reservation remains authoritative even though its canonical lock is free.

The [new voice tests](../tests/test_voice_callback_admission.py) reach this
interleaving through real handler registration, PTB `Application.process_update`,
the real `UserScopedUpdateProcessor`, canonical state and TaskManager. Synthetic
events hold the two admission windows. Busy rejection preserves the exact pending
voice item and the text owner, without voice provider/ASR work, history changes,
edit-mode mutation or success UI. Healthy and held-lock controls, another user,
text completion/cancellation followed by retry, and `voice:cancel` protect the
surrounding lifecycle.

The demonstrated prior effect was overtaking and unwanted dual admission. The
tests do not establish corruption, registry loss or a cleanup leak. Bot identity,
lifecycle and external effects are synthetic; this is not live PTB startup or
Telegram delivery. Local state protection does not establish distributed or
cross-process serialization.

## Bulk document deletion outcomes

The [document repository](../app/documents/repository.py) now propagates exhausted
`asyncpg.PostgresError` and `asyncpg.InterfaceError` from bulk deletion. The real
facade carries them to the callback's existing controlled deletion-error path.
Successful deletion retains its exact count; a genuine empty result retains zero.
The SQL, caller scoping, retry/reconnect policy, facade signature and callback
messages remain their existing contracts.

The [new document tests](../tests/test_document_delete_all_outcomes.py) use the
real DB manager/query, repository, facade, callback, menu and UserState, with a
synthetic driver pool and external boundaries. Error assertions distinguish
failure from empty/success feedback and preserve document selection and other
users' state. Positive/empty/replay, transient retry and cancellation before
mutation controls inspect counts, state, query parameters and resource release.

Previously the exhausted error returned zero, causing inaccurate “no documents”
feedback and a menu refresh showing surviving documents. It did not report a
positive deleted count or demonstrate lost data. Fake driver errors and
cancellation before mutation do not establish an actual PostgreSQL commit,
rollback, durable erasure or post-commit cancellation outcome.

## Verification and review

Final focused cohorts contain **171 distinct
passing cases**, including all 42 new cases. Every focused case joins the
final full result. All **938 selected verification inputs**
had identical before/after full-run hashes. The full run was serial, without
global coverage instrumentation or concurrent heavy verification.

The first full attempt stopped after 1786 passing cases at a new real-menu
assertion. A focused predecessor reproduction traced it to an existing
[`test_callbacks.py`](../tests/test_callbacks.py) assignment that leaked a menu
mock into later tests. Its mock now has an exception-safe scoped lifetime.
The new regression module and its real-menu assertions retain their reviewed
bytes. The failed full attempt and superseded fixture-workaround runs remain
classified separately; the final full result follows the root-cause correction
and a scoped independent re-review.

Current global Ruff, format, Mypy and environment-registry gates passed. Exact
commands, outputs, phase counts, all new nodeids, raw recorder hashes, source
hashes and preservation evidence are in the
[evidence JSON](test-coverage/2026-10-16-admission-document/evidence.json).
The [appendix](test-coverage/2026-10-16-admission-document/README.md) distinguishes
audit, intended RED, exploratory failure and final GREEN evidence.

All agents used `gpt-6.1-sol`: `xhigh` for concurrent admission implementation,
its review and broad source review; `high` for bounded document work. Task gates
reviewed both specification compliance and quality. Final review status:
**approved; zero open findings; documentation checks passed**. Earlier reports and all unrelated entry bytes were preserved.

## Scoped coverage and limits

The measurements below cover selected modules and cohorts, not the whole project.
Baseline and final test sets differ; these counters do not isolate gains caused
by individual new tests. Admission interleavings can add protection without a
numerical line or branch increase. Raw baseline/final metadata and counters are
retained in the JSON; no per-test context attribution is inferred.

| Cohort | Module | Statements | Branches | Statement coverage | Branch coverage |
| --- | --- | ---: | ---: | ---: | ---: |
| Voice | `app/handlers/cb_voice.py` | 294/331 | 89/112 | 88.82% | 79.46% |
| Documents | `app/documents/repository.py` | 78/120 | 6/12 | 65.00% | 50.00% |
| Documents | `app/handlers/cb_documents.py` | 70/135 | 18/36 | 51.85% | 50.00% |

All recorded runs with complete JSON report zero attempted blocked Python socket
connections. Runs used synthetic credentials, denied repository `.env` reads and
unset integration targets. The Python guard is not an OS network sandbox. No
live provider, Telegram, PostgreSQL or Redis was exercised. Integration-directory
tests were excluded and integration-marked cases deselected. No commits, pushes,
PRs, deployments, migrations or dependency changes were performed.

The [implementation plan](superpowers/plans/2026-10-16-admission-document-coverage.md)
records acceptance checks. The current 15-second network-stall threshold and
150-ms recheck remain unchanged; the successful quick/delayed audit schedules
are separate from permanent regression coverage added here.
