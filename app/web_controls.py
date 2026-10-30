"""Authenticated, versioned runtime control endpoints, separate from metrics."""

import asyncio
import hmac
import re
import secrets
import time
from collections.abc import Generator, Mapping
from typing import Any

from quart import Blueprint, jsonify, render_template, request, session
from quart.wrappers import Body, Request
from werkzeug.exceptions import RequestEntityTooLarge

from app import bot_instance
from app.command_aliases import (
    command_alias_inventory,
    reset_aliases,
    save_aliases,
    validate_alias_values,
    validate_aliases,
)
from app.observability.events import emit
from app.process_policies import PROCESSES, baseline_models, list_processes, resolve_policy_value, validate_policy
from app.runtime_settings import audio, models, prompts
from app.runtime_settings.store import (
    RevisionConflict,
    get_state,
    reset_value,
    restore_revision,
    set_value,
)

MAX_CONTROL_BODY_BYTES = 300_000
_QUEUE_ID = re.compile(r"[A-Za-z0-9_-]{1,128}\Z")
_OPERATION_ID = re.compile(r"[A-Za-z0-9_-]{8,128}\Z")
_PUBLIC_QUEUE_REASONS = frozenset(
    {
        "revision_conflict",
        "owner_or_revision_conflict",
        "operation_id_conflict",
        "retry_not_available",
        "privacy_revoked",
        "payload_not_available",
        "payload_expired",
        "cancel_requested",
        "attempts_exhausted",
        "retry_after_deadline",
        "deadline_expired",
        "owner_lost",
        "owner_expired",
        "worker_interrupted",
        "execution_interrupted",
        "delivery_unconfirmed",
        "document_storage_unconfirmed",
        "handler_failed",
        "admission_paused",
        "lane_capacity",
        "payload_capacity",
        "invalid_public_summary",
        "unknown_lane",
    }
)


def _queue_reason(value) -> str | None:
    return (
        value
        if isinstance(value, str) and value in _PUBLIC_QUEUE_REASONS
        else "job_outcome_recorded"
        if value
        else None
    )


def _public_job(snapshot) -> dict[str, Any]:
    from app.queue_contracts import JobState, public_summary

    data = snapshot.as_dict()
    # Do not expose new store fields implicitly or arbitrary result dictionaries.
    fields = {
        "task_id",
        "kind",
        "lane",
        "state",
        "revision",
        "attempts_started",
        "max_attempts",
        "submitted_at",
        "started_at",
        "finished_at",
        "not_before",
        "deadline_at",
        "heartbeat_at",
        "storage_mode",
        "parent_task_id",
    }
    result = {name: data[name] for name in fields}
    result["reason_code"] = _queue_reason(data.get("reason_code"))
    result["result_summary"] = public_summary(snapshot.result_summary)
    result["age_seconds"] = max(0, time.time() - snapshot.submitted_at)
    result["scope"] = "process" if snapshot.storage_mode == "local" else "shared"
    result["capabilities"] = {
        "cancel": snapshot.state in {JobState.QUEUED, JobState.DELAYED, JobState.RUNNING},
        "retry": snapshot.state in {JobState.FAILED, JobState.EXPIRED}
        and snapshot.retry_eligible
        and snapshot.retry_payload_available,
    }
    return result


def _queue_identifier(value: str) -> str:
    if not _QUEUE_ID.fullmatch(value):
        raise ValueError("Некорректный ID задания")
    return value


def _queue_lane(value: str) -> str:
    from app.queue_store import LANE_WORKERS

    if value not in LANE_WORKERS:
        raise ValueError("Неизвестная очередь")
    return value


async def _queue_command_body(*, lane_state: bool = False) -> dict[str, Any]:
    data = await _mutation_body()
    allowed = {"expected_revision", "operation_id"}
    if lane_state:
        allowed.update({"admission_paused", "claim_paused"})
    if data.keys() - allowed:
        raise ValueError("Неподдерживаемые поля команды. Пределы очереди доступны только для чтения.")
    operation_id = data.get("operation_id")
    if not isinstance(operation_id, str) or not _OPERATION_ID.fullmatch(operation_id):
        raise ValueError("Необходим operation_id длиной 8–128 символов")
    return data


class _ControlsBody(Body):
    """Wake the JSON reader as soon as streamed input crosses the controls bound."""

    def __init__(self, content_length: int | None) -> None:
        super().__init__(content_length, MAX_CONTROL_BODY_BYTES)
        self._received_bytes = 0
        self._oversized = False

    async def put(self, data: bytes) -> None:
        if not data or self._oversized:
            return
        self._received_bytes += len(data)
        if self._received_bytes > MAX_CONTROL_BODY_BYTES:
            self._oversized = True
            # Quart 0.23 checks the bound only after EOF. Wake its reader via
            # the public completion API; raising here would cancel ASGI's
            # request task before it could return a 413 response.
            self.set_complete()
            return
        await super().put(data)

    def __await__(self) -> Generator[Any, None, bytes]:
        data = yield from super().__await__()
        if self._oversized:
            raise RequestEntityTooLarge()
        return data


class ControlsRequest(Request):
    """Apply the mutation bound while ASGI receives even a chunked body."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        if self.path in {
            "/api/admin/controls",
            "/api/admin/daily/jobs",
            "/api/admin/daily/jobs/preview",
        } or self.path.startswith(("/api/admin/controls/", "/api/controls/", "/api/admin/broadcast/")):
            self.body = _ControlsBody(self.content_length)


def _csrf() -> str:
    token = session.get("controls_csrf")
    if not isinstance(token, str) or not token:
        token = secrets.token_urlsafe(32)
        session["controls_csrf"] = token
    return token


async def _mutation_body() -> dict[str, Any]:
    expected = session.get("controls_csrf", "")
    supplied = request.headers.get("X-CSRF-Token", "")
    if not expected or not hmac.compare_digest(str(expected).encode(), supplied.encode()):
        raise PermissionError("Обновите страницу: токен изменения отсутствует или устарел")
    if request.content_length is not None and request.content_length > MAX_CONTROL_BODY_BYTES:
        raise ValueError("Слишком большой запрос")
    raw = await request.get_data()
    if len(raw) > MAX_CONTROL_BODY_BYTES:
        raise RequestEntityTooLarge()
    data = await request.get_json(silent=True)
    if not isinstance(data, dict):
        raise ValueError("Ожидается JSON object")
    revision = data.get("expected_revision")
    if type(revision) is not int or revision < 0:
        raise ValueError("Необходим expected_revision")
    return data


def _validated(data: dict[str, Any]) -> tuple[str, str, Any]:
    section, identity = data.get("section"), data.get("id")
    if not isinstance(identity, str):
        raise ValueError("Необходим id")
    value = data.get("value")
    if section == "process":
        if identity not in PROCESSES:
            raise KeyError(identity)
        if not data.get("reset"):
            value = validate_policy(identity, value)
    elif section == "prompt":
        from app.prompt_registry import get_registry, validate_prompt_text

        registry = get_registry()
        registry.get_prompt_text(identity)
        if not data.get("reset"):
            validate_prompt_text(identity, value)
    elif section == "catalog":
        if identity not in models.PROVIDERS:
            raise KeyError(identity)
        if not data.get("reset"):
            value = models.validate_catalog(identity, value)
    elif section == "limit":
        models.validate_model_id(identity)
        if not data.get("reset"):
            value = models.validate_limit(identity, value)
    elif section == "audio":
        if identity not in audio.PROVIDERS:
            raise KeyError(identity)
        if not data.get("reset"):
            value = audio.validate_config(identity, value)
    elif section == "command":
        value = validate_aliases(bot_instance.get_application(), identity, [] if data.get("reset") else value)
    else:
        raise ValueError("Неизвестный раздел")
    return section, identity, value


def _validate_restored_values(values: Mapping[str, Any]) -> None:
    from app.prompt_registry import get_global_registry

    for key, value in values.items():
        section, _, identity = key.partition(":")
        if section == "process" and identity not in PROCESSES:
            continue
        if section == "prompt":
            try:
                get_global_registry().get_prompt_text(identity)
            except KeyError:
                # Retired values remain in history, without being applied.
                continue
        if section == "catalog_baseline":
            if identity not in models.PROVIDERS or value is not True:
                raise ValueError("Invalid catalog baseline marker")
            continue
        if section == "legacy_model":
            from app.runtime_settings.legacy_models import validate_value

            validate_value(identity, value)
            continue
        if section == "command_aliases":
            continue
        _validated({"section": "limit" if section == "model_limit" else section, "id": identity, "value": value})
    validate_alias_values(bot_instance.get_application(), values)


def register_controls(app, require_auth) -> None:
    app.request_class = ControlsRequest
    bp = Blueprint("controls", __name__)

    @bp.errorhandler(PermissionError)
    async def permission_error(error):
        return jsonify(error=str(error)), 403

    @bp.errorhandler(RevisionConflict)
    async def revision_error(error):
        return jsonify(error="Настройки изменились в другой вкладке. Обновите данные и сравните свой черновик."), 409

    @bp.errorhandler(ValueError)
    async def validation_error(error):
        return jsonify(error=str(error)), 400

    @bp.errorhandler(KeyError)
    async def unknown_error(error):
        return jsonify(error="Неизвестный процесс, промпт или провайдер"), 400

    @bp.route("/controls")
    @require_auth
    async def controls_page():
        return await render_template("controls.html", csrf_token=_csrf())

    @bp.route("/api/controls/queues")
    @require_auth
    async def queues_overview():
        from app.observability.load_metrics import load_snapshot
        from app.queue import task_queue

        load = load_snapshot()
        try:
            async with asyncio.timeout(5):
                queue = await task_queue.get_queue_stats()
        except Exception:
            queue = {"available": False, "reason_code": "source_unavailable", "scope": "unknown", "lanes": {}}
        for lane in queue.get("lanes", {}).values():
            lane["capabilities"] = {"set_state": lane.get("available") is True}
        return jsonify(
            queue=queue,
            load=load,
            observed_at=time.time(),
            capabilities={"queue_actions": True, "process_load_actions": False, "policy_edit": False},
        )

    @bp.route("/api/controls/jobs")
    @require_auth
    async def queue_jobs():
        from app.queue import task_queue
        from app.queue_contracts import JobState

        if any(len(request.args.getlist(key)) != 1 for key in request.args):
            raise ValueError("Повторяющиеся параметры фильтра")
        lane = request.args.get("lane") or None
        if lane is not None:
            _queue_lane(lane)
        raw_state = request.args.get("state") or None
        if raw_state is not None and raw_state not in {item.value for item in JobState}:
            raise ValueError("Неизвестное состояние задания")
        cursor = request.args.get("cursor") or None
        if cursor is not None and (not cursor.isascii() or not cursor.isdecimal() or len(cursor) > 12):
            raise ValueError("Некорректный курсор")
        raw_limit = request.args.get("limit", "50")
        if not raw_limit.isascii() or not raw_limit.isdecimal() or not 1 <= int(raw_limit) <= 100:
            raise ValueError("Размер страницы должен быть от 1 до 100")
        try:
            async with asyncio.timeout(5):
                page = await task_queue.list_jobs(
                    lane=lane, state=JobState(raw_state) if raw_state else None, cursor=cursor, limit=int(raw_limit)
                )
            items = [_public_job(item) for item in page.items]
            scopes = {item["scope"] for item in items}
            return jsonify(
                items=items,
                next_cursor=page.next_cursor,
                available=True,
                observed_at=time.time(),
                scope=next(iter(scopes)) if len(scopes) == 1 else "mixed" if scopes else "unknown",
            )
        except Exception:
            return jsonify(available=False, reason_code="source_unavailable", items=[], next_cursor=None), 503

    @bp.route("/api/controls/jobs/<task_id>")
    @require_auth
    async def queue_job(task_id):
        from app.queue import task_queue

        _queue_identifier(task_id)
        try:
            async with asyncio.timeout(5):
                job = await task_queue.read_job(task_id)
            if job is None:
                return jsonify(available=False, reason_code="job_not_found"), 404
            return jsonify(job=_public_job(job), available=True, observed_at=time.time())
        except Exception:
            return jsonify(available=False, reason_code="source_unavailable"), 503

    @bp.route("/api/controls/jobs/<task_id>/<action>", methods=["POST"])
    @require_auth
    async def queue_job_command(task_id, action):
        from app.queue import task_queue
        from app.queue_contracts import AdmissionState, QueueConflict

        _queue_identifier(task_id)
        if action not in {"cancel", "retry"}:
            raise ValueError("Неизвестная команда задания")
        data = await _queue_command_body()
        try:
            async with asyncio.timeout(5):
                result = await (task_queue.request_cancel if action == "cancel" else task_queue.retry_job)(
                    task_id,
                    expected_revision=data["expected_revision"],
                    actor="admin",
                    operation_id=data["operation_id"],
                )
            revision = result.revision if action == "cancel" else data["expected_revision"]
            confirmed = action == "cancel" or result.state is not AdmissionState.UNKNOWN
            emit(
                "admin.queue_command",
                actor_ref="configured_admin",
                action=action,
                job_id=task_id,
                previous_revision=data["expected_revision"],
                new_revision=revision if confirmed else None,
                reason_code="owner_confirmed" if confirmed else "command_unconfirmed",
            )
            if action == "cancel":
                return jsonify(confirmed=True, job=_public_job(result), observed_at=time.time())
            source_job = None
            if result.state in {AdmissionState.ACCEPTED, AdmissionState.EXISTING}:
                try:
                    async with asyncio.timeout(1):
                        source = await task_queue.read_job(task_id)
                    source_job = _public_job(source) if source is not None else None
                except Exception:
                    pass  # An optional read cannot undo confirmed admission.
            return jsonify(
                confirmed=confirmed,
                source_job=source_job,
                observed_at=time.time(),
                admission={
                    "state": result.state.value,
                    "task_id": result.task_id,
                    "reason_code": _queue_reason(result.reason_code),
                    "storage_mode": result.storage_mode,
                },
            ), (
                200
                if result.state in {AdmissionState.ACCEPTED, AdmissionState.EXISTING}
                else 409
                if result.state is AdmissionState.REJECTED
                else 503
            )
        except QueueConflict as error:
            emit(
                "admin.queue_command",
                actor_ref="configured_admin",
                action=action,
                job_id=task_id,
                previous_revision=data["expected_revision"],
                new_revision=None,
                reason_code=_queue_reason(error.reason_code),
            )
            return jsonify(confirmed=False, reason_code=_queue_reason(error.reason_code)), 409
        except Exception:
            emit(
                "admin.queue_command",
                actor_ref="configured_admin",
                action=action,
                job_id=task_id,
                previous_revision=data["expected_revision"],
                new_revision=None,
                reason_code="command_unconfirmed",
            )
            return jsonify(confirmed=False, reason_code="command_unconfirmed"), 503

    @bp.route("/api/controls/queues/<lane>/state", methods=["POST"])
    @require_auth
    async def queue_lane_command(lane):
        from app.queue import task_queue
        from app.queue_contracts import QueueConflict

        _queue_lane(lane)
        data = await _queue_command_body(lane_state=True)
        if type(data.get("admission_paused")) is not bool or type(data.get("claim_paused")) is not bool:
            raise ValueError("Оба режима паузы должны быть boolean")
        try:
            async with asyncio.timeout(5):
                result = await task_queue.set_lane_state(
                    lane,
                    expected_revision=data["expected_revision"],
                    admission_paused=data["admission_paused"],
                    claim_paused=data["claim_paused"],
                    actor="admin",
                    operation_id=data["operation_id"],
                )
            emit(
                "admin.queue_command",
                actor_ref="configured_admin",
                action="set_lane_state",
                lane=lane,
                previous_revision=data["expected_revision"],
                new_revision=result.revision,
                reason_code="owner_confirmed",
            )
            return jsonify(
                confirmed=True,
                observed_at=time.time(),
                scope="unknown",
                reason_code="owner_confirmed",
                capabilities={"set_state": True, "policy_edit": False},
                lane={
                    "lane": result.lane,
                    "revision": result.revision,
                    "admission_paused": result.admission_paused,
                    "claim_paused": result.claim_paused,
                    "workers": result.workers,
                    "policy": result.policy.as_dict(),
                },
            )
        except QueueConflict as error:
            emit(
                "admin.queue_command",
                actor_ref="configured_admin",
                action="set_lane_state",
                lane=lane,
                previous_revision=data["expected_revision"],
                new_revision=None,
                reason_code=_queue_reason(error.reason_code),
            )
            return jsonify(confirmed=False, reason_code=_queue_reason(error.reason_code)), 409
        except Exception:
            emit(
                "admin.queue_command",
                actor_ref="configured_admin",
                action="set_lane_state",
                lane=lane,
                previous_revision=data["expected_revision"],
                new_revision=None,
                reason_code="command_unconfirmed",
            )
            return jsonify(confirmed=False, reason_code="command_unconfirmed"), 503

    @bp.route("/api/admin/controls", methods=["GET", "POST"])
    @require_auth
    async def controls_api():
        if request.method == "GET":
            from app.runtime_settings.lifecycle import load_controlled_prompts, runtime_settings_scope

            await asyncio.to_thread(load_controlled_prompts)
            snapshot, history = await get_state(force=True)
            async with runtime_settings_scope(snapshot):
                catalogs = await models.list_catalogs(snapshot=snapshot)
                processes = await list_processes()
                return jsonify(
                    revision=snapshot.revision,
                    degraded=snapshot.degraded,
                    csrf_token=_csrf(),
                    processes=processes,
                    prompts=await prompts.list_prompts(),
                    catalogs=catalogs,
                    audio=await audio.list_audio_configs(snapshot=snapshot),
                    limits=await models.list_limits(snapshot=snapshot, processes=processes),
                    commands=command_alias_inventory(bot_instance.get_application(), snapshot),
                    history=[
                        {**row, "created_at": row.get("at"), "changed_key": ", ".join(row.get("changed_keys", []))}
                        for row in history
                    ],
                )
        data = await _mutation_body()
        section, identity, value = _validated(data)
        options = {"expected_revision": data["expected_revision"], "actor": "admin"}
        try:
            if section == "prompt":
                snapshot = (
                    await prompts.reset_prompt(identity, **options)
                    if data.get("reset")
                    else await prompts.save_prompt(identity, value, **options)
                )
            elif section == "catalog":
                snapshot = (
                    await models.reset_catalog(identity, **options)
                    if data.get("reset")
                    else await models.save_catalog(identity, value, **options)
                )
            elif section == "limit":
                snapshot = (
                    await models.reset_limit(identity, **options)
                    if data.get("reset")
                    else await models.save_limit(identity, value, **options)
                )
            elif section == "audio":
                snapshot = (
                    await audio.reset_config(identity, **options)
                    if data.get("reset")
                    else await audio.save_config(identity, value, **options)
                )
            elif section == "command":
                snapshot = (
                    await reset_aliases(bot_instance.get_application(), identity, **options)
                    if data.get("reset")
                    else await save_aliases(bot_instance.get_application(), identity, value, **options)
                )
            else:
                key = f"process:{identity}"
                snapshot = (
                    await reset_value(key, **options) if data.get("reset") else await set_value(key, value, **options)
                )
        except ValueError, KeyError:
            raise
        except Exception:
            # Provider/DB errors may contain connection details. Never return them.
            return jsonify(
                error="Не удалось сохранить настройки. Данные не подтверждены; обновите состояние перед повтором."
            ), 503
        return jsonify(revision=snapshot.revision)

    @bp.route("/api/admin/controls/commands", methods=["GET"])
    @require_auth
    async def controls_commands():
        from app.runtime_settings.store import get_snapshot

        snapshot = await get_snapshot(force=True)
        return jsonify(command_alias_inventory(bot_instance.get_application(), snapshot))

    @bp.route("/api/admin/controls/preview", methods=["POST"])
    @require_auth
    async def controls_preview():
        data = await _mutation_body()
        section, identity, value = _validated(data)
        planned_models = []
        if section == "command":
            from app.command_aliases import PREFIX

            snapshot, _ = await get_state(force=True)
            values = dict(snapshot.values)
            if data.get("reset"):
                values.pop(f"{PREFIX}{identity}", None)
            else:
                values[f"{PREFIX}{identity}"] = value
            validate_alias_values(bot_instance.get_application(), values)
        if section == "process":
            baseline = await baseline_models(identity)
            selected = data.get("selected_model")
            if selected is not None:
                baseline = (models.validate_model_id(selected),)
            planned_models = list(resolve_policy_value(identity, None if data.get("reset") else value, baseline).models)
        return jsonify(
            valid=True,
            models=planned_models,
            notes=(
                ["Проверены формат и конфликты алиасов. Основная команда и проверки доступа сохраняются."]
                if section == "command"
                else ["Проверена структура. Доступность модели у провайдера и остаток внешней квоты не проверялись."]
            ),
        )

    @bp.route("/api/admin/controls/restore", methods=["POST"])
    @require_auth
    async def controls_restore():
        data = await _mutation_body()
        revision = data.get("revision")
        if type(revision) is not int or revision < 0:
            raise ValueError("Некорректная версия")
        from app.runtime_settings.lifecycle import load_controlled_prompts

        await asyncio.to_thread(load_controlled_prompts)
        try:
            snapshot = await restore_revision(
                revision, expected_revision=data["expected_revision"], actor="admin", validate=_validate_restored_values
            )
        except ValueError, KeyError:
            raise
        except Exception:
            return jsonify(
                error="Не удалось подтвердить восстановление настроек. Обновите состояние перед повтором."
            ), 503
        await prompts.refresh_prompts(force=True)
        await models.refresh_catalogs()
        # A restore can remove overrides as well as add them. Clear all cached
        # admissions so restored limits take effect on the next attempt.
        from app.repos.keys import db_manager, invalidate_model_limit_cache

        for model in set(db_manager._model_config_cache) | {
            key.removeprefix("model_limit:") for key in snapshot.values if key.startswith("model_limit:")
        }:
            await invalidate_model_limit_cache(model)
        return jsonify(revision=snapshot.revision)

    app.register_blueprint(bp)
