"""Authenticated, versioned runtime control endpoints, separate from metrics."""

import hmac
import secrets
from collections.abc import Mapping
from typing import Any

from quart import Blueprint, jsonify, render_template, request, session
from quart.wrappers import Request
from werkzeug.exceptions import RequestEntityTooLarge

from app.process_policies import PROCESSES, baseline_models, list_processes, resolve_policy_value, validate_policy
from app.runtime_settings import models, prompts
from app.runtime_settings.store import (
    RevisionConflict,
    get_state,
    reset_value,
    restore_revision,
    set_value,
)

MAX_CONTROL_BODY_BYTES = 300_000


class ControlsRequest(Request):
    """Apply the mutation bound while ASGI receives even a chunked body."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        if self.path == "/api/admin/controls" or self.path.startswith("/api/admin/controls/"):
            self.body = self.body_class(self.content_length, MAX_CONTROL_BODY_BYTES)


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
    else:
        raise ValueError("Неизвестный раздел")
    return section, identity, value


def _validate_restored_values(values: Mapping[str, Any]) -> None:
    for key, value in values.items():
        section, _, identity = key.partition(":")
        if section == "catalog_baseline":
            if identity not in models.PROVIDERS or value is not True:
                raise ValueError("Invalid catalog baseline marker")
            continue
        if section == "legacy_model":
            from app.runtime_settings.legacy_models import validate_value

            validate_value(identity, value)
            continue
        _validated({"section": "limit" if section == "model_limit" else section, "id": identity, "value": value})


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

    @bp.route("/api/admin/controls", methods=["GET", "POST"])
    @require_auth
    async def controls_api():
        if request.method == "GET":
            from app.runtime_settings.lifecycle import load_controlled_prompts, runtime_settings_scope

            load_controlled_prompts()
            snapshot, history = await get_state(force=True)
            async with runtime_settings_scope(snapshot):
                return jsonify(
                    revision=snapshot.revision,
                    degraded=snapshot.degraded,
                    csrf_token=_csrf(),
                    processes=await list_processes(),
                    prompts=await prompts.list_prompts(),
                    catalogs=await models.list_catalogs(snapshot=snapshot),
                    limits=await models.list_limits(snapshot=snapshot),
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

    @bp.route("/api/admin/controls/preview", methods=["POST"])
    @require_auth
    async def controls_preview():
        data = await _mutation_body()
        section, identity, value = _validated(data)
        planned_models = []
        if section == "process":
            baseline = await baseline_models(identity)
            selected = data.get("selected_model")
            if selected is not None:
                baseline = (models.validate_model_id(selected),)
            planned_models = list(resolve_policy_value(identity, None if data.get("reset") else value, baseline).models)
        return jsonify(
            valid=True,
            models=planned_models,
            notes=["Проверена структура. Доступность модели у провайдера и остаток внешней квоты не проверялись."],
        )

    @bp.route("/api/admin/controls/restore", methods=["POST"])
    @require_auth
    async def controls_restore():
        data = await _mutation_body()
        revision = data.get("revision")
        if type(revision) is not int or revision < 0:
            raise ValueError("Некорректная версия")
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
