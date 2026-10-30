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
from typing import TYPE_CHECKING, Any

from telegram import Bot

from app.metrics import role_conv_metrics
from app.process_policies import TTS_DEFAULT_MODELS, resolve_process
from app.runtime_settings.lifecycle import runtime_settings_scope
from app.utils.background_tasks import try_submit_task

if TYPE_CHECKING:
    from app.providers.tts import SpeechSettings
    from app.providers.tts_tokens import SpeechTokenCache, SpeechTokenReceipt

logger = logging.getLogger(__name__)

ELEVENLABS_TTS_CONCURRENCY = 2
# Counts actual provider requests; TTS never races duplicate generations.
GEMINI_TTS_CONCURRENCY = 3
_HEARTBEAT_INTERVAL_S = 4.0
_GEMINI_PLAN_TIMEOUT_FACTOR = 2.0
_MAX_USER_JOBS = 8
_MAX_JOBS = 128
_MAX_JOB_BYTES = 64 * 1024
_MAX_RETAINED_BYTES = 8 * 1024 * 1024
_SYNTHESIS_TIMEOUT_S = 300.0
_MAX_SYNTHESIS_TIMEOUT_S = 900.0
_MAX_ADAPTIVE_SPLITS = 16
_MAX_ADAPTIVE_DEPTH = 4
_MIN_ADAPTIVE_BYTES = 32
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
    text_is_clean: bool = False
    token_receipts: tuple[SpeechTokenReceipt, ...] = ()
    synthesis_timeout: asyncio.Timeout | None = field(default=None, repr=False, compare=False)


@dataclass(frozen=True)
class VoiceEnqueueResult:
    queued: bool
    deduped: bool
    job_id: str | None
    queue_position: int
    reason_code: str | None = None


@dataclass(frozen=True)
class VoiceMergeAdmission:
    allowed: bool
    token_receipts: tuple[SpeechTokenReceipt, ...] = ()


@runtime_settings_scope()
async def plan_voice_source_merge(
    response_text: str,
    voice: str = "",
    tts_temperature: float | None = None,
) -> VoiceMergeAdmission:
    """Check one merged transcript under the caller's lease/settings snapshot.

    This performs no synthesis, changes no settings and reserves no generation
    quota. Unknown limits/counts leave the original source boundaries intact.
    """
    from app.config import settings
    from app.i18n import detect_language
    from app.providers.elevenlabs_tts import _MODEL_CHARACTER_LIMITS
    from app.providers.tts import get_cached_genai_client, prepare_speech_settings
    from app.providers.tts_audio import TTSContractError
    from app.providers.tts_budget import GEMINI_TTS_INPUT_TOKENS
    from app.providers.tts_tokens import SpeechTokenCache, count_speech_tokens
    from app.repos.keys import get_available_gemini_key

    if not response_text.strip() or len(response_text.encode("utf-8")) > _MAX_JOB_BYTES:
        return VoiceMergeAdmission(False)
    delivery = await resolve_process("tts.delivery", ("elevenlabs", "gemini"))
    for provider in delivery.models:
        if provider == "elevenlabs" and settings.ELEVENLABS_API_KEYS:
            policy = await resolve_process("tts.elevenlabs", (settings.ELEVENLABS_MODEL,))
            models = policy.models if policy.explicit else (settings.ELEVENLABS_MODEL,)
            if not models:
                continue
            limit = _MODEL_CHARACTER_LIMITS.get(models[0])
            return VoiceMergeAdmission(limit is not None and len(response_text) <= limit)
        if provider == "gemini":
            policy = await resolve_process("tts", TTS_DEFAULT_MODELS)
            models = policy.models if policy.explicit else TTS_DEFAULT_MODELS
            if not models:
                continue
            model = models[0]
            key = await get_available_gemini_key(model, excluded_hashes=set(), use_cache=False)
            if not key:
                return VoiceMergeAdmission(False)
            language = detect_language(response_text)
            language_code = "ru-RU" if language == "ru" else "en-US" if language == "en" else None
            try:
                prepared = await prepare_speech_settings(
                    voice=voice,
                    tts_temperature=tts_temperature,
                    model_name=model,
                    language_code=language_code,
                )
            except TTSContractError:
                return VoiceMergeAdmission(False)
            receipt = await count_speech_tokens(
                response_text,
                key["api_key"],
                prepared,
                client=get_cached_genai_client(key["api_key"]),
                cache=SpeechTokenCache(),
            )
            allowed = receipt is not None and receipt.input_tokens <= GEMINI_TTS_INPUT_TOKENS
            return VoiceMergeAdmission(allowed, (receipt,) if allowed and receipt is not None else ())
    return VoiceMergeAdmission(False)


async def _generate_single_chunk_gemini(
    text_chunk: str,
    voice: str,
    failed_keys: set[str],
    timeout: float = 50.0,
    tts_temperature: float | None = None,
    model_name: str = TTS_DEFAULT_MODELS[0],
    language_code: str | None = None,
    request_semaphore: asyncio.Semaphore | None = None,
    speech_settings: SpeechSettings | None = None,
    token_cache: SpeechTokenCache | None = None,
) -> bytes | None:
    """Synthesize once, trying other keys only after a rejected/failed attempt."""
    from app.errors import classify_key_error
    from app.providers.tts import TTSAdmissionRejected, generate_speech
    from app.providers.tts_audio import TTSContractError
    from app.repos.keys import get_available_gemini_key, get_key_status_manager

    status_mgr = get_key_status_manager()
    minute_retries: dict[str, float] = {}
    while True:
        # Fresh usage ordering spreads jobs across the pool, including when a
        # model has no local daily cap. Never pin TTS to the chat key cache.
        key_data = await get_available_gemini_key(model_name, excluded_hashes=failed_keys, use_cache=False)
        if not key_data:
            if not minute_retries:
                return None
            # Every remaining key is temporarily full: wait for the earliest
            # minute window under the owning whole-message deadline.
            await asyncio.sleep(max(0.01, min(minute_retries.values()) - time.monotonic()))
            failed_keys.difference_update(minute_retries)
            minute_retries.clear()
            continue
        key_hash = key_data["key_hash"]
        if key_hash in failed_keys:
            return None
        failed_keys.add(key_hash)
        try:
            async with request_semaphore if request_semaphore is not None else contextlib.nullcontext():
                pcm = await generate_speech(
                    text_chunk,
                    key_data["api_key"],
                    voice=voice,
                    tts_temperature=tts_temperature,
                    timeout=timeout,
                    model_name=model_name,
                    language_code=language_code,
                    speech_settings=speech_settings,
                    token_cache=token_cache,
                )
            if not pcm:
                return None
            return pcm
        except TTSContractError as error:
            if error.reason_code in {
                "incomplete_audio",
                "output_token_limit",
                "input_too_large",
                "output_budget_exceeded",
            }:
                raise
            # The transcript/output is independent of credentials. Advance the
            # complete-message model plan without spending another key.
            return None
        except TTSAdmissionRejected as error:
            if error.retry_after is not None:
                minute_retries[key_hash] = time.monotonic() + error.retry_after
        except Exception as error:
            with contextlib.suppress(Exception):
                category = classify_key_error(str(error))
                if getattr(error, "code", None) == 429 or getattr(error, "status_code", None) == 429:
                    # "Quota exceeded" also describes RPM. Only explicit daily
                    # evidence may suspend a key until the next Pacific day.
                    evidence = str(error).lower().replace("_", "").replace(" ", "")
                    category = "quota" if "perday" in evidence or "dailylimit" in evidence else "rate_limit"
                await status_mgr.suspend_key(key_hash, model_name, category, type(error).__name__)


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
    speech_settings: SpeechSettings | None = None,
    token_cache: SpeechTokenCache | None = None,
) -> list[bytes] | None:
    """Collect sequential pieces, subdividing explicitly incomplete output."""
    from app.providers.tts_audio import MAX_TTS_AUDIO_BYTES, TTSContractError
    from app.providers.tts_budget import bisect_speech_text
    from app.providers.tts_tokens import SpeechInputSplitRequired, SpeechTokenCache

    token_cache = token_cache if token_cache is not None else SpeechTokenCache()
    pcm_parts: list[bytes] = []
    audio_size = 0
    # Lossless text splitting can isolate a long whitespace run. It carries no
    # speech and must not become an API request that returns empty audio.
    pending = deque((transcript, 0) for transcript in chunks if transcript.strip())
    total = len(pending)
    subdivisions = 0
    while pending:
        transcript, depth = pending.popleft()
        try:
            result = await _generate_single_chunk_gemini(
                transcript,
                voice,
                set(),
                timeout=adaptive_timeout,
                tts_temperature=tts_temperature,
                model_name=model_name,
                language_code=language_code,
                request_semaphore=request_semaphore,
                speech_settings=speech_settings,
                token_cache=token_cache,
            )
        except TTSContractError as error:
            pieces = error.pieces if isinstance(error, SpeechInputSplitRequired) else bisect_speech_text(transcript)
            if pieces is not None:
                pieces = [piece for piece in pieces if piece.strip()]
            if (
                error.reason_code
                not in {"incomplete_audio", "output_token_limit", "input_too_large", "output_budget_exceeded"}
                or depth >= _MAX_ADAPTIVE_DEPTH
                or subdivisions + (len(pieces) - 1 if pieces else 1) > _MAX_ADAPTIVE_SPLITS
                or (
                    not isinstance(error, SpeechInputSplitRequired)
                    and len(transcript.encode("utf-8")) <= _MIN_ADAPTIVE_BYTES
                )
                or not pieces
            ):
                return None
            subdivisions += len(pieces) - 1
            total += len(pieces) - 1
            pending.extendleft((piece, depth + 1) for piece in reversed(pieces))
            if on_chunk_complete:
                await on_chunk_complete(len(pcm_parts), total)
            continue
        if not result:
            return None
        audio_size += len(result)
        if audio_size > MAX_TTS_AUDIO_BYTES:
            logger.warning("Gemini TTS message exceeds the audio size budget; advancing model plan")
            return None
        pcm_parts.append(result)
        if on_chunk_complete:
            await on_chunk_complete(len(pcm_parts), total)
    return pcm_parts or None


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

    def metrics_snapshot(self) -> dict[str, Any]:
        return {
            "scope": "process",
            "available": True,
            "observed_at": time.time(),
            "jobs": self._retained_jobs,
            "bytes": self._retained_bytes,
            "running": len(self._active_jobs),
            "queued": sum(len(queue) for queue in self._queues.values()),
            "accepting": not self._closed,
        }

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
        text_is_clean: bool = False,
        token_receipts: tuple[SpeechTokenReceipt, ...] = (),
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
                text_is_clean=text_is_clean,
                token_receipts=token_receipts,
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
        text_is_clean: bool = False,
        token_receipts: tuple[SpeechTokenReceipt, ...] = (),
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
                text_is_clean=text_is_clean,
                token_receipts=token_receipts,
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
        job.token_receipts = ()
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
                generate_speech_with_key_rotation,
            )
            from app.providers.tts import _clean_text_for_speech, prepare_speech_settings
            from app.providers.tts_audio import TTSContractError
            from app.providers.tts_tokens import SpeechTokenCache
            from app.runtime_settings.audio import get_config, resolve_voice
            from app.utils.audio import pcm_to_ogg_opus

            clean_text = (
                job.response_text if getattr(job, "text_is_clean", False) else _clean_text_for_speech(job.response_text)
            )
            if not clean_text:
                return None

            language = detect_language(clean_text)
            language_code = "ru-RU" if language == "ru" else "en-US" if language == "en" else None

            el_keys = settings.ELEVENLABS_API_KEYS

            pcm_parts: list[bytes] | None = None
            token_cache = SpeechTokenCache(getattr(job, "token_receipts", ()))

            delivery = await resolve_process("tts.delivery", ("elevenlabs", "gemini"))
            model_deadline: asyncio.Timeout | None = None
            model_started = 0.0
            model_unit_timeout = 0.0

            async def progress(completed: int, total: int) -> None:
                if total > 1:
                    if model_deadline is not None:
                        expanded = model_started + min(_MAX_SYNTHESIS_TIMEOUT_S - 30, model_unit_timeout * total)
                        model_deadline.reschedule(
                            min(
                                model_started + _MAX_SYNTHESIS_TIMEOUT_S - 30,
                                max(model_deadline.when() or expanded, expanded),
                            )
                        )
                    job_deadline = getattr(job, "synthesis_timeout", None)
                    if job_deadline is not None and job.state.started_at is not None:
                        expanded = job.state.started_at + min(_MAX_SYNTHESIS_TIMEOUT_S, _SYNTHESIS_TIMEOUT_S * total)
                        job_deadline.reschedule(
                            min(
                                job.state.started_at + _MAX_SYNTHESIS_TIMEOUT_S,
                                max(job_deadline.when() or expanded, expanded),
                            )
                        )
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
                    el_chunks = [clean_text]
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
                    gemini_voice = resolve_voice(await get_config("gemini"), job.voice)
                    gemini_timeout = min(120.0, max(40.0, len(clean_text) / 40.0 + 40.0))

                    policy = await resolve_process("tts", TTS_DEFAULT_MODELS)
                    model_plan = policy.models if policy.explicit else TTS_DEFAULT_MODELS
                    # A shared deadline covers the complete model plan; each
                    # actual key request acquires the semaphore independently.
                    # Budget the sequential plan for every required piece, with
                    # an absolute cap retained by the owning whole-job timeout.
                    plan_settings = {}
                    for model in model_plan:
                        try:
                            prepared = await prepare_speech_settings(
                                voice=gemini_voice,
                                tts_temperature=job.tts_temperature,
                                model_name=model,
                                language_code=language_code,
                            )
                            plan_settings[model] = prepared
                        except TTSContractError as error:
                            logger.warning("TTS local model plan rejected: reason=%s", error.reason_code)
                    # Fitting sources keep the normal failover deadline. Extend
                    # only after actual input/output evidence proves more pieces.
                    model_unit_timeout = gemini_timeout * _GEMINI_PLAN_TIMEOUT_FACTOR
                    model_started = asyncio.get_running_loop().time()
                    plan_timeout = min(_MAX_SYNTHESIS_TIMEOUT_S - 30, model_unit_timeout)
                    try:
                        async with asyncio.timeout(plan_timeout) as model_deadline:
                            for model_index, model_name in enumerate(model_plan):
                                if model_name not in plan_settings:
                                    continue
                                gemini_chunks = [clean_text]
                                await self._set_status(
                                    job,
                                    status="synthesizing",
                                    detail=f"Генерирую части: 0/{len(gemini_chunks)}",
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
                                    speech_settings=plan_settings[model_name],
                                    token_cache=token_cache,
                                )
                                if pcm_parts:
                                    break
                    except TimeoutError:
                        pcm_parts = None
                        logger.warning(
                            "Gemini TTS message plan exhausted its deadline; trying next configured provider"
                        )
                    finally:
                        model_deadline = None
                if pcm_parts:
                    break

            if not pcm_parts:
                return None

            await self._set_status(job, status="encoding", detail="Упаковываю аудио…")
            # No overlap or silence trimming: a boundary may contain speech.
            pcm_audio = b"".join(pcm_parts)
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
            async with asyncio.timeout(_SYNTHESIS_TIMEOUT_S) as synthesis_timeout:
                job.synthesis_timeout = synthesis_timeout
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
            job.synthesis_timeout = None
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
    text_is_clean: bool = False,
    token_receipts: tuple[SpeechTokenReceipt, ...] = (),
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
        text_is_clean=text_is_clean,
        token_receipts=token_receipts,
    )
