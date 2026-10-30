"""Bounded Daily commands, read-only preflight and queue-owned execution."""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import asdict, dataclass
from datetime import date, timedelta
from typing import Any

from app.queue import current_job_guard, task_queue
from app.queue_contracts import DAILY_QUEUE_POLICY, JobOutcome, JobResult, QueueConflict, QueueError

KINDS = frozenset(
    {
        "croc.prepare_day",
        "croc.regenerate_image",
        "croc.reset_word",
        "trivia.regenerate",
        "trivia.validate_save",
        "tarot.prepare",
    }
)
EXECUTION_TIMEOUT_SECONDS = {kind: 1800 if kind == "tarot.prepare" else 600 for kind in KINDS}


@dataclass(frozen=True)
class DailyCommand:
    kind: str
    date: str
    difficulty: str | None = None
    part: str = "all"
    mode: str = "missing"
    model: str | None = None
    language: str = "ru"
    expected_revision: int | None = None
    questions: tuple[dict[str, Any], ...] = ()

    @classmethod
    def parse(cls, body: Any) -> DailyCommand:
        if not isinstance(body, dict) or not isinstance(body.get("kind"), str) or body["kind"] not in KINDS:
            raise ValueError("invalid_kind")
        try:
            day = date.fromisoformat(body.get("date", "")).isoformat()
        except (TypeError, ValueError) as exc:
            raise ValueError("invalid_date") from exc
        kind = body["kind"]
        difficulty, part = body.get("difficulty"), body.get("part", "all")
        replaces_content = kind in {
            "croc.reset_word",
            "croc.regenerate_image",
            "trivia.regenerate",
            "trivia.validate_save",
        }
        mode = body.get("mode", "replace" if replaces_content else "missing")
        model, language = body.get("model"), body.get("language", "ru")
        revision = body.get("expected_revision")
        if (
            not isinstance(mode, str)
            or not isinstance(part, str)
            or mode not in {"missing", "replace"}
            or part not in {"all", "main", "super"}
            or language != "ru"
        ):
            raise ValueError("invalid_daily_options")
        if model is not None and (not isinstance(model, str) or not model.strip() or len(model) > 200):
            raise ValueError("invalid_model")
        if difficulty is not None and (not isinstance(difficulty, str) or difficulty not in {"easy", "hard"}):
            raise ValueError("invalid_difficulty")
        if difficulty is not None and kind not in {"croc.reset_word", "croc.regenerate_image"}:
            raise ValueError("unexpected_difficulty")
        if not kind.startswith("trivia.") and part != "all":
            raise ValueError("unexpected_part")
        if (kind == "croc.prepare_day" and mode != "missing") or (replaces_content and mode != "replace"):
            raise ValueError("unsupported_mode")
        if revision is not None and (type(revision) is not int or revision < 0):
            raise ValueError("invalid_revision")
        if kind in {"croc.reset_word", "croc.regenerate_image"} and (difficulty is None or not revision):
            raise ValueError("expected_revision_required")
        if kind.startswith("trivia.") and revision is None:
            raise ValueError("expected_revision_required")
        questions = body.get("questions", [])
        if not isinstance(questions, (list, tuple)) or any(not isinstance(q, dict) for q in questions):
            raise ValueError("invalid_questions")
        if len(json.dumps(questions, ensure_ascii=False).encode()) > 240_000:
            raise ValueError("draft_too_large")
        if kind == "trivia.validate_save" and (
            part not in {"main", "super"} or len(questions) != (5 if part == "main" else 3)
        ):
            raise ValueError("invalid_question_count")
        if kind != "trivia.validate_save" and questions:
            raise ValueError("unexpected_draft")
        return cls(kind, day, difficulty, part, mode, model, language, revision, tuple(questions))

    @property
    def conflict_keys(self) -> tuple[str, ...]:
        if self.kind == "croc.prepare_day":
            return (f"croc:{self.date}:easy", f"croc:{self.date}:hard")
        if self.kind.startswith("croc."):
            return (f"croc:{self.date}:{self.difficulty}",)
        if self.kind.startswith("trivia."):
            return (f"trivia:{self.date}",)
        return (f"tarot:{self.date}:{self.language}",)


async def checkpoint() -> None:
    guard = current_job_guard()
    if guard is not None:
        await guard.checkpoint()


async def publish_progress(summary: dict[str, Any]) -> None:
    guard = current_job_guard()
    if guard is not None:
        await guard.update_progress(summary)


async def effective_policy(command: DailyCommand) -> dict[str, Any]:
    from app.process_policies import resolve_process
    from app.runtime_settings.lifecycle import operation_snapshot

    snapshot = await operation_snapshot()
    if command.kind == "tarot.prepare":
        from app.tarot_daily import resolve_daily_model_policy

        policy = await resolve_daily_model_policy(command.model)
        models = list(policy.models)
    elif command.kind.startswith("trivia."):
        from app.games.daily_trivia import TRIVIA_MODEL

        models = list((await resolve_process("daily.trivia", (TRIVIA_MODEL,))).models)
        if command.model is not None:
            raise ValueError("trivia_operation_model_unsupported")
    else:
        from app.games.crocodile_daily import get_daily_image_model
        from app.games.daily_ai import get_daily_text_model_for

        if command.kind == "croc.reset_word":
            models = [await get_daily_text_model_for("words") or "auto"]
            if command.model is not None:
                raise ValueError("croc_word_operation_model_unsupported")
        else:
            model = command.model or await get_daily_image_model()
            if command.model is not None and model not in {"fta-gpt-image-2", "vhr/gpt_image_2"}:
                from app.providers.pollinations import fetch_models

                catalog = {m["id"] for m in await fetch_models("image")}
                if model not in catalog:
                    raise ValueError("invalid_image_model")
            models = [model]
    return {
        "models": models,
        "revision": snapshot.revision,
        "at_execution": True,
        "explicit": command.model is not None,
    }


async def preview(command: DailyCommand) -> dict[str, Any]:
    day = date.fromisoformat(command.date)
    blocked = None
    readiness: dict[str, Any] = {}
    if command.kind.startswith("croc."):
        from app.games.daily_preparation import get_day_readiness

        readiness = await get_day_readiness(day)
        relevant = [
            p for p in readiness["puzzles"] if command.difficulty is None or p["difficulty"] == command.difficulty
        ]
        legacy = readiness.get("job")
        if legacy and legacy.get("legacy") and legacy.get("state") in {"queued", "running", "unknown"}:
            blocked = "legacy_preparation_running"
        for puzzle in relevant:
            if command.expected_revision is not None and puzzle["content_revision"] != command.expected_revision:
                blocked = "revision_conflict"
            if command.kind == "croc.reset_word" and puzzle["played"]:
                blocked = "puzzle_in_use"
            if command.kind == "croc.regenerate_image" and puzzle["played"] and puzzle["image_ready"]:
                blocked = "puzzle_in_use"
        count = sum(
            sum(not p[key] for key in ("word_ready", "hints_ready", "prompt_ready", "image_ready")) for p in relevant
        )
        if command.kind != "croc.prepare_day":
            count = 1
    elif command.kind.startswith("trivia."):
        from app.games.daily_trivia import has_complete_sibling
        from app.repos import daily_trivia

        puzzle = await daily_trivia.get_puzzle(day)
        current = puzzle.revision if puzzle else 0
        if current != command.expected_revision:
            blocked = "revision_conflict"
        if command.kind == "trivia.validate_save" and puzzle is None:
            blocked = "puzzle_missing"
        if blocked is None and not has_complete_sibling(puzzle, command.part):
            blocked = "incomplete_sibling"
        readiness = {"revision": current, "exists": puzzle is not None}
        count = 8 if command.part == "all" else 5 if command.part == "main" else 3
    else:
        from app.tarot import iter_daily_card_variants
        from app.tarot_daily import count_prepared_daily_readings

        total = len(list(iter_daily_card_variants()))
        ready = await count_prepared_daily_readings(day, language=command.language)
        readiness = {"ready": ready, "total": total}
        count = total if command.mode == "replace" else max(0, total - ready)
    return {
        "scope": {
            "kind": command.kind,
            "date": command.date,
            "difficulty": command.difficulty,
            "part": command.part,
            "mode": command.mode,
        },
        "blocked_reason": blocked,
        "work_items": count,
        "effective_policy": await effective_policy(command),
        "readiness": readiness,
    }


async def enqueue(command: DailyCommand, operation_id: str, *, queue=None):
    queue = queue or task_queue
    if not isinstance(operation_id, str) or not 8 <= len(operation_id) <= 128:
        raise ValueError("invalid_operation_id")
    existing = await queue.read_job_by_operation(0, f"daily:{operation_id}")
    if existing is None:
        plan = await preview(command)
        if plan["blocked_reason"]:
            raise QueueConflict(plan["blocked_reason"])
    return await queue.enqueue_job(
        0,
        command.kind,
        {"command": asdict(command)},
        idempotency_key=f"daily:{operation_id}",
        lane="daily",
        policy=DAILY_QUEUE_POLICY,
        conflict_keys=command.conflict_keys,
        summary={
            "date": command.date,
            "mode": command.mode,
            "part": command.part,
            "difficulty": command.difficulty or "all",
        },
    )


async def execute(*, command: dict[str, Any]) -> JobResult:
    from app.repos import crocodile_daily, daily_trivia
    from app.repos.tarot_generation import TarotGenerationConflict

    cmd = DailyCommand.parse(command)
    day = date.fromisoformat(cmd.date)
    counters: dict[str, Any] = {"date": cmd.date, "generated": 0, "skipped": 0, "failed": 0, "remaining": 0}
    revision_conflict = False

    async def progress(**values):
        counters.update(values)
        counters["completed"] = counters["generated"] + counters["skipped"] + counters["failed"]
        counters["total"] = counters["completed"] + counters["remaining"]
        await publish_progress(counters)

    try:
        async with asyncio.timeout(EXECUTION_TIMEOUT_SECONDS[cmd.kind]):
            await checkpoint()
            policy = await effective_policy(cmd)
            counters.update(effective_revision=policy["revision"], effective_models=", ".join(policy["models"])[:500])
            await publish_progress(counters)
            if cmd.kind == "tarot.prepare":
                from app.tarot_daily import prepare_daily_readings

                result = await prepare_daily_readings(
                    target_date=day,
                    force=cmd.mode == "replace",
                    model=cmd.model,
                    language=cmd.language,
                    progress=progress,
                )
                counters.update(
                    generated=result.generated, skipped=result.skipped, failed=result.failed, remaining=result.remaining
                )
                if result.locked:
                    return JobResult(JobOutcome.PERMANENT_FAILURE, "generation_busy", counters)
            elif cmd.kind.startswith("croc."):
                from app.bot_instance import get_bot
                from app.games.crocodile_daily import prepare_daily_puzzle

                if cmd.kind == "croc.reset_word":
                    await crocodile_daily.regenerate_puzzle_word(
                        day, cmd.difficulty or "easy", expected_revision=cmd.expected_revision or 0
                    )
                    counters["generated"] = 1
                else:
                    bot = get_bot()
                    if bot is None:
                        return JobResult(JobOutcome.PERMANENT_FAILURE, "bot_unavailable", counters)
                    difficulties = ("easy", "hard") if cmd.kind == "croc.prepare_day" else (cmd.difficulty or "easy",)
                    unit_size = 4 if cmd.kind == "croc.prepare_day" else 1
                    counters["remaining"] = len(difficulties) * unit_size
                    for difficulty in difficulties:
                        await checkpoint()
                        before = await crocodile_daily.get_puzzle(day, difficulty=difficulty)

                        def assets(p):
                            return [
                                bool(p and p.target_word),
                                bool(p and crocodile_daily.is_text_ready(p)),
                                bool(p and p.image_prompt),
                                bool(p and p.image_file_id),
                            ]

                        before_assets = assets(before)
                        image_confirmed = False
                        try:
                            prepared = await prepare_daily_puzzle(
                                day,
                                bot=bot,
                                difficulty=difficulty,
                                include_image=True,
                                force_image=cmd.kind == "croc.regenerate_image",
                                image_model=cmd.model,
                                bypass_image_quota=True,
                                expected_revision=cmd.expected_revision,
                            )
                            image_confirmed = bool(prepared and prepared.image_file_id)
                        except crocodile_daily.DailyCrocConflict:
                            revision_conflict = True
                            counters["failed"] += unit_size
                            counters["remaining"] -= unit_size
                            await progress()
                            continue
                        except QueueError:
                            raise
                        except Exception:
                            # Read persisted stages below; provider errors never become public text.
                            pass
                        puzzle = await crocodile_daily.get_puzzle(day, difficulty=difficulty)
                        if puzzle and puzzle.image_file_id:
                            counters["actual_model"] = puzzle.image_model
                        after_assets = assets(puzzle)
                        if cmd.kind == "croc.prepare_day":
                            counters["generated"] += sum(
                                after and not before for before, after in zip(before_assets, after_assets, strict=True)
                            )
                            counters["skipped"] += sum(
                                before and after for before, after in zip(before_assets, after_assets, strict=True)
                            )
                            counters["failed"] += sum(not ready for ready in after_assets)
                        else:
                            counters[
                                "generated" if image_confirmed and puzzle and puzzle.image_file_id else "failed"
                            ] += 1
                        counters["remaining"] -= unit_size
                        await progress()
            else:
                from app.games import daily_trivia as game
                from app.games import daily_trivia_authoring as authoring
                from app.repos.settings_repo import get_global_setting

                existing = await daily_trivia.get_puzzle(day)
                revision = existing.revision if existing else 0
                if revision != cmd.expected_revision:
                    raise daily_trivia.RevisionConflictError(expected=cmd.expected_revision or 0, actual=revision)
                if not game.has_complete_sibling(existing, cmd.part):
                    return JobResult(JobOutcome.PERMANENT_FAILURE, "incomplete_sibling", counters)
                if cmd.kind == "trivia.regenerate":
                    published = await game.prepare_daily_puzzle(
                        day, force=True, mode=cmd.part, expected_revision=cmd.expected_revision
                    )
                else:
                    if existing is None:
                        return JobResult(JobOutcome.PERMANENT_FAILURE, "puzzle_missing", counters)
                    questions = [authoring.parse_admin_question(q, idx) for idx, q in enumerate(cmd.questions)]
                    published = await authoring.publish_authored_day(
                        day,
                        main=questions if cmd.part == "main" else existing.questions,
                        super_questions=questions if cmd.part == "super" else existing.super_questions,
                        model_name=await get_global_setting("daily_trivia_llm_model", game.TRIVIA_MODEL),
                        expected_revision=cmd.expected_revision,
                        actor="admin",
                    )
                counters["puzzle_revision"] = published.revision
                counters["generated"] = 8 if cmd.part == "all" else 5 if cmd.part == "main" else 3
        await progress()
        await checkpoint()
    except crocodile_daily.DailyCrocConflict, daily_trivia.RevisionConflictError, TarotGenerationConflict:
        return JobResult(JobOutcome.PERMANENT_FAILURE, "revision_conflict", counters)
    except TimeoutError:
        return JobResult(
            JobOutcome.PERMANENT_FAILURE,
            "partial_generation" if counters["generated"] else "execution_timeout",
            counters,
        )
    except ValueError:
        return JobResult(JobOutcome.PERMANENT_FAILURE, "invalid_content", counters)
    except RuntimeError as error:
        from app.errors import extract_error_code

        if extract_error_code(str(error)) is not None:
            return JobResult(JobOutcome.PERMANENT_FAILURE, "generation_unavailable", counters)
        raise
    if revision_conflict:
        return JobResult(JobOutcome.PERMANENT_FAILURE, "revision_conflict", counters)
    if counters["failed"] or counters["remaining"]:
        return JobResult(JobOutcome.PERMANENT_FAILURE, "partial_generation", counters)
    return JobResult(JobOutcome.SUCCEEDED, result_summary=counters)


def register_daily_jobs(queue=None) -> None:
    queue = queue or task_queue
    for kind in sorted(KINDS):
        queue.register_handler(
            kind,
            execute,
            lane="daily",
            effectful=True,
            image_work=kind in {"croc.prepare_day", "croc.regenerate_image"},
            execution_timeout_seconds=EXECUTION_TIMEOUT_SECONDS[kind],
        )


async def readiness_calendar(start: date, days: int = 8) -> dict[str, Any]:
    from app.games.daily_preparation import get_day_readiness
    from app.repos import daily_2048, daily_trivia
    from app.repos.crocodile_daily import today_puzzle_date
    from app.tarot_daily import count_prepared_daily_readings

    if type(days) is not int or not 1 <= days <= 8:
        raise ValueError("invalid_days")
    result = []
    for offset in range(days):
        day = start + timedelta(days=offset)
        croc, board, trivia, tarot_count = await asyncio.gather(
            get_day_readiness(day),
            daily_2048.get_puzzle(day),
            daily_trivia.get_puzzle(day),
            count_prepared_daily_readings(day),
            return_exceptions=True,
        )
        result.append(
            {
                "date": day.isoformat(),
                "croc": {"available": False} if isinstance(croc, BaseException) else {**croc, "available": True},
                "daily2048": {"available": False}
                if isinstance(board, BaseException)
                else {"available": True, "ready": bool(board and board.status == "ready")},
                "trivia": {"available": False}
                if isinstance(trivia, BaseException)
                else {"available": True, "ready": bool(trivia and trivia.status == "ready")},
                "tarot": {"available": False}
                if isinstance(tarot_count, BaseException)
                else {"available": True, "ready": tarot_count, "total": 156},
            }
        )
    return {
        "timezone": "Europe/Kyiv",
        "today": today_puzzle_date().isoformat(),
        "days": result,
        "observed_at": time.time(),
    }
