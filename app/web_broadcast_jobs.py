"""Short authenticated broadcast commands; Telegram delivery belongs to workers."""

import asyncio
import re
from datetime import date
from uuid import UUID

from quart import Blueprint, jsonify, request
from werkzeug.exceptions import HTTPException

from app import broadcast_jobs
from app.web_daily_jobs import read_body

CHANNELS = frozenset({"daily_challenge", "horoscope", "tarot"})


def _operation(value):
    if not isinstance(value, str) or len(value) > 128:
        raise ValueError("invalid_operation_id")
    return str(UUID(value))


def _offers(body, *, single=False):
    ids = [body.get("user_id")] if single else body.get("user_ids")
    channel, force = body.get("channel"), body.get("force", False)
    if (
        not isinstance(ids, list)
        or not 1 <= len(ids) <= 100
        or any(type(uid) is not int or not 0 < uid <= 2**63 - 1 for uid in ids)
        or not isinstance(channel, str)
        or channel not in CHANNELS
        or type(force) is not bool
    ):
        raise ValueError("invalid_broadcast_command")
    return {"channel": channel, "user_ids": sorted(set(ids)), "force": force}


async def _admit(body, *, single=False):
    operation_id = _operation(body.get("operation_id"))
    command = _offers(body, single=single)
    try:
        async with asyncio.timeout(10):
            result = await broadcast_jobs.enqueue_offers(operation_id=operation_id, **command)
        return admission_response(result, operation_id)
    except TimeoutError:
        return unconfirmed(operation_id)


def unconfirmed(operation_id):
    return jsonify(
        admission={"state": "unknown", "reason_code": "admission_unconfirmed"},
        operation_id=operation_id,
        status_url=f"/api/admin/broadcast/jobs/by-operation/{operation_id}",
    ), 503


def admission_response(result, operation_id):
    admission = result["admission"]
    state = admission["state"]
    status = 202 if state in {"accepted", "existing"} else 429 if state == "rejected" else 503
    return jsonify(
        **result, operation_id=operation_id, status_url=f"/api/admin/broadcast/jobs/by-operation/{operation_id}"
    ), status


async def legacy_enqueue(*, single=False):
    try:
        return await _admit(await read_body(), single=single)
    except PermissionError:
        return jsonify(error="csrf_required"), 403
    except broadcast_jobs.BroadcastConflict as error:
        return jsonify(error=error.reason_code), 409
    except ValueError, TypeError:
        return jsonify(error="invalid_broadcast_command"), 400
    except Exception:
        # The caller must look up its existing operation; a journal/queue write
        # may already have committed even when its acknowledgment was lost.
        return jsonify(error="broadcast_command_unconfirmed", outcome_unknown=True), 503


async def journal_response():
    if any(len(request.args.getlist(key)) != 1 for key in request.args):
        raise ValueError("duplicate_filter")
    channel, day = request.args.get("channel") or None, request.args.get("date") or None
    state, reason = request.args.get("state") or None, request.args.get("reason") or None
    cursor = request.args.get("cursor") or None
    raw_limit = request.args.get("limit", "50")
    if channel is not None and channel not in CHANNELS:
        raise ValueError("invalid_channel")
    if day:
        day = date.fromisoformat(day).isoformat()
    if state is not None and state not in {"started", "sent", "failed", "skipped", "unknown_delivery"}:
        raise ValueError("invalid_state")
    if reason is not None and re.fullmatch(r"[a-z0-9_]{1,64}", reason) is None:
        raise ValueError("invalid_reason")
    if cursor:
        cursor = _operation(cursor)
    if not raw_limit.isascii() or not raw_limit.isdecimal() or not 1 <= int(raw_limit) <= 100:
        raise ValueError("invalid_limit")
    async with asyncio.timeout(5):
        return jsonify(
            await broadcast_jobs.list_delivery_events(
                channel=channel, date=day, state=state, reason=reason, cursor=cursor, limit=int(raw_limit)
            )
        )


def register_broadcast_routes(app, require_auth, rate_limit_api):
    bp = Blueprint("broadcast_jobs", __name__)

    @bp.errorhandler(PermissionError)
    async def forbidden(error):
        return jsonify(error="csrf_required"), 403

    @bp.errorhandler(broadcast_jobs.BroadcastConflict)
    async def conflict(error):
        return jsonify(error=error.reason_code), 409

    @bp.errorhandler(ValueError)
    async def invalid(error):
        return jsonify(error="invalid_broadcast_command"), 400

    @bp.errorhandler(RuntimeError)
    @bp.errorhandler(TimeoutError)
    @bp.errorhandler(Exception)
    async def unavailable(error):
        if isinstance(error, HTTPException):
            return error
        return jsonify(error="broadcast_source_unavailable"), 503

    @bp.post("/api/admin/broadcast/preview")
    @require_auth
    @rate_limit_api
    async def preview():
        command = _offers(await read_body())
        async with asyncio.timeout(5):
            return jsonify(await broadcast_jobs.preview_offers(**command))

    @bp.get("/api/admin/broadcast/jobs/by-operation/<operation_id>")
    @require_auth
    @rate_limit_api
    async def by_operation(operation_id):
        async with asyncio.timeout(5):
            batch = await broadcast_jobs.read_batch_by_operation(_operation(operation_id))
        return jsonify(batch) if batch else (jsonify(error="batch_not_found"), 404)

    @bp.get("/api/admin/broadcast/jobs")
    @require_auth
    @rate_limit_api
    async def batch_status():
        raw = request.args.get("operations", "").split(",")
        if not 1 <= len(raw) <= 10:
            raise ValueError("invalid_operations")
        operations = [_operation(value) for value in raw]
        async with asyncio.timeout(5):
            results = await asyncio.gather(
                *(broadcast_jobs.read_batch_by_operation(operation) for operation in operations),
                return_exceptions=True,
            )
        return jsonify(
            items=[
                {
                    "operation_id": operation,
                    "available": not isinstance(batch, BaseException),
                    "batch": batch if not isinstance(batch, BaseException) else None,
                }
                for operation, batch in zip(operations, results, strict=True)
            ]
        )

    @bp.get("/api/admin/broadcast/jobs/<batch_id>")
    @require_auth
    @rate_limit_api
    async def status(batch_id):
        async with asyncio.timeout(5):
            batch = await broadcast_jobs.read_batch(_operation(batch_id))
        return jsonify(batch) if batch else (jsonify(error="batch_not_found"), 404)

    @bp.post("/api/admin/broadcast/jobs/<batch_id>/retry")
    @require_auth
    @rate_limit_api
    async def retry(batch_id):
        batch_id = _operation(batch_id)
        operation_id = _operation((await read_body()).get("operation_id"))
        try:
            async with asyncio.timeout(10):
                result = await broadcast_jobs.retry_failed(batch_id, operation_id=operation_id)
            return admission_response(result, operation_id)
        except broadcast_jobs.BroadcastConflict:
            raise
        except Exception:
            return unconfirmed(operation_id)

    app.register_blueprint(bp)
