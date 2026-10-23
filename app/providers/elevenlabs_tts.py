# /app/providers/elevenlabs_tts.py
"""ElevenLabs TTS provider — text-to-speech via REST API.

Uses the ElevenLabs v1 API to generate speech audio.
Returns raw PCM 24kHz 16-bit mono bytes — identical contract to providers/tts.py.

NO new package dependencies: uses the existing httpx AsyncClient from the venv.

Key design decisions:
  - Output format: pcm_24000 — byte-identical to Gemini TTS output so the
    shared ffmpeg transcoding pipeline (pcm_to_ogg_opus) needs zero changes.
  - Key rotation: simple sequential iteration over the ELEVENLABS_API_KEYS pool.
    On 401/403 (key invalid) or 429 (quota/rate) the caller tries the next key.
  - Text cleaning: strips Markdown/code/URLs but does NOT inject Director's Notes
    (ElevenLabs would read them aloud — they are plain text to the model).
  - Chunking: a conservative local 4500-byte UTF-8 budget. Provider character
    limits vary by model; this is not a universal ElevenLabs API limit.
"""

import asyncio
import logging
from typing import Final

import httpx

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
    """Raised when all ElevenLabs API keys are quota-exhausted (HTTP 429/401).

    The voice_engine's Atomic Router catches this to fall back to Gemini TTS.
    """


class ElevenLabsAPIError(Exception):
    """Raised for non-recoverable ElevenLabs errors (HTTP 4xx/5xx that are not quota)."""


# ─── Constants ───────────────────────────────────────────────────────────────

_BASE_URL: Final = "https://api.elevenlabs.io"
_TTS_ENDPOINT: Final = "/v1/text-to-speech/{voice_id}/stream"

# PCM 24kHz — matches Gemini TTS output format exactly.
# This eliminates all audio-format concerns when concatenating chunks or
# when pcm_to_ogg_opus() processes the final buffer.
_OUTPUT_FORMAT: Final = "pcm_24000"

# Model: eleven_multilingual_v2 is the recommended high-quality model for
# non-English languages (Russian, Ukrainian, etc.).  Available on all tiers.
_DEFAULT_MODEL: Final = "eleven_multilingual_v2"
_FLASH_MODEL: Final = "eleven_flash_v2_5"

# HTTP timeouts: connect fast, allow up to 90 s for speech generation.
_HTTP_TIMEOUT: Final = httpx.Timeout(connect=10.0, read=90.0, write=10.0, pool=5.0)

# Conservative local budget; model maximum_text_length_per_request is in characters.
ELEVENLABS_CHUNK_MAX_BYTES: Final = 4500
_STITCH_CONTEXT_CHARS: Final = 500
_VOICE_CATALOG_MAX_BYTES: Final = 1024 * 1024


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
    model_id: str = _DEFAULT_MODEL,
    timeout: float = 90.0,
    previous_text: str | None = None,
    next_text: str | None = None,
) -> bytes | None:
    """Generate PCM 24kHz audio for a single text chunk via ElevenLabs REST API.

    Args:
        text:          Pre-cleaned text to synthesise (raw, no Director's Notes).
        api_key:       ElevenLabs API key (xi-api-key header).
        voice_id:      ElevenLabs voice ID. Callers pass settings.ELEVENLABS_VOICE_ID.
        model_id:      ElevenLabs model. Defaults to eleven_multilingual_v2.
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
        ElevenLabsQuotaError: On HTTP 401 (key invalid / expired) or 429 (quota).
        ElevenLabsAPIError:   On unexpected HTTP/server errors.
        TTSContractError:     On an invalid request or incompatible/incomplete audio.
        TimeoutError:         On the absolute request deadline; transport errors retain their type.
    """
    if not text or not text.strip():
        return None

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
            payload: dict[str, object] = {
                "text": text,
                "model_id": model_id,
                "voice_settings": audio_config["voice_settings"],
                "apply_text_normalization": audio_config["apply_text_normalization"],
            }
            if previous_text:
                payload["previous_text"] = previous_text[-_STITCH_CONTEXT_CHARS:]
            if next_text:
                payload["next_text"] = next_text[:_STITCH_CONTEXT_CHARS]
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
                if status_code in (401, 403, 429):
                    reason = "rate_limited" if status_code == 429 else "auth_error"
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


async def generate_speech_with_key_rotation(
    text_chunks: list[str],
    api_keys: list[str],
    *,
    voice_id: str,
    model_id: str = _DEFAULT_MODEL,
    timeout: float = 90.0,
    on_chunk_complete=None,
) -> list[bytes] | None:
    from app.runtime_settings.result_execution import run_result_process

    async def execute(candidate: str, explicit: bool) -> list[bytes] | None:
        return await _generate_message_with_key_rotation(
            text_chunks,
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
        timeout=timeout * max(1, len(text_chunks)) * max(1, len(api_keys)),
        on_timeout=lambda: None,
    )


async def _generate_message_with_key_rotation(
    text_chunks: list[str],
    api_keys: list[str],
    *,
    voice_id: str,
    model_id: str = _DEFAULT_MODEL,
    timeout: float = 90.0,
    on_chunk_complete=None,
) -> list[bytes] | None:
    """Generate PCM audio for all text chunks using key rotation on quota errors.

    This is the Atomic Router's ElevenLabs pipeline:
      - Runs all chunks sequentially (free tier has rate-limit constraints).
      - If any chunk hits a quota error, the function immediately returns None
        so the caller can fall back to Gemini TTS for the ENTIRE message.
        This guarantees voice consistency — the user never hears two different
        TTS systems spliced together in one message.
      - Key rotation happens transparently within a single chunk attempt.

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
                    "ElevenLabs key quota exhausted (chunk %d/%d), rotating key",
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


async def fetch_voices(api_key: str, *, timeout: float = 10.0) -> list[dict]:
    """Fetch available voices from ElevenLabs GET /v1/voices.

    Returns list of {id, name, category, labels} dicts, or [] on error.
    """
    if not api_key:
        return []

    attempt = start_workload_attempt(
        workload="voice_catalog",
        provider="elevenlabs",
        model="voices",
        api_key=api_key,
        origin="elevenlabs_voice_catalog",
    )
    reason = "request_failed"
    try:
        async with asyncio.timeout(timeout):
            async with _get_client().stream(
                "GET",
                "/v1/voices",
                headers={"xi-api-key": api_key, "Accept": "application/json", "Accept-Encoding": "identity"},
            ) as response:
                status_code = response.status_code
                body = await _read_bounded(response, _VOICE_CATALOG_MAX_BYTES) if status_code == 200 else b""
        # Closing the response is part of the attempt; cancellation during
        # close must not produce a second terminal after an HTTP failure.
        if status_code != 200:
            attempt.finish(outcome="failed", level="warning", reason_code="http_error", status_code=status_code)
            return []
        reason = "invalid_response"
        from app.utils.json_compat import json

        data = json.loads(body)
        if not isinstance(data, dict):
            raise TypeError("ElevenLabs voices response must be an object")
        voices_list = data.get("voices", [])
        if not isinstance(voices_list, list):
            raise TypeError("ElevenLabs voices field must be a list")
    except asyncio.CancelledError:
        attempt.finish(outcome="cancelled", level="warning", reason_code="cancelled")
        raise
    except Exception as exc:
        logging.warning("Failed to fetch ElevenLabs voices (%s)", type(exc).__name__)
        attempt.fail(exc, reason_code=reason)
        return []

    result = []
    for v in voices_list:
        if not isinstance(v, dict):
            continue
        voice_id = v.get("voice_id")
        name = v.get("name")
        category = v.get("category")
        if not voice_id or not name:
            continue
        if category not in ("premade", "cloned"):
            continue
        result.append(
            {
                "id": voice_id,
                "name": name,
                "category": category,
                "labels": v.get("labels", {}),
            }
        )
    attempt.finish(outcome="succeeded", result_count=len(result))
    return result
