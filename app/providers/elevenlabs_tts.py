# /app/providers/elevenlabs_tts.py
"""ElevenLabs TTS provider — text-to-speech via REST API.

Uses the ElevenLabs v1 API to generate speech audio.
Returns raw PCM 24kHz 16-bit mono bytes — identical contract to providers/tts.py.

NO new package dependencies: uses the existing httpx AsyncClient from the venv.

Key design decisions:
  - Output format: pcm_24000 — byte-identical to Gemini TTS output so the
    shared ffmpeg transcoding pipeline (pcm_to_ogg_opus) needs zero changes.
  - Key rotation: simple sequential iteration over the ELEVENLABS_API_KEYS pool.
    On 401/422 (key invalid) or 429 (quota/rate) the caller tries the next key.
  - Text cleaning: strips Markdown/code/URLs but does NOT inject Director's Notes
    (ElevenLabs would read them aloud — they are plain text to the model).
  - Chunking: ElevenLabs v1 text-to-speech accepts up to 5000 bytes. We reuse
    the existing _chunk_text_by_sentences() from providers/tts.py with a 4500-byte
    limit to stay clear of the cap.
"""

import asyncio
import logging
from typing import Final

import httpx

# ─── Public exceptions ────────────────────────────────────────────────────────


class ElevenLabsQuotaError(Exception):
    """Raised when all ElevenLabs API keys are quota-exhausted (HTTP 429/401).

    The voice_engine's Atomic Router catches this to fall back to Gemini TTS.
    """


class ElevenLabsAPIError(Exception):
    """Raised for non-recoverable ElevenLabs errors (HTTP 4xx/5xx that are not quota)."""


# ─── Constants ───────────────────────────────────────────────────────────────

_BASE_URL: Final = "https://api.elevenlabs.io"
_TTS_ENDPOINT: Final = "/v1/text-to-speech/{voice_id}"

# PCM 24kHz — matches Gemini TTS output format exactly.
# This eliminates all audio-format concerns when concatenating chunks or
# when pcm_to_ogg_opus() processes the final buffer.
_OUTPUT_FORMAT: Final = "pcm_24000"

# Model: eleven_multilingual_v2 is the recommended high-quality model for
# non-English languages (Russian, Ukrainian, etc.).  Available on all tiers.
_DEFAULT_MODEL: Final = "eleven_multilingual_v2"

# Voice settings tuned for a warm assistant delivery (Siri/Alexa parity).
# stability=0.50      — golden mean: expressive yet never erratic on long RU text
# similarity_boost=0.80 — strong voice identity without amplifying EL artefacts
# style=0.25          — light stylisation (v2+ models only)
# use_speaker_boost=True — final clarity post-processing
_DEFAULT_VOICE_SETTINGS: Final = {
    "stability": 0.50,
    "similarity_boost": 0.80,
    "style": 0.25,
    "use_speaker_boost": True,
}

# HTTP timeouts: connect fast, allow up to 90 s for speech generation.
_HTTP_TIMEOUT: Final = httpx.Timeout(connect=10.0, read=90.0, write=10.0, pool=5.0)

# Max bytes per chunk for ElevenLabs (their limit is 5000 bytes; we leave
# 500 bytes of headroom for any overhead added by the JSON request envelope).
ELEVENLABS_CHUNK_MAX_BYTES: Final = 4500


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
        Raw PCM 24kHz 16-bit mono bytes, or None if the API returned no audio.

    Raises:
        ElevenLabsQuotaError: On HTTP 401 (key invalid / expired) or 429 (quota).
        ElevenLabsAPIError:   On non-retryable HTTP errors (4xx/5xx excl. quota).
        RuntimeError:         On network/timeout errors (caller should rotate key).
    """
    if not text or not text.strip():
        return None

    endpoint = _TTS_ENDPOINT.format(voice_id=voice_id)
    payload: dict[str, object] = {
        "text": text,
        "model_id": model_id,
        "voice_settings": _DEFAULT_VOICE_SETTINGS,
        "output_format": _OUTPUT_FORMAT,
        # Text normalisation: let EL convert dates / numbers to natural speech.
        # Our _clean_text_for_speech() already strips code blocks, so the risk of
        # misreading inline code is negligible.
        "apply_text_normalization": "on",
    }
    # Request Stitching: only include when non-None to avoid sending empty strings
    # that could confuse the model into producing extra breath/pause artefacts.
    if previous_text:
        payload["previous_text"] = previous_text
    if next_text:
        payload["next_text"] = next_text

    client = _get_client()
    try:
        response = await asyncio.wait_for(
            client.post(
                endpoint,
                json=payload,
                headers={"xi-api-key": api_key},
            ),
            timeout=timeout,
        )
    except TimeoutError:
        logging.warning("ElevenLabs TTS request timed out after %.0fs", timeout)
        raise

    if response.status_code in (401, 403):
        # Key is invalid, banned, or subscription lapsed.
        logging.warning(
            "ElevenLabs key rejected (HTTP %d): quota/auth failure",
            response.status_code,
        )
        raise ElevenLabsQuotaError(f"Key rejected: HTTP {response.status_code}")

    if response.status_code == 429:
        # Rate-limited or character quota exhausted.
        logging.warning("ElevenLabs rate/quota limit hit (HTTP 429)")
        raise ElevenLabsQuotaError("ElevenLabs quota or rate limit exceeded")

    if response.status_code != 200:
        # Non-recoverable server error — don't rotate key, just fail.
        body_preview = response.text[:300] if response.text else "<no body>"
        logging.error(
            "ElevenLabs unexpected HTTP %d: %s",
            response.status_code,
            body_preview,
        )
        raise ElevenLabsAPIError(f"HTTP {response.status_code}: {body_preview}")

    audio_bytes = response.content
    if not audio_bytes or len(audio_bytes) < 100:
        logging.warning(
            "ElevenLabs returned suspiciously small audio (%d bytes)",
            len(audio_bytes) if audio_bytes else 0,
        )
        return None

    logging.info(
        "ElevenLabs TTS: voice=%s, text_len=%d, pcm_bytes=%d",
        voice_id,
        len(text),
        len(audio_bytes),
    )
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
            pcm_parts.append(chunk_pcm)
            if on_chunk_complete is not None:
                await on_chunk_complete(pos + 1, len(non_empty))

    if not pcm_parts:
        return None

    return pcm_parts
