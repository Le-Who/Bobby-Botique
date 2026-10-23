# Codebase audit — 2026-10-23

Audit baseline: `41edf8397999156cf003fdf2641d5bbb1cecaa7b`, branch `vps_testai`.
The user authorized investigation, implementation, commit and push. Three
`gpt-6.1-sol` agents used `xhigh` reasoning for independent runtime/provider,
data/product and web/operations work. The coordinator reviewed lifecycle,
concurrency, queues and caching and integrated the fixes.

This dated report records source inspection and local checks. It does not
establish that every possible defect has been found or that a live VPS,
Telegram account or provider works with these changes.

## Coverage and method

- Runtime/provider audit: typed streaming, fallback and terminal events,
  response delivery, research, context/document boundaries, media request
  ownership and speech integration.
- Data/product audit: repository JSON codecs, transactions and counters,
  consent/provenance/erasure, graph preparation, daily games/preparation,
  natal parsing/calculation/storage, horoscopes and Tarot.
- Web/operations audit: dashboard and Mini App guards, templates, sensitive
  logging, bounded writer output, dependency tooling, workflows and private
  collector configuration contracts.
- Coordinator review: circuit recovery ownership, process monitor lifecycle,
  queue retry capacity, local state/cache and concurrency boundaries.

Project-wide Ruff/format and Mypy gates covered their configured source scopes.
Focused source review followed the repository's ownership contracts; deeper
behavioral inspection targeted concurrency, external inputs and persistence.
This is broader than a single feature review, but it is not a claim of an
exhaustive manual line-by-line proof of every module. Suspected defects were
reproduced with failing tests before their fixes. Pure style suggestions and
non-security cache hashes were not treated as bugs.

## Confirmed corrections

| Area | Corrected behavior | Regression evidence |
| --- | --- | --- |
| Circuit breaker | One recovery probe; cancellation/local rejection releases its ownership; old calls cannot override a newer recovery or manual reset | `tests/test_circuit_breaker_ownership.py` |
| Process monitor | Startup, statistics and cleanup share one instance; blocking CPU sampling runs outside the event loop | `tests/test_memory_monitor_ownership.py`, `tests/test_web_security.py` |
| Queue overload | A rejected in-memory retry has a terminal failed outcome and capacity event, rather than an unreachable pending state or false retry event | `tests/test_queue_retry_capacity.py` |
| Provider streaming | Embedded compatible SSE errors become typed failures; explicit early closure drains delegated Opencode streams | `tests/test_audit_runtime_provider_streams.py` |
| Media request tasks | Draw/music heartbeats and concurrent translation/placeholder tasks are cancelled and awaited on failure; timed waits no longer leave shielded waiter tasks | `tests/test_audit_runtime_media_tasks.py` |
| Music captions | User prompt and generated text are escaped before constructing Markdown captions | `tests/test_audit_runtime_media_tasks.py` |
| Generated audio | Signed download URLs retain query signatures; MIME selection uses the URL path | `tests/test_audit_runtime_audio_urls.py` |
| Research text | Direct answers retain every visible text part and exclude thought summaries | `tests/test_audit_runtime_research_text.py` |
| Gemini shutdown | Still-referenced clients evicted from the LRU and Vertex singletons remain visible to explicit shutdown | `tests/test_audit_runtime_client_shutdown.py` |
| Tarot subscription | First INSERT applies subscription status, timezone and delivery hour as well as later updates | `tests/test_tarot_daily_subscriptions.py` |
| Boards | JSONB writes pass native objects through the configured codec once | `tests/test_board_jsonb.py` |
| Tarot chat | Long formatted replies use safe chunks and the final reply keyboard; failed delivery does not append an undelivered model turn | `tests/test_tarot_chat.py` |
| Groups | Registration starts with the real member count; membership changes commit atomically; repeated removal does not decrement other members | `tests/test_group_membership_atomicity.py` |
| Graph consolidation | Malformed model entity/relation fields are rejected before embedding coroutines are created | `tests/test_memory_consolidation_safety.py` |
| Admin web | Session configuration fails closed; untrusted display names are rendered as text | `tests/test_admin_auth_contracts.py`, `tests/test_admin_daily_browser_security.py` |
| Log privacy | Additional credential encodings are scrubbed consistently across the existing pipeline | `tests/observability/test_redaction.py` |
| Log correlation | Complete provider events retain correlation fields within the bounded event envelope | `tests/observability/test_pipeline.py` |
| Sanitizer allocation | Large input containers are sampled with bounded iteration instead of first copying every item | `tests/observability/test_redaction.py` |

The queue cancellation unit test now isolates the unrelated runtime-settings
store. Its previous one-second event deadline could expire during an initial
database-backed control refresh in a cold standalone run; the queue contract
itself retains its real worker, execution cancellation and next-job checks.

Current guides were corrected alongside code: browser instructions now run the
whole browser marker selection, and the architecture distinguishes CI's actual
synthetic collector-ingestion gate from health/auth probes and live VPS evidence.

Security changes are summarized by their resulting behavior. Private data,
credentials and exploitation instructions are excluded under [SECURITY.md](../SECURITY.md).

## Independent review

Agents reviewed changes outside their own implementation areas. That review
reproduced the image translation/placeholder ownership defect and caught missing
terminal statistics for obsolete circuit-breaker calls. Both received regression
coverage and corrections. A separate statistics epoch preserves manual-reset
semantics while state generations protect recovery ownership.

Review also rejected a false positive about raw HTML chunk length: Telegram's
[sendMessage contract](https://core.telegram.org/bots/api#sendmessage) limits text
after entity parsing, so appended closing tags alone do not establish a delivery
defect. No shared splitter redesign was made for that observation.

## Verification and limits

Initial configured Ruff, format and Mypy checks passed before implementation.
Each correction has focused red/green evidence. The final integrated offline
suite passed **5,328 tests in 257.82 seconds**, using four isolated pytest
workers and the repository's load-group scheduling:

```powershell
uv run --locked pytest tests/ --ignore=tests/integration -m "not integration" --override-ini="addopts=" --timeout=30 -n 4 --dist=loadgroup -q
```

The final lint/format checks passed (`786 files already formatted`); Mypy
reported no issues in 290 source files. UTF-8 checks passed for 97 Markdown
files, current documentation had zero link errors, and the environment registry
reported 111 names with zero changes. `git diff --check` passed.

The separate required-browser run also passed **124 tests in 275.05 seconds**
(`5,405 deselected`), with `GEMAIBOT_REQUIRE_BROWSER_TESTS=1` and
`pytest tests/ -m browser -n 0 --override-ini="addopts=" --timeout=30 -q`.

Focused agent runs included 479 runtime/provider cases, 50 combined directly
affected data/product cases, 72 web/logging cases and 163 dependency/workflow/
collector-contract cases. These overlap the final suite and are not additional
unique coverage counts.

An earlier full serial run had 5,258 passes and one Chromium subprocess timeout;
an intermediate wider logging run had 123 passes and two subprocess timeouts.
Their assertions did not fail. Subsequent focused runs and the final full run
passed without increasing timeout limits. The serial run began before all
regressions were added; it is not the final integration result.

No live application was started, migrations were not applied, and no private
environment or log archive was read. Production dependencies were exported
from the unchanged lock and checked with `pip-audit --no-deps --disable-pip`:
no known vulnerabilities were reported. This advisory result is time-bound
and is not a guarantee against undisclosed vulnerabilities.

Local checks do not establish live PostgreSQL/Redis, Telegram/provider access,
container-build success, collector ingestion or deployment readiness. Skipped
checks must remain distinct from passed checks.
