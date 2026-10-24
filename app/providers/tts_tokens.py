"""Model-native TTS token counts with bounded, job-scoped hash receipts.

Speech metadata is materialized as text for counting because countTokens does
not account for Interactions speech annotations. The actual speech payload is
unchanged. No tokenizer, character ratio or output-duration estimate substitutes
for a count from the selected TTS model.
"""

from __future__ import annotations

import asyncio
import hashlib
from bisect import bisect_right
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from google.genai import types

from app.observability.workload_events import observe_workload_call
from app.providers.tts_audio import TTSRequestError
from app.providers.tts_budget import GEMINI_TTS_INPUT_TOKENS, speech_text_boundaries

if TYPE_CHECKING:
    from app.providers.tts import SpeechSettings

_MAX_COUNT_REQUESTS = 64
_MAX_RECEIPTS = 96
_COUNT_TIMEOUT_S = 10.0
_MAX_INPUT_PIECES = 17
_PREFIX_PROBES = 8


@dataclass(frozen=True, slots=True)
class SpeechTokenReceipt:
    model_name: str
    source_hash: str
    metadata_hash: str
    input_tokens: int


def _identity(text: str, prepared: SpeechSettings) -> tuple[str, str, str]:
    source_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
    metadata = "\0".join((prepared.direction, prepared.voice, prepared.language_code or ""))
    return prepared.model_name, source_hash, hashlib.sha256(metadata.encode("utf-8")).hexdigest()


class SpeechTokenCache:
    """One source's bounded counts; never retain private source text or keys."""

    def __init__(self, receipts: tuple[SpeechTokenReceipt, ...] = ()) -> None:
        self.receipts = {
            (receipt.model_name, receipt.source_hash, receipt.metadata_hash): receipt
            for receipt in receipts[:_MAX_RECEIPTS]
            if isinstance(receipt.input_tokens, int)
            and not isinstance(receipt.input_tokens, bool)
            and receipt.input_tokens >= 0
        }
        self.unavailable: set[tuple[str, str]] = set()
        self.requests = 0


async def count_speech_tokens(
    text: str,
    api_key: str,
    prepared: SpeechSettings,
    *,
    client: Any,
    cache: SpeechTokenCache,
    timeout: float = _COUNT_TIMEOUT_S,
) -> SpeechTokenReceipt | None:
    identity = _identity(text, prepared)
    if identity in cache.receipts:
        return cache.receipts[identity]
    capability = (identity[0], identity[2])
    if capability in cache.unavailable or cache.requests >= _MAX_COUNT_REQUESTS:
        return None
    # Explicit text parts count all caller-controlled speech material. In
    # particular, using speech_metadata.style here silently omits its tokens.
    contents = types.Content(
        role="user",
        parts=[
            types.Part(text=f"Delivery direction:\n{prepared.direction}"),
            types.Part(text=f"Voice: {prepared.voice}"),
            types.Part(text=f"Language: {prepared.language_code or 'original language'}"),
            types.Part(text=text),
        ],
    )
    cache.requests += 1
    try:
        async with asyncio.timeout(min(_COUNT_TIMEOUT_S, timeout)):
            response = await observe_workload_call(
                client.aio.models.count_tokens(model=prepared.model_name, contents=contents),
                workload="tts.token_count",
                provider="gemini",
                model=prepared.model_name,
                api_key=api_key,
                origin="gemini_tts",
                input_chars=len(text),
            )
        count = getattr(response, "total_tokens", None)
        if not isinstance(count, int) or isinstance(count, bool) or count < 0:
            cache.unavailable.add(capability)
            return None
    except Exception:
        # Counting is not generation admission and must never penalize a key.
        # A real TTS request can still validate a source when counting fails.
        cache.unavailable.add(capability)
        return None
    receipt = SpeechTokenReceipt(*identity, count)
    if len(cache.receipts) < _MAX_RECEIPTS:
        cache.receipts[identity] = receipt
    return receipt


async def plan_speech_chunks(
    text: str,
    api_key: str,
    prepared: SpeechSettings,
    *,
    client: Any,
    cache: SpeechTokenCache,
    timeout: float = 50.0,
) -> list[str]:
    """Keep a fitting source whole; measure prefixes only after oversize proof."""
    # All planning shares a small fraction of the generation deadline. A slow
    # counter leaves time for real TTS validation instead of penalizing its key.
    deadline = asyncio.get_running_loop().time() + min(_COUNT_TIMEOUT_S, timeout / 5)

    async def count(transcript: str) -> SpeechTokenReceipt | None:
        return await count_speech_tokens(
            transcript,
            api_key,
            prepared,
            client=client,
            cache=cache,
            timeout=max(0.0, deadline - asyncio.get_running_loop().time()),
        )

    whole = await count(text)
    if whole is None or whole.input_tokens <= GEMINI_TTS_INPUT_TOKENS:
        return [text]
    metadata = await count("")
    if metadata is None:
        raise TTSRequestError("token_count_unavailable")
    if metadata.input_tokens >= GEMINI_TTS_INPUT_TOKENS:
        raise TTSRequestError("input_too_large")
    chunks: list[str] = []
    remaining, counted = text, whole
    while counted.input_tokens > GEMINI_TTS_INPUT_TOKENS:
        if len(chunks) >= _MAX_INPUT_PIECES - 1:
            raise TTSRequestError("input_too_large")
        boundaries = speech_text_boundaries(remaining)
        if not boundaries:
            raise TTSRequestError("unsplittable_text")
        available = GEMINI_TTS_INPUT_TOKENS - metadata.input_tokens
        estimated_end = len(remaining) * available / max(1, counted.input_tokens - metadata.input_tokens)
        low, high = -1, len(boundaries)
        candidate = max(0, min(high - 1, bisect_right(boundaries, estimated_end) - 1))
        fitting = None
        for _ in range(_PREFIX_PROBES):
            end = boundaries[candidate]
            receipt = await count(remaining[:end])
            if receipt is None:
                raise TTSRequestError("token_count_unavailable")
            if receipt.input_tokens <= GEMINI_TTS_INPUT_TOKENS:
                low, fitting = candidate, end
                if receipt.input_tokens == GEMINI_TTS_INPUT_TOKENS:
                    break
            else:
                high = candidate
            if high - low <= 1:
                break
            candidate = (low + high) // 2
        if fitting is None:
            raise TTSRequestError("unsplittable_text")
        chunks.append(remaining[:fitting])
        remaining = remaining[fitting:]
        next_count = await count(remaining)
        if next_count is None:
            raise TTSRequestError("token_count_unavailable")
        counted = next_count
    if remaining:
        chunks.append(remaining)
    return chunks


class SpeechInputSplitRequired(TTSRequestError):
    """Verified fitting pieces for a source exceeding the real input window."""

    def __init__(self, pieces: list[str]):
        super().__init__("input_too_large")
        self.pieces = pieces
