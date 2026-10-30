"""Read-only Daily readiness and compatibility with the retired preparation status."""

from __future__ import annotations

import time
from datetime import date
from typing import Any

from app.repos import crocodile_daily as repo
from app.utils.json_compat import json

_STATUS_TTL = 86400


async def _read_job(puzzle_date: date) -> dict[str, Any] | None:
    """Observe old Redis ownership; never claim, resume or create legacy work."""
    from app.cache import redis_client

    if redis_client is None:
        return None
    try:
        raw = await redis_client.get(_keys(puzzle_date)[1])
        owner = await redis_client.get(_keys(puzzle_date)[0])
        if isinstance(owner, bytes):
            owner = owner.decode("utf-8")
        job = json.loads(raw) if raw else None
        if owner and (not job or job.get("id") != owner):
            return {"id": owner, "state": "running", "legacy": True}
        if not isinstance(job, dict) or time.time() - job.get("started_at", 0) > _STATUS_TTL:
            return None
        if job.get("state") in {"queued", "running"} and (not owner or time.time() >= job.get("deadline", 0)):
            job = dict(job, state="failed", reason_code="legacy_preparation_interrupted")
        return {
            "id": job.get("id"),
            "state": job.get("state"),
            "legacy": True,
            "reason_code": "legacy_preparation_interrupted" if job.get("state") == "failed" else None,
        }
    except Exception:
        return {"state": "unknown", "legacy": True, "reason_code": "legacy_status_unavailable"}


def _keys(puzzle_date: date) -> tuple[str, str]:
    prefix = f"daily:admin-preparation:{puzzle_date.isoformat()}"
    return f"{prefix}:lease", f"{prefix}:status"


async def get_day_readiness(puzzle_date: date) -> dict[str, Any]:
    """Read persisted assets only. Never create puzzles or invoke providers."""
    puzzles = []
    for difficulty in repo.DAILY_DIFFICULTIES:
        puzzle = await repo.get_puzzle(puzzle_date, difficulty=difficulty)
        word = bool(puzzle and puzzle.target_word.strip())
        hints = bool(puzzle and repo.is_text_ready(puzzle))
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
                "content_revision": puzzle.content_revision if puzzle else 0,
                "played": puzzle.played if puzzle else False,
            }
        )
    return {
        "date": puzzle_date.isoformat(),
        "puzzles": puzzles,
        "job": await _current_or_legacy_job(puzzle_date),
        "fully_ready": all(p["fully_ready"] for p in puzzles),
    }


async def _current_or_legacy_job(puzzle_date: date) -> dict[str, Any] | None:
    legacy = await _read_job(puzzle_date)
    if legacy and legacy.get("state") in {"queued", "running", "unknown"}:
        return dict(legacy, legacy=True)
    from app.queue import task_queue
    from app.queue_contracts import QueueUnavailable

    try:
        jobs = await task_queue.read_jobs_by_conflicts(
            0, "daily", tuple(f"croc:{puzzle_date.isoformat()}:{difficulty}" for difficulty in repo.DAILY_DIFFICULTIES)
        )
    except QueueUnavailable:
        return dict(legacy, legacy=True) if legacy else None
    for job in jobs:
        if job.kind.startswith("croc.") and job.result_summary.get("date") == puzzle_date.isoformat():
            return dict(job.as_dict(), id=job.task_id, legacy=False)
    return dict(legacy, legacy=True) if legacy else None


async def start_day_preparation(puzzle_date: date, bot: Any = None, *, operation_id: str) -> dict[str, Any]:
    """Compatibility service entry point; new work always uses the versioned queue."""
    from app.games.admin_jobs import DailyCommand, enqueue

    admitted = await enqueue(DailyCommand("croc.prepare_day", puzzle_date.isoformat()), operation_id)
    return {
        "id": admitted.task_id,
        "state": admitted.state.value,
        "reason_code": admitted.reason_code,
        "storage_mode": admitted.storage_mode,
    }
