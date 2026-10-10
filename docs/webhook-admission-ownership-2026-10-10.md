# Webhook admission ownership and provider failure contracts — 2026-10-10

This dated report records a bounded follow-up to the
[priority coverage round](test-coverage-priority-round2-2026-10-09.md).
The confirmed webhook cancellation defect has a regression fix; provider-key
failure behavior remains correct and receives new coverage. Independent provider
and scoped admission correction reviews approve the current change. Final offline
and affected Redis verification passed. The previous round's measurements remain
historical and unchanged.

## Confirmed defect and resulting behavior

Previously, cancelling the actual HTTP request task after Redis persisted a
dedupe claim could end the request before its update reached the PTB queue.
A subsequent Telegram retry could receive a duplicate acknowledgement while
the update had never been handed off. A pre-implementation RED test reproduces
that ordering through the lifecycle-registered Quart route.

[WebhookAdmissionGate](../app/webhook_admission.py) now owns the entire serialized
capacity-check, claim, origin registration and synchronous queue insertion
operation supplied by [bot.py](../bot.py). Lock waiters remain cancellable and
start no claim. Once admission starts, repeated caller cancellation waits for
the same owned child to finish before propagating the first cancellation reason.
The lock remains held until that completion. Child self-cancellation propagates
promptly; operation failures are observed and diagnostic causes are retained.

Normal shutdown closes admission, drains that lock barrier and stops PTB through
one owned shared task. Exceptional cleanup closes admission before its other
resource awaits and owns its entire existing resource sequence; repeated
cancellation waits for that sequence before propagating. Cancellation during
webhook registration after route creation
owns cleanup through the same shutdown task, preserving the first startup
cancellation. PTB already starts before the route is registered; the new tests
characterize that existing ordering. Polling and pre-gate startup cancellation
retain their existing policies. Dedupe identities, NX/TTL behavior and queue
backpressure remain as before.

## New behavioral coverage

| Area | New cases | Observable contract |
| --- | ---: | --- |
| [Ownership unit tests](../tests/test_webhook_admission.py) | 11 | Repeated caller cancellation, unclaimed waiters, self-cancel/error outcomes, simultaneous completion/cancellation, idempotent shared shutdown, stop errors and resource cleanup after a terminal stop |
| [Actual webhook and lifecycle route](../tests/e2e/test_webhook_admission_cancellation.py) | 17 | True HTTP task cancellation after ordinary and both command claims, exact queue/origin handoff, duplicate replay, local-history lock, closure/drain and startup/error cleanup including repeated cancellation |
| [Real Redis contracts](../tests/integration/redis_contracts/test_webhook_admission_ownership.py) | 6 | Actual SET NX persistence and TTLs, two clients, repeated request/shutdown cancellation, one handoff before stop and replay rejection |
| [Provider failure contracts](../tests/test_provider_override_failure_contracts.py) | 20 | Real Fernet and cache invalidation, unreadable override without env fallback, absent/empty override semantics, masked status and Pollinations guards before HTTP |

The provider tests replace SQL and HTTP only at their I/O boundaries. Provider
repositories, settings writers/readers, cache and encryption remain real. An
unreadable stored override already fails closed even with a nonempty environment
key; no runtime provider correction was required. Tests restore synthetic
credentials, crypto/cache globals and patched constructors.

The corrected admission focused run records 101 passes; the provider focused run records
83 passes. These overlap existing neighbor tests and are not additive to a full
suite. The six real Redis cases all pass. Five corrected provider mutations fail
at their intended behavioral assertions; three additional admission mutations
confirm sensitivity at each persisted claim. Genuine RED/GREEN cycles also drove
closure at exceptional-cleanup entry and startup-cancellation ownership. Initial
ineffective writer-alias mutation probes remain identified in private evidence.

Independent admission review reproduced one Important gap in the initial
98-pass snapshot: after an ordinary registration error, cancellation during the
cleanup alert could exit before drain and stop began. The correction establishes
an owned cleanup child before that first await, preserves resource order and
does not skip resources or repeat PTB stop when the shared stop already has a
terminal result. Its genuine RED and 101-pass GREEN remain separate from the
original review and evidence. Scoped correction review marks the finding
ADDRESSED and approves the correction, with no new Critical or Important issue.

## Aggregate verification

The [compact evidence](test-coverage/2026-10-10-webhook-admission/evidence.json)
derives counts and exact nodeids from final pytest phase reports. The full offline
unit/E2E run passed **4,570** cases in 567.526 seconds, including **118 browser
cases**. The affected `tests/integration/redis_contracts` selection passed
**29** cases in 17.710 seconds. Together they contain **4,599 unique passing
cases** and **54 exact new nodeids** relative to the previous corpus. Neither
selection has a failure, skip or collection error; the offline Python audit
recorded zero blocked socket connection attempts. Seven integration-marked
cases outside `tests/integration` were deliberately deselected from the offline
command.

The parent launcher requested `GEMAIBOT_REQUIRE_BROWSER_TESTS=1`, but the dated
runner removes that flag during bootstrap and unit mode does not restore it.
The final evidence audit caught and corrected the launch metadata. Browser
coverage here is established by 118 actual passing cases and zero skips,
rather than by an effective mandatory-prerequisite flag. The runner and test
results were preserved; the correction affects reporting only.

| Offline Python runtime metric (`app` and `bot.py`) | Previous round | Current run |
| --- | ---: | ---: |
| Statements | 31,499 / 44,533 = 70.732% | 31,623 / 44,617 = 70.877% |
| Branches | 7,813 / 13,050 = 59.870% | 7,851 / 13,074 = 60.050% |
| Combined coverage.py measure | 68.270% | 68.423% |

The new admission module has **100% statement coverage and 93.750% branch
coverage**. The aggregate denominator includes unexecuted runtime branches;
these metrics are measured Python coverage, not proof of every delivery outcome
or coverage of JavaScript/SQL. Browser and service contracts supply their own
behavioral evidence.

Locked repository-wide Ruff, format (729 files), mypy (288 source files) and
configuration registry (111 names, zero changes) passed. Pre-documentation
UTF-8 passed for 76 Markdown files; final documentation checks are recorded in
the appendix. All **857** frozen first-party runtime/config and Python test
file hashes match after the runs. Earlier unrelated work and dated historical
artifacts also match their pre-task bytes. Owned Redis process and port closure
were verified; PostgreSQL was not started. The immediate post-stop port probe
still observed a listener; the subsequent read-only process/port checks confirmed
termination and are both retained in the service record.

The prior corpus has **163 nodeids unexecuted in this follow-up**: six collector
E2E cases and 157 other integration cases, including PostgreSQL and legacy queue
Lua tests outside the selected Redis-contract directory. Their nodeids are listed
in the evidence; none is represented as a current pass or skip. Earlier passing
PostgreSQL evidence remains dated to its own round.

## Limits and retained evidence

This fix covers cooperative caller cancellation and graceful lifecycle drain.
Process kill/crash, event-loop destruction or an exhausted shutdown grace can
still leave a Redis claim without a durable queue item. Unknown Redis transport
acknowledgements and dependency self-cancellation retain their existing limits;
local fallback cannot coordinate replicas during a Redis outage. The queue is
process-local, with no exactly-once or durable-delivery claim.

Provider checks cover unreadable overrides present at consumer entry, using
SQL/HTTP doubles; they do not establish live-provider availability,
cross-process cache propagation or an administrative key change during a request.
No PostgreSQL, live Telegram/provider, deployment or migration result is claimed
for this follow-up.

Raw synthetic logs, RED/mutation evidence, scoped review packages and reports
remain in the ignored local workspace
`.superpowers/sdd/2026-10-10-webhook-admission-ownership/`. Public documentation
contains no live credentials, user messages or unrestricted logs. This work
preserves the previous uncommitted changes; no commit, push or deployment is part
of this follow-up.
