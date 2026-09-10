# Observability event catalog — schema v1

Event names and required correlation fields below are stable machine-facing
contracts. Optional fields may be added without changing `schema_version`.
Owners emit exactly one semantic terminal for controlled completion; a process
kill can leave a start without a terminal, which means `unknown`.

| Events | Owner | Required domain fields | Terminal outcomes / meaning | Privacy |
| --- | --- | --- | --- | --- |
| `logging.configured`, `logging.invalid_configuration`, `logging.configuration_conflict` | logging bootstrap | queue limits or invalid/conflicting parameter names | startup configuration evidence | metadata |
| `logging.loss_summary`, `logging.file_sink_unavailable`, `logging.sink_failed` | bounded writer/bootstrap | drop count/level/reason or sink/phase/error type | evidence is incomplete/degraded; partial fan-out failures are reported through a surviving sink | metadata |
| `diagnostic.enabled`, `diagnostic.expired` | diagnostic policy | incident ID, subsystem, selector presence, deadline | bounded preview policy activated or automatically expired | no content |
| `process.uncaught_exception`, `thread.uncaught_exception`, `asyncio.unhandled_exception` | lifecycle hooks | operation, error ID; thread/future type when known | unowned exception reached a runtime boundary | raw exception message suppressed |
| `telegram.update_started`, `telegram.update_finished` | update processor | request/update kind; outcome, duration on finish | `succeeded`, `failed`, `cancelled`, `rejected` | actor IDs allowed; content policy applies |
| `telegram.message_received`, `telegram.inline_query_received`, `telegram.handler_failed`, `telegram.handler_finished` | authenticated handlers/decorators | handler/update kind, admission/auth facts, duration/outcome | handler lifecycle below the update owner; not a second update terminal | preview only under matching diagnostic policy |
| `http.request_started`, `http.request_finished`, `http.request_failed` | web ingress | method, route, server request ID; status/outcome/duration | HTTP owner terminal and exception evidence | no raw headers/query/body |
| `provider.key_selected`, `provider.key_answered` | compatibility router | provider, model, `key_suffix`, `key_fingerprint` | selected/answered key evidence | credential suffix + fingerprint |
| `provider.attempt_started`, `provider.first_text`, `provider.call_failed`, `provider.attempt_failed`, `provider.attempt_finished` | typed chat router | attempt/provider/requested+actual model/key identity; outcome/duration | `succeeded`, `failed`, `deferred`, `cancelled`; caught adapter exceptions receive an error ID before typed conversion | no prompt/body; diagnostic scrubbed |
| `provider.http_error`, `provider.response_invalid`, `provider.key_rotation_failed`, `provider.model_capability_check` | provider adapters/router | provider/model/key identity/status or validation reason | bounded HTTP/shape/selection evidence used to explain a terminal | no response body |
| `api.request_started`, `api.request_finished`, `api.request_failed` | API compatibility facade | attempt/provider/endpoint/model/key identity; duration/outcome | one real SDK/HTTP attempt | endpoint is logical, not credential URL |
| `workload.attempt_started`, `workload.attempt_failed`, `workload.attempt_finished` | specialized adapters | attempt/workload/provider/model/key identity/origin | image, audio, TTS, STT, embeddings, memory and research direct calls | sizes/counts only; last-four key suffix required |
| `delivery.started`, `delivery.edit_failed`, `delivery.recovery_decision`, `delivery.error_notification_failed`, `delivery.transport_failed`, `delivery.finished` | response-delivery facade | delivery ID/kind; generation+delivery status, receipt IDs, recovery action, duration | `complete`, `partial`, `failed`, `deferred`, `cancelled` as mapped from immutable outcome | no publication URL |
| `job.enqueued`, `job.started`, `job.retry_scheduled`, `job.finished`, `job.cancelled`, `job.capacity_rejected` | persistent queue/background task manager | task/execution/operation; attempts, wait/duration, outcome | returned business failure differs from raised exception; exhaustion explicit | portable trace only, bounded metadata |
| `background_task.started`, `background_task.retry_scheduled`, `background_task.attempt_failed`, `background_task.error_callback_failed`, `background_task.rejected`, `background_task.finished` | in-process task manager | task/execution/operation/origin, attempt, wait/duration/outcome | detached task lifecycle, capacity and drain evidence | bounded metadata only |
| `database.operation_started`, `database.retry_scheduled`, `database.operation_failed`, `database.operation_finished` | database manager | operation ID/name, statement kind+fingerprint; pool/query/total duration | `succeeded` or `failed`; never SQL/params | metadata |
| `cache.lookup_finished`, `cache.write_finished`, `cache.backend_retry_scheduled`, `cache.backend_operation_failed`, `cache.backend_operation_finished` | cache adapter | namespace/layer/backend/outcome; bounded key fingerprint | hit/miss/fallback/error | no cache value/full key |
| `concurrency.acquire_finished`, `concurrency.released` | semaphore adapter | semaphore/mode/wait or hold duration/outcome | acquired/rejected and release evidence | metadata |
| `state.hydration_finished`, `state.hydration_failed`, `state.persistence_finished`, `state.persistence_failed`, `state.persistence_coalesced` | process state | actor ID, outcome/duration/debounce | DB hydration/write and coalescing | no state payload |
| `context.assembly_finished` | context assembler | turn counts, token budgets, truncation/summary strategy, audit hash | assembled provider context | no messages/system prompt |
| `memory.graph_write_started`, `memory.graph_write_failed`, `memory.graph_write_finished`, `memory.graph_write_committed` | graph writer + transaction owner | source/node/edge counts; `applied` versus `committed` | writer never claims commit; transaction owner does | no facts/vectors/predicates |
| `memory.consolidation_finished`, `memory.consolidation_failed`, `memory.extraction_retry_scheduled` | memory workflows | source/fact/node/edge counts, consent epoch/reason | committed/skipped/failed | no memory content |
| `research.iteration_started`, `research.tool_started`, `research.tool_input`, `research.tool_denied`, `research.tool_failed`, `research.tool_finished`, `research.page_truncated` | agentic research | iteration/tool-call/tool name/budget/count/reason | logical tool execution, not per-token noise | host and counts only; no query/page text |
| `research.url_deduplicated` | agentic research | safe host and URL fingerprint | duplicate URL suppressed | no full URL |
| `live.turn_started`, `live.turn_finished`, `live.session_disconnected` | Mini App Live boundary | session attempt/model/transport/key identity; turn sequence, duration, byte/character counters, close reason | turn/session success, empty, interrupted, failed or cancelled | no initData, transcript text or audio frames |
| `image.generation_finished`, `image.delivery_finished` | image handlers | provider/model/outcome, generated/delivered counts or reason | separates generation from Telegram delivery | no prompt/base64 |
| `configuration.setting_updated` | configuration owner | setting name and value type | configuration changed | never the value |
| `migration.run_started`, `migration.check_finished`, `migration.apply_started`, `migration.apply_failed`, `migration.apply_finished` | migration runner | mode/version/filename/duration/outcome | deploy schema evidence | no DSN, SQL or params |

All exception events carry `error_id` and a scrubbed structured `exception`.
Raw exception messages are suppressed; `message_length` and a bounded
`message_fingerprint` retain correlation while type, stack and cause chain retain
the actionable failure location.
Terminal events that recover from an upstream error use `upstream_error_id` or
the same `error_id`. Every record also carries source/release/environment and the
available request/trace/span/task/execution context from the common envelope.

Synthetic provider example:

```json
{"schema_version":1,"event":"provider.attempt_finished","request_id":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","attempt_id":"bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb","provider":"gemini","requested_model":"gemini-test","actual_model":"gemini-test","key_suffix":"1234","key_fingerprint":"d55e7c1d4f26d3d8","outcome":"failed","reason_code":"rate_limited","error_id":"cccccccccccccccccccccccccccccccc"}
```

Retention class for all events is restricted operational. Docker rotation is
bounded but not archival; a future collector must define and verify retention,
ACL, backup and deletion separately.
