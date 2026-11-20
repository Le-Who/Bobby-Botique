# ADR 0010. Revision-owned admin and daily interfaces

- **Status** Accepted
- **Date** 2026-11-20, recorded from existing behavior

## Admin revisions

The browser transport deadline includes the bounded response body. A lost
reply or server failure can follow a committed mutation. Reconcile by the
existing operation ID or expected revision and content before exposing a retry.
A definite refusal establishes that admission did not create a batch. An
optional read cannot revoke confirmed admission, and delayed fetches cannot
replace a card while its write is pending. Edits typed during a save can follow
the revision acknowledged by their own write. A later unrelated fetch cannot
authorize those edits. Retry lineage is reconstructed from server IDs after
reload. Queue cards use allowed server metadata and never expose payloads.
Server-provided display values enter the DOM as text, and server errors expose
safe reason codes. See [admin transport](../../app/static/js/admin-api.js),
[controls](../../app/static/js/controls.js), [broadcast UI](../../app/static/js/daily-broadcast-jobs.js)
and [runtime controls](../runtime-controls.md#работа-с-панелью).

Dashboard reads invalidated by saves use one shared refresh to reconcile a
burst. Failed supervisor evidence survives cleanup. Complete required reads
and validation before publishing a new catalog so a single failure leaves the
last-confirmed snapshot coherent. A baseline marker written in the same CAS
as reset shadows historical catalogs across replicas. An empty process route
can intentionally shadow an older selection. Optional secrets forwarded as
empty values should leave the baseline usable. Prompt operations retain their
pinned override revision even when a template module loads late. Late modules
can supply their immutable baseline. Only original template text participates
in placeholder parsing, so inserted user braces remain data. See [dashboard
sources](../../app/dashboard_sources.py), [model controls](../../app/runtime_settings/models.py)
and [prompt registry](../../app/prompt_registry.py).

Lazy settings creation keeps imports available before configuration loads. A
reload preserves shared settings object identity for direct imports and hands
watchers a deep snapshot. Synchronous callers can use the environment fallback
until the stored setting has loaded. The short settings cache bounds read load
and keeps inter-worker propagation eventual. Controls batch model cache misses
while preserving explicit nulls. On load failure keep the last confirmed values
and keep configuration content out of error logs. Result allowlists and safe
status fields do not automatically expose additions to store records.

The image and Live entry points import their media prompt definitions for
registration before using those controls.

## Daily editing

Crocodile keeps per-slot process guards alongside distributed ownership.
Evicting a held lock could admit a second preparation of the same slot. The
worker checks its local ownership before adding background work. Automatic
image attempts spend one budget slot per puzzle, including failures, to bound
provider retries. Explicit admin runs bypass this budget and still need
preparation and provider admission. FreeTheAI image admission waits under its
own concurrency and rate bounds. See [Crocodile preparation](../../app/games/crocodile_daily.py)
and [daily operations](../pollinations-daily-croc.md#automatic-image-quota).

Committed text readiness can be observed before the optional art finishes.
Regeneration succeeds only when it produces a new asset. A failed Telegram
reference can be renewed using the original clue asset. Date-based category
rotation offsets Easy and Hard so players see a broader mix.

Daily job IDs can survive reload. Authored drafts and provider inputs stay in
tab memory. A conflict alias can identify a command for a different section,
so only a confirmed fresh admission owns a submitted draft. Unknown admissions
remain external updates until compared with the actual revision. A validated
Trivia fallback can be reused within one run without changing saved defaults.
Regular and super-game scores settle atomically. Admin and player subscriptions
retain independent recipients and preparation windows, and manual delivery
records suppress duplicate scheduler sends. See [daily job UI](../../app/static/js/daily-jobs.js),
[Trivia storage](../../app/repos/daily_trivia.py) and [daily subscriptions](../../app/handlers/daily_crocodile.py).

Trivia review links encode a date and regular or Super question index. Legacy
results remain unpinned after their migration. A completed player gets a
result view registered for later leaderboard refresh.

Daily 2048 conditionally inserts a revision-zero puzzle when there is no row to
lock. A following read recovers the winner and can recover an uncertain commit.
A future-date failure cannot block today's preparation. Local slides can
recover play, while ranked provenance requires the persisted source board.
After admission, a socket disconnect drains the handler through the cached
move reply. A held local owner retains its slot until exit. The client leaves
uncertain moves to the next server snapshot, reconciles only the server-selected
spawn on ordinary acknowledgment, and preserves active paths during recovery.
Tile identity follows the lane order. Scaling changes the face around its
center without scaling the cell translation. Reset/reconnect or a later merge
owns cancellation. See [Daily 2048](../../app/games/daily_2048.py), [storage](../../app/repos/daily_2048.py)
and [browser play](../../app/templates/daily_2048.html).

Simultaneous reconnects can both miss an optimistic lookup. The server claims
a move and its replayable response under the same lock.

## Game state

Crocodile uses a longer pre-play expiry so an unopened invitation remains
usable, then resets a shorter expiry during active play. Ephemeral hints can
be regenerated and stay outside Redis game state. If judging is unavailable,
the caller leaves the guess uncounted. Orthographic similarity cannot issue a
semantic score for a guess. Trivia uses similarity for shortlisting, keeping
changes in numbers or versions separate from typos. The judgment cache retains
the older topic-free key lookup for migration and keeps generated hints apart
from scores. File writes take a per-file thread lock because concurrent
`asyncio.to_thread` flushes can target the same temporary path. Redis pruning
bounds its per-call work and disallows inserting new fields while a legacy
hash is still oversized. See [game lifecycle](../../app/games/crocodile.py),
[judging](../../app/games/judge.py) and [cache](../../app/games/judgement_cache.py).

Crocodile persists the best score across reconnects and reads game state
through a known-field projection. Its Redis-less fallback is a bounded
process-local store. Puzzle art includes a negative prompt for unwanted
lettering. Word selection broadens Easy within its intended difficulty
range, and Redis word deduplication remains best effort. Persisted progress
uses safe fields, keeping arbitrary provider errors out of public status.

A game creator can reconnect to see its final state, while other actors are
closed out of completed play. A terminal socket response stops automatic
reconnect. Accepting a guess and refreshing Telegram presentation share
ownership so a late snapshot cannot overwrite a finished result. Completion
can persist without waiting indefinitely for a slow socket. See [Crocodile
browser](../../app/templates/crocodile.html), [Telegram results](../../app/games/crocodile_telegram.py)
and [sockets](../../app/web_miniapp.py).

The socket's broadcast drain runs independently of incoming user messages.
The web module also continues exposing the older local-lock registry hooks
to the classic game's Redis-less tests.

## Streamed admin limits

The Quart 0.23 body path cannot wait for EOF before enforcing the controls
request limit. The controls body adapter tracks bytes as they arrive and uses
Quart's public completion API to wake the request reader so it can return
413 immediately. Raising inside the input receiver would cancel ASGI's request
task before it could send that response. The real-server test sends an
oversized body without its final chunk. See [the adapter](../../app/web_controls.py)
and [streaming regression coverage](../../tests/test_controls_http_stream.py).
