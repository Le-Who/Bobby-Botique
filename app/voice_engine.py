# /app/voice_engine.py
"""Voice Engine — queued TTS orchestration and Telegram voice delivery."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import logging
import time
import uuid
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from telegram import Bot

from app.metrics import role_conv_metrics
from app.process_policies import TTS_DEFAULT_MODELS, resolve_process
from app.runtime_settings.lifecycle import runtime_settings_scope
from app.utils.background_tasks import try_submit_task

logger = logging.getLogger(__name__)

ELEVENLABS_TTS_CONCURRENCY = 2
# Counts actual requests, including both keys in every hedged chunk.
GEMINI_TTS_CONCURRENCY = 3
_HEARTBEAT_INTERVAL_S = 4.0
# Maximum chunks to generate in parallel within a single TTS job.
# Lower = fewer RPD consumed per job (2 × 2 key-racing = 4 RPD vs. old 8).
_MAX_PARALLEL_CHUNKS = 2
_GEMINI_PLAN_TIMEOUT_FACTOR = 2.0
_MAX_USER_JOBS = 8
_MAX_JOBS = 128
_MAX_JOB_BYTES = 64 * 1024
_MAX_RETAINED_BYTES = 8 * 1024 * 1024
_SYNTHESIS_TIMEOUT_S = 300.0
_STATUS_TIMEOUT_S = 5.0


@dataclass
class VoiceJobState:
    status: str = "queued"
    queue_position: int = 1
    total_chunks: int = 0
    completed_chunks: int = 0
    provider: str = "pending"
    started_at: float | None = None
    fallback_used: bool = False
    detail: str = "Ожидает запуска"


@dataclass
class VoiceJob:
    job_id: str
    user_id: int
    chat_id: int
    reply_to_message_id: int
    response_text: str
    voice: str
    tts_temperature: float | None
    source_key: str
    bot: Bot
    expected_epoch: int
    require_ltm: bool = False
    status_message_id: int | None = None
    response_hash: str = ""
    enqueued_at: float = field(default_factory=time.monotonic)
    state: VoiceJobState = field(default_factory=VoiceJobState)
    status_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    discarded: bool = False
    retained_bytes: int = 0
    status_text: str = ""
    status_revision: int = 0
    status_rendered: int = 0
    cleanup_scheduled: bool = False


@dataclass(frozen=True)
class VoiceEnqueueResult:
    queued: bool
    deduped: bool
    job_id: str | None
    queue_position: int
    reason_code: str | None = None


async def _generate_single_chunk_gemini(
    text_chunk: str,
    voice: str,
    failed_keys: set[str],
    timeout: float = 50.0,
    tts_temperature: float | None = None,
    model_name: str = TTS_DEFAULT_MODELS[0],
    language_code: str | None = None,
    request_semaphore: asyncio.Semaphore | None = None,
) -> bytes | None:
    """Generate PCM audio for one chunk via Gemini TTS with key racing."""
    from app.errors import classify_key_error
    from app.providers.tts import TTSAdmissionRejected, generate_speech
    from app.providers.tts_audio import TTSContractError
    from app.repos.keys import get_available_gemini_key, get_key_status_manager

    status_mgr = get_key_status_manager()

    async def _tts_race_call(key_data: dict[str, Any]) -> bytes | None:
        async with request_semaphore if request_semaphore is not None else contextlib.nullcontext():
            pcm = await generate_speech(
                text_chunk,
                key_data["api_key"],
                voice=voice,
                tts_temperature=tts_temperature,
                timeout=timeout,
                model_name=model_name,
                language_code=language_code,
            )
        if not pcm:
            raise ValueError("TTS provider returned empty audio buffer")
        return pcm

    for pair_attempt in range(2):
        key_a = await get_available_gemini_key(model_name, excluded_hashes=failed_keys)
        if not key_a:
            break

        failed_keys.add(key_a["key_hash"])
        key_b = await get_available_gemini_key(model_name, excluded_hashes=failed_keys)
        failed_keys.discard(key_a["key_hash"])

        keys_to_race = [key_a] + ([key_b] if key_b else [])

        def _suppress(task: asyncio.Task[Any]) -> None:
            try:
                task.exception()
            except asyncio.CancelledError, Exception:
                pass

        tasks = {asyncio.create_task(_tts_race_call(key_data)): key_data for key_data in keys_to_race}
        for task in tasks:
            task.add_done_callback(_suppress)

        winner_pcm: bytes | None = None
        pending = set(tasks)
        try:
            while pending:
                done, pending = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
                for task in done:
                    key_data = tasks[task]
                    try:
                        exc = task.exception()
                    except asyncio.CancelledError:
                        exc = asyncio.CancelledError("TTS task was cancelled")

                    if exc is None and winner_pcm is None:
                        winner_pcm = task.result()
                        break

                    if isinstance(exc, TTSContractError):
                        return None
                    if isinstance(exc, TTSAdmissionRejected):
                        failed_keys.add(key_data["key_hash"])
                        continue
                    if exc is not None and not isinstance(exc, asyncio.CancelledError):
                        with contextlib.suppress(Exception):
                            err_cat = classify_key_error(str(exc))
                            await status_mgr.suspend_key(key_data["key_hash"], model_name, err_cat, type(exc).__name__)
                        failed_keys.add(key_data["key_hash"])

                if winner_pcm is not None:
                    break

        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

        if winner_pcm is not None:
            return winner_pcm

        logger.warning("TTS Race: all %d key(s) in pair %d failed", len(keys_to_race), pair_attempt + 1)

    return None


async def _run_gemini_pipeline(
    chunks: list[str],
    voice: str,
    adaptive_timeout: float,
    *,
    tts_temperature: float | None = None,
    model_name: str = TTS_DEFAULT_MODELS[0],
    language_code: str | None = None,
    on_chunk_complete: Callable[[int, int], Awaitable[None]] | None = None,
    require_complete: bool = False,
    request_semaphore: asyncio.Semaphore | None = None,
) -> list[bytes] | None:
    """Run the full Gemini TTS pipeline across all chunks.

    Generates up to _MAX_PARALLEL_CHUNKS chunks concurrently via asyncio.gather
    to reduce total latency.  Each chunk still uses internal key-racing.
    """
    from app.providers.tts_audio import MAX_TTS_AUDIO_BYTES
    from app.utils.audio import trim_trailing_silence

    total = len(chunks)
    pcm_parts: list[bytes] = []
    completed_count = 0

    for batch_start in range(0, total, _MAX_PARALLEL_CHUNKS):
        batch = chunks[batch_start : batch_start + _MAX_PARALLEL_CHUNKS]
        # Each task gets its own failed_keys set so key-racing is independent.
        coros = [
            _generate_single_chunk_gemini(
                chunk,
                voice,
                set(),  # independent failed_keys per chunk
                timeout=adaptive_timeout,
                tts_temperature=tts_temperature,
                model_name=model_name,
                language_code=language_code,
                request_semaphore=request_semaphore,
            )
            for chunk in batch
        ]
        results = await asyncio.gather(*coros, return_exceptions=True)

        for i, result in enumerate(results):
            global_idx = batch_start + i
            if isinstance(result, BaseException) or result is None:
                if require_complete:
                    logger.warning("Configured Gemini TTS message incomplete; advancing the whole-message plan")
                    return None
                if global_idx == 0:
                    logger.warning("Gemini TTS: first chunk failed, aborting")
                else:
                    logger.warning(
                        "Gemini TTS: chunk %d/%d failed, sending partial audio",
                        global_idx + 1,
                        total,
                    )
                # Stop collecting — return what we have so far
                return pcm_parts if pcm_parts else None

            pcm_parts.append(trim_trailing_silence(result))
            if sum(map(len, pcm_parts)) > MAX_TTS_AUDIO_BYTES:
                logger.warning("Gemini TTS message exceeds the audio size budget; advancing model plan")
                return None
            completed_count += 1
            if on_chunk_complete:
                await on_chunk_complete(completed_count, total)

    return pcm_parts if pcm_parts else None


class VoiceReplyManager:
    """Per-user FIFO queue for reply TTS jobs."""

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._queues: dict[int, deque[VoiceJob]] = {}
        self._active_jobs: dict[int, VoiceJob] = {}
        self._worker_tasks: dict[int, asyncio.Task[Any]] = {}
        self._elevenlabs_sem = asyncio.Semaphore(ELEVENLABS_TTS_CONCURRENCY)
        self._gemini_sem = asyncio.Semaphore(GEMINI_TTS_CONCURRENCY)
        self._job_tasks: dict[int, asyncio.Task[Any]] = {}
        self._refresh_tasks: dict[int, asyncio.Task[Any]] = {}
        self._status_tasks: dict[str, asyncio.Task[Any]] = {}
        self._refresh_dirty: set[int] = set()
        self._cleanup_tasks: set[asyncio.Task[Any]] = set()
        self._retained_jobs = 0
        self._retained_bytes = 0
        self._closed = False

    async def enqueue(
        self,
        *,
        bot: Bot,
        user_id: int,
        chat_id: int,
        reply_to_message_id: int,
        response_text: str,
        voice: str = "",
        tts_temperature: float | None = None,
        source_key: str,
        expected_epoch: int,
        require_ltm: bool = False,
    ) -> VoiceEnqueueResult:
        """Atomically authorize queue capture of private response text."""
        from app.repos.memory_consent import private_data_lease

        async with private_data_lease(
            user_id,
            expected_epoch,
            purpose="ltm:tts_enqueue" if require_ltm else "conversation:tts_enqueue",
            require_ltm=require_ltm,
        ) as lease_current:
            if not lease_current:
                return VoiceEnqueueResult(False, False, None, 0, "stale_epoch")
            return await self._enqueue_leased(
                bot=bot,
                user_id=user_id,
                chat_id=chat_id,
                reply_to_message_id=reply_to_message_id,
                response_text=response_text,
                voice=voice,
                tts_temperature=tts_temperature,
                source_key=source_key,
                expected_epoch=expected_epoch,
                require_ltm=require_ltm,
            )

    async def _enqueue_leased(
        self,
        *,
        bot: Bot,
        user_id: int,
        chat_id: int,
        reply_to_message_id: int,
        response_text: str,
        voice: str = "",
        tts_temperature: float | None = None,
        source_key: str,
        expected_epoch: int,
        require_ltm: bool = False,
    ) -> VoiceEnqueueResult:
        if len(response_text) > _MAX_JOB_BYTES:
            return VoiceEnqueueResult(False, False, None, 0, "text_too_large")
        text_bytes = len(response_text.encode("utf-8"))
        if text_bytes > _MAX_JOB_BYTES:
            return VoiceEnqueueResult(False, False, None, 0, "text_too_large")
        if not response_text.strip():
            return VoiceEnqueueResult(False, False, None, 0, "empty_text")
        response_hash = hashlib.sha1(response_text.strip().encode("utf-8")).hexdigest()
        async with self._lock:
            if self._closed:
                return VoiceEnqueueResult(False, False, None, 0, "closed")
            active = self._active_jobs.get(user_id)
            queue = self._queues.get(user_id, deque())
            existing = ([active] if active else []) + list(queue)
            for index, pending in enumerate(existing, start=1):
                if pending.source_key == source_key and pending.response_hash == response_hash:
                    await role_conv_metrics.record_tts_job_deduped()
                    return VoiceEnqueueResult(False, True, pending.job_id, index)
            if len(existing) >= _MAX_USER_JOBS:
                return VoiceEnqueueResult(False, False, None, 0, "user_capacity")
            if self._retained_jobs >= _MAX_JOBS or self._retained_bytes + text_bytes > _MAX_RETAINED_BYTES:
                return VoiceEnqueueResult(False, False, None, 0, "capacity")
            worker = self._worker_tasks.get(user_id)
            if worker is None or worker.done():
                worker = try_submit_task(self._run_user_queue(user_id))
                if worker is None:
                    return VoiceEnqueueResult(False, False, None, 0, "capacity")
                self._worker_tasks[user_id] = worker
            position = len(existing) + 1
            job = VoiceJob(
                job_id=str(uuid.uuid4()),
                user_id=user_id,
                chat_id=chat_id,
                reply_to_message_id=reply_to_message_id,
                response_text=response_text,
                voice=voice,
                tts_temperature=tts_temperature,
                source_key=source_key,
                bot=bot,
                expected_epoch=expected_epoch,
                require_ltm=require_ltm,
                response_hash=response_hash,
                retained_bytes=text_bytes,
            )
            job.state.queue_position = position
            self._queues.setdefault(user_id, deque()).append(job)
            self._retained_jobs += 1
            self._retained_bytes += text_bytes
            self._schedule_refresh(user_id)
        await role_conv_metrics.record_tts_job_queued()
        return VoiceEnqueueResult(True, False, job.job_id, position)

    def _scrub_job(self, job: VoiceJob) -> None:
        if job.retained_bytes:
            self._retained_bytes -= job.retained_bytes
            self._retained_jobs -= 1
            job.retained_bytes = 0
        job.response_text = ""
        job.response_hash = ""
        job.discarded = True
        if job.state.status not in {"done", "failed"}:
            self._schedule_status_cleanup(job)

    def _schedule_refresh(self, user_id: int) -> None:
        if self._closed:
            return
        self._refresh_dirty.add(user_id)
        current = self._refresh_tasks.get(user_id)
        if current is None or current.done():
            task = try_submit_task(self._refresh_loop(user_id))
            if task is not None:
                self._refresh_tasks[user_id] = task

    async def _refresh_loop(self, user_id: int) -> None:
        try:
            while user_id in self._refresh_dirty and not self._closed:
                self._refresh_dirty.discard(user_id)
                await self._refresh_queued_statuses(user_id)
        finally:
            self._refresh_tasks.pop(user_id, None)

    async def purge_user_jobs(self, user_id: int, *, ltm_only: bool = False) -> int:
        """Cancel owned processing and scrub selected input after a privacy barrier."""
        removed = []
        task = None
        async with self._lock:
            retained: deque[VoiceJob] = deque()
            for job in self._queues.pop(user_id, ()):
                if not ltm_only or job.require_ltm:
                    removed.append(job)
                else:
                    retained.append(job)
            if retained:
                self._queues[user_id] = retained
            active = self._active_jobs.get(user_id)
            if active is not None and (not ltm_only or active.require_ltm):
                removed.append(active)
                task = self._job_tasks.get(user_id)
            for job in removed:
                self._scrub_job(job)
            if task is not None and not task.cancelling():
                task.cancel()
            status_tasks = []
            for job in removed:
                status_task = self._status_tasks.get(job.job_id)
                if status_task is not None:
                    status_tasks.append(status_task)
                    if not status_task.cancelling():
                        status_task.cancel()
            self._schedule_refresh(user_id)
        if task is not None:
            status_tasks.append(task)
        await _await_task_cleanup(*status_tasks)
        return len(removed)

    async def close(self) -> None:
        """Stop admission and cancel/await every owned task before storage closes."""
        async with self._lock:
            self._closed = True
            for queue in self._queues.values():
                for job in queue:
                    self._scrub_job(job)
            self._queues.clear()
            for job in self._active_jobs.values():
                self._scrub_job(job)
            # Workers cancel their shielded children exactly once and await
            # lease release; repeated caller cancellation cannot interrupt it.
            owners = set(self._worker_tasks.values()) | set(self._refresh_tasks.values()) | self._cleanup_tasks
            owners.update(self._status_tasks.values())
            tasks = owners | set(self._job_tasks.values())
            for task in owners:
                if not task.cancelling():
                    task.cancel()
        try:
            await _await_task_cleanup(*tasks)
        finally:
            self._worker_tasks.clear()
            self._job_tasks.clear()
            self._active_jobs.clear()
            self._refresh_tasks.clear()
            self._status_tasks.clear()
            self._refresh_dirty.clear()
            self._cleanup_tasks.clear()

    async def start(self) -> None:
        """Reopen a fully drained queue for a new Telegram application."""
        async with self._lock:
            if not self._closed:
                return
            if (
                self._worker_tasks
                or self._job_tasks
                or self._refresh_tasks
                or self._status_tasks
                or self._cleanup_tasks
            ):
                raise RuntimeError("Voice queue must finish draining before restart")
            self._closed = False

    async def wait_until_idle(self, user_id: int, timeout: float = 3.0) -> None:
        """Best-effort helper for tests."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            async with self._lock:
                has_queue = bool(self._queues.get(user_id))
                has_active = user_id in self._active_jobs
                has_worker = user_id in self._worker_tasks and not self._worker_tasks[user_id].done()
            if not has_queue and not has_active and not has_worker:
                return
            await asyncio.sleep(0.01)
        raise TimeoutError(f"Voice queue for user {user_id} did not drain in time")

    async def _run_user_queue(self, user_id: int) -> None:
        owner = asyncio.current_task()
        try:
            while not self._closed:
                async with self._lock:
                    queue = self._queues.get(user_id)
                    if not queue:
                        self._queues.pop(user_id, None)
                        return
                    job = queue.popleft()
                    self._active_jobs[user_id] = job
                    task = asyncio.create_task(self._process_job(job))
                    self._job_tasks[user_id] = task
                    self._schedule_refresh(user_id)
                try:
                    await asyncio.shield(task)
                except asyncio.CancelledError:
                    if owner is not None and owner.cancelling():
                        raise
                    # A failed consent renewal cancels this job, not the FIFO.
                except Exception:
                    logger.exception("TTS job owner failed: job_id=%s", job.job_id)
                finally:
                    try:
                        if not task.done():
                            if not task.cancelling():
                                task.cancel()
                            await _await_task_cleanup(task)
                    finally:
                        async with self._lock:
                            self._job_tasks.pop(user_id, None)
                            self._active_jobs.pop(user_id, None)
                            self._scrub_job(job)
                            self._schedule_refresh(user_id)
        finally:
            async with self._lock:
                if self._worker_tasks.get(user_id) is owner:
                    self._worker_tasks.pop(user_id, None)
                if not self._queues.get(user_id):
                    self._refresh_dirty.discard(user_id)

    @runtime_settings_scope()
    async def _pregenerate_audio(self, job: VoiceJob) -> bytes | None:
        """Generate one job's audio inside its FIFO worker and durable lease."""
        try:
            from app.config import settings
            from app.i18n import detect_language
            from app.providers.elevenlabs_tts import (
                ELEVENLABS_CHUNK_MAX_BYTES,
                generate_speech_with_key_rotation,
            )
            from app.providers.tts import _chunk_text_by_sentences, _clean_text_for_speech
            from app.runtime_settings.audio import get_config, resolve_voice
            from app.utils.audio import crossfade_pcm_chunks, pcm_to_ogg_opus

            clean_text = _clean_text_for_speech(job.response_text)
            if not clean_text:
                return None

            language = detect_language(clean_text)
            language_code = "ru-RU" if language == "ru" else "en-US" if language == "en" else None

            el_keys = settings.ELEVENLABS_API_KEYS

            pcm_parts: list[bytes] | None = None

            delivery = await resolve_process("tts.delivery", ("elevenlabs", "gemini"))

            async def progress(completed: int, total: int) -> None:
                await self._set_status(
                    job,
                    status="synthesizing",
                    detail=f"Готово частей: {completed}/{total}",
                    completed_chunks=completed,
                    total_chunks=total,
                )

            for provider_index, provider in enumerate(delivery.models):
                if provider == "elevenlabs" and el_keys:
                    el_voice_id = resolve_voice(await get_config("elevenlabs"), job.voice)
                    el_chunks = _chunk_text_by_sentences(clean_text, max_bytes=ELEVENLABS_CHUNK_MAX_BYTES)
                    el_timeout = min(90.0, max(30.0, len(clean_text) / 50.0 + 15.0))
                    await self._set_status(
                        job,
                        status="synthesizing",
                        detail="Генерирую аудио…",
                        provider=provider,
                        total_chunks=len(el_chunks),
                        completed_chunks=0,
                        fallback_used=provider_index > 0,
                    )
                    async with self._elevenlabs_sem:
                        pcm_parts = await generate_speech_with_key_rotation(
                            el_chunks,
                            el_keys,
                            voice_id=el_voice_id,
                            model_id=settings.ELEVENLABS_MODEL,
                            timeout=el_timeout,
                            on_chunk_complete=progress,
                        )

                if provider == "gemini":
                    gemini_chunks = _chunk_text_by_sentences(clean_text, max_bytes=800)
                    gemini_voice = resolve_voice(await get_config("gemini"), job.voice)
                    gemini_timeout = min(120.0, max(40.0, len(clean_text) / 40.0 + 40.0))

                    policy = await resolve_process("tts", TTS_DEFAULT_MODELS)
                    model_plan = policy.models if policy.explicit else TTS_DEFAULT_MODELS
                    # A shared deadline covers the complete model plan; each
                    # actual key request acquires the semaphore independently.
                    batches = max(1, (len(gemini_chunks) + _MAX_PARALLEL_CHUNKS - 1) // _MAX_PARALLEL_CHUNKS)
                    plan_timeout = gemini_timeout * batches * _GEMINI_PLAN_TIMEOUT_FACTOR
                    try:
                        async with asyncio.timeout(plan_timeout):
                            for model_index, model_name in enumerate(model_plan):
                                await self._set_status(
                                    job,
                                    status="synthesizing",
                                    detail="Генерирую аудио…",
                                    provider=provider,
                                    total_chunks=len(gemini_chunks),
                                    completed_chunks=0,
                                    fallback_used=provider_index > 0 or model_index > 0,
                                )
                                pcm_parts = await _run_gemini_pipeline(
                                    gemini_chunks,
                                    gemini_voice,
                                    gemini_timeout,
                                    tts_temperature=job.tts_temperature,
                                    model_name=model_name,
                                    language_code=language_code,
                                    require_complete=True,
                                    on_chunk_complete=progress,
                                    request_semaphore=self._gemini_sem,
                                )
                                if pcm_parts:
                                    break
                    except TimeoutError:
                        pcm_parts = None
                        logger.warning(
                            "Gemini TTS message plan exhausted its deadline; trying next configured provider"
                        )
                if pcm_parts:
                    break

            if not pcm_parts:
                return None

            await self._set_status(job, status="encoding", detail="Упаковываю аудио…")
            pcm_audio = crossfade_pcm_chunks(pcm_parts)
            ogg_bytes = await pcm_to_ogg_opus(pcm_audio)
            return ogg_bytes
        except (OSError, TimeoutError, ValueError, RuntimeError) as exc:
            logger.warning(
                "TTS pregenerate failed: job_id=%s user_id=%s error=%s",
                job.job_id,
                job.user_id,
                exc,
            )
            return None

    async def _process_job(self, job: VoiceJob) -> None:
        from app.repos.memory_consent import private_data_lease

        async with private_data_lease(
            job.user_id,
            job.expected_epoch,
            purpose="ltm:tts" if job.require_ltm else "conversation:tts",
            require_ltm=job.require_ltm,
        ) as lease_current:
            if not lease_current:
                logger.info(
                    "Discarded stale TTS job %s for user %s",
                    job.job_id,
                    job.user_id,
                )
                return
            await self._process_job_leased(job)

    async def _process_job_leased(self, job: VoiceJob) -> None:
        wait_ms = max(0.0, (time.monotonic() - job.enqueued_at) * 1000.0)
        heartbeat_stop = asyncio.Event()
        heartbeat_task = asyncio.create_task(self._heartbeat(job, heartbeat_stop))
        try:
            job.state.started_at = time.monotonic()
            await role_conv_metrics.record_tts_job_started(wait_ms)
            logger.info("TTS job started: job_id=%s user_id=%s queue_wait_ms=%.1f", job.job_id, job.user_id, wait_ms)

            await self._set_status(job, status="synthesizing", detail="Генерирую аудио…")
            async with asyncio.timeout(_SYNTHESIS_TIMEOUT_S):
                ogg_bytes = await self._pregenerate_audio(job)
            if ogg_bytes is None:
                raise RuntimeError("no_audio_generated")
            await self._send_ogg(job, ogg_bytes)

            await role_conv_metrics.record_tts_job_completed()
        except Exception as exc:
            logger.error("TTS job failed: job_id=%s user_id=%s error=%s", job.job_id, job.user_id, exc, exc_info=True)
            await role_conv_metrics.record_tts_job_failed()
            await self._set_status(
                job,
                status="failed",
                detail=f"Ошибка: {type(exc).__name__}",
                error_text="❌ Озвучка не удалась. Попробуйте ещё раз.",
            )
        finally:
            heartbeat_stop.set()
            heartbeat_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await heartbeat_task

    async def _send_ogg(self, job: VoiceJob, ogg_bytes: bytes) -> None:
        """Send pre-generated OGG audio as a Telegram voice message."""
        from app.utils.audio import make_voice_file

        await self._set_status(job, status="sending", detail="Отправляю голосовое сообщение")
        voice_file = make_voice_file(ogg_bytes)
        try:
            await job.bot.send_voice(
                chat_id=job.chat_id,
                voice=voice_file,
                reply_to_message_id=job.reply_to_message_id,
            )
        finally:
            voice_file.close()

        await self._set_status(job, status="done", detail="Готово")

    async def _heartbeat(self, job: VoiceJob, stop_event: asyncio.Event) -> None:
        while not stop_event.is_set():
            with contextlib.suppress(Exception):
                await job.bot.send_chat_action(chat_id=job.chat_id, action="upload_voice")
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=_HEARTBEAT_INTERVAL_S)
            except TimeoutError:
                continue

    async def _refresh_queued_statuses(self, user_id: int) -> None:
        async with self._lock:
            active_exists = user_id in self._active_jobs
            queued_jobs = list(self._queues.get(user_id, ()))

        for index, job in enumerate(queued_jobs, start=1):
            queue_position = index + (1 if active_exists else 0)
            await self._set_status(
                job,
                status="queued",
                queue_position=queue_position,
                detail=f"В очереди: #{queue_position}",
            )

    async def _ensure_status_message(self, job: VoiceJob, text: str) -> None:
        if job.status_message_id is not None:
            return
        try:
            msg = await job.bot.send_message(
                chat_id=job.chat_id,
                text=text,
                reply_to_message_id=job.reply_to_message_id,
                disable_notification=True,
            )
            job.status_message_id = getattr(msg, "message_id", None)
        except Exception as exc:
            logger.debug("Could not create TTS status message for job %s: %s", job.job_id, exc)

    async def _set_status(
        self,
        job: VoiceJob,
        *,
        status: str,
        detail: str,
        queue_position: int | None = None,
        total_chunks: int | None = None,
        completed_chunks: int | None = None,
        provider: str | None = None,
        fallback_used: bool | None = None,
        error_text: str | None = None,
    ) -> None:
        async with job.status_lock:
            if job.discarded:
                return
            if status == "queued" and (job.state.status != "queued" or self._active_jobs.get(job.user_id) is job):
                return
            if job.state.status in {"done", "failed"} and status != job.state.status:
                return
            job.state.status = status
            job.state.detail = detail
            if queue_position is not None:
                job.state.queue_position = queue_position
            if total_chunks is not None:
                job.state.total_chunks = total_chunks
            if completed_chunks is not None:
                job.state.completed_chunks = completed_chunks
            if provider is not None:
                job.state.provider = provider
            if fallback_used is not None:
                job.state.fallback_used = fallback_used

            job.status_text = error_text or self._render_status(job)
            job.status_revision += 1
            current = self._status_tasks.get(job.job_id)
            if current is None or current.done():
                task = try_submit_task(self._render_status_loop(job))
                if task is not None:
                    self._status_tasks[job.job_id] = task

    async def _render_status_loop(self, job: VoiceJob) -> None:
        """One bounded UI owner coalesces revisions without blocking synthesis."""
        try:
            while not self._closed:
                async with job.status_lock:
                    if job.discarded and job.state.status not in {"done", "failed"}:
                        return
                    revision = job.status_revision
                    if revision == job.status_rendered:
                        return
                    rendered = job.status_text
                with contextlib.suppress(Exception):
                    async with asyncio.timeout(_STATUS_TIMEOUT_S):
                        await self._ensure_status_message(job, rendered)
                        if job.discarded and job.state.status not in {"done", "failed"}:
                            return
                        if revision != job.status_revision:
                            continue
                        if job.status_message_id is not None:
                            await job.bot.edit_message_text(
                                chat_id=job.chat_id,
                                message_id=job.status_message_id,
                                text=rendered,
                            )
                job.status_rendered = revision
                if job.state.status == "done":
                    self._schedule_status_cleanup(job)
        finally:
            if job.discarded and job.state.status not in {"done", "failed"}:
                self._schedule_status_cleanup(job)
            if self._status_tasks.get(job.job_id) is asyncio.current_task():
                self._status_tasks.pop(job.job_id, None)

    def _schedule_status_cleanup(self, job: VoiceJob) -> None:
        if job.status_message_id is None or job.cleanup_scheduled or self._closed:
            return
        cleanup = try_submit_task(_delete_status_later(job.bot, job.chat_id, job.status_message_id))
        if cleanup is not None:
            job.cleanup_scheduled = True
            self._cleanup_tasks.add(cleanup)
            cleanup.add_done_callback(self._cleanup_tasks.discard)

    def _render_status(self, job: VoiceJob) -> str:
        state = job.state
        percent = self._estimate_progress_percent(state)
        bar = self._progress_bar(percent)

        title_map = {
            "queued": "🎙️ Озвучка в очереди",
            "preparing_text": "🎙️ Подготавливаю озвучку",
            "chunking": "🎙️ Разбиваю текст",
            "synthesizing": "🎙️ Генерирую аудио",
            "encoding": "🎙️ Упаковываю аудио",
            "sending": "🎙️ Отправляю голос",
            "done": "✅ Озвучка готова",
            "failed": "❌ Ошибка озвучки",
        }

        return "\n".join(
            [
                title_map.get(state.status, "🎙️ Озвучка"),
                f"{bar} {percent}%",
                state.detail,
            ]
        )

    @staticmethod
    def _progress_bar(percent: int, width: int = 10) -> str:
        filled = max(0, min(width, round((percent / 100) * width)))
        return "[" + "#" * filled + "-" * (width - filled) + "]"

    @staticmethod
    def _estimate_progress_percent(state: VoiceJobState) -> int:
        if state.status == "queued":
            return 5
        if state.status == "preparing_text":
            return 10
        if state.status == "chunking":
            return 15
        if state.status == "synthesizing":
            total = max(1, state.total_chunks)
            ratio = min(1.0, state.completed_chunks / total)
            return min(80, max(15, round(15 + ratio * 65)))
        if state.status == "encoding":
            return 88
        if state.status == "sending":
            return 96
        if state.status == "done":
            return 100
        return 0


async def _delete_status_later(bot: Bot, chat_id: int, message_id: int) -> None:
    """Own only public identifiers, never a private job or its consent lease."""
    await asyncio.sleep(5.0)
    with contextlib.suppress(Exception):
        async with asyncio.timeout(_STATUS_TIMEOUT_S):
            await bot.delete_message(chat_id=chat_id, message_id=message_id)


async def _await_task_cleanup(*tasks: asyncio.Task[Any]) -> None:
    """Defer repeated cancellation until every already-cancelled owner exits."""
    joined = asyncio.gather(*tasks, return_exceptions=True)
    cancellation = None
    while not joined.done():
        try:
            await asyncio.shield(joined)
        except asyncio.CancelledError as exc:
            cancellation = cancellation or exc
    joined.result()
    if cancellation is not None:
        raise cancellation


_voice_reply_manager = VoiceReplyManager()


def get_voice_reply_manager() -> VoiceReplyManager:
    return _voice_reply_manager


async def fire_voice_reply(
    *,
    bot: Bot,
    user_id: int,
    chat_id: int,
    reply_to_message_id: int,
    response_text: str,
    voice: str = "",
    tts_temperature: float | None = None,
    source_key: str,
    expected_epoch: int | None = None,
    require_ltm: bool = False,
) -> VoiceEnqueueResult:
    """Enqueue reply TTS for per-user serialized processing."""
    if expected_epoch is None:
        from app.repos.memory_consent import resolve_current_epoch

        expected_epoch = await resolve_current_epoch(user_id, require_ltm=require_ltm)
    if expected_epoch is None:
        return VoiceEnqueueResult(False, False, None, 0, "stale_epoch")
    return await _voice_reply_manager.enqueue(
        bot=bot,
        user_id=user_id,
        chat_id=chat_id,
        reply_to_message_id=reply_to_message_id,
        response_text=response_text,
        voice=voice,
        tts_temperature=tts_temperature,
        source_key=source_key,
        expected_epoch=expected_epoch,
        require_ltm=require_ltm,
    )
