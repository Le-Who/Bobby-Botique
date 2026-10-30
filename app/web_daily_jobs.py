"""Authenticated short HTTP operations for Daily queue admission and reads."""

import asyncio
import hmac
from datetime import date
from uuid import UUID

from quart import Blueprint, jsonify, request, session

from app.games import admin_jobs
from app.queue import task_queue
from app.queue_contracts import AdmissionState, QueueConflict, QueueUnavailable

MAX_BODY_BYTES = 256 * 1024


async def read_body():
    supplied = request.headers.get("X-CSRF-Token", "")
    expected = session.get("controls_csrf", "")
    if not expected or not hmac.compare_digest(str(expected).encode(), supplied.encode()):
        raise PermissionError("csrf_required")
    if request.content_length is not None and request.content_length > MAX_BODY_BYTES:
        raise ValueError("payload_too_large")
    if len(await request.get_data()) > MAX_BODY_BYTES:
        raise ValueError("payload_too_large")
    body = await request.get_json(silent=True)
    if not isinstance(body, dict):
        raise ValueError("invalid_json")
    return body


async def command_body():
    body = await read_body()
    return body, admin_jobs.DailyCommand.parse(body)


async def legacy_enqueue(kind: str, *, part: str | None = None):
    """Keep legacy URLs short; all generation is owned by the same job service."""
    try:
        body = await read_body()
        command = dict(body, kind=kind)
        if part is not None:
            command["part"] = part
        if kind == "trivia.regenerate":
            command.update(part=body.get("mode", "all"), mode="replace")
        if kind == "trivia.validate_save":
            command["questions"] = body.get("super_questions" if part == "super" else "questions", [])
        if "expected_revision" not in command and "revision" in body:
            command["expected_revision"] = body["revision"]
        return admission_response(
            await admin_jobs.enqueue(admin_jobs.DailyCommand.parse(command), body.get("operation_id"))
        )
    except PermissionError:
        return jsonify(error="csrf_required"), 403
    except ValueError:
        return jsonify(error="invalid_daily_command"), 400
    except QueueConflict as error:
        return jsonify(error=error.reason_code), 409
    except QueueUnavailable:
        return jsonify(error="queue_unavailable"), 503


def admission_response(admission):
    payload = {
        "job_id": admission.task_id,
        "state": admission.state.value,
        "reason_code": admission.reason_code,
        "storage_mode": admission.storage_mode,
        "status_url": f"/api/admin/daily/jobs/{admission.task_id}" if admission.task_id else None,
    }
    status = (
        202
        if admission.state in {AdmissionState.ACCEPTED, AdmissionState.EXISTING}
        else 503
        if admission.state is AdmissionState.UNKNOWN
        or admission.reason_code in {"queue_starting", "queue_unavailable", "storage_unavailable"}
        else 429
    )
    return jsonify(payload), status


def register_daily_routes(app, require_auth, rate_limit_api):
    bp = Blueprint("daily_jobs", __name__)

    @bp.errorhandler(ValueError)
    async def invalid(error):
        return jsonify(error="invalid_daily_command"), 400

    @bp.errorhandler(PermissionError)
    async def forbidden(error):
        return jsonify(error="csrf_required"), 403

    @bp.errorhandler(QueueConflict)
    async def conflict(error):
        return jsonify(error=error.reason_code), 409

    @bp.errorhandler(QueueUnavailable)
    async def unavailable(error):
        return jsonify(error="queue_unavailable"), 503

    @bp.errorhandler(RuntimeError)
    async def source_unavailable(error):
        return jsonify(error="daily_source_unavailable"), 503

    @bp.post("/api/admin/daily/jobs/preview")
    @require_auth
    @rate_limit_api
    async def preview():
        _, command = await command_body()
        return jsonify(await admin_jobs.preview(command))

    @bp.post("/api/admin/daily/jobs")
    @require_auth
    @rate_limit_api
    async def enqueue():
        body, command = await command_body()
        return admission_response(await admin_jobs.enqueue(command, body.get("operation_id")))

    @bp.get("/api/admin/daily/jobs/by-operation/<operation_id>")
    @require_auth
    @rate_limit_api
    async def by_operation(operation_id):
        if not 8 <= len(operation_id) <= 128:
            raise ValueError("invalid_operation_id")
        return job_response(await task_queue.read_job_by_operation(0, f"daily:{operation_id}"))

    @bp.get("/api/admin/daily/jobs")
    @require_auth
    @rate_limit_api
    async def batch_status():
        operations = request.args.get("operations", "").split(",")
        if not 1 <= len(operations) <= 32 or any(not 8 <= len(value) <= 128 for value in operations):
            raise ValueError("invalid_operations")
        jobs = await asyncio.gather(
            *(task_queue.read_job_by_operation(0, f"daily:{operation}") for operation in operations),
            return_exceptions=True,
        )
        return jsonify(
            items=[
                {
                    "operation_id": operation,
                    "available": not isinstance(job, BaseException),
                    "job": job.as_dict()
                    if not isinstance(job, BaseException) and job is not None and job.kind in admin_jobs.KINDS
                    else None,
                }
                for operation, job in zip(operations, jobs, strict=True)
            ]
        )

    @bp.get("/api/admin/daily/jobs/<job_id>")
    @require_auth
    @rate_limit_api
    async def status(job_id):
        UUID(job_id)
        job = await task_queue.read_job(job_id)
        return job_response(job)

    def job_response(job):
        if job is None or job.kind not in admin_jobs.KINDS:
            return jsonify(error="job_not_found"), 404
        return jsonify(job.as_dict())

    @bp.get("/api/admin/daily/readiness")
    @require_auth
    @rate_limit_api
    async def readiness():
        from app.repos.crocodile_daily import today_puzzle_date

        start = date.fromisoformat(request.args.get("start") or today_puzzle_date().isoformat())
        return jsonify(await admin_jobs.readiness_calendar(start, int(request.args.get("days", 8))))

    app.register_blueprint(bp)
