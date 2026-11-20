# ADR 0009. Provider attempts and admission

- **Status** Accepted
- **Date** 2026-11-20, recorded from existing behavior

## Provider selection

Resolve known provider prefixes before the generic slash-based OpenRouter
rule. Old Imagen identifiers remain input aliases for persisted draw state
and callbacks. Image-only selections cannot analyze uploaded photos, so that
path uses a vision model. Mixed-provider search uses supplied evidence, with
native tools reserved for an all-Gemini plan or a supported direct OpenAI
plan. Direct provider selections use the typed transport and rotation code,
with their candidate set remaining confined to the selected route. A configured
plan owns its own preflight and fallback. History preferences only enable
direct OpenAI reasoning when an explicit admin policy or internal typed
request permits it. The active primary-provider setting comes from the
database and can fall back to the environment baseline when unavailable. See
[provider routing](../../app/providers/router.py), [provider dispatch](../../app/providers/base.py)
and [configuration](../../app/config.py).

An explicit Lyria selection routes to audio generation. Ask the QnA router
for a grounding-capable model independently of ordinary chat preferences.
AI Studio grounding stays on the configured Gemini 2.5 variant compatible
with the bot's key tier. Opencode's live fallback mapping follows model
reloads. OpenRouter owns its own fallback, so the local transient-model picker
returns no candidate for that route. The weather and fiat shortcuts have their
own provider fallback paths. Image generation honors the selected canonical
model or saved alias because substituting a different model can change price.
See [fast intent providers](../../app/intent_router.py) and [Pollinations](../../app/providers/pollinations.py).

The primary streaming pair can overlap one reserve model. The companion uses
the same model as the original key, and each reserve stays serial with other
reserves. Reserve preparation belongs to its own task so storage latency cannot
delay the original's first text or deadline. A provider failure and the
deadline that ended a race describe separate evidence. Preserve both when
reserve preparation timed out. Repeated model-level failures across keys
trigger advancing to the next eligible model. Caller cancellation and the
whole-plan deadline propagate, while owned attempt expiry can be recoverable.
See [stream routing](../../app/providers/router.py) and [Gemini execution](../../app/runtime_settings/gemini_execution.py).

Opencode can use different transports: qualified chat-completions models get
function definitions, and MiniMax uses `/messages`. Graded reasoning and
on-off thinking have separate mappings. Only supported Opencode vision models
take multimodal inputs through that route. Key selection and health evidence
follow the actual model attempted, because access to a different model is
insufficient evidence. The Gemini client cache keeps weak ownership for
shutdown so eviction does not close borrowed clients. Count dispatch at the
SDK boundary, including cancelled attempts that reached it. Parse provider
cooldown metadata before truncating diagnostic text. Lower legacy output
budgets reserve room for a classifier label even when tokens are spent on
reasoning. See [Opencode transports](../../app/providers/opencode.py), [Gemini
client](../../app/providers/gemini.py) and [Gemini config](../../app/providers/gemini_config.py).

Return a structured provider result when an error escapes retries. The
thinking classifier can choose fixed, model-specific defaults; an unrecognized
model family is insufficient evidence for an upgrade. Agentic synthesis builds
its own configuration so it cannot mutate the loop's tool-call settings. The
admin-only model catalog imports its SDK at use time. Its key-health probe
uses a bounded direct HTTP request to measure one attempt independent of SDK
retries. See [classifier](../../app/thinking_classifier.py) and [admin probes](../../app/handlers/cmd_admin.py).

In the locked GenAI SDK, Pydantic converts a validated string passed to
`ThinkingConfig.thinking_level` into `ThinkingLevel`. It also converts the
dictionaries passed to `GenerateContentConfig.safety_settings` into
`SafetySetting` objects. Keep the scoped type suppressions for these specific
calls while the SDK annotations require the constructed types. See
[thinking configuration](../../app/providers/base.py) and [Gemini safety settings](../../app/providers/gemini.py).

## Quotas

A provider request is the unit of quota admission. Gemini Interactions retries
are disabled for reserved TTS calls so one reservation corresponds to one
dispatch. Imagen maintains a separate daily budget from chat and audio even
though they share keys. Redis-backed quota uses the next UTC rollover for
Imagen. A configured backend failure leaves an uncertain reservation rather
than opening another budget. Process-local mode is reserved for an absent
backend. Redis server time keeps shared game allowance windows consistent
across workers, and an operation's reservation is idempotent. Foreground and
background spend from the same model window, with the lower background cap
reserving capacity for interactive work. Quota storage failure cannot count as
a key or provider failure. See [Imagen admission](../../app/providers/imagen_provider.py),
[game budgets](../../app/games/ai_budget.py) and [key quotas](../../app/repos/keys.py).

The pinned GenAI SDK normalizes attempt counts during construction. Set the
generated Interactions transport configuration explicitly to enforce one
dispatch. A pending TTS daily reservation keeps its minute admission until
commit starts the actual usage window. Redis expires an uncertain token. The
first local key-selection pressure is recorded before returning a key so
concurrent preparations can spread across the pool, while PostgreSQL retains
authority for the daily cap. Legacy role calls with a selected key own their
dispatch admission. Scheduled generation and Telegram album downloads have
separate concurrency limits. See [TTS quota](../../app/providers/tts_quota.py)
and [SDK deadline tests](../../tests/test_provider_real_sdk_deadlines.py).

Keep the minute reservation through bounded daily storage work, including an
uncertain write. Duplicate suspension requests are coalesced in an await-free
section. Gemini 429 without daily evidence temporarily suppresses the model/key
pair for a minute. For speech, only explicit daily evidence can invoke the
Pacific-day suspension. Structured HTTP status and known timeout type outrank
text heuristics. Legacy errors may still expose a safe numeric cooldown,
without carrying arbitrary response text. Opencode and FreeTheAI health is
process-local because their keys are outside the DB status trigger's key sets.
See [health and retries](../../app/resilience_policy.py), [error
classification](../../app/errors.py) and [provider health](../../app/providers/router.py).

Use explicit provider retry timing where available. A 45-second cooldown
gives overload time to clear. Truncated model output can trigger a bounded
retry with more output room; schema and output failures do not imply key
health failures. Judge retries skip keys already suspended by an attempt.

Game generation and Tarot prep bound background attempts and distinguish
transient pool exhaustion from a genuine quota/configuration failure. The
judge handles its own bounded retry. LTM repair also uses its own admission and
key-health owner under the original extraction deadline and configured model
chain. These boundaries let background work yield capacity without changing
the plan. Safety blocks fail directly because rotating a healthy key cannot
repair them. See [Tarot preparation](../../app/tarot_daily.py), [game
generation](../../app/games/daily_ai.py) and [memory repair](../../app/repos/memory_consolidation.py).

## Speech audio

The provider speech planner separates direct recitation from requests for a
voice reply. It reads authored command boundaries and preserves delimited
source text, including Ukrainian apostrophes. A voice input alone does not
request a voice response. The media memory pipeline stores file provenance
and the enriched transcript, so the voice handler avoids a duplicate text
entry. Voice transcription gets a bounded grace interval in debounce and
continues under its background owner if that interval expires. See [voice
intent](../../app/voice_intent.py), [voice callbacks](../../app/handlers/cb_voice.py)
and [debounce](../../app/middleware/debounce.py).

TTS follows a sequential plan with a shared deadline and a separate
semaphore acquisition for each dispatch. Long-source extension needs actual
input or output evidence that more pieces are required. Token counting uses
a small planning budget and a failed count cannot suspend a generation key.
Fresh key usage ordering shares load across the speech pool independently of
chat. Whitespace-only pieces produce no speech and are skipped. Ordered
text filters preserve provenance by applying only deletions. Legacy Gemini
speech uses notes and a recitation prompt. Gemini 3.8 receives the transcript
as text with concise metadata so style prose cannot intrude on the intended
voice. ElevenLabs reuses the client across detached requests, reads identity
encoding within the audio size limit, and retains pre-filter chunk indices
for stable stitching. See [speech execution](../../app/voice_engine.py),
[Gemini TTS](../../app/providers/tts.py) and [ElevenLabs](../../app/providers/elevenlabs_tts.py).

Both speech providers output mono, 24 kHz, signed little-endian PCM to make
concatenation and final encoding consistent. Recalculate input limits for each
fallback model. ElevenLabs abandons a failed partial result in favor of whole
message fallback and closes the stream without reading error bodies. Speech
pieces preserve boundary audio without overlap or silence trimming. Text
debounce honors both character and UTF-16 budgets because client limits may
use either representation.

Live input downsampling follows one continuous input timeline across render
quanta, carrying the fractional boundary to the next block. Playback treats
the tail fade as provisional, restores continuity for a late adjoining
packet, and publishes start resources only for the current owner. A stale
resume handle may belong to an older deployment and is removed for one fresh
retry. A late input transcript does not close the audio input gate. The
optional completion marker can be omitted on a completed transcript. See
[audio processor](../../app/static/js/audio-processor.js), [Live playback](../../app/templates/live_audio.html)
and [Live server](../../app/web_miniapp.py).

The browser registers the worklet as `mic-processor`. It sends 16 kHz mono
PCM16 `ArrayBuffer` chunks to the main thread for WebSocket transmission.

Keep the requested native Live voice first in its own catalog and leave the
administrator-managed TTS catalog independent. A Live lease outlasts setup,
the session deadline and bounded cleanup. Live input is ordered until output
is idle when interruption is disabled, because the provider may drop
simultaneous input. The input wait is bounded.

## Caches

Search result limits participate in cache identity so a smaller request
cannot satisfy a later larger one. QnA retains its existing identity. Join
concurrent identical searches before the provider call so a cold cache cannot
duplicate provider spend. Synchronous operations on the process-local search
cache do not suspend, so a second asyncio lock around a single operation
adds overhead without protecting cross-thread use. Agentic client reuse avoids
per-request connection setup and its cache has a bounded size. Model lookup
can traverse the current list directly when it holds no mutable reference
across an await. For memory retrieval, an SQL admission floor precedes
relative score filtering. The latter prunes outliers without reapplying a
second hard floor to accepted candidates. See [search caching](../../app/cache.py),
[search services](../../app/search_services.py) and [memory retrieval](../../app/repos/memory.py).

Clear only the keys owned by search because Redis also stores queues, locks
and Reader data. A key lookup excludes failed keys even when its cached choice
would otherwise be fresh. Compress album photos once per immutable Telegram
file identity, with a typed marker that distinguishes already-compressed input.
