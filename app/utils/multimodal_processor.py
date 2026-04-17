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
    "   INTENT:CONVERSATIONAL — if the speaker is addressing the bot: asking a question, giving a command, chatting, or asking to GENERATE/compose/write something. DO NOT use this for real-time/factual data questions.\n"
    "   INTENT:TRANSCRIPTION — ONLY if the speaker is using you as a dictaphone: dictating personal notes, a diary, or explicitly asking merely to 'transcribe' or 'record' text without a conversational reply.\n"
    "   INTENT:SEARCH — if the speaker asks to search the internet, look up current events, find factual info online, or asks about real-time data like current weather, news, or exchange rates in any city.\n"
    "   INTENT:DRAW — if the speaker asks to DRAW, GENERATE, or CREATE AN IMAGE/PICTURE/ART. (e.g., 'draw a cat', 'сгенерируй картинку леса', 'сделай такое же фото').\n"
    "7. If INTENT is DRAW, add an additional line RIGHT ABOVE the intent line: DRAW_PROMPT: <clean descriptive subject>\n"
    "   (Extract ONLY the visual subject, resolving context. E.g. 'I saw a forest. Draw the same' -> 'a forest.').\n"
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


# ── Intent classification model chain ───────────────────────────────────────
# Tried in order until one succeeds. gemini-3.1-flash-lite is first (cheapest)
# but is often offline; gemini-3-flash-preview is the reliable last-stand;
# opencode-go/big-pickle is used as final insurance (different infra pool).
_INTENT_MODEL_CHAIN = [
    "gemini-3.1-flash-lite-preview",
    "gemini-3-flash-preview",
    "opencode-go/big-pickle",
]


async def _classify_intent_with_fallback(
    prompt_parts: list[types.Part],
    api_key: str | None = None,
) -> str | None:
    """Run a text-only intent classification through a multi-provider fallback chain.

    Tries each model in ``_INTENT_MODEL_CHAIN`` in order.  For each Gemini model,
    uses ``_generate_with_resilience`` which itself does up to 3-key rotation.
    For Opencode models, resolves an Opencode key via ``_resolve_ai_request`` and
    posts a single-turn chat completion directly.

    Returns the first non-empty response, or None if all models fail.
    """
    from app.providers.base import get_provider_for_model, is_opencode_model

    for model in _INTENT_MODEL_CHAIN:
        try:
            if is_opencode_model(model):
                # ── Opencode path ─────────────────────────────────────────
                from app.handlers.ai_core import _resolve_ai_request

                key_data, model_used, _ = await _resolve_ai_request(model, use_openrouter=False)
                if not key_data:
                    logging.debug("Intent fallback: no key for %s, skipping", model)
                    continue

                # Build minimal OpenAI-style history from the single text part
                text = "".join(p.text for p in prompt_parts if hasattr(p, "text") and p.text)
                if not text:
                    continue

                provider = get_provider_for_model(model_used or model, key_data["api_key"])
                resp = await provider.get_response(
                    history=[{"role": "user", "parts": [text]}],
                    model_name=model_used or model,
                    max_retries=2,
                    timeout=30.0,
                )
                if resp.success and resp.text and resp.text.strip():
                    logging.debug("Intent classified via Opencode %s", model)
                    return resp.text.strip()
                logging.debug("Intent Opencode %s returned empty/error, trying next", model)

            else:
                # ── Gemini path (with key rotation) ──────────────────────
                result = await _generate_with_resilience(
                    parts=prompt_parts,
                    model=model,
                    system_prompt="",
                    thinking_config=None,  # Disabled: Intent extraction is trivial, avoid CoT overhead
                    api_key=api_key,
                )
                if result:
                    logging.debug("Intent classified via Gemini %s", model)
                    return result
                logging.debug("Intent Gemini %s returned empty, trying next model", model)

        except Exception as exc:
            logging.debug("Intent model %s failed: %s, trying next", model, exc)

    logging.warning("All intent classification models failed")
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
) -> tuple[str | None, str, str | None]:
    """Transcribe a voice message to text using Gemini.

    Args:
        audio_bytes: Raw audio file content (e.g. OGG Opus from Telegram).
        api_key: Explicit API key. If None, uses automatic key rotation.
        mime_type: MIME type of the audio. Telegram voices are 'audio/ogg'.
        model: Model to use. Defaults to gemini-3.1-flash-lite-preview.

    Returns:
        Tuple of (transcript_text, intent, draw_prompt).
        transcript_text is the clean transcript (with summary), or None on failure.
        intent is "conversational", "transcription", "search", or "draw".
        draw_prompt is the extracted visual subject if intent is "draw", else None.
    """
    if not audio_bytes:
        logging.warning("transcribe_voice called with empty audio_bytes")
        return None, "conversational", None

    from app.providers.pollinations import get_pollinations_provider

    pollinations_provider = get_pollinations_provider()

    # Try Pollinations Whisper first (no Gemini quota consumed)
    raw_text = await pollinations_provider.transcribe_audio(audio_bytes, model="whisper")

    if raw_text:
        # Whisper gives a clean transcript but no INTENT:/DRAW_PROMPT: tags.
        # Run a cheap text-only call through the multi-provider intent chain
        # (gemini-3.1-flash-lite → gemini-3-flash-preview → opencode-go/big-pickle).
        # This avoids re-uploading the audio while preserving DRAW/SEARCH routing.
        intent_prompt = (
            f"{_VOICE_SYSTEM_PROMPT}\n\n"
            f"[Pre-transcribed audio — do NOT re-transcribe. "
            f"Apply ONLY the INTENT and DRAW_PROMPT rules to this text:]\n{raw_text}"
        )
        tagged = await _classify_intent_with_fallback(
            prompt_parts=[types.Part.from_text(text=intent_prompt)],
            api_key=api_key,
        )
        if tagged:
            return _parse_voice_intent(tagged)
        # All intent models failed — return clean transcript with safe default
        logging.warning("All intent models failed after Whisper ASR, defaulting to conversational")
        return raw_text.strip(), "conversational", None

    # Whisper failed — fall back to full Gemini ASR (transcription + intent in one call)
    logging.info("Pollinations Whisper unavailable, falling back to Gemini ASR")
    audio_part = types.Part(
        inline_data=types.Blob(mime_type=mime_type, data=audio_bytes),
    )

    # Try Gemini ASR through the model chain (lite-preview → 3-flash-preview).
    # Opencode models cannot handle audio blobs so they are excluded here.
    # Deduplicate in case `model` arg is already gemini-3-flash-preview.
    _seen: set[str] = set()
    _GEMINI_ASR_MODELS = [m for m in [model, "gemini-3-flash-preview"] if not (m in _seen or _seen.add(m))]  # type: ignore[func-returns-value]
    raw_text = None
    for _model in _GEMINI_ASR_MODELS:
        raw_text = await _generate_with_resilience(
            parts=[audio_part],
            model=_model,
            system_prompt=_VOICE_SYSTEM_PROMPT,
            thinking_config=THINKING_CONFIG_HIGH,
            api_key=api_key,
        )
        if raw_text:
            break
        logging.info("Gemini ASR failed on %s, trying next model", _model)

    if raw_text is None:
        return None, "conversational", None

    return _parse_voice_intent(raw_text)


def _parse_voice_intent(raw_text: str) -> tuple[str, str, str | None]:
    """Parse INTENT: and DRAW_PROMPT: tags from the transcript's end.

    Returns (clean_transcript, intent_type, draw_prompt).
    """
    lines = raw_text.rstrip().split("\n")

    intent = "conversational"  # default
    draw_prompt = None

    # Check last line for INTENT: tag
    if lines and lines[-1].strip().upper().startswith("INTENT:"):
        tag = lines[-1].strip().split(":", 1)[1].strip().lower()
        if tag in ("conversational", "transcription", "search", "draw"):
            intent = tag
        lines.pop()

    # Strip empty lines between intent and draw_prompt just in case
    while lines and not lines[-1].strip():
        lines.pop()

    # If intent is draw, check for DRAW_PROMPT
    if intent == "draw" and lines and lines[-1].strip().upper().startswith("DRAW_PROMPT:"):
        draw_prompt = lines[-1].strip().split(":", 1)[1].strip()
        lines.pop()

    # Strip trailing empty lines left after removing tags
    while lines and not lines[-1].strip():
        lines.pop()

    return "\n".join(lines), intent, draw_prompt


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

    memory_id = await store_memory(
        user_id,
        extracted,
        res_key,
        source_type=media_type,
        metadata=metadata,
    )

    # ── Real-time graph extraction from media (background, non-blocking) ──
    if memory_id and extracted and len(extracted) >= 30:
        from app.utils.background_tasks import submit_task

        submit_task(
            _extract_graph_from_media(
                user_id=user_id,
                text=extracted,
                api_key=res_key,
                source_memory_id=memory_id,
                media_type=media_type,
                telegram_file_id=telegram_file_id,
            )
        )

    return memory_id


async def _extract_graph_from_media(
    user_id: int,
    text: str,
    api_key: str,
    source_memory_id: int | None,
    media_type: str,
    telegram_file_id: str | None,
) -> None:
    """Fire real-time graph extraction from media-derived text.

    After extraction, if a file_id is available, upsert it on all resulting
    memory_nodes so the bot can later re-send the original media.
    """
    try:
        from app.repos.memory_extraction import extract_and_store_graph

        edges = await extract_and_store_graph(
            user_id,
            text,
            api_key,
            source_memory_id=source_memory_id,
        )

        # Attach file_id to memory_nodes created from this media
        if edges > 0 and telegram_file_id and source_memory_id:
            from app.database import db_manager
            from app.repos.db_helpers import clear_user_context, set_user_context

            async with db_manager.pool.acquire() as conn:
                await set_user_context(user_id, False, conn=conn)
                try:
                    await conn.execute(
                        """
                        UPDATE memory_nodes
                        SET file_id = $1, file_type = $2
                        WHERE user_id = $3
                          AND updated_at >= now() - INTERVAL '30 seconds'
                          AND file_id IS NULL
                        """,
                        telegram_file_id,
                        media_type,
                        user_id,
                    )
                finally:
                    await clear_user_context(conn=conn)

        if edges > 0:
            logging.info(
                "Media graph extraction: %d edges from %s for user %d",
                edges,
                media_type,
                user_id,
            )
    except Exception as e:
        logging.debug("Media graph extraction skipped: %s", e)


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
