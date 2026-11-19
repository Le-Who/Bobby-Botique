# /app/providers/elevenlabs_tts.py
"""ElevenLabs TTS provider — text-to-speech via REST API.

Uses the ElevenLabs Stream speech endpoint to generate speech audio.
Returns raw PCM 24kHz 16-bit mono bytes — identical contract to providers/tts.py.

NO new package dependencies: uses the existing httpx AsyncClient from the venv.

Key design decisions:
  - Output format: pcm_24000 — byte-identical to Gemini TTS output so the
    shared ffmpeg transcoding pipeline (pcm_to_ogg_opus) needs zero changes.
  - Key rotation: simple sequential iteration over the ELEVENLABS_API_KEYS pool.
    Try the next key if its workspace cannot access the voice/model or hits quota.
  - Text cleaning: strips Markdown/code/URLs but does NOT inject Director's Notes
    (ElevenLabs would read them aloud — they are plain text to the model).
  - Chunking: only when a transcript exceeds the selected model's character
    limit. Replies within that limit use one coherent generation.
"""

import asyncio
import logging
import re
from typing import Final

import httpx

from app.config import DEFAULT_ELEVENLABS_MODEL
from app.observability.workload_events import start_workload_attempt
from app.providers.tts_audio import (
    MAX_TTS_AUDIO_BYTES,
    TTSContractError,
    TTSOutputError,
    TTSRequestError,
    normalize_tts_audio,
)
from app.runtime_settings.audio import get_config
from app.runtime_settings.lifecycle import runtime_settings_scope

# ─── Public exceptions ────────────────────────────────────────────────────────


class ElevenLabsQuotaError(Exception):
    """This key lacks access, credit or capacity for the requested synthesis."""


class ElevenLabsAPIError(Exception):
    """Raised for non-recoverable ElevenLabs errors (HTTP 4xx/5xx that are not quota)."""


# ─── Constants ───────────────────────────────────────────────────────────────

_BASE_URL: Final = "https://api.elevenlabs.io"
_TTS_ENDPOINT: Final = "/v1/text-to-speech/{voice_id}/stream"

# PCM 24kHz — matches Gemini TTS output format exactly.
# This eliminates all audio-format concerns when concatenating chunks or
# when pcm_to_ogg_opus() processes the final buffer.
_OUTPUT_FORMAT: Final = "pcm_24000"
_KEY_FAILURE_REASONS: Final = {
    401: "auth_error",
    402: "payment_required",
    403: "auth_error",
    404: "resource_unavailable",
    429: "rate_limited",
}

# HTTP timeouts: connect fast, allow up to 90 s for speech generation.
_HTTP_TIMEOUT: Final = httpx.Timeout(connect=10.0, read=90.0, write=10.0, pool=5.0)

# Documented model limits count characters, not UTF-8 bytes.
_MODEL_CHARACTER_LIMITS: Final = {
    "eleven_multilingual_v2": 10000,
    "eleven_multilingual_v1": 10000,
    "eleven_monolingual_v1": 10000,
    "eleven_flash_v2_5": 40000,
    "eleven_turbo_v2_5": 40000,
    "eleven_flash_v2": 30000,
    "eleven_turbo_v2": 30000,
    "eleven_v3": 5000,
    "eleven_v4": 10000,
}
_STITCH_CONTEXT_CHARS: Final = 500


# ─── Shared async HTTP client ─────────────────────────────────────────────────

# A single module-level client is reused across all requests to benefit from
# connection pooling (Keep-Alive). The client is intentionally not closed
# between requests because voice replies fire in the background.
_http_client: httpx.AsyncClient | None = None


def _get_client() -> httpx.AsyncClient:
    global _http_client
    if _http_client is None or _http_client.is_closed:
        _http_client = httpx.AsyncClient(
            base_url=_BASE_URL,
            timeout=_HTTP_TIMEOUT,
            http2=True,  # ElevenLabs supports HTTP/2; reduces connection overhead
            headers={"Content-Type": "application/json", "Accept": "application/octet-stream"},
        )
    return _http_client


async def close_elevenlabs_client() -> None:
    """Release the provider-owned pool after voice jobs have stopped."""
    global _http_client
    client, _http_client = _http_client, None
    if client is not None:
        await client.aclose()


async def _read_bounded(response: httpx.Response, maximum: int) -> bytes:
    # Request identity encoding and reject a compressed response: decoding an
    # untrusted compressed chunk before checking size could allocate far more
    # than the audio limit. Identity chunks otherwise retain no full body.
    if response.headers.get("content-encoding", "identity").lower() != "identity":
        raise TTSOutputError("unsupported_content_encoding")
    length = response.headers.get("content-length")
    if length is not None:
        try:
            declared = int(length)
        except ValueError as error:
            raise TTSOutputError("invalid_content_length") from error
        if declared < 0:
            raise TTSOutputError("invalid_content_length")
        if declared > maximum:
            raise TTSOutputError("audio_too_large")
    chunks: list[bytes] = []
    size = 0
    async for chunk in response.aiter_bytes(chunk_size=16384):
        size += len(chunk)
        if size > maximum:
            raise TTSOutputError("audio_too_large")
        chunks.append(chunk)
    if length is not None and size != declared:
        raise TTSOutputError("truncated_http_body")
    return b"".join(chunks)


# ─── Text normalisation ───────────────────────────────────────────────────────


def _clean_text_for_elevenlabs(text: str) -> str:
    """Strip Markdown / bot formatting that ElevenLabs would read aloud literally.

    Unlike Gemini TTS, ElevenLabs does not accept a Director's Notes prefix —
    all text is treated as the transcript, so markup must be removed at source.

    Re-uses the same regex suite as providers/tts._clean_text_for_speech()
    by importing it lazily to avoid circular imports at module load time.
    """
    from app.providers.tts import _clean_text_for_speech

    return _clean_text_for_speech(text)


# ─── Core generation ─────────────────────────────────────────────────────────


@runtime_settings_scope()
async def generate_speech_elevenlabs(
    text: str,
    api_key: str,
    *,
    voice_id: str,
    model_id: str = DEFAULT_ELEVENLABS_MODEL,
    timeout: float = 90.0,
    previous_text: str | None = None,
    next_text: str | None = None,
) -> bytes | None:
    """Generate PCM 24kHz audio for a single text chunk via ElevenLabs REST API.

    Args:
        text:          Pre-cleaned text to synthesise (raw, no Director's Notes).
        api_key:       ElevenLabs API key (xi-api-key header).
        voice_id:      ElevenLabs voice ID. Callers pass settings.ELEVENLABS_VOICE_ID.
        model_id:      ElevenLabs model. Defaults to Eleven v4.
        timeout:       Maximum seconds to wait for the API response.
        previous_text: Preceding chunk text for Request Stitching — helps the model
                       carry forward the right prosodic context from the last sentence.
        next_text:     Following chunk text for Request Stitching — lets the model
                       shape the correct final intonation (falling vs rising) toward
                       the next sentence rather than treating the chunk as a complete
                       utterance.

    Returns:
        Validated PCM 24kHz 16-bit mono bytes, or None for empty input.

    Raises:
        ElevenLabsQuotaError: If another workspace key can retry access, payment or rate limits.
        ElevenLabsAPIError:   On unexpected HTTP/server errors.
        TTSContractError:     On an invalid request or incompatible/incomplete audio.
        TimeoutError:         On the absolute request deadline; transport errors retain their type.
    """
    if not text or not text.strip():
        return None

    if model_id == "eleven_v3":
        previous_text = next_text = None  # v3 has no request stitching.

    attempt = start_workload_attempt(
        workload="tts",
        provider="elevenlabs",
        model=model_id,
        api_key=api_key,
        origin="elevenlabs_tts",
        voice_id=voice_id,
        input_chars=len(text),
        has_previous_context=previous_text is not None,
        has_next_context=next_text is not None,
    )

    reason = "transport_error"
    status_code: int | None = None
    try:
        async with asyncio.timeout(timeout):
            audio_config = await get_config("elevenlabs")
            voice_settings = audio_config["voice_settings"]
            if model_id == "eleven_v4":
                voice_settings = {key: voice_settings[key] for key in ("stability", "similarity_boost")}
            elif model_id == "eleven_v3":
                # v3 exposes the Creative, Natural and Robust stability presets.
                stability = voice_settings["stability"]
                voice_settings = {"stability": min((0.5, 0.0, 1.0), key=lambda value: abs(value - stability))}
            payload: dict[str, object] = {
                "text": text,
                "model_id": model_id,
                "voice_settings": voice_settings,
                "apply_text_normalization": audio_config["apply_text_normalization"],
            }
            if previous_text:
                payload["previous_text"] = previous_text[-_STITCH_CONTEXT_CHARS:]
            if next_text:
                payload["next_text"] = next_text[:_STITCH_CONTEXT_CHARS]
            attempt.record_dispatch()
            async with _get_client().stream(
                "POST",
                _TTS_ENDPOINT.format(voice_id=voice_id),
                json=payload,
                params={"output_format": _OUTPUT_FORMAT},
                headers={"xi-api-key": api_key, "Accept-Encoding": "identity"},
            ) as response:
                status_code = response.status_code
                # No upstream error body is needed for classification. Do not
                # read it at all; the context closes the stream on every exit.
                key_failure = _KEY_FAILURE_REASONS.get(status_code)
                if key_failure:
                    reason = key_failure
                    raise ElevenLabsQuotaError(f"HTTP {status_code}")
                if 400 <= status_code < 500:
                    raise TTSRequestError("provider_request_rejected")
                if status_code != 200:
                    reason = "http_error"
                    raise ElevenLabsAPIError(f"HTTP {status_code}")
                audio_bytes = normalize_tts_audio(
                    await _read_bounded(response, MAX_TTS_AUDIO_BYTES),
                    response.headers.get("content-type"),
                    allow_octet_stream=True,
                    max_bytes=MAX_TTS_AUDIO_BYTES,
                )
    except asyncio.CancelledError:
        attempt.finish(outcome="cancelled", level="warning", reason_code="cancelled")
        raise
    except TimeoutError as exc:
        logging.warning("ElevenLabs TTS request timed out after %.0fs", timeout)
        attempt.fail(exc, reason_code="timeout")
        raise
    except TTSContractError as exc:
        attempt.fail(exc, reason_code=exc.reason_code, status_code=status_code)
        raise
    except Exception as exc:
        attempt.fail(exc, reason_code=reason, status_code=status_code)
        raise

    logging.info(
        "ElevenLabs TTS: voice=%s, text_len=%d, pcm_bytes=%d",
        voice_id,
        len(text),
        len(audio_bytes),
    )
    attempt.finish(outcome="succeeded", output_bytes=len(audio_bytes))
    return audio_bytes


@runtime_settings_scope()
async def generate_speech_with_key_rotation(
    text_chunks: list[str],
    api_keys: list[str],
    *,
    voice_id: str,
    model_id: str = DEFAULT_ELEVENLABS_MODEL,
    timeout: float = 90.0,
    on_chunk_complete=None,
) -> list[bytes] | None:
    from app.process_policies import resolve_process
    from app.runtime_settings.result_execution import run_result_process

    policy = await resolve_process("tts.elevenlabs", (model_id,))
    models = policy.models if policy.explicit else (model_id,)
    chunk_count = (
        max(len(split_transcript(text_chunks[0], model)) for model in models)
        if len(text_chunks) == 1
        else len(text_chunks)
    )

    async def execute(candidate: str, explicit: bool) -> list[bytes] | None:
        # Production passes a single whole transcript. Recalculate for every
        # selected model: its fallback can have a different hard input limit.
        chunks = split_transcript(text_chunks[0], candidate) if len(text_chunks) == 1 else text_chunks
        return await _generate_message_with_key_rotation(
            chunks,
            api_keys,
            voice_id=voice_id,
            model_id=candidate,
            timeout=timeout,
            on_chunk_complete=on_chunk_complete,
        )

    return await run_result_process(
        "tts.elevenlabs",
        model_id,
        execute,
        success=bool,
        timeout=timeout * max(1, chunk_count) * max(1, len(api_keys)),
        on_timeout=lambda: None,
    )


def split_transcript(text: str, model: str) -> list[str]:
    """Split only at a known model character limit, preserving all text."""
    limit = _MODEL_CHARACTER_LIMITS.get(model)
    remaining = text.strip()
    chunks = []
    while limit is not None and len(remaining) > limit:
        prefix = remaining[:limit]
        boundaries = list(re.finditer(r"[.!?…](?=\s)|\s+", prefix))
        end = boundaries[-1].end() if boundaries else limit
        chunks.append(remaining[:end].strip())
        remaining = remaining[end:].lstrip()
    if remaining:
        chunks.append(remaining)
    return chunks


async def _generate_message_with_key_rotation(
    text_chunks: list[str],
    api_keys: list[str],
    *,
    voice_id: str,
    model_id: str = DEFAULT_ELEVENLABS_MODEL,
    timeout: float = 90.0,
    on_chunk_complete=None,
) -> list[bytes] | None:
    """Generate the chunks in order with one voice and model.

    A key that cannot serve a chunk is skipped for the rest of this message.
    If generation fails, the caller can retry the entire transcript with its
    next configured model or provider.

    Args:
        text_chunks: Pre-cleaned, pre-split text chunks.
        api_keys:    Pool of ElevenLabs API keys.
        voice_id:    ElevenLabs voice ID.
        model_id:    ElevenLabs TTS model.
        timeout:     Per-request timeout in seconds.

    Returns:
        List of PCM byte blobs (one per chunk) on full success, or None if the
        ElevenLabs pipeline failed and Gemini fallback should be attempted.
    """
    if not api_keys:
        return None

    # Filter out blank chunks once so we don't have to skip them inside the loop.
    # We keep original indices to build correct Request Stitching neighbours.
    non_empty: list[tuple[int, str]] = [(i, c) for i, c in enumerate(text_chunks) if c.strip()]

    pcm_parts: list[bytes] = []
    audio_size = 0
    exhausted_keys: set[str] = set()

    for pos, (original_idx, chunk) in enumerate(non_empty):
        # ── Request Stitching: resolve neighbouring chunks ─────────────────
        # Use the original (pre-filter) index so that neighbouring lookups are
        # stable even when some intermediate chunks were blank.
        prev_raw_idx = original_idx - 1
        next_raw_idx = original_idx + 1
        previous_text: str | None = (
            text_chunks[prev_raw_idx] if prev_raw_idx >= 0 and text_chunks[prev_raw_idx].strip() else None
        )
        next_text: str | None = (
            text_chunks[next_raw_idx] if next_raw_idx < len(text_chunks) and text_chunks[next_raw_idx].strip() else None
        )

        chunk_pcm: bytes | None = None
        chunk_success = False

        # Try each available key in order.
        for api_key in api_keys:
            if api_key in exhausted_keys:
                continue

            try:
                chunk_pcm = await generate_speech_elevenlabs(
                    chunk,
                    api_key,
                    voice_id=voice_id,
                    model_id=model_id,
                    timeout=timeout,
                    previous_text=previous_text,
                    next_text=next_text,
                )
                if chunk_pcm:
                    chunk_success = True
                    break

            except TTSContractError as error:
                # Invalid payload/audio is independent of the selected key.
                # Advance the whole-message model/provider plan immediately.
                logging.warning("ElevenLabs message contract failed: %s", error.reason_code)
                return None

            except ElevenLabsQuotaError:
                logging.warning(
                    "ElevenLabs key unavailable (chunk %d/%d), rotating key",
                    pos + 1,
                    len(non_empty),
                )
                exhausted_keys.add(api_key)
                continue

            except Exception as e:
                # Network error, timeout, etc. — try next key.
                logging.warning(
                    "ElevenLabs chunk %d/%d failed (%s): %s",
                    pos + 1,
                    len(non_empty),
                    type(e).__name__,
                    e,
                )
                exhausted_keys.add(api_key)
                continue

        if not chunk_success:
            # All keys failed for this chunk — trigger Gemini fallback for the
            # entire message (atomic guarantee).
            logging.warning(
                "ElevenLabs pipeline failed on chunk %d/%d — all keys exhausted. "
                "Triggering Gemini TTS fallback for entire message.",
                pos + 1,
                len(non_empty),
            )
            return None  # Signal: fall back to Gemini

        if chunk_pcm:
            audio_size += len(chunk_pcm)
            if audio_size > MAX_TTS_AUDIO_BYTES:
                logging.warning("ElevenLabs message exceeded its aggregate audio bound")
                return None
            pcm_parts.append(chunk_pcm)
            if on_chunk_complete is not None:
                await on_chunk_complete(pos + 1, len(non_empty))

    if not pcm_parts:
        return None

    return pcm_parts
