# TTS resilience implementation plan

> **For agentic workers:** Use subagent-driven-development or executing-plans for the independently reviewable tasks below. Source and tests, rather than this plan, establish implemented behavior.

**Goal:** Make existing voice replies bounded, ordered, cancellation-safe, format-correct and compatible with current Gemini TTS.

**Architecture:** `VoiceReplyManager` owns admission, FIFO, consent and publication. Provider adapters return validated PCM through a shared audio contract; current Gemini requests and legacy requests share that contract. Delayed UI cleanup owns identifiers only.

**Tech Stack:** Python 3.14, locked uv 0.13.5, existing google-genai/httpx, Telegram, ffmpeg.

**Spec:** [TTS design](../specs/2026-10-23-tts-resilience-design.md)

## Global constraints

- Preserve explicit administrator order/disabled plans, all voice/prompt controls and per-job snapshots.
- Preserve consent capture and publication leases, selective purge and single publication ownership.
- Use no live credentials, application startup or database migrations for local verification.
- Upgrade only the Gemini SDK required by the speech schema; use `python -m uv` because the host's bare executable is stale.
- Shared PCM is mono 24 kHz signed 16-bit little-endian; cap individual provider audio at 32 MiB.

## Review focus

Cancellation before a coroutine first runs; lease-driven cancellation distinct from process shutdown; overlapping status creation; incompatible or truncated audio that is nonempty; explicit old/new model plans with edited prompts. Each owning task must test these cases.

### Task 1: Queue ownership and responsiveness

Files: `app/voice_engine.py`, `app/utils/background_tasks.py`, shutdown integration in `bot.py`, listen-button rejection handling, queue/lifecycle tests.

- [x] Reproduce concurrent admission, orphaned capture, rejected mandatory submission, lease cancellation and status races with deterministic tests.
- [x] Atomically reserve workers, return capacity reasons, bound retained input, isolate job cancellation and add explicit close.
- [x] Coalesce status work, detach identifier-only delayed deletion, expose chunk progress and bound actual Gemini requests.
- [x] Use the shared `TTSContractError` classification to avoid key penalties for invalid requests/output; require full message plans.
- [x] Run owned queue, consent, callback and shutdown tests and review the diff.

### Task 2: Shared audio and ElevenLabs

Files: new `app/providers/tts_audio.py`, `app/providers/elevenlabs_tts.py`, related audio/provider tests.

- [x] Add red tests for WAV/PCM validation, MIME/rate/alignment/size failures, malformed successful HTTP bodies, cancellation and absolute deadlines.
- [x] Implement `normalize_tts_audio(data, mime_type, *, allow_octet_stream=False, max_bytes=MAX_TTS_AUDIO_BYTES) -> bytes`, `TTSContractError`, `TTSOutputError` and `TTSRequestError` with bounded `reason_code` values.
- [x] Read ElevenLabs responses within fixed bounds and deadline; preserve error/key rotation and exactly one terminal event. Add client close and bounded neighbor-text context.
- [x] Run scoped tests and review the diff.

### Task 3: Gemini model/API adaptation and chunking

Files: `app/providers/tts.py`, process model defaults/inventory, Gemini contract tests, existing provider payload fixtures and current runtime documentation.

- [x] Add red tests for UTF-8 chunk bounds, completion/MIME validation, multipart audio, exact Interactions payload and explicit legacy plans.
- [x] Implement current-model Interactions requests with `store=False`, speech metadata and explicit PCM while preserving legacy GenerateContent support.
- [x] Centralize the Lite/Flash baseline model chain, preserve administrator overrides and forward configured prompts and limits.
- [x] Preserve typed credential/transient failures, close provider clients through the application lifecycle and update source evidence.
- [x] Run targeted provider, source inventory and snapshot tests.

### Task 4: Integration and review

- [x] Verify all three diffs together; update current operational documentation with implemented bounds and behavior.
- [x] Run full offline tests with required Chromium, Ruff, mypy, encoding/link/environment checks and exact production audit.
- [x] Request independent review of consent/cancellation, payloads, limits and publication; resolve concrete findings and verify affected tests again.
- [x] Prepare verified changes for the user's existing TTS commit/push scope. Delivery: commit and push the branch, then report exact validation and live-evidence limits.

## Verification record

- Final locked SDK: google-genai 2.31.0; no transitive version changes.
- Full offline suite with required Chromium: 5005 passed in 200.01 seconds.
- Ruff check and format check: passed; mypy: passed, 289 source files.
- Encoding, current documentation links, environment registry and diff whitespace: passed.
- Exact exported production dependency audit: no known vulnerabilities; 104 installed packages compatible.
- Independent review findings on repeated purge, blocking presentation, contract-key retries and aggregate-audio fallback were reproduced and resolved; follow-up review found no further actionable issues.
- No live provider generation, Telegram publication, production database access or deployment was used for this verification.
