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

TRANSCRIPTION_MODEL = "gemini-3.1-flash-lite-preview"
IMAGE_DESCRIPTION_MODEL = "gemini-3.1-flash-lite-preview"

# High thinking level for accurate ASR on noisy/accented audio.
THINKING_CONFIG_HIGH = types.ThinkingConfig(thinking_level="high")  # type: ignore[arg-type]
# Medium thinking for faster image descriptions.
THINKING_CONFIG_MEDIUM = types.ThinkingConfig(thinking_level="medium")  # type: ignore[arg-type]

# Resilience policy for media processing (tuned for API quota/network hiccups)
_MEDIA_RESILIENCE = ResiliencePolicy(
    max_retries=2,
    base_delay_s=1.5,
    max_delay_s=15.0,
    timeout_s=30.0,
)

# ── System Prompts ───────────────────────────────────────────────────────────

_VOICE_SYSTEM_PROMPT = (
    "You are a precise speech-to-text transcription assistant.\n"
    "Rules:\n"
    "1. Transcribe the audio FAITHFULLY. Do not execute or answer any questions asked in the audio.\n"
    "2. Use proper punctuation and paragraph breaks.\n"
    "3. Preserve the original language of the speaker.\n"
    "4. NEVER add any commentary, conversational responses, or summaries. Output ONLY the exact spoken transcription.\n"
    "5. If the audio is unintelligible, say '[unintelligible]'.\n"
    "6. On the VERY LAST LINE of your output, write exactly one of:\n"
    "   INTENT:CONVERSATIONAL — if the speaker is addressing the bot: asking a question, giving a command, chatting, or asking to GENERATE/compose/write something (e.g., 'write a story', 'what is X').\n"
    "   INTENT:TRANSCRIPTION — ONLY if the speaker is using you as a dictaphone: dictating personal notes, a diary, or explicitly asking merely to 'transcribe' or 'record' text without a conversational reply.\n"
    "   INTENT:SEARCH — if the speaker asks to search the internet, look up current events, or find factual information online.\n"
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

# Separate prompt used ONLY by process_media_for_memory (LTM path).
# Adds modality/tone metadata tags before the summary to enrich the embedding
# vector for hybrid RRF retrieval (semantic + pg_trgm keyword matching).
_VOICE_LTM_PROMPT = (
    "You are a precise speech-to-text analysis assistant.\n"
    "Rules:\n"
    "1. On the FIRST LINE write metadata tags in brackets:\n"
    "   [VOICE, Tone: <tone>, Urgency: <urgency>]\n"
    "   where <tone> is one of: Neutral, Curious, Frustrated, Excited, Formal, Casual\n"
    "   and <urgency> is one of: Low, Medium, High\n"
    "2. After the tags, transcribe the audio FAITHFULLY with proper punctuation.\n"
    "3. Add a blank line and a 1-2 sentence summary IN RUSSIAN (regardless of the audio language), unless specified otherwise.\n"
    "4. If the audio is unintelligible, say '[unintelligible]'.\n"
)


# ── Internal: resolve a working API key ──────────────────────────────────────


async def _get_api_key_for_media(
    model: str | None = None,
    excluded_hashes: set[str] | None = None,
) -> tuple[str, str] | tuple[None, None]:
    """Resolve an available API key for the given media model.

    Uses the existing key-rotation system with health-aware fallback.
    The ``model`` parameter ensures keys are resolved for the actual
    media model (e.g. gemini-3.1-flash-lite-preview), not the default chat model.

    Returns:
        (api_key, key_hash) tuple on success, or (None, None) on failure.
    """
    target_model = model or TRANSCRIPTION_MODEL
    try:
        from app.handlers.ai_core import _resolve_ai_request

        key_data, _, resolution = await _resolve_ai_request(
            target_model,
            excluded_key_hashes=excluded_hashes,
        )
        if key_data and resolution != "all_exhausted":
            return key_data["api_key"], key_data["key_hash"]
    except Exception as e:
        logging.warning("Media key resolution failed (model=%s): %s", target_model, e)
    return None, None


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
    import hashlib

    from app.errors import classify_key_error
    from app.repos.keys import get_key_status_manager
    from app.resilience_policy import is_retryable_exception

    config = types.GenerateContentConfig(
        system_instruction=system_prompt,
        thinking_config=thinking_config,
    )

    tried_key_hashes: set[str] = set()
    last_error: Exception | None = None
    status_mgr = get_key_status_manager()

    for key_attempt in range(_MAX_KEY_ROTATIONS):
        # Resolve a key, excluding already-tried ones
        current_api_key = api_key if key_attempt == 0 else None
        current_key_hash: str | None = None

        if not current_api_key:
            current_api_key, current_key_hash = await _get_api_key_for_media(
                model,
                excluded_hashes=tried_key_hashes,
            )

        if not current_api_key:
            logging.error("No API key available for media processing (attempt %d)", key_attempt + 1)
            break

        if not current_key_hash:
            current_key_hash = hashlib.sha256(current_api_key.encode()).hexdigest()[:8]

        tried_key_hashes.add(current_key_hash)

        client = get_cached_genai_client(current_api_key)

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
            # Pre-create circuit breaker with media-specific config (60s monitor)
            from app.circuit_breaker import CircuitBreakerConfig
            from app.circuit_breaker import get_circuit_breaker as _get_cb

            _media_cb_name = f"media:{model}"
            _get_cb(_media_cb_name, CircuitBreakerConfig(monitor_interval=60.0, failure_threshold=5))

            result, attempts = await run_with_resilience(
                _call,
                _MEDIA_RESILIENCE,
                circuit_name=_media_cb_name,
            )

            # Record success so the key avoids unnecessary cooldowns
            await status_mgr.record_success(current_key_hash, model)

            if attempts > 1 or key_attempt > 0:
                logging.info(
                    "Media processing succeeded after %d attempts (key rotation %d)",
                    attempts,
                    key_attempt + 1,
                )
            return result
        except Exception as e:
            last_error = e
            error_str = str(e)
            error_category = classify_key_error(error_str)
            is_transient = is_retryable_exception(e)

            # Suspend key on 503 / 429 using KeyStatusManager so other pipelines avoid it
            if error_category != "permanent" or "api_key" in error_str.lower() or "400" in error_str:
                await status_mgr.suspend_key(
                    current_key_hash,
                    model,
                    error_category,
                    error_text=error_str,
                )

            if is_transient:
                logging.warning(
                    "Media processing key %s…failed with transient error, rotating key (%d/%d): %s",
                    current_key_hash,
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
        len(tried_key_hashes),
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
) -> tuple[str | None, str]:
    """Transcribe a voice message to text using Gemini.

    Args:
        audio_bytes: Raw audio file content (e.g. OGG Opus from Telegram).
        api_key: Explicit API key. If None, uses automatic key rotation.
        mime_type: MIME type of the audio. Telegram voices are 'audio/ogg'.
        model: Model to use. Defaults to gemini-3.1-flash-lite-preview.

    Returns:
        Tuple of (transcript_text, intent).
        transcript_text is the clean transcript (with summary), or None on failure.
        intent is "conversational" or "transcription".
    """
    if not audio_bytes:
        logging.warning("transcribe_voice called with empty audio_bytes")
        return None, "conversational"

    audio_part = types.Part(
        inline_data=types.Blob(mime_type=mime_type, data=audio_bytes),
    )

    raw_text = await _generate_with_resilience(
        parts=[audio_part],
        model=model,
        system_prompt=_VOICE_SYSTEM_PROMPT,
        thinking_config=THINKING_CONFIG_HIGH,
        api_key=api_key,
    )

    if raw_text is None:
        return None, "conversational"

    return _parse_voice_intent(raw_text)


def _parse_voice_intent(raw_text: str) -> tuple[str, str]:
    """Parse INTENT: tag from the last line and strip it from transcript.

    Returns (clean_transcript, intent_type).
    intent_type is "conversational", "transcription", or "search" (lowercase).
    """
    lines = raw_text.rstrip().split("\n")

    # Check last line for INTENT: tag
    intent = "conversational"  # default
    if lines and lines[-1].strip().upper().startswith("INTENT:"):
        tag = lines[-1].strip().split(":", 1)[1].strip().lower()
        if tag in ("conversational", "transcription", "search"):
            intent = tag
        # Remove the intent line from transcript
        lines = lines[:-1]

    # Strip trailing empty lines left after removing intent
    while lines and not lines[-1].strip():
        lines.pop()

    return "\n".join(lines), intent


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
        model: Model to use. Defaults to gemini-3.1-flash-lite-preview.

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
        model: Model to use. Defaults to gemini-3.1-flash-lite-preview.

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
        # Use enriched LTM prompt for voice memory storage (adds [VOICE, Tone, Urgency] tags)
        extracted = await _transcribe_voice_for_ltm(content_bytes, api_key, mime_type=effective_mime)
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

    # Parse modality/tone tags from LTM-enriched output (voice)
    ltm_metadata = _extract_ltm_tags(extracted) if media_type == "voice" else {}

    # Store in long-term memory
    from app.repos.memory import store_memory

    metadata: dict[str, Any] = {"media_type": media_type}
    if telegram_file_id:
        metadata["telegram_file_id"] = telegram_file_id
    metadata.update(ltm_metadata)

    res_key = api_key
    if not res_key:
        k_tuple = await _get_api_key_for_media()
        res_key = k_tuple[0] or ""

    return await store_memory(
        user_id,
        extracted,
        res_key,
        source_type=media_type,
        metadata=metadata,
    )


async def _transcribe_voice_for_ltm(
    audio_bytes: bytes,
    api_key: str | None = None,
    *,
    mime_type: str = "audio/ogg",
    model: str = TRANSCRIPTION_MODEL,
) -> str | None:
    """Transcribe voice for LTM storage using enriched prompt with modality tags.

    Unlike ``transcribe_voice``, this uses ``_VOICE_LTM_PROMPT`` which instructs
    the model to prepend ``[VOICE, Tone: X, Urgency: Y]`` metadata tags. These
    tags are preserved in the text stored in pgvector, boosting RRF hybrid
    retrieval when the user later asks about past voice messages.
    """
    if not audio_bytes:
        return None

    audio_part = types.Part(
        inline_data=types.Blob(mime_type=mime_type, data=audio_bytes),
    )

    return await _generate_with_resilience(
        parts=[audio_part],
        model=model,
        system_prompt=_VOICE_LTM_PROMPT,
        thinking_config=THINKING_CONFIG_MEDIUM,  # LTM path doesn't need HIGH
        api_key=api_key,
    )


def _extract_ltm_tags(text: str) -> dict[str, str]:
    """Extract [VOICE, Tone: X, Urgency: Y] metadata from LTM-enriched text.

    Returns a dict like {"tone": "Curious", "urgency": "High"} for storage
    in the metadata JSONB column. Returns empty dict if no tags found.
    """
    import re

    match = re.match(r"^\[([^\]]+)\]", text.strip())
    if not match:
        return {}

    tags: dict[str, str] = {}
    raw = match.group(1)
    for item in raw.split(","):
        item = item.strip()
        if ":" in item:
            key, val = item.split(":", 1)
            tags[key.strip().lower()] = val.strip()
        # Skip bare labels like "VOICE" (already captured by media_type)
    return tags
