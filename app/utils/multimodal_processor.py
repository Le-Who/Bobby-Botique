# app/utils/multimodal_processor.py
"""Multimodal content processor — transcribes/describes media via Gemini models.

Handles voice messages, images, and document text with:
  - Retry logic via ``run_with_resilience``
  - API-key fallback via ``_resolve_ai_request`` (ProviderRouter)
  - Configurable thinking budget for quality control

Usage::

    from app.utils.multimodal_processor import transcribe_voice, describe_image

    text = await transcribe_voice(ogg_bytes, user_id=123)
    desc = await describe_image(jpeg_bytes, user_id=456, prompt="What's in this photo?")
"""

import logging
from typing import Any

from google.genai import types

from app.providers.gemini import get_cached_genai_client
from app.resilience_policy import ResiliencePolicy, run_with_resilience

# ── Constants ────────────────────────────────────────────────────────────────

TRANSCRIPTION_MODEL = "gemini-3.1-flash-lite"
IMAGE_DESCRIPTION_MODEL = "gemini-3.1-flash-lite"

# High thinking level for accurate ASR on noisy/accented audio.
THINKING_CONFIG_HIGH = types.ThinkingConfig(thinking_level="high")  # type: ignore[arg-type]
# Medium thinking for faster image descriptions.
THINKING_CONFIG_MEDIUM = types.ThinkingConfig(thinking_level="medium")  # type: ignore[arg-type]

# Resilience policy for media processing (tuned for API quota/network hiccups)
_MEDIA_RESILIENCE = ResiliencePolicy(
    max_retries=3,
    base_delay_s=1.5,
    max_delay_s=15.0,
    timeout_s=60.0,
)

# ── System Prompts ───────────────────────────────────────────────────────────

_VOICE_SYSTEM_PROMPT = (
    "You are a precise speech-to-text transcription assistant.\n"
    "Rules:\n"
    "1. Transcribe the audio FAITHFULLY. Do not add or omit words.\n"
    "2. Use proper punctuation and paragraph breaks.\n"
    "3. Preserve the original language of the speaker.\n"
    "4. After the transcript, add a blank line and a short summary (1–2 sentences) "
    "   prefixed with 'Summary: '.\n"
    "5. If the audio is unintelligible, say '[unintelligible]'.\n"
)

_IMAGE_SYSTEM_PROMPT = (
    "You are a precise visual analysis assistant.\n"
    "Describe the image in detail: objects, text, people, emotions, context.\n"
    "Preserve the language of any visible text. Be factual and concise.\n"
    "If the image contains a document or screenshot, extract the key content.\n"
)

_DOCUMENT_SUMMARY_PROMPT = (
    "You are a document analysis assistant.\n"
    "Summarize the following document text. Highlight key points, "
    "decisions, action items, and important data.\n"
    "Preserve the original language.\n"
)


# ── Internal: resolve a working API key ──────────────────────────────────────


async def _get_api_key_for_media(model: str | None = None) -> str | None:
    """Resolve an available API key for the given media model.

    Uses the existing key-rotation system with health-aware fallback.
    The ``model`` parameter ensures keys are resolved for the actual
    media model (e.g. gemini-3.1-flash-lite), not the default chat model.

    Returns None if no keys are available.
    """
    target_model = model or TRANSCRIPTION_MODEL
    try:
        from app.handlers.ai_core import _resolve_ai_request

        key_data, _, resolution = await _resolve_ai_request(target_model)
        if key_data and resolution != "all_exhausted":
            return key_data["api_key"]
    except Exception as e:
        logging.warning("Media key resolution failed (model=%s): %s", target_model, e)
    return None


# Max number of different API keys to try before giving up
_MAX_KEY_ROTATIONS = 3


async def _generate_with_resilience(
    *,
    parts: list[types.Part],
    model: str,
    system_prompt: str,
    thinking_config: types.ThinkingConfig,
    api_key: str | None = None,
) -> str | None:
    """Send content to Gemini with retry + key-rotation + circuit-breaker resilience.

    On 503/UNAVAILABLE errors, rotates to a different API key (up to 3 keys).
    Each key gets its own retry cycle via ``run_with_resilience``.

    Args:
        parts: Content parts (audio blob, image blob, text, etc.)
        model: Gemini model name.
        system_prompt: System instruction.
        thinking_config: ThinkingConfig for the model.
        api_key: Explicit API key. If None, resolved via key-rotation.

    Returns:
        Generated text, or None on total failure.
    """
    from app.resilience_policy import is_transient_error

    config = types.GenerateContentConfig(
        system_instruction=system_prompt,
        thinking_config=thinking_config,
    )

    tried_keys: set[str] = set()
    last_error: Exception | None = None

    for key_attempt in range(_MAX_KEY_ROTATIONS):
        # Resolve a key, excluding already-tried ones
        current_key = api_key if key_attempt == 0 else None
        if not current_key:
            current_key = await _get_api_key_for_media(model)

        if not current_key:
            logging.error("No API key available for media processing (attempt %d)", key_attempt + 1)
            break

        # Skip keys we already tried
        if current_key in tried_keys:
            # Try to get a different key
            current_key = await _get_api_key_for_media(model)
            if not current_key or current_key in tried_keys:
                break
        tried_keys.add(current_key)

        client = get_cached_genai_client(current_key)

        async def _call(_c=client) -> str | None:
            response = await _c.aio.models.generate_content(
                model=model,
                contents=parts,
                config=config,
            )
            if response and response.text:
                return response.text.strip()
            return None

        try:
            result, attempts = await run_with_resilience(
                _call,
                _MEDIA_RESILIENCE,
                circuit_name=f"media:{model}",
            )
            if attempts > 1 or key_attempt > 0:
                logging.info(
                    "Media processing succeeded after %d attempts (key rotation %d)",
                    attempts,
                    key_attempt + 1,
                )
            return result
        except Exception as e:
            last_error = e
            if is_transient_error(str(e)):
                logging.warning(
                    "Media processing key %s…failed with transient error, rotating key (%d/%d): %s",
                    current_key[:8],
                    key_attempt + 1,
                    _MAX_KEY_ROTATIONS,
                    e,
                )
                continue  # Try next key
            else:
                # Non-transient (invalid key, bad request) — don't rotate
                logging.error("Media processing failed with non-transient error (%s): %s", model, e)
                break

    # All keys exhausted
    logging.error(
        "Media processing failed after %d key rotations (%s): %s",
        len(tried_keys),
        model,
        last_error,
    )
    try:
        from app.metrics import metrics_collector

        await metrics_collector.record_error("media_processing_fail", str(last_error))
    except Exception:
        pass
    return None


# ═══════════════════════════════════════════════════════════════════════════
#  PUBLIC API
# ═══════════════════════════════════════════════════════════════════════════


async def transcribe_voice(
    audio_bytes: bytes,
    api_key: str | None = None,
    *,
    mime_type: str = "audio/ogg",
    model: str = TRANSCRIPTION_MODEL,
) -> str | None:
    """Transcribe a voice message to text using Gemini.

    Args:
        audio_bytes: Raw audio file content (e.g. OGG Opus from Telegram).
        api_key: Explicit API key. If None, uses automatic key rotation.
        mime_type: MIME type of the audio. Telegram voices are 'audio/ogg'.
        model: Model to use. Defaults to gemini-3.1-flash-lite.

    Returns:
        Transcript text (with summary appended), or None on failure.
    """
    if not audio_bytes:
        logging.warning("transcribe_voice called with empty audio_bytes")
        return None

    audio_part = types.Part(
        inline_data=types.Blob(mime_type=mime_type, data=audio_bytes),
    )

    return await _generate_with_resilience(
        parts=[audio_part],
        model=model,
        system_prompt=_VOICE_SYSTEM_PROMPT,
        thinking_config=THINKING_CONFIG_HIGH,
        api_key=api_key,
    )


async def describe_image(
    image_bytes: bytes,
    api_key: str | None = None,
    *,
    mime_type: str = "image/jpeg",
    prompt: str | None = None,
    model: str = IMAGE_DESCRIPTION_MODEL,
) -> str | None:
    """Describe an image using Gemini vision capabilities.

    Args:
        image_bytes: Raw image file content (JPEG, PNG, WebP, etc.).
        api_key: Explicit API key. If None, uses automatic key rotation.
        mime_type: MIME type of the image.
        prompt: Optional user prompt to guide the description.
        model: Model to use. Defaults to gemini-3.1-flash-lite.

    Returns:
        Image description text, or None on failure.
    """
    if not image_bytes:
        logging.warning("describe_image called with empty image_bytes")
        return None

    parts: list[types.Part] = [
        types.Part(inline_data=types.Blob(mime_type=mime_type, data=image_bytes)),
    ]
    if prompt:
        parts.append(types.Part.from_text(text=prompt))

    return await _generate_with_resilience(
        parts=parts,
        model=model,
        system_prompt=_IMAGE_SYSTEM_PROMPT,
        thinking_config=THINKING_CONFIG_MEDIUM,
        api_key=api_key,
    )


async def summarize_document_text(
    text: str,
    api_key: str | None = None,
    *,
    model: str = TRANSCRIPTION_MODEL,
) -> str | None:
    """Summarize extracted document text (from PDF, DOCX, etc.) using Gemini.

    This function takes already-extracted text (from DocumentProcessor) and
    produces a concise summary suitable for long-term memory storage.

    Args:
        text: Pre-extracted document text content.
        api_key: Explicit API key. If None, uses automatic key rotation.
        model: Model to use. Defaults to gemini-3.1-flash-lite.

    Returns:
        Summary text, or None on failure.
    """
    if not text or len(text.strip()) < 50:
        logging.warning("summarize_document_text called with insufficient text")
        return None

    # Truncate to avoid exceeding model context
    truncated = text[:30_000]

    text_part = types.Part.from_text(text=truncated)

    return await _generate_with_resilience(
        parts=[text_part],
        model=model,
        system_prompt=_DOCUMENT_SUMMARY_PROMPT,
        thinking_config=THINKING_CONFIG_MEDIUM,
        api_key=api_key,
    )


# ── Convenience: full pipeline into long-term memory ─────────────────────────


async def process_media_for_memory(
    content_bytes: bytes,
    user_id: int,
    media_type: str,
    api_key: str | None = None,
    *,
    mime_type: str | None = None,
    telegram_file_id: str | None = None,
    extra_prompt: str | None = None,
) -> int | None:
    """Full pipeline: process media → extract text → store as long-term memory.

    Args:
        content_bytes: Raw media file content.
        user_id: Owner of the memory.
        media_type: One of "voice", "image", "document_text".
        api_key: Explicit API key. If None, uses automatic key rotation.
        mime_type: MIME type override.
        telegram_file_id: Optional Telegram file ID for future retrieval.
        extra_prompt: Optional user prompt (for images).

    Returns:
        Memory ID on success, None on failure.
    """
    # Determine MIME type defaults
    mime_defaults = {
        "voice": "audio/ogg",
        "image": "image/jpeg",
    }
    effective_mime = mime_type or mime_defaults.get(media_type, "application/octet-stream")

    # Route to appropriate processor
    if media_type == "voice":
        extracted = await transcribe_voice(content_bytes, api_key, mime_type=effective_mime)
    elif media_type == "image":
        extracted = await describe_image(content_bytes, api_key, mime_type=effective_mime, prompt=extra_prompt)
    elif media_type == "document_text":
        # For documents, content_bytes is already extracted text (UTF-8)
        extracted = await summarize_document_text(content_bytes.decode("utf-8", errors="replace"), api_key)
    else:
        logging.error("Unknown media_type: %s", media_type)
        return None

    if not extracted:
        logging.warning("Media processing returned no text for %s", media_type)
        return None

    # Store in long-term memory
    from app.repos.memory import store_memory

    metadata: dict[str, Any] = {"media_type": media_type}
    if telegram_file_id:
        metadata["telegram_file_id"] = telegram_file_id

    return await store_memory(
        user_id,
        extracted,
        api_key or (await _get_api_key_for_media() or ""),
        source_type=media_type,
        metadata=metadata,
    )
