"""Bounded model selection for direct Gemini SDK workloads.

The callback owns generation config, response parsing, observation, and its
domain error handling. This helper owns bounded key rotation and exactly one RPD
reservation for each SDK attempt. Callbacks must not reserve again.
Absent overrides return ``None`` so callers retain their legacy path.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import math
from collections.abc import Awaitable, Callable

from app.config import is_gemini_chat_model_id
from app.process_policies import resolve_process


async def run_gemini_override[T](
    process_id: str,
    baseline: tuple[str, ...],
    execute: Callable[[str, str], Awaitable[T]],
    *,
    initial_api_key: str | None = None,
    max_key_attempts: int = 3,
    use_baseline: bool = False,
    timeout: float = 120.0,
) -> T | None:
    """Bound the entire configured chain, including selection and quota storage.

    A caller's shorter deadline still wins. Reserve models and keys never restart
    this budget; external cancellation propagates without a key-health penalty.
    """
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("timeout must be finite and positive")
    async with asyncio.timeout(timeout):
        return await _run_gemini_override(
            process_id,
            baseline,
            execute,
            initial_api_key=initial_api_key,
            max_key_attempts=max_key_attempts,
            use_baseline=use_baseline,
        )


async def _run_gemini_override[T](
    process_id: str,
    baseline: tuple[str, ...],
    execute: Callable[[str, str], Awaitable[T]],
    *,
    initial_api_key: str | None = None,
    max_key_attempts: int = 3,
    use_baseline: bool = False,
) -> T | None:
    """Run configured models in order, rotating keys for provider failures.

    ``initial_api_key`` preserves callers that already received a selected key.
    It is consumed only for the first model; later models select keys against
    their own local limits. Key attempts are bounded for each model, including
    reservation races. Domain parsing failures advance to the next model without
    penalizing a key. The public wrapper owns the total deadline; cancellation and
    reservation infrastructure errors propagate. There is no implicit provider
    or baseline fallback.
    """
    from app.errors import classify_key_error
    from app.repos.keys import get_available_gemini_key, get_key_status_manager, reserve_gemini_key_usage

    policy = await resolve_process(process_id, baseline)
    if not policy.explicit and not use_baseline:
        return None
    if type(max_key_attempts) is not int or not 1 <= max_key_attempts <= 10:
        raise ValueError("max_key_attempts must be positive")
    if any(not is_gemini_chat_model_id(model) for model in policy.models):
        raise ValueError(f"Process {process_id} requires Gemini chat models")

    last_error: Exception | None = None
    for index, model in enumerate(policy.models):
        excluded_hashes: set[str] = set()
        for key_attempt in range(max_key_attempts):
            if index == 0 and key_attempt == 0 and initial_api_key:
                key = initial_api_key
                key_hash = hashlib.sha256(key.encode()).hexdigest()
            else:
                key_data = await get_available_gemini_key(model, excluded_hashes=set(excluded_hashes))
                if not key_data:
                    if last_error is None:
                        last_error = RuntimeError(f"No Gemini key available for {model}")
                    break
                key = key_data["api_key"]
                key_hash = key_data["key_hash"]
            if key_hash in excluded_hashes:
                break
            excluded_hashes.add(key_hash)

            if not await reserve_gemini_key_usage(key_hash, model):
                last_error = RuntimeError(f"Gemini RPD limit reached for {model}")
                continue
            try:
                result = await execute(model, key)
                if result is None:
                    raise ValueError("Configured Gemini attempt returned an empty result")
            except Exception as error:
                last_error = error
                # Validation/JSON parsing belongs to the callback, not key health.
                if isinstance(error, ValueError):
                    break
                category = classify_key_error(str(error))
                status = getattr(error, "status_code", None) or getattr(error, "code", None)
                if (
                    category == "transient"
                    and status not in (408, 429, 500, 502, 503, 504)
                    and not isinstance(error, (TimeoutError, ConnectionError))
                ):
                    break
                try:
                    await get_key_status_manager().suspend_key(key_hash, model, category, type(error).__name__)
                except Exception as health_error:
                    logging.debug("Gemini process key cooldown failed: %s", type(health_error).__name__)
                continue
            try:
                await get_key_status_manager().record_success(key_hash, model)
            except Exception as health_error:
                logging.debug("Gemini process key success recording failed: %s", type(health_error).__name__)
            return result

    if last_error is not None:
        raise last_error
    raise ValueError(f"Process {process_id} has no configured models")
