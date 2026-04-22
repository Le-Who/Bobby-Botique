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
from app.utils.background_tasks import submit_task

logger = logging.getLogger(__name__)

ELEVENLABS_TTS_CONCURRENCY = 2
GEMINI_TTS_CONCURRENCY = 2
_HEARTBEAT_INTERVAL_S = 4.0


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
    status_message_id: int | None = None
    response_hash: str = ""
    enqueued_at: float = field(default_factory=time.monotonic)
    state: VoiceJobState = field(default_factory=VoiceJobState)


@dataclass(frozen=True)
class VoiceEnqueueResult:
    queued: bool
    deduped: bool
    job_id: str | None
    queue_position: int


async def _generate_single_chunk_gemini(
    text_chunk: str,
    voice: str,
    failed_keys: set[str],
    timeout: float = 50.0,
    tts_temperature: float | None = None,
    model_name: str = "gemini-3.1-flash-tts-preview",
    language_code: str | None = None,
) -> bytes | None:
    """Generate PCM audio for one chunk via Gemini TTS with key racing."""
    from app.errors import classify_key_error
    from app.handlers.ai_core import _resolve_ai_request
    from app.providers.tts import generate_speech
    from app.repos.keys import get_key_status_manager

    status_mgr = get_key_status_manager()

    async def _tts_race_call(key_data: dict[str, Any]) -> bytes | None:
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
        key_a, model_a, _ = await _resolve_ai_request(model_name, excluded_key_hashes=failed_keys)
        if not key_a:
            break

        failed_keys.add(key_a["key_hash"])
        key_b, _, _ = await _resolve_ai_request(model_name, excluded_key_hashes=failed_keys)
        failed_keys.discard(key_a["key_hash"])

        keys_to_race = [key_a] + ([key_b] if key_b else [])

        def _suppress(task: asyncio.Task[Any]) -> None:
            try:
                task.exception()
            except (asyncio.CancelledError, Exception):
                pass

        tasks = {asyncio.create_task(_tts_race_call(key_data)): key_data for key_data in keys_to_race}
        for task in tasks:
            task.add_done_callback(_suppress)

        winner_pcm: bytes | None = None
        pending = set(tasks)
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
                    for pending_task in pending:
                        pending_task.cancel()
                    break

                if exc is not None and not isinstance(exc, asyncio.CancelledError):
                    with contextlib.suppress(Exception):
                        err_cat = classify_key_error(str(exc))
                        await status_mgr.suspend_key(key_data["key_hash"], model_a, err_cat, str(exc)[:200])
                    failed_keys.add(key_data["key_hash"])

            if winner_pcm is not None:
                break

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
    model_name: str = "gemini-3.1-flash-tts-preview",
    language_code: str | None = None,
    on_chunk_complete: Callable[[int, int], Awaitable[None]] | None = None,
) -> list[bytes] | None:
    """Run the full Gemini TTS pipeline across all chunks."""
    from app.utils.audio import trim_trailing_silence

    failed_keys: set[str] = set()
    pcm_parts: list[bytes] = []

    for index, chunk in enumerate(chunks):
        pcm = await _generate_single_chunk_gemini(
            chunk,
            voice,
            failed_keys,
            timeout=adaptive_timeout,
            tts_temperature=tts_temperature,
            model_name=model_name,
            language_code=language_code,
        )
        if not pcm:
            if index == 0:
                logger.warning("Gemini TTS: first chunk failed, aborting")
            else:
                logger.warning("Gemini TTS: chunk %d/%d failed, sending partial audio", index + 1, len(chunks))
            break

        pcm_parts.append(trim_trailing_silence(pcm))
        if on_chunk_complete:
            await on_chunk_complete(index + 1, len(chunks))

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

    async def enqueue(
        self,
        *,
        bot: Bot,
        user_id: int,
        chat_id: int,
        reply_to_message_id: int,
        response_text: str,
        voice: str = "Aoede",
        tts_temperature: float | None = None,
        source_key: str,
    ) -> VoiceEnqueueResult:
        response_hash = hashlib.sha1(response_text.strip().encode("utf-8")).hexdigest()
        queue_position = 1
        queued_job: VoiceJob | None = None
        start_worker = False

        async with self._lock:
            active_job = self._active_jobs.get(user_id)
            if active_job and active_job.source_key == source_key and active_job.response_hash == response_hash:
                await role_conv_metrics.record_tts_job_deduped()
                return VoiceEnqueueResult(False, True, active_job.job_id, 1)

            queue = self._queues.setdefault(user_id, deque())
            for pending_job in queue:
                if pending_job.source_key == source_key and pending_job.response_hash == response_hash:
                    await role_conv_metrics.record_tts_job_deduped()
                    queue_position = list(queue).index(pending_job) + (2 if active_job else 1)
                    return VoiceEnqueueResult(False, True, pending_job.job_id, queue_position)

            queue_position = len(queue) + (2 if active_job else 1)
            queued_job = VoiceJob(
                job_id=str(uuid.uuid4()),
                user_id=user_id,
                chat_id=chat_id,
                reply_to_message_id=reply_to_message_id,
                response_text=response_text,
                voice=voice,
                tts_temperature=tts_temperature,
                source_key=source_key,
                bot=bot,
                response_hash=response_hash,
            )
            queued_job.state.queue_position = queue_position
            queue.append(queued_job)

            existing_worker = self._worker_tasks.get(user_id)
            start_worker = existing_worker is None or existing_worker.done()

        assert queued_job is not None
        await self._set_status(
            queued_job,
            status="queued",
            queue_position=queue_position,
            detail=f"В очереди: #{queue_position}",
        )
        await role_conv_metrics.record_tts_job_queued()
        logger.info(
            "TTS job queued: job_id=%s user_id=%s queue_position=%s source_key=%s",
            queued_job.job_id,
            user_id,
            queue_position,
            source_key,
        )

        if start_worker:
            worker_task = submit_task(self._run_user_queue(user_id))
            async with self._lock:
                self._worker_tasks[user_id] = worker_task

        await self._refresh_queued_statuses(user_id)
        return VoiceEnqueueResult(True, False, queued_job.job_id, queue_position)

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
        try:
            while True:
                async with self._lock:
                    queue = self._queues.get(user_id)
                    if not queue:
                        self._queues.pop(user_id, None)
                        self._worker_tasks.pop(user_id, None)
                        return
                    job = queue.popleft()
                    self._active_jobs[user_id] = job

                await self._refresh_queued_statuses(user_id)
                try:
                    await self._process_job(job)
                finally:
                    async with self._lock:
                        self._active_jobs.pop(user_id, None)
                    await self._refresh_queued_statuses(user_id)
        finally:
            async with self._lock:
                worker_task = self._worker_tasks.get(user_id)
                if worker_task and worker_task.done():
                    self._worker_tasks.pop(user_id, None)

    async def _process_job(self, job: VoiceJob) -> None:
        wait_ms = max(0.0, (time.monotonic() - job.enqueued_at) * 1000.0)
        heartbeat_stop = asyncio.Event()
        heartbeat_task = asyncio.create_task(self._heartbeat(job, heartbeat_stop))
        try:
            job.state.started_at = time.monotonic()
            await role_conv_metrics.record_tts_job_started(wait_ms)
            logger.info("TTS job started: job_id=%s user_id=%s queue_wait_ms=%.1f", job.job_id, job.user_id, wait_ms)
            await self._generate_and_send_voice(job)
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
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await heartbeat_task

    async def _generate_and_send_voice(self, job: VoiceJob) -> None:
        """Generate TTS audio and send it as Telegram voice."""
        from app.config import settings
        from app.i18n import detect_language
        from app.providers.elevenlabs_tts import ELEVENLABS_CHUNK_MAX_BYTES, generate_speech_with_key_rotation
        from app.providers.tts import _chunk_text_by_sentences, _clean_text_for_speech
        from app.utils.audio import crossfade_pcm_chunks, make_voice_file, pcm_to_ogg_opus

        clean_text = _clean_text_for_speech(job.response_text)
        if not clean_text:
            raise ValueError("empty_clean_text")

        language = detect_language(clean_text)
        language_code = "ru-RU" if language == "ru" else "en-US" if language == "en" else None

        await self._set_status(job, status="preparing_text", detail="Подготавливаю текст")
        await self._set_status(job, status="chunking", detail="Разбиваю текст на фрагменты")

        el_keys = settings.ELEVENLABS_API_KEYS
        el_voice_id = job.voice if job.voice and len(job.voice) > 10 else settings.ELEVENLABS_VOICE_ID

        pcm_parts: list[bytes] | None = None

        if el_keys:
            el_chunks = _chunk_text_by_sentences(clean_text, max_bytes=ELEVENLABS_CHUNK_MAX_BYTES)
            await self._set_status(
                job,
                status="synthesizing",
                provider="elevenlabs",
                total_chunks=len(el_chunks),
                completed_chunks=0,
                detail=f"Синтезирую: ElevenLabs, чанк 0/{len(el_chunks)}",
            )
            el_timeout = min(90.0, max(30.0, len(clean_text) / 50.0 + 15.0))
            async with self._elevenlabs_sem:
                pcm_parts = await generate_speech_with_key_rotation(
                    el_chunks,
                    el_keys,
                    voice_id=el_voice_id,
                    timeout=el_timeout,
                    on_chunk_complete=lambda completed, total: self._set_status(
                        job,
                        status="synthesizing",
                        provider="elevenlabs",
                        total_chunks=total,
                        completed_chunks=completed,
                        detail=f"Синтезирую: ElevenLabs, чанк {completed}/{total}",
                    ),
                )

        if pcm_parts is None:
            if el_keys:
                job.state.fallback_used = True
                await role_conv_metrics.record_tts_fallback()
                await self._set_status(
                    job,
                    status="synthesizing",
                    provider="gemini",
                    fallback_used=True,
                    detail="Переключаю провайдер: Gemini",
                )

            gemini_chunks = _chunk_text_by_sentences(clean_text, max_bytes=1800)
            gemini_voice = job.voice if job.voice and len(job.voice) <= 10 else "Aoede"
            gemini_timeout = min(120.0, max(40.0, len(clean_text) / 40.0 + 40.0))

            async with self._gemini_sem:
                await self._set_status(
                    job,
                    status="synthesizing",
                    provider="gemini-3.1",
                    total_chunks=len(gemini_chunks),
                    completed_chunks=0,
                    fallback_used=job.state.fallback_used,
                    detail=f"Синтезирую: Gemini 3.1, чанк 0/{len(gemini_chunks)}",
                )
                pcm_parts = await _run_gemini_pipeline(
                    gemini_chunks,
                    gemini_voice,
                    gemini_timeout,
                    tts_temperature=job.tts_temperature,
                    model_name="gemini-3.1-flash-tts-preview",
                    language_code=language_code,
                    on_chunk_complete=lambda completed, total: self._set_status(
                        job,
                        status="synthesizing",
                        provider="gemini-3.1",
                        total_chunks=total,
                        completed_chunks=completed,
                        fallback_used=job.state.fallback_used,
                        detail=f"Синтезирую: Gemini 3.1, чанк {completed}/{total}",
                    ),
                )

                if not pcm_parts:
                    await self._set_status(
                        job,
                        status="synthesizing",
                        provider="gemini-2.5",
                        total_chunks=len(gemini_chunks),
                        completed_chunks=0,
                        fallback_used=True,
                        detail=f"Синтезирую: Gemini 2.5, чанк 0/{len(gemini_chunks)}",
                    )
                    pcm_parts = await _run_gemini_pipeline(
                        gemini_chunks,
                        gemini_voice,
                        gemini_timeout,
                        tts_temperature=job.tts_temperature,
                        model_name="gemini-2.5-flash-preview-tts",
                        language_code=language_code,
                        on_chunk_complete=lambda completed, total: self._set_status(
                            job,
                            status="synthesizing",
                            provider="gemini-2.5",
                            total_chunks=total,
                            completed_chunks=completed,
                            fallback_used=True,
                            detail=f"Синтезирую: Gemini 2.5, чанк {completed}/{total}",
                        ),
                    )

        if not pcm_parts:
            raise RuntimeError("no_audio_generated")

        await self._set_status(job, status="encoding", detail="Упаковываю аудио")
        pcm_audio = crossfade_pcm_chunks(pcm_parts)
        ogg_bytes = await pcm_to_ogg_opus(pcm_audio)
        if ogg_bytes is None:
            raise RuntimeError("pcm_to_ogg_failed")

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
        await asyncio.sleep(5.0)
        if job.status_message_id is not None:
            with contextlib.suppress(Exception):
                await job.bot.delete_message(chat_id=job.chat_id, message_id=job.status_message_id)

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

        rendered = error_text or self._render_status(job)
        await self._ensure_status_message(job, rendered)
        if job.status_message_id is None:
            return
        with contextlib.suppress(Exception):
            await job.bot.edit_message_text(
                chat_id=job.chat_id,
                message_id=job.status_message_id,
                text=rendered,
            )

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
    voice: str = "Aoede",
    tts_temperature: float | None = None,
    source_key: str,
) -> VoiceEnqueueResult:
    """Enqueue reply TTS for per-user serialized processing."""
    return await _voice_reply_manager.enqueue(
        bot=bot,
        user_id=user_id,
        chat_id=chat_id,
        reply_to_message_id=reply_to_message_id,
        response_text=response_text,
        voice=voice,
        tts_temperature=tts_temperature,
        source_key=source_key,
    )
