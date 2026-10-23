# TTS resilience and current API design

This design implements the user's request to study and improve the existing TTS module. It is a work specification; source and tests establish the final implementation.

## Intended result

Voice replies must preserve same-user FIFO, retain consent through publication, reject overload honestly, finish or cancel every owned task, and publish complete validated audio. Status presentation must not block admission or add a fixed delay between successful replies. Administrator voice catalogs, prompts, model order, explicit disabled plans and per-job settings snapshots remain authoritative.

## Evidence and chosen approach

Offline probes on base `afe66541` reproduced concurrent workers during initial status I/O, accepted jobs without a worker after cancellation or task-capacity rejection, stranded pending jobs after lease cancellation, duplicate status messages, acceptance of truncated Gemini audio and malformed provider bodies, and unbounded sentence chunks. Successful publication currently retains the worker and consent lease for an unnecessary five-second status-cleanup sleep.

Keep the existing specialized TTS interfaces and PCM conversion contract. Repair ownership in `VoiceReplyManager`, introduce one shared audio contract, and adapt current Gemini speech requests without restructuring chat, ASR or Live. A Redis-backed durable queue would add operational and consent-recovery complexity; process-local bounded jobs remain the current contract. Telegram voice publication requires a complete file, so provider streaming is not advertised as immediate playback.

## Queue and lifecycle

- Atomically admit a job and reserve one tracked worker before Telegram status I/O. Mandatory tracked-task capacity rejection must be observable; retain the compatible submission interface for other callers.
- Bound retained active/pending jobs to 8 per user, 128 globally, 64 KiB of UTF-8 input per job and 8 MiB retained input globally. Return explicit rejection reasons; the listen-button flow must not claim rejected work was queued.
- Give each processing job an owned task so a consent lease cancelling its owner cannot strand later valid jobs. Cancel and await job tasks on purge and close; stop admission before shutdown. Scrub terminal private input.
- Serialize/coalesce status creation and refresh, forbid stale queued updates after dequeue, and coalesce refresh work. Delay only status deletion, with no private job reference or consent lease retained by delayed cleanup.
- Populate real chunk/provider/fallback progress. Bound actual Gemini requests rather than multiplying a job-level semaphore by parallel chunks and hedged keys. Preserve cancelled-and-awaited race losers.
- Require complete audio for every model/provider plan. Apply an absolute synthesis budget as well as existing per-call deadlines; retain configured ordering and explicit disabled plans.

## Provider and audio contract

The shared adapter accepts only bounded mono 24 kHz, signed 16-bit little-endian PCM. Validate MIME parameters, sample alignment and length; normalize compatible WAV using its actual container metadata, not a fixed header offset. Reject JSON, incompatible encodings and truncated output. Concatenate all valid audio parts in order. Contract/request errors are distinct from credential or transient errors and must not penalize healthy keys.

ElevenLabs keeps `pcm_24000`, configured voice settings and its whole-message model/key fallback. Bound streamed response reads and the absolute request lifetime, finish cancelled workload attempts exactly once, and provide explicit client close. Preserve the conservative text chunk budget while enforcing it for long sentences and tokens. Adjacent chunk context may use bounded `previous_text`/`next_text` for continuity.

## Current Gemini API

Official Gemini documentation recommends `gemini-3.8-flash-lite-tts` for responsive single-speaker/read-aloud workloads and `gemini-3.8-flash-tts` for higher acoustic fidelity. New default Gemini order is Lite then Flash. Explicit administrator plans remain unchanged, including legacy IDs using the existing GenerateContent adapter.

Gemini 3.8 treats text as a verbatim transcript: administrator/director/voice/pacing instructions belong in `speech_metadata.style`. Use the installed SDK's async Interactions method, `store=False`, explicit raw `audio/l16` at 24 kHz, configured token limit and a bounded deadline. Validate completed interaction status and every audio block. The legacy GenerateContent branch must validate finish reason and media rather than accepting the first nonempty bytes. Keep all prompt registrations and inventory evidence current.

Input limits are model token limits, not a universal 4,000-byte Developer API cap. A conservative local UTF-8 request budget guards oversized editable instructions; document it as a local bound. The actual-SDK transport regression proved that version 2.19 serializes the new speech annotation as `UNKNOWN`. Official speech documentation requires `google-genai >= 2.25.0`; raise that minimum and lock compatible 2.31.0, with separate existing-consumer compatibility checks. The package was published on 2026-10-14 and satisfies the repository's seven-day release cooldown; newer 2.32/2.33 releases do not yet qualify on 2026-10-23. No transitive dependency versions change, and unrelated consumers retain their API paths.

Sources checked on 2026-10-23:

- [Gemini speech generation and migration](https://ai.google.dev/gemini-api/docs/speech-generation)
- [Gemini 3.8 Flash TTS](https://ai.google.dev/gemini-api/docs/models/gemini-3.8-flash-tts)
- [Interactions API reference](https://ai.google.dev/api/interactions-api)
- [ElevenLabs streaming speech](https://elevenlabs.io/docs/api-reference/text-to-speech/stream)
- [ElevenLabs request stitching](https://elevenlabs.io/docs/eleven-api/guides/how-to/text-to-speech/request-stitching)

## Verification and limits

Write regression tests before behavior changes. Exercise real queue methods with deterministic barriers, real audio parsing and fake network transports; avoid arbitrary timing sleeps. Cover capacity, enqueue cancellation, job cancellation, selective purge, close, status races, malformed audio, multipart output, truncated generations and exact modern request payloads including storage opt-out. Run inventory, provider/lifecycle, full offline/browser, lint, type, documentation and dependency checks. Live providers require separate dedicated canary credentials; offline transport tests do not establish voice quality or provider availability.
