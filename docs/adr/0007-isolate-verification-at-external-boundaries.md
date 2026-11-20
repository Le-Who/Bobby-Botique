# ADR 0007 — Isolate verification at external boundaries

**Status** Accepted

## Verify real behavior through isolated boundaries

The production image smoke imports and renders the application through its
locked dependency graph with offline inputs. Separate jobs exercise disposable
PostgreSQL and Redis. Browser probes run production scripts and templates in
Chromium with synthetic HTTP responses. The browser job selects by marker so
new tests in nested or parametrized modules receive coverage automatically.
The workflow parser uses PyYAML's `BaseLoader` for trigger keys because the
default YAML 1.1 resolver treats `on` as a boolean. Job contracts use the typed
loader to assert values such as permissions and boolean flags.

Service-backed tests exercise the real transaction, lock and constraint
contracts. Tests that change schema use a generated clone, distinct from
its source. The RLS tests create Supabase roles only when absent and remove
only roles they own. Repository queries that already filter by owner can
pass even when PostgreSQL fails to enforce the intended `USING` and
`WITH CHECK` rules. The RLS contracts issue raw SQL as a non-owner role
without `BYPASSRLS` to verify which rows it can read, update, delete, and
insert. They also check that changing a row's owner fails. [PostgreSQL RLS
contracts](../../tests/integration/contracts/test_rls_postgres.py).

Redis tests use UUID-scoped resources. A synthetic Lua
comment changes a script's SHA to exercise the real `NOSCRIPT` path without
clearing the server's shared script cache. PostgreSQL lock tests wait for
blocking evidence from the server. The document and conversation fixtures
account for PostgreSQL's transaction-scoped `CURRENT_TIMESTAMP` when asserting
wall-clock ordering or pagination.

The settings concurrency contract makes two connections read the same state
before either writes. It exercises both a missing row, where the insert's
conflict clause selects the winner, and an existing row, where the update
compares the original stored value. The cancellation test injects failure
after a real write and before commit to prove that PostgreSQL rolls back the
whole settings document. [PostgreSQL settings contracts](../../tests/integration/contracts/test_settings_postgres.py).

Offline tools set synthetic configuration before importing the application
and isolate transport and process creation. The dated coverage runner allows
Python's Windows socketpair fallback so the event loop still works. The intent
routing evaluator supplies host metadata on Windows to keep imports from
launching an OS command and isolates connecting through alternate asyncio
transports. It resolves only predefined synthetic cities. The natal readiness
CLI initializes a pool only after cheap checks pass and a storage or smoke
check is requested. The UTF-8 checker splits on LF because `str.splitlines()`
treats some forbidden controls as line terminators, losing the evidence.

Sources [verification guide](../../CONTRIBUTING.md),
[workflow contracts](../../tests/test_ci_workflow_config.py),
[intent evaluator](../../scripts/evaluate_intent_routing.py),
[UTF-8 checker](../../scripts/check_encoding.py) and
[Redis script contracts](../../tests/integration/test_queue_v2_contracts.py).

## Preserve the oracle and external contracts

Protocol and authorization fixtures construct their own inputs. This includes
the independent Telegram signer and the raw Latin-1 wire bytes needed to test
Quart's decoding of a non-ASCII header. Tests replace service boundaries while
keeping the routing, admission, quota, and delivery behavior they check. Actual
HTTP delivery is observed separately from JavaScript dispatch. Durable
scheduled-claim behavior is verified in PostgreSQL, while unit tests check
how the application consumes that boundary. Acknowledgment lost across a
remote claim or send remains uncertain. A new local owner cannot fabricate
external delivery. An unknown SQL commit causes state to reload from durable
storage before a caller proceeds.

Tests control time where the scheduler could obscure the oracle. Browser audio
resumes offline rendering only after automation has been scheduled. A linear
input ramp has an independent interpolation oracle at the output sample rate.
The Daily 2048 replay regression gates both clients at the initial cache read
before allowing either to race for the mutation lock. Both observe a miss.
The later read under the lock must distinguish a completed replay from a
duplicate mutation.
For animation checks, controllable browser timelines expose the slide's actual
start and midpoint. The natal wheel's SVG anchor centers advance width, while
natural glyph bearings can change the painted bounds by a fraction of a pixel.
Real timeout fixtures wait for SDK entry before advancing controlled clocks,
and allow normal OS timer and log-capture overhead. PTB startup verification
guards the argument set because v22 removed previously accepted polling options.

The Redis capacity tests use independent clock skew for the holder and
contender. They check both admission and the timestamp scored by Redis, since
an ahead host can reserve capacity beyond its lease and a behind host can lose
an active slot early. Queued document tests mock the storage boundary behind
the real lookup facade. That facade supports callers that treat a lookup
failure as a miss, while queued uploads must surface failure before storage.
The messaging test drives submitted coroutines explicitly so its observation
does not depend on unmocked background processing. The metrics snapshot test
stages a partial persistence failure, verifying that a user metrics retry
cannot re-add a committed global delta.

The compatibility form probe waits through a queued blur timer after validation
returns focus, then verifies that selection still works. A controls probe
replays a prior save acknowledgment after an explicit rebase. The earlier
response must not silently reinstate the old draft's revision. Page auth uses
a redirect, while the API returns a denial with the applicable security
headers. Test both because a redirect that works for a browser page can
misrepresent authentication failure to an API consumer.

Provider probes distinguish model access from credential rejection. A generic
mention of access, or a model name beside an invalid credential, cannot turn
that credential failure into a model-only error and stop key rotation.
Visible text fixes the selected lane. Later errors retain their category
without restarting generation or overwriting the lane's result. Tests supply
unique upstream body markers and check that they never reach emitted text or
diagnostics. They also cover losing lanes that fault during cleanup and verify
the owner does not interrupt cleanup with a second cancellation. Paid direct OpenAI routing
remains serial even if a general request leaves key racing enabled.

Sources [admin auth fixtures](../../tests/test_admin_auth_contracts.py),
[playback probe](../../tests/browser/live_audio_playback_probe.cjs),
[audio interpolation probe](../../tests/browser/audio_processor_probe.cjs),
[Daily 2048 action ownership tests](../../tests/test_daily_2048_action_ownership.py),
[game browser tests](../../tests/test_daily_2048_frontend.py) and
[webhook cancellation contracts](../../tests/e2e/test_webhook_admission_cancellation.py).
