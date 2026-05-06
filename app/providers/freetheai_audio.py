"""FreeTheAI audio generation provider — Lyria models.

Uses ``https://api.freetheai.xyz/v1/chat/completions`` with Lyria model slugs.

Supported models:
    or/google/lyria-3-pro-preview   — full song generation (~3 min)
    or/google/lyria-3-clip-preview  — short clip (~30s)

The FreeTheAI API wraps Google's Lyria music generation behind the standard
OpenAI-compatible chat completions endpoint.  The response may contain:
- Audio data as a URL in ``choices[0].message.content``
- Audio data as base64 inline
- A mix of text + audio link

We parse all variants and return raw audio bytes ready for Telegram ``send_audio()``.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import logging
import re
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import httpx

from app.config import get_freetheai_keys

logger = logging.getLogger(__name__)

# Lyria models exposed through FreeTheAI
LYRIA_MODELS: frozenset[str] = frozenset({
    "or/google/lyria-3-pro-preview",
    "or/google/lyria-3-clip-preview",
})

LYRIA_MODEL_LABELS: dict[str, str] = {
    "or/google/lyria-3-pro-preview": "🎵 Lyria Pro",
    "or/google/lyria-3-clip-preview": "🎶 Lyria Clip",
}

LYRIA_DEFAULT: str = "or/google/lyria-3-clip-preview"

_FTA_CHAT_URL = "https://api.freetheai.xyz/v1/chat/completions"
_AUDIO_TIMEOUT = 300.0  # music generation can be very slow

# In-memory key health
_lyria_key_health: dict[str, datetime] = {}
_LYRIA_COOLDOWN = timedelta(seconds=60)

# Pattern to detect audio URLs in the response text
_AUDIO_URL_RE = re.compile(
    r'https?://[^\s"\'<>]+\.(?:mp3|wav|ogg|m4a|flac|webm|opus)',
    re.IGNORECASE,
)
# Pattern to detect base64 audio data
_B64_AUDIO_RE = re.compile(
    r'data:audio/[a-z0-9]+;base64,([A-Za-z0-9+/=]+)',
    re.IGNORECASE,
)


def is_lyria_model(model_name: str) -> bool:
    """Check if a model is a Lyria audio generation model."""
    return model_name in LYRIA_MODELS


@dataclass
class AudioGenResult:
    """Result of a FreeTheAI audio generation call."""

    success: bool
    audio_bytes: bytes = b""
    mime_type: str = "audio/mpeg"  # default to mp3
    text_content: str = ""  # any text (lyrics etc.) returned alongside audio
    error_message: str = ""
    model_used: str = ""
    duration_s: float = 0.0


def _suspend_lyria_key(key_hash: str, cooldown: timedelta | None = None) -> None:
    # With a single key, suspending it means total unavailability — skip.
    if len(get_freetheai_keys()) <= 1:
        logger.info("Lyria: single-key mode, skipping suspension for %s…", key_hash[:8])
        return
    _lyria_key_health[key_hash] = datetime.now(UTC) + (cooldown or _LYRIA_COOLDOWN)
    logger.warning(
        "Lyria key %s… suspended for %.0fs",
        key_hash[:8],
        (cooldown or _LYRIA_COOLDOWN).total_seconds(),
    )


def _pick_key() -> tuple[str, str] | None:
    """Pick a healthy FreeTheAI key. Returns (api_key, key_hash) or None."""
    keys = get_freetheai_keys()
    if not keys:
        return None
    now = datetime.now(UTC)
    for key in keys:
        kh = hashlib.sha256(key.encode()).hexdigest()[:16]
        suspended_until = _lyria_key_health.get(kh)
        if suspended_until and now < suspended_until:
            continue
        return key, kh
    # All suspended — clean expired
    expired = [h for h, until in list(_lyria_key_health.items()) if now >= until]
    for h in expired:
        del _lyria_key_health[h]
    return None


def _extract_audio_from_response(content: str) -> tuple[bytes | None, str, str]:
    """Parse the response content to extract audio data.

    Returns:
        (audio_bytes, mime_type, remaining_text)
    """
    # 1. Check for inline base64 audio
    b64_match = _B64_AUDIO_RE.search(content)
    if b64_match:
        try:
            audio_data = base64.b64decode(b64_match.group(1))
            # Determine mime type from the data URI
            full_match = b64_match.group(0)
            if "audio/wav" in full_match:
                mime = "audio/wav"
            elif "audio/ogg" in full_match or "audio/opus" in full_match:
                mime = "audio/ogg"
            else:
                mime = "audio/mpeg"
            remaining = content[:b64_match.start()] + content[b64_match.end():]
            return audio_data, mime, remaining.strip()
        except Exception:
            pass

    # 2. No inline data found — return None so caller can check for URL
    return None, "audio/mpeg", content.strip()


class FreeTheAIAudioProvider:
    """Music generation via FreeTheAI using Lyria models."""

    async def generate(
        self,
        prompt: str,
        model: str = LYRIA_DEFAULT,
    ) -> AudioGenResult:
        """Generate music from a text prompt.

        Args:
            prompt: Text description of the desired music.
            model: One of LYRIA_MODELS.

        Returns:
            AudioGenResult with raw audio bytes on success.
        """
        if model not in LYRIA_MODELS:
            model = LYRIA_DEFAULT

        key_pair = _pick_key()
        if key_pair is None:
            logger.warning("Lyria: no FreeTheAI keys available")
            return AudioGenResult(success=False, error_message="no_keys")

        api_key, key_hash = key_pair
        key_suffix = api_key[-4:] if len(api_key) >= 4 else "????"

        payload = {
            "model": model,
            "messages": [
                {"role": "user", "content": prompt},
            ],
            "stream": False,
        }

        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }

        logger.info(
            "Lyria: model=%s key=…%s prompt_len=%d",
            model,
            key_suffix,
            len(prompt),
        )

        t0 = time.monotonic()
        try:
            async with httpx.AsyncClient(timeout=_AUDIO_TIMEOUT) as client:
                response = await client.post(_FTA_CHAT_URL, json=payload, headers=headers)

            elapsed = time.monotonic() - t0

            if response.status_code != 200:
                body = response.text[:500]
                logger.error(
                    "Lyria: HTTP %d after %.1fs (model=%s key=…%s): %s",
                    response.status_code,
                    elapsed,
                    model,
                    key_suffix,
                    body,
                )
                if response.status_code == 429:
                    _suspend_lyria_key(key_hash, timedelta(seconds=120))
                    return AudioGenResult(success=False, error_message="rate_limited", model_used=model)
                if response.status_code in (401, 403):
                    _suspend_lyria_key(key_hash, timedelta(minutes=30))
                    return AudioGenResult(success=False, error_message="auth_error", model_used=model)
                return AudioGenResult(success=False, error_message=f"http_{response.status_code}", model_used=model)

            data = response.json()

            # Extract content from OpenAI-compatible response
            choices = data.get("choices", [])
            if not choices:
                logger.warning("Lyria: empty choices in response")
                return AudioGenResult(success=False, error_message="empty_response", model_used=model)

            message = choices[0].get("message", {})
            content = message.get("content", "")

            if not content:
                logger.warning("Lyria: empty content in response")
                return AudioGenResult(success=False, error_message="empty_content", model_used=model)

            # Try to extract inline audio data
            audio_data, mime_type, remaining_text = _extract_audio_from_response(content)

            if audio_data:
                logger.info(
                    "Lyria: success (inline) — %.1fKB in %.1fs (model=%s key=…%s)",
                    len(audio_data) / 1024,
                    elapsed,
                    model,
                    key_suffix,
                )
                return AudioGenResult(
                    success=True,
                    audio_bytes=audio_data,
                    mime_type=mime_type,
                    text_content=remaining_text,
                    model_used=model,
                    duration_s=elapsed,
                )

            # Try to find and download audio URL from the text
            url_match = _AUDIO_URL_RE.search(content)
            if url_match:
                audio_url = url_match.group(0)
                remaining = content[:url_match.start()] + content[url_match.end():]
                try:
                    async with httpx.AsyncClient(timeout=120) as dl_client:
                        dl_resp = await dl_client.get(audio_url)
                        dl_resp.raise_for_status()
                        audio_data = dl_resp.content

                    # Detect mime type from URL
                    url_lower = audio_url.lower()
                    if url_lower.endswith(".wav"):
                        mime_type = "audio/wav"
                    elif url_lower.endswith((".ogg", ".opus")):
                        mime_type = "audio/ogg"
                    else:
                        mime_type = "audio/mpeg"

                    logger.info(
                        "Lyria: success (URL download) — %.1fKB in %.1fs (model=%s)",
                        len(audio_data) / 1024,
                        elapsed,
                        model,
                    )
                    return AudioGenResult(
                        success=True,
                        audio_bytes=audio_data,
                        mime_type=mime_type,
                        text_content=remaining.strip(),
                        model_used=model,
                        duration_s=elapsed,
                    )
                except Exception as dl_exc:
                    logger.error("Lyria: failed to download audio from %s: %s", audio_url[:100], dl_exc)
                    return AudioGenResult(
                        success=False,
                        error_message="download_failed",
                        text_content=content,
                        model_used=model,
                    )

            # No audio found — return the text content as-is
            # This might happen if the model returned lyrics or a description
            logger.warning(
                "Lyria: response contained text but no audio data (model=%s). Content prefix: %s",
                model,
                content[:200],
            )
            return AudioGenResult(
                success=False,
                error_message="no_audio_in_response",
                text_content=content,
                model_used=model,
            )

        except TimeoutError:
            logger.error("Lyria: timeout after %.0fs (model=%s)", _AUDIO_TIMEOUT, model)
            return AudioGenResult(success=False, error_message="timeout", model_used=model)
        except Exception as exc:
            logger.error("Lyria: unexpected error: %s", exc, exc_info=True)
            return AudioGenResult(success=False, error_message=f"unexpected:{exc}", model_used=model)


# Module-level singleton
_lyria_provider: FreeTheAIAudioProvider | None = None


def get_lyria_provider() -> FreeTheAIAudioProvider:
    """Return the singleton FreeTheAIAudioProvider."""
    global _lyria_provider
    if _lyria_provider is None:
        _lyria_provider = FreeTheAIAudioProvider()
    return _lyria_provider
