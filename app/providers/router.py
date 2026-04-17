"""
ProviderRouter — routes AI requests with key rotation and health scoring.

Also provides the module-level convenience functions:
- get_provider_router() → singleton
- get_ai_response()     → backward-compat wrapper
"""

import logging
from typing import Any

from app.config import settings
from app.errors import (
    ErrorCode,
    classify_key_error,
    is_error_message,
    is_key_related_error,
    tag_error,
)
from app.providers.base import is_opencode_model
from app.providers.openrouter import _has_multimodal_content


# ── Opencode Go → Gemini cross-provider fallback map ──────────────────────────
# When ALL Opencode Go keys are exhausted or fail, silently retry on Gemini.
# Returns live mapping so hot-reloaded model names are correctly reflected.
def _get_opencode_gemini_fallback() -> dict[str, str]:
    """Build the Opencode → Gemini fallback map from current (live) settings."""
    return {
        "opencode-go/minimax-m2.7": settings.DEFAULT_MODEL,
        "opencode-go/minimax-m2.5": settings.DEFAULT_MODEL,
        "opencode-go/qwen3.6-plus": settings.RESEARCH_MODEL,
        "opencode-go/qwen3.5-plus": settings.QNA_MODEL,
        "opencode-go/kimi-k2.5": settings.RESEARCH_MODEL,
        "opencode-go/big-pickle": settings.DEFAULT_MODEL,
        "opencode-go/mimo-v2-omni": "gemini-3-flash-preview",  # vision-capable fallback
    }


class ProviderRouter:
    """
    Routes AI requests to the right provider with key rotation and health scoring.

    Uses DB-backed KeyStatusManager for persistent per-model key health tracking.
    Keys are suspended with error-category-aware cooldowns and automatically
    recover after their cooldown expires (two-tier selection in SQL).
    """

    def __init__(self, rate_limit_per_minute: int = 20) -> None:
        # Use the consolidated RateLimiter from security.py (includes periodic cleanup)
        from app.security import RateLimiter

        self._rate_limiter = RateLimiter(max_requests=rate_limit_per_minute, window_seconds=60)

    async def get_response(
        self,
        preferred_model: str,
        history: list,
        system_instruction: str | None = None,
        user_id: int | None = None,
        chat_id: int | None = None,
        use_openrouter: bool | None = None,
        max_key_retries: int = 3,
        thinking_level: str | None = None,
        timeout: float | None = None,
        *,
        _is_fallback: bool = False,
    ) -> tuple[str, int | None]:
        """
        Get AI response with automatic key rotation and health-aware selection.

        Delegates to AgentRequestUseCase for key resolution, which uses
        two-tier SQL (active first, then cooldown-expired) to pick keys.
        On failure, classifies the error and suspends the key with appropriate
        cooldown. On success, promotes the key back to active.
        """
        from app.agent_use_cases import AgentRequestUseCase
        from app.repos.keys import get_key_status_manager

        # Per-user rate limiting (async — RateLimiter from security.py)
        if user_id and not await self._rate_limiter.check_rate_limit(user_id):
            return (
                tag_error(
                    ErrorCode.RATE_LIMIT,
                    "⏳ Слишком много запросов. Пожалуйста, подождите минуту.",
                ),
                None,
            )

        # Auto-detect multimodal content.
        # For Opencode vision models (mimo-v2-omni / mimo-v2-pro), allow multimodal
        # passthrough — OpencodeGoProvider supports image_url in messages.
        # For all other models, force Gemini path which has native vision.
        if use_openrouter is None and _has_multimodal_content(history) and not is_opencode_model(preferred_model):
            use_openrouter = False

        use_case = AgentRequestUseCase()
        status_mgr = get_key_status_manager()
        failed_keys: set[str] = set()
        all_permanent: bool = True  # Track if ALL failures are permanent (model-level)

        for attempt in range(max_key_retries):
            key_data, model_used, resolution = await use_case.resolve_ai_request(
                preferred_model,
                use_openrouter=use_openrouter,
                excluded_key_hashes=failed_keys,
            )

            if not key_data:
                if resolution == "all_exhausted":
                    # ── Cross-provider fallback: Opencode Go → Gemini ─────────
                    if is_opencode_model(preferred_model) and not _is_fallback:
                        gemini_fallback = _get_opencode_gemini_fallback().get(preferred_model, settings.DEFAULT_MODEL)
                        logging.warning(
                            "Opencode keys exhausted for %s, falling back to Gemini %s",
                            preferred_model,
                            gemini_fallback,
                        )
                        return await self.get_response(
                            gemini_fallback,
                            history,
                            system_instruction=system_instruction,
                            user_id=user_id,
                            chat_id=chat_id,
                            use_openrouter=False,
                            max_key_retries=max_key_retries,
                            thinking_level=thinking_level,
                            timeout=timeout,
                            _is_fallback=True,
                        )
                    is_or = use_openrouter if use_openrouter is not None else ("/" in preferred_model and not is_opencode_model(preferred_model))
                    provider_name = "Opencode Go" if is_opencode_model(preferred_model) else ("OpenRouter" if is_or else "Gemini")
                    return (
                        tag_error(
                            ErrorCode.KEYS_EXHAUSTED,
                            f"🚫 Все ключи {provider_name} недоступны или исчерпаны. Попробуйте позже.",
                        ),
                        None,
                    )
                if resolution == "no_keys":
                    return (
                        tag_error(
                            ErrorCode.NO_KEYS,
                            "❌ OpenRouter не настроен. Добавьте ключи OpenRouter в настройки.",
                        ),
                        None,
                    )
                if resolution == "decryption_failed":
                    return (
                        tag_error(
                            ErrorCode.DECRYPTION_FAILED,
                            "🔐 Ошибка расшифровки API-ключей. Обратитесь к администратору (возможно, изменился ADMIN_SECRET).",
                        ),
                        None,
                    )
                return (
                    tag_error(
                        ErrorCode.KEYS_EXHAUSTED,
                        "🚫 Не удалось получить доступный ключ API. Попробуйте позже.",
                    ),
                    None,
                )

            # Execute the request
            assert model_used is not None  # guaranteed by _resolve_ai_request
            response_text, token_count = await use_case.get_ai_response(
                key_data["api_key"],
                history,
                model_used,
                system_instruction,
                user_id,
                chat_id,
                use_openrouter,
                thinking_level=thinking_level,
                timeout=timeout,
            )

            # Track health based on response
            if response_text and is_error_message(response_text) and is_key_related_error(response_text):
                failed_keys.add(key_data["key_hash"])
                error_category = classify_key_error(response_text)

                if error_category != "permanent":
                    all_permanent = False

                if error_category != "transient":
                    try:
                        await status_mgr.suspend_key(
                            key_data["key_hash"],
                            model_used,  # type: ignore[arg-type]  # asserted above
                            error_category,
                            response_text[:200],
                        )
                    except Exception as e:
                        logging.warning(
                            "Non-critical: failed to suspend key: %s",
                            e,
                        )

                logging.warning(
                    "Key %s… failed (category=%s, attempt %d/%d). Error: %s",
                    key_data["key_hash"][:8],
                    error_category,
                    attempt + 1,
                    max_key_retries,
                    response_text[:100],
                )
                continue

            # Success — update health and increment usage
            if response_text and not is_error_message(response_text):
                try:
                    await status_mgr.record_success(
                        key_data["key_hash"],
                        model_used,  # type: ignore[arg-type]  # asserted above
                    )
                except Exception as e:
                    logging.debug("Non-critical: record_success failed: %s", e)

                try:
                    await use_case.increment_key_usage(key_data["key_hash"], model_used, use_openrouter)  # type: ignore[arg-type]  # asserted above
                except Exception as e:
                    logging.warning("Non-critical: failed to increment key usage: %s", e)

            return response_text, token_count

        # ── Model-level fallback ─────────────────────────────────────────
        # All keys failed for the preferred model. If every failure was
        # "permanent" (API_KEY_INVALID — Google rejects the key for this
        # specific model), try alternative models before giving up.
        if all_permanent and failed_keys:
            fallback_result = await self._try_model_fallback(
                preferred_model,
                history,
                system_instruction,
                user_id,
                chat_id,
                use_openrouter,
                use_case,
                status_mgr,
            )
            if fallback_result is not None:
                return fallback_result

        is_or = use_openrouter if use_openrouter is not None else ("/" in preferred_model)
        provider_name = "OpenRouter" if is_or else "Gemini"
        return (
            tag_error(
                ErrorCode.KEYS_EXHAUSTED,
                f"🚫 Все доступные ключи {provider_name} не сработали ({max_key_retries} попыток). Попробуйте позже.",
            ),
            None,
        )

    async def stream_response(
        self,
        preferred_model: str,
        history: list,
        system_instruction: str | None = None,
        user_id: int | None = None,
        chat_id: int | None = None,
        use_openrouter: bool | None = None,
        max_key_retries: int = 3,
        thinking_level: str | None = None,
        enable_web_search: bool = False,
        *,
        _is_fallback: bool = False,
    ):
        """
        Stream AI response with Race Requests and automatic key rotation.

        Race Requests: on each attempt, resolves up to 2 keys and fires them
        in parallel. The first key to yield a chunk wins; the loser is cancelled.
        Zero artificial delays between retries for maximum speed.

        After exhausting all retries for transient errors (503), cascades to a
        lighter fallback model before returning an error to the user.

        Yields chunks of text.
        """
        import asyncio

        from app.agent_use_cases import AgentRequestUseCase
        from app.providers.base import get_provider_for_model
        from app.repos.keys import get_key_status_manager

        if user_id and not await self._rate_limiter.check_rate_limit(user_id):
            yield tag_error(
                ErrorCode.RATE_LIMIT,
                "⏳ Слишком много запросов. Пожалуйста, подождите минуту.",
            )
            return

        if use_openrouter is None and _has_multimodal_content(history) and not is_opencode_model(preferred_model):
            use_openrouter = False

        use_case = AgentRequestUseCase()
        status_mgr = get_key_status_manager()
        failed_keys: set[str] = set()
        all_permanent: bool = True
        had_transient: bool = False  # Track if any 503/transient errors occurred

        for _attempt in range(max_key_retries):
            # ── Race Requests: resolve up to 2 keys in parallel ──────────
            keys_to_race: list[dict] = []
            resolved_model: str | None = None

            for _race_idx in range(2):
                key_data, model_used, _resolution = await use_case.resolve_ai_request(
                    preferred_model,
                    use_openrouter=use_openrouter,
                    excluded_key_hashes=failed_keys | {k["key_hash"] for k in keys_to_race},
                )
                if key_data and model_used:
                    keys_to_race.append(key_data)
                    resolved_model = model_used
                else:
                    break  # No more keys available

            if not keys_to_race or not resolved_model:
                # ── Cross-provider fallback: Opencode Go → Gemini (streaming) ─────
                if is_opencode_model(preferred_model) and not _is_fallback:
                    gemini_fallback = _get_opencode_gemini_fallback().get(preferred_model, settings.DEFAULT_MODEL)
                    logging.warning(
                        "Opencode stream keys unavailable for %s, cascading to Gemini %s",
                        preferred_model,
                        gemini_fallback,
                    )
                    async for chunk in self.stream_response(
                        preferred_model=gemini_fallback,
                        history=history,
                        system_instruction=system_instruction,
                        user_id=user_id,
                        chat_id=chat_id,
                        use_openrouter=False,
                        max_key_retries=max_key_retries,
                        thinking_level=thinking_level,
                        enable_web_search=enable_web_search,
                        _is_fallback=True,
                    ):
                        yield chunk
                    return
                is_or = use_openrouter if use_openrouter is not None else ("/" in preferred_model and not is_opencode_model(preferred_model))
                provider_name = "Opencode Go" if is_opencode_model(preferred_model) else ("OpenRouter" if is_or else "Gemini")
                yield tag_error(
                    ErrorCode.KEYS_EXHAUSTED,
                    f"🚫 Все ключи {provider_name} недоступны.",
                )
                return

            model_used = resolved_model

            if len(keys_to_race) == 1:
                # Single key available — use direct streaming (no race overhead)
                key_data = keys_to_race[0]
                kh = key_data["key_hash"][:8]
                raw_key = key_data.get("api_key", "")
                key_suffix = raw_key[-4:] if len(raw_key) >= 4 else "????"
                logging.info(
                    "Streaming: model=%s key=%s…(…%s) attempt=%d/%d",
                    model_used,
                    kh,
                    key_suffix,
                    _attempt + 1,
                    max_key_retries,
                )

                provider = get_provider_for_model(model_used, key_data["api_key"])
                stream_started = False
                try:
                    async for chunk in provider.stream_response(  # type: ignore[attr-defined]
                        history=history,
                        model_name=model_used,
                        system_instruction=system_instruction,
                        thinking_level=thinking_level,
                        enable_web_search=enable_web_search,
                    ):
                        # Guard: provider may yield a tagged error string instead of
                        # raising an exception (e.g. OpenRouter 429 → RATE_LIMIT tag).
                        # Treat that as a stream-level failure so key rotation kicks in.
                        if not stream_started and is_error_message(chunk):
                            raise RuntimeError(f"Provider yielded error tag before streaming: {chunk[:200]}")

                        if not stream_started:
                            stream_started = True
                            try:
                                await status_mgr.record_success(key_data["key_hash"], model_used)
                                await use_case.increment_key_usage(key_data["key_hash"], model_used, use_openrouter)
                            except Exception as e:
                                logging.debug("Non-critical stats update failed: %s", e)
                        yield chunk

                    if stream_started:
                        return

                except Exception as e:
                    if stream_started:
                        logging.error("Stream failed mid-flight, escalating to streaming layer: %s", e)
                        raise

                    error_msg = str(e)
                    # Use the error tag (if present) to determine the correct penalty
                    # category rather than always hard-coding "transient".
                    # e.g. RATE_LIMIT → "rate_limit" (15 s cooldown),
                    #      QUOTA_EXCEEDED → "quota" (until midnight PT).
                    inner_tag = error_msg[error_msg.find("\u200b["):] if "\u200b[" in error_msg else error_msg
                    error_category = classify_key_error(inner_tag)
                    if error_category == "transient":
                        had_transient = True
                    if error_category != "permanent":
                        all_permanent = False

                    failed_keys.add(key_data["key_hash"])
                    logging.warning(
                        "Single-key stream failed (category=%s) for key=%s… model=%s: %s",
                        error_category,
                        key_data["key_hash"][:8],
                        model_used,
                        error_msg[:120],
                    )
                    try:
                        await status_mgr.suspend_key(
                            key_data["key_hash"],
                            model_used,
                            error_category,
                            error_msg[:200],
                        )
                    except Exception as db_e:
                        logging.warning("Failed to suspend key: %s", db_e)
                    continue  # Next retry attempt — no sleep!

            else:
                # ── Race: 2 keys in parallel ─────────────────────────────
                #
                # Strategy: fire both streams as tasks. The first to yield a chunk
                # wins — we cancel the loser and forward the winner's chunks.
                # If both fail before yielding, mark both as failed and retry.
                # Sentinel-based completion: each racer puts a _StreamEndSignal after
                # all chunks, so the consumer never relies on task.done() which
                # has a TOCTOU race (task finishes while last chunk is still in queue).
                # _StreamEndSignal also carries finish_reason from the task's own ContextVar
                # (asyncio.create_task copies ContextVars so mutations inside the task
                # are invisible to the parent).

                class _StreamEndSignal:
                    """Sentinel put by the producer after all chunks.

                    Carries finish_reason extracted from within the task's own context,
                    since asyncio.create_task copies ContextVars and mutations inside
                    the task are invisible to the parent context.
                    """

                    __slots__ = ("finish_reason",)

                    def __init__(self, finish_reason: str | None) -> None:
                        self.finish_reason = finish_reason

                winner_queue: asyncio.Queue[tuple[int, str | object | None, Exception | None]] = asyncio.Queue()

                async def _race_stream(
                    idx: int,
                    kd: dict,
                    mod: str = str(model_used),
                    q: asyncio.Queue = winner_queue,
                ) -> None:
                    """Race participant: stream from one key, push chunks + sentinel to queue."""
                    first_chunk_seen = False
                    try:
                        prov = get_provider_for_model(mod, kd["api_key"])
                        async for chunk in prov.stream_response(  # type: ignore[attr-defined]
                            history=history,
                            model_name=mod,
                            system_instruction=system_instruction,
                            thinking_level=thinking_level,
                            enable_web_search=enable_web_search,
                        ):
                            # Guard: provider may yield a tagged error string instead of
                            # raising an exception — treat it as a race failure so the
                            # partner key can win the race and serve the user.
                            if not first_chunk_seen and is_error_message(chunk):
                                raise RuntimeError(f"Provider yielded error tag before streaming: {chunk[:200]}")
                            first_chunk_seen = True
                            await q.put((idx, chunk, None))
                    except asyncio.CancelledError:
                        # Loser was cancelled — put sentinel so consumer doesn't hang
                        await q.put((idx, _StreamEndSignal(None), None))
                        return
                    except Exception as exc_:
                        await q.put((idx, None, exc_))
                        return
                    # Normal completion: read finish_reason from this task's own ContextVar
                    # and carry it in the sentinel so the parent context can apply it.
                    from app.streaming import _last_finish_reason as _fr_var

                    own_fr = _fr_var.get()
                    await q.put((idx, _StreamEndSignal(own_fr), None))

                kh_a = keys_to_race[0]["key_hash"][:8]
                kh_b = keys_to_race[1]["key_hash"][:8]
                logging.info(
                    "Race Requests: model=%s keys=[%s…, %s…] attempt=%d/%d",
                    model_used,
                    kh_a,
                    kh_b,
                    _attempt + 1,
                    max_key_retries,
                )

                tasks = [
                    asyncio.create_task(_race_stream(0, keys_to_race[0])),
                    asyncio.create_task(_race_stream(1, keys_to_race[1])),
                ]

                winner_idx: int | None = None
                loser_idx: int | None = None
                race_errors: dict[int, Exception] = {}

                # Wait for the first signal from either racer
                while winner_idx is None and len(race_errors) < len(keys_to_race):
                    try:
                        idx, chunk, exc = await asyncio.wait_for(winner_queue.get(), timeout=30.0)
                    except TimeoutError:
                        # Both racers hung — cancel both and retry
                        for t in tasks:
                            t.cancel()
                        for kd in keys_to_race:
                            failed_keys.add(kd["key_hash"])
                        had_transient = True
                        all_permanent = False
                        break
                    if exc is not None:
                        race_errors[idx] = exc
                        continue
                    if isinstance(chunk, _StreamEndSignal):
                        # Racer finished without yielding any real chunk — treat as error
                        race_errors[idx] = RuntimeError("stream ended without chunks")
                        continue
                    # We have a winner!
                    winner_idx = idx
                    loser_idx = 1 - idx

                if winner_idx is None:
                    # Both failed (or timed out) — mark keys as failed, no sleep, retry
                    for t in tasks:
                        t.cancel()
                    for i, kd in enumerate(keys_to_race):
                        failed_keys.add(kd["key_hash"])
                        # Safe default: treat unknown errors as transient
                        err_category = "transient"
                        try:
                            raw_err = str(race_errors.get(i, "race timeout"))
                            # Derive penalty category from the error tag embedded in the
                            # message (e.g. RATE_LIMIT, QUOTA_EXCEEDED) rather than always
                            # defaulting to "transient", so cooldown durations are accurate.
                            inner_tag = raw_err[raw_err.find("\u200b["):] if "\u200b[" in raw_err else raw_err
                            err_category = classify_key_error(inner_tag)
                            logging.warning(
                                "Race key=%s… failed (category=%s): %s",
                                kd["key_hash"][:8],
                                err_category,
                                raw_err[:120],
                            )
                            await status_mgr.suspend_key(kd["key_hash"], model_used, err_category, raw_err[:200])
                        except Exception:
                            pass
                        # Update outer flags regardless of whether suspend_key succeeded
                        if err_category == "transient":
                            had_transient = True
                        if err_category != "permanent":
                            all_permanent = False
                    continue  # Next retry — zero delay!

                # Cancel the loser
                assert loser_idx is not None
                tasks[loser_idx].cancel()
                winner_key = keys_to_race[winner_idx]

                # Record success for the winner
                try:
                    await status_mgr.record_success(winner_key["key_hash"], model_used)
                    await use_case.increment_key_usage(winner_key["key_hash"], model_used, use_openrouter)
                except Exception as e:
                    logging.debug("Non-critical stats update failed: %s", e)

                logging.info(
                    "Race winner: key=%s… (loser %s… cancelled)",
                    winner_key["key_hash"][:8],
                    keys_to_race[loser_idx]["key_hash"][:8],
                )

                # Yield the first winning chunk
                assert chunk is not None
                yield chunk

                # Forward remaining chunks from winner — break only on sentinel
                try:
                    while True:
                        try:
                            idx, chunk, exc = await asyncio.wait_for(winner_queue.get(), timeout=120.0)
                        except TimeoutError:
                            logging.warning("Race drain timed out after 120s — forcing exit")
                            break
                        if idx != winner_idx:
                            continue  # Stale chunk from loser
                        if isinstance(chunk, _StreamEndSignal):
                            # Winner stream completed — apply finish_reason to parent context
                            if chunk.finish_reason:
                                from app.streaming import set_last_finish_reason

                                set_last_finish_reason(chunk.finish_reason)
                            break  # All chunks delivered
                        if exc is not None:
                            raise exc
                        if chunk is not None:
                            yield chunk
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    logging.error("Race winner stream failed mid-flight: %s", e)
                    raise
                finally:
                    # Suppress unhandled-task-exception warning from loser
                    for t in tasks:
                        if not t.done():
                            t.cancel()
                    try:
                        await asyncio.wait(tasks, timeout=0.5)
                    except Exception:
                        pass
                return  # Race completed successfully

        # ── Exhausted retries — try model fallback ───────────────────────
        # For transient errors (503), cascade to lighter model before giving up.
        # This is the key UX improvement: user gets an answer from lite model
        # instead of a cold error message.
        if not _is_fallback and had_transient:
            # Determine fallback model — prefer lite variant of same family
            fallback_model = self._pick_transient_fallback_model(preferred_model, use_openrouter)
            if fallback_model:
                logging.info(
                    "Cascade fallback: %s → %s (all keys returned transient errors)",
                    preferred_model,
                    fallback_model,
                )
                async for chunk in self.stream_response(
                    preferred_model=fallback_model,
                    history=history,
                    system_instruction=system_instruction,
                    user_id=user_id,
                    chat_id=chat_id,
                    use_openrouter=use_openrouter,
                    max_key_retries=2,  # Fewer retries for fallback
                    thinking_level=thinking_level,
                    enable_web_search=enable_web_search,
                    _is_fallback=True,
                ):
                    yield chunk
                return

        # Permanent-error model fallback (existing behavior)
        if all_permanent and failed_keys:
            fallback_result = await self._try_model_fallback(
                preferred_model,
                history,
                system_instruction,
                user_id,
                chat_id,
                use_openrouter,
                use_case,
                status_mgr,
            )
            if fallback_result is not None:
                yield fallback_result[0]
                return

        # ── Deferred Queue (Plan §5): last resort before hard error ──────
        # If user_id and chat_id are available, enqueue for background retry
        # instead of showing a cold error.
        if user_id and chat_id and had_transient:
            try:
                from app.deferred_response import enqueue_deferred_generation

                task_id = await enqueue_deferred_generation(
                    user_id=user_id,
                    chat_id=chat_id,
                    history=history,
                    model_name=preferred_model,
                    system_instruction=system_instruction,
                )
                if task_id:
                    yield tag_error(
                        ErrorCode.KEYS_EXHAUSTED,
                        "⏳ Серверы AI временно перегружены. Я отправлю ответ, как только они освободятся.",
                    )
                    return
            except Exception as e:
                logging.warning("Deferred queue fallback failed: %s", e)

        is_or = use_openrouter if use_openrouter is not None else ("/" in preferred_model and not is_opencode_model(preferred_model))
        provider_name = "Opencode Go" if is_opencode_model(preferred_model) else ("OpenRouter" if is_or else "Gemini")
        yield tag_error(
            ErrorCode.KEYS_EXHAUSTED,
            f"🚫 Все доступные ключи {provider_name} не сработали ({max_key_retries} попыток). Попробуйте позже.",
        )
        return

    def _pick_transient_fallback_model(
        self,
        failed_model: str,
        use_openrouter: bool | None,
    ) -> str | None:
        """Pick a lighter fallback model for transient (503) errors.

        Maps heavy models to their lite counterparts. Returns None if
        the failed model is already the lightest available.
        """
        # Gemini cascade: heavy → lite (canonical model list only)
        _GEMINI_CASCADE = {
            "gemini-3-flash-preview": "gemini-2.5-flash-lite",
            "gemini-3.1-flash-lite-preview": "gemini-2.5-flash-lite",
            "gemini-2.5-flash": "gemini-2.5-flash-lite",
        }

        # Opencode Go: cascade to Gemini via the cross-provider fallback map
        if is_opencode_model(failed_model):
            gemini_fallback = _get_opencode_gemini_fallback().get(failed_model, settings.DEFAULT_MODEL)
            if gemini_fallback in settings.AVAILABLE_MODELS:
                return gemini_fallback
            return None

        is_or = use_openrouter if use_openrouter is not None else ("/" in failed_model and not is_opencode_model(failed_model))
        if is_or:
            return None  # OpenRouter handles its own fallbacks

        fallback = _GEMINI_CASCADE.get(failed_model)
        if fallback:
            # Verify the fallback model is actually in our available models list
            available = settings.AVAILABLE_MODELS
            if fallback in available:
                return fallback
            # If the exact match isn't configured, try any lite model in the list
            for m in available:
                if m != failed_model and ("lite" in m or "8b" in m):
                    return m

        return None

    async def _try_model_fallback(
        self,
        failed_model: str,
        history: list,
        system_instruction: str | None,
        user_id: int | None,
        chat_id: int | None,
        use_openrouter: bool | None,
        use_case,
        status_mgr,
    ) -> tuple[str, int | None] | None:
        """Try fallback models when all keys fail with permanent errors for one model.

        Returns (response_text, token_count) on success, or None if no fallback works.
        """
        from app.errors import is_error_message

        is_or = use_openrouter if use_openrouter is not None else ("/" in failed_model)
        fallback_models = settings.OPENROUTER_AVAILABLE_MODELS if is_or else settings.AVAILABLE_MODELS

        for fallback_model in fallback_models:
            if fallback_model == failed_model:
                continue

            key_data, model_used, _ = await use_case.resolve_ai_request(
                fallback_model,
                use_openrouter=use_openrouter,
            )
            if not key_data:
                continue

            logging.info(
                "Model fallback: trying %s instead of %s (all keys rejected by API for original model)",
                fallback_model,
                failed_model,
            )

            response_text, token_count = await use_case.get_ai_response(
                key_data["api_key"],
                history,
                model_used,
                system_instruction,
                user_id,
                chat_id,
                use_openrouter,
            )

            if response_text and not is_error_message(response_text):
                logging.info(
                    "Model fallback succeeded: %s → %s",
                    failed_model,
                    model_used,
                )
                try:
                    await status_mgr.record_success(key_data["key_hash"], model_used)
                except Exception as e:
                    logging.debug("Non-critical: record_success failed: %s", e)
                try:
                    await use_case.increment_key_usage(
                        key_data["key_hash"],
                        model_used,
                        use_openrouter,
                    )
                except Exception as e:
                    logging.warning("Non-critical: failed to increment key usage: %s", e)
                return response_text, token_count

            logging.warning(
                "Model fallback %s also failed: %s",
                fallback_model,
                (response_text or "")[:100],
            )

        return None

    async def get_key_stats(self) -> list[dict[str, Any]]:
        """Return health stats for all tracked keys (for diagnostics)."""
        from app.repos.keys import get_key_status_manager

        return await get_key_status_manager().get_all_statuses()


# Module-level singleton
_provider_router: ProviderRouter | None = None


def get_provider_router() -> ProviderRouter:
    """Get the singleton ProviderRouter instance."""
    global _provider_router
    if _provider_router is None:
        _provider_router = ProviderRouter()
    return _provider_router


async def get_ai_response(
    api_key: str,
    history: list[dict[str, Any]],
    model_name: str,
    system_instruction: str | None = None,
    user_id: int | None = None,
    chat_id: int | None = None,
    max_retries: int = 3,
) -> tuple[str, int | None]:
    """
    Unified entry point for AI responses.

    Automatically selects the appropriate provider based on model name.
    Returns tuple (response_text, token_count) for backwards compatibility.

    Args:
        api_key: API key for the provider
        history: Message history
        model_name: Model identifier (Gemini or OpenRouter format)
        system_instruction: Optional system prompt
        user_id: User ID for logging
        chat_id: Chat ID for logging
        max_retries: Maximum retry attempts

    Returns:
        Tuple of (response_text, token_count)
    """
    from app.providers.base import get_provider_for_model

    provider = get_provider_for_model(model_name, api_key)

    response = await provider.get_response(
        history=history,
        model_name=model_name,
        system_instruction=system_instruction,
        user_id=user_id,
        chat_id=chat_id,
        max_retries=max_retries,
    )

    return response.text, response.token_count if response.success else None
