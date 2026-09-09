"""Explicit admin preparation of both Daily Crocodile slots, without regeneration."""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import date
from typing import Any
from uuid import uuid4

from app.games.crocodile_daily import prepare_daily_puzzle
from app.repos import crocodile_daily as repo
from app.utils.background_tasks import submit_task
from app.utils.json_compat import json

logger = logging.getLogger(__name__)
_TIMEOUT = 600
_LEASE_TTL = 660
_STATUS_TTL = 86400
_jobs: dict[date, dict[str, Any]] = {}
_tasks: dict[date, asyncio.Task] = {}
_start_lock = asyncio.Lock()
_RELEASE = "if redis.call('get',KEYS[1]) == ARGV[1] then return redis.call('del',KEYS[1]) end return 0"
_SAVE = """
if redis.call('get',KEYS[1]) == ARGV[1] then
    return redis.call('set',KEYS[2],ARGV[2],'EX',ARGV[3])
end
return 0
"""


def _keys(puzzle_date: date) -> tuple[str, str]:
    prefix = f"daily:admin-preparation:{puzzle_date.isoformat()}"
    return f"{prefix}:lease", f"{prefix}:status"


async def _read_job(puzzle_date: date) -> dict[str, Any] | None:
    from app.cache import redis_client

    job = _jobs.get(puzzle_date)
    lease_missing = False
    if redis_client is not None:
        try:
            raw = await redis_client.get(_keys(puzzle_date)[1])
            owner = await redis_client.get(_keys(puzzle_date)[0])
            if isinstance(owner, bytes):
                owner = owner.decode("utf-8")
            if raw:
                job = json.loads(raw)
            if owner and (not job or job["id"] != owner):
                return {"id": owner, "state": "running", "errors": []}
            lease_missing = owner is None
        except Exception:
            logger.warning("Daily preparation status store unavailable")
    if not job or time.time() - job.get("started_at", 0) > _STATUS_TTL:
        return None
    job = dict(job)
    if job["state"] in {"queued", "running"}:
        task = _tasks.get(puzzle_date)
        local = _jobs.get(puzzle_date)
        local_active = bool(local and local["id"] == job["id"] and task is not None and not task.done())
        if (
            time.time() >= job["deadline"]
            or (lease_missing and not local_active)
            or (local and local["id"] == job["id"] and task is not None and task.done())
        ):
            job.update(state="failed", errors=["Preparation interrupted or timed out; retry is available."])
    return job


async def get_day_readiness(puzzle_date: date) -> dict[str, Any]:
    """Read persisted assets only. Never create puzzles or invoke providers."""
    puzzles = []
    for difficulty in repo.DAILY_DIFFICULTIES:
        puzzle = await repo.get_puzzle(puzzle_date, difficulty=difficulty)
        word = bool(puzzle and puzzle.target_word.strip())
        hints = bool(puzzle and puzzle.hints)
        prompt = bool(puzzle and puzzle.image_prompt.strip())
        image = bool(puzzle and puzzle.image_file_id.strip())
        puzzles.append(
            {
                "difficulty": difficulty,
                "exists": puzzle is not None,
                "word_ready": word,
                "hints_ready": hints,
                "prompt_ready": prompt,
                "image_ready": image,
                # Delivery intentionally remains text-capable without an image.
                "delivery_ready": bool(puzzle and repo.is_puzzle_fully_prepared(puzzle)),
                "fully_ready": word and hints and prompt and image,
            }
        )
    return {
        "date": puzzle_date.isoformat(),
        "puzzles": puzzles,
        "job": await _read_job(puzzle_date),
        "fully_ready": all(p["fully_ready"] for p in puzzles),
    }


async def _save(puzzle_date: date, job: dict[str, Any], client: Any) -> None:
    _jobs[puzzle_date] = dict(job)
    if client is not None:
        try:
            lease, status = _keys(puzzle_date)
            await client.eval(_SAVE, 2, lease, status, job["id"], json.dumps(job), _STATUS_TTL)
        except Exception:
            logger.warning("Daily preparation status update unavailable")


async def _run(puzzle_date: date, bot: Any, job: dict[str, Any], client: Any) -> None:
    errors: list[str] = []
    try:
        job["state"] = "running"
        await _save(puzzle_date, job, client)
        async with asyncio.timeout(_TIMEOUT):
            for difficulty in repo.DAILY_DIFFICULTIES:
                try:
                    await prepare_daily_puzzle(
                        puzzle_date,
                        bot,
                        difficulty=difficulty,
                        include_image=True,
                        force_image=False,
                        bypass_image_quota=True,
                    )
                except Exception:
                    # Do not expose provider error payloads or credentials through admin JSON.
                    errors.append(f"{difficulty}: preparation failed")
            readiness = await get_day_readiness(puzzle_date)
            job["state"] = (
                "completed"
                if readiness["fully_ready"]
                else "partial"
                if any(p["exists"] for p in readiness["puzzles"])
                else "failed"
            )
    except asyncio.CancelledError:
        job["state"] = "failed"
        errors.append("Preparation cancelled; retry is available.")
        raise
    except Exception:
        job["state"] = "failed"
        errors.append("Preparation interrupted or readiness unavailable; retry is available.")
    finally:
        job["errors"] = errors
        await _save(puzzle_date, job, client)
        if client is not None:
            try:
                await client.eval(_RELEASE, 1, _keys(puzzle_date)[0], job["id"])
            except Exception:
                logger.warning("Daily preparation lease release unavailable; lease will expire")


async def start_day_preparation(puzzle_date: date, bot: Any) -> dict[str, Any]:
    """Queue missing assets for easy AND hard, deduplicating concurrent requests."""
    from app.cache import redis_client

    async with _start_lock:
        task = _tasks.get(puzzle_date)
        if task is not None and not task.done():
            return dict(_jobs[puzzle_date])
        previous = _jobs.get(puzzle_date)
        if task is not None and task.done() and previous:
            # Cancellation before the coroutine starts (or TaskManager rejection)
            # skips _run's finally. Reconcile under the start lock before retrying.
            if previous["state"] in {"queued", "running"}:
                previous = dict(previous, state="failed", errors=["Preparation did not finish; retry is available."])
                await _save(puzzle_date, previous, redis_client)
            if redis_client is not None:
                try:
                    await redis_client.eval(_RELEASE, 1, _keys(puzzle_date)[0], previous["id"])  # type: ignore[misc]
                except Exception:
                    logger.warning("Daily preparation abandoned lease release unavailable")
        now = time.time()
        for old_date, old_job in list(_jobs.items()):
            old_task = _tasks.get(old_date)
            if (old_task is None or old_task.done()) and now - old_job["started_at"] > _STATUS_TTL:
                _jobs.pop(old_date, None)
                _tasks.pop(old_date, None)
        job: dict[str, Any] = {
            "id": uuid4().hex,
            "state": "queued",
            "started_at": now,
            "deadline": now + _LEASE_TTL,
            "errors": [],
        }
        client = redis_client
        if client is not None:
            try:
                acquired = await client.set(_keys(puzzle_date)[0], job["id"], nx=True, ex=_LEASE_TTL)
            except Exception:
                logger.warning("Daily preparation lease store unavailable; using process-local deduplication")
                client = None
            else:
                if not acquired:
                    existing = await _read_job(puzzle_date)
                    return existing or {"state": "running", "errors": []}
        await _save(puzzle_date, job, client)
        coro = _run(puzzle_date, bot, job, client)
        _tasks[puzzle_date] = submit_task(coro)
        # TaskManager's wrapper may be cancelled before awaiting this coroutine.
        _tasks[puzzle_date].add_done_callback(lambda _task: coro.close())
        return dict(job)
