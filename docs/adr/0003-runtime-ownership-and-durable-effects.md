# ADR 0003. Runtime ownership and durable effects

- **Status** Accepted
- **Date** 2026-11-20, recorded from existing behavior

## Repo transactions

Startup runs numbered migrations before installing idempotent RLS policies so
new tenant tables are present when policies are checked. The migration runner
holds a transaction-scoped advisory lock through version reads, DDL and version
writes. This works with PgBouncer transaction pooling and serializes concurrent
replicas. Nested asyncpg transactions provide the existing per-file savepoint
boundary. Resetting tenant context can fail, and callers must stop using that
connection path with stale context. See [the migration runner](../../app/db/migrations.py)
and [the database lifecycle](../ARCHITECTURE.md#database-lifecycle-and-privacy-limits).

The pool applies session timeouts per connection. Startup checks required
columns as well as table presence. Group membership anchors authorization, so
its own RLS policy avoids reading itself and excludes tenant self-enrollment.
Startup replaces historical recursive policies in place to preserve policy
identity and grants. Group startup relies on numbered DDL. The migration runner
keeps its compatibility column checks for legacy installations. Drift telemetry
starts in the background after schema initialization so the bot can report it
when its admin transport is ready. See [RLS setup](../../app/db/rls.py) and
[schema checks](../../app/db/schema.py).

A missing commit acknowledgment leaves the write outcome unknown. Document
upload and saved-conversation paths report success after the repository's
transaction exits. Chat persistence advances its local history checkpoint only
after commit, keeping the pending suffix available for recovery. See
[the document processor](../../app/document_processor.py),
[conversations](../../app/repos/conversations.py) and [chat persistence](../../app/repos/chats.py).

The document processor still exports its parser size limit for older imports.
PDF parsing uses a readable file or an in-memory byte stream through the same
reader interface.

Saved branches can still contain history encoded twice as JSON. When the
decoded value is a string, parse it once more to recover the original history
without changing the saved format. See [branch reads](../../app/repos/branches.py).

## Runtime admission

Acquiring the per-user lock before the request semaphore prevents one user's
queued requests from occupying scarce global slots. The wait queue is bounded
and Redis lease expiry follows server time. Admission wait and lease duration
have independent budgets. Initial Redis infrastructure failure can permit local
admission. An elapsed deadline still rejects. A delayed acknowledgment cannot
extend the lease from the server's earlier renewal time. Request exit drains a
bounded release attempt. The lease wrapper distinguishes its own cancellation
from caller cancellation and preserves any original provider exception. See
[concurrency admission](../../app/adapters/concurrency.py).

Webhook ingress owns capacity, deduplication and queue handoff together. Waiting
for admission is cancellable. After admission starts, the gate retains and drains
the child even when the HTTP request is cancelled. The gate distinguishes a child
that cancelled itself from cancellation of its owner. PTB owns processing
concurrency. Shutdown registers closure before yielding and stops producers
before draining writes and closing transports. Failure cleanup retains its own
resource sequence if the PTB-stop task already terminated. See [the ingress
gate](../../app/webhook_admission.py) and [application lifecycle](../../bot.py).

If `__aenter__` fails, Python does not call `__aexit__`. Admission must therefore
unwind any already-acquired local slot or Redis token on entry failure. The bot
leaves global resource cleanup with the outer lifecycle to avoid double teardown.
Webhook deduplication tolerates loading before the Redis import is available.

A stalled request can be replaced once its stall meets the cancellation policy.
The successor owns admission through predecessor cleanup, including error UI
outside the canonical lock. Further edits drain that successor before they
reuse the placeholder. See [message handlers](../../app/handlers/messages.py).

Callback admission inspects and reserves the lock without suspending, so a
busy callback cannot disturb current work. Role preview cancellation can
interrupt its active provider call. Save and tweak respect the current owner.
The inline retry store also caps active records because sustained traffic can
insert faster than expiry. An edited message cancels its predecessor without
publishing a separate failure for the cancelled request.

## Queue effects

The v2 workers use a versioned Redis namespace and server-time scripts. Old Lua
constants and unowned lists remain for controlled compatibility and migration
tools. Startup observes legacy work without claiming, replaying or deleting it.
Where legacy claims use queue-instance leases, recovery waits for expiry and
works on that owner's processing records. Malformed entries lack safe
attribution. Draining a worker alone cannot establish the outcome of a send.
See [queue storage](../../app/queue_store.py), [legacy queues](../../app/queue_legacy.py)
and [queue rollout](../runtime-controls.md#переход-на-очередь-с-владением).

The queue rotates wake generations so a waiting worker can retain the generation
it observed before claiming, even if Redis has yet to answer. The two admission
APIs distinguish one-shot legacy callers from request-aware callers with an
operation identity. Redis reload after `NOSCRIPT` is safe because the mutation
did not run. Transport failure during a claim leaves recovery to its owner or
lease expiry. See [the queue worker](../../app/queue_runtime.py).

After accepted webhook handoff, a lost Redis acknowledgment leaves definitive
local evidence for process-local deduplication. Broadcast reconciliation leaves
already recovered batches intact on later database failure. Maintenance sweeps
use a bounded batch and a fixed cutoff so the timestamp index can cap each scan.
See [webhook deduplication](../../app/webhook_dedupe.py).

The broadcast repository can retry lost transaction setup on a new pool lease
before caller work begins. It captures a native asyncpg connection and lease
generation before awaits because a disconnected pool proxy detaches. Once
caller work begins, a lost commit acknowledgment cannot be treated as rollback.
Operation lookup and journal reconciliation can recover confirmed admissions.
Unknown admissions without queue evidence are quarantined. Read endpoints only
observe metadata and the journal. Confirmed receipts survive failure of the
legacy convenience marker. One definite failure grants one child even across
different tabs and operation IDs. An unknown child keeps its parent from being
retried. Scheduled sends lock claims before settlement and require the original
source version and live account epoch. Each transport call, including chunks,
rechecks ownership and eligibility. Keyboard updates observe that ownership
without creating a second receipt. Detached TTS owns its own admission and
privacy lease. See [broadcast recovery](../runtime-controls.md#рассылки-и-журнал-доставки),
[broadcast storage](../../app/repos/broadcast_jobs.py) and [scheduled delivery](../../app/scheduled_delivery.py).

Once a reminder shows its placeholder, timeout or cancellation leaves the
later effect uncertain. Automatic replay could create a second answer.

## Private work

The fixed key-derivation salt, `ADMIN_SECRET`, and PBKDF2-HMAC-SHA256 parameters
allow Fernet to read previously encrypted API keys. Existing records depend on
the current iteration count. Changing any of these inputs requires a deliberate
credential migration because existing ciphertext would become unreadable. See
[credential storage](../../app/crypto.py).

Capture the account generation at ingress, before preparation, provider calls
or data access. Preserve the captured value through retries and persistence so
old work cannot adopt a recreated account. A missing chat row and a persisted
generation-zero row have separate meanings. A chatless legacy reminder can acquire
its initial durable generation before work starts. Synchronous direct callers
capture the generation before dispatch. A separate atomic consent write keeps
a stale full ChatState save from restoring consent. Provider privacy leases span
calls containing active history and any injected private memory. LTM opt-out
drains LTM leases and erasure drains both variants. Failed LTM admission can fall
back to a base prompt under an ordinary conversation lease. See [chat
dispatch](../../app/handlers/ai_chat.py), [chat storage](../../app/repos/chats.py),
[private memory](../../app/repos/memory.py) and [natal generation](../../app/natal/service.py).

The memory repository enforces consent even for direct callers. A read
transaction can end before an external judge consumes candidate facts, so the
lease or durable check must still cover that call. A failed retrieval fallback
also remains bound to the captured epoch. The account barrier first blocks new
work and then drains leases. Its compensation is armed before commit can expose
the blocked state. The deletion transaction reacquires the guard, checks the
same epoch and advances it again when unblocking. Summary tasks with raw history
inside a provider call drain before account rows are removed. A cancelled caller
cannot interrupt heartbeat and SQL lease release. Confirmed deletion clears local
state and authorization. An uncertain commit discards stale local snapshots and
reports the durable outcome as unknown. Addressable broadcast attempts cascade
with personal snapshots and anonymous counts survive. See [consent leases](../../app/repos/memory_consent.py)
and [account deletion](../../app/repos/users.py).

Lease acquisition and revocation take the same lock. The first commit therefore
either registers work the barrier must drain or makes the new generation
visible to the next lease attempt. An owner cancels provider work when renewal
can no longer keep it covered, with TTL as a crash fallback. Account erasure
releases its database lock before Redis cleanup and queue cancellation. A
concurrent debounce source may arrive while its owner is still reading the
provisional epoch, but generation work waits for the pinned epoch.

Recall preserves the original instruction so a failed lease can restore it.
Deferred work cannot carry private recall beyond a request-scoped lease.
Duplicate document confirmation covers one upload attempt, including mismatch
or rejection. Saved role previews retain identity across restart. If only part
of a speech group enters the queue, each rejected source receives an outcome.

Background source IDs share the lifetime of their canonical state. Detached
pre-erasure references cannot schedule writes. Persistence snapshots follow
completion of the preceding write, including retries. Request mutation can
continue during I/O under its independent lock. Clear operations fence old
state before hydration and preserve unrelated fields when writing the coherent
snapshot. Maintenance discovers work in a short transaction, then isolates
tenant work so one failure leaves other attempts intact. Storage failures reach
retry-aware callers. Account exports depend on complete LTM and graph reads.
See [process-local state](../../app/state.py) and [memory maintenance](../../app/repos/memory.py).

Hydration treats an unavailable store separately from a missing row so a failed
read cannot authorize writes from defaults. Once database retries are exhausted,
keep the latest state for the debounced retry or shutdown flush.
Recheck the hydration marker after taking the lock because another task can
load the state between the initial read and lock acquisition.

## Provenance

The graph stores each source's attributes separately from the aggregate edge.
Alias resolution can merge edges, so deleting a stronger source must still
restore the surviving source's original weight and core status. Consolidation
only persists entities anchored to provenance-backed edges. It uses endpoint
names and types because free-form entity descriptions lack exact support
indexes. A successful Telegram response receives attribution to the retrieval
edges used for that message. Empty or failed retrieval clears previous
task-local attribution. Feedback bookkeeping remains independent of delivery
success. See [ADR 0002](0002-provenance-safe-memory-graph-writes.md), [graph
projection](../../app/repos/memory_graph_writer.py) and [consolidation](../../app/repos/memory_consolidation.py).

Task-local reads isolate concurrent retrieval, and a bounded cache bridges the
retrieval to a later Telegram callback. Context assembly strips graph display
labels when matching source triples. Expiry excludes records at read time and
maintenance later reclaims them, letting the database remove orphan graph
records through provenance cascades at commit.

Return provenance explicitly from a `gather` child because changes to its
context variables do not propagate back to the caller.

## Async cleanup

Task owners register before work can race shutdown and drain children on every
exit path. A cancellation before the coroutine's first instruction bypasses
its `finally` block, so a completion callback can close an unstarted coroutine
or retire its own album or image state. A superseded summary stays strongly
referenced until cancellation reaches its provider. A failing background job
owns notification cleanup so callbacks cannot escape the drain snapshot or
replace the failure. Repeated cancellation leaves process reaping and voice
lease release owned until they finish. A late circuit-breaker result cannot
settle a newer circuit generation. See [background tasks](../../app/utils/background_tasks.py),
[summary tasks](../../app/context/summarizer.py) and [circuit state](../../app/circuit_breaker.py).

The image process pool starts lazily for restricted import environments.
CPython can deliver a broken-pool callback while holding the executor's shutdown
lock. The image service must release its own lifecycle lock before executor
calls, and the executor's manager retains responsibility for terminating and
joining its workers. Cancellation may race an already running callback. See
[image processing](../../app/utils/image_utils.py).

The typing transport keeps per-loop bot state and releases it at loop shutdown.
Emergency memory cleanup yields between collection passes to let other loop work
advance between synchronous collections.

Progress tasks cancel and drain on request exit so completion does not wait
out an animation interval. Detached search and reminder tasks hold strong
references through completion because the event loop takes weak references.

## Conversation history

Provider history may contain a synthetic summary exchange. Canonical history
stores real turns only, so summaries and the current request are accounted for
separately before trimming. Older summaries may predate the byte-aware cap.
The first trim reserves the full summary allowance. External summarization
starts only after a successful response so rollback cannot leave a summary of
turns still present in history. Wholesale history replacement forces a full
rewrite. Retrying a legacy interrupted answer may need to account for an absent
model turn, and rendered Telegram text is an unreliable equality check because
entities and formatting are normalized. Voice and search handlers leave
canonical persistence with the owner for each path so the current transcript
does not enter history twice. See [context assembly](../../app/context/assembler.py),
[chat](../../app/handlers/ai_chat.py) and [voice messages](../../app/handlers/msg_voice.py).

A synchronous local summary is already authoritative when the summarizer
checks the provider budget. Preserve it if provider synthesis would be
oversized or partial. Explicitly enforce the token-estimator boundary even
if the estimator changes. Document retrieval keeps the strongest result when
it needs truncation, so an unrelated short fragment cannot displace it. See
[summarization](../../app/context/summarizer.py) and [document context](../../app/documents/chunking.py).
The assembler retains its compatibility import of the summary scheduler.

Tarot reset installs a replacement so a running turn cannot mutate the new
session. A completed turn uses its previously captured generation directly
because the public setter represents a replacement boundary.
