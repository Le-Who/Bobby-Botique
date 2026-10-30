"""Daily Tarot preparation makes operation mode and model selection explicit."""

import asyncio
from datetime import date
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from app import tarot_daily as tarot
from app.process_policies import ResolvedPolicy
from app.queue_contracts import AdmissionState, QueueAdmission
from app.web import quart_app

DAY = date(2026, 10, 28)


@pytest.fixture(autouse=True)
def isolated_generation_ownership(monkeypatch):
    monkeypatch.setattr(tarot, "begin_generation", AsyncMock(return_value=object()))
    monkeypatch.setattr(tarot, "finish_generation", AsyncMock(return_value=True))


MODEL = "gemini-3.5-flash-lite"


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["missing", "replace"])
async def test_admin_passes_mode_model_language_to_managed_job(mode):
    prepare = AsyncMock(return_value=QueueAdmission(AdmissionState.ACCEPTED, "job-id"))
    client = quart_app.test_client()
    async with client.session_transaction() as session:
        session["authenticated"] = True
        session["controls_csrf"] = "synthetic"
    with patch("app.games.admin_jobs.enqueue", prepare):
        response = await client.post(
            "/api/admin/tarot/regenerate",
            headers={"X-CSRF-Token": "synthetic"},
            json={
                "date": DAY.isoformat(),
                "mode": mode,
                "model": MODEL,
                "language": "ru",
                "operation_id": "operation-123",
            },
        )
    assert response.status_code == 202
    command, operation = prepare.await_args.args
    assert (command.mode, command.model, command.language, command.date, operation) == (
        mode,
        MODEL,
        "ru",
        DAY.isoformat(),
        "operation-123",
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("field,value", [("mode", "destroy"), ("language", "invalid"), ("model", {"id": MODEL})])
async def test_invalid_input_never_calls_preparation(field, value):
    prepare = AsyncMock(return_value=tarot.TarotDailyPreparationResult(DAY))
    body = {"date": DAY.isoformat(), "mode": "missing", field: value}
    client = quart_app.test_client()
    async with client.session_transaction() as session:
        session["authenticated"] = True
        session["controls_csrf"] = "synthetic"
    with patch("app.games.admin_jobs.enqueue", prepare):
        response = await client.post("/api/admin/tarot/regenerate", json=body, headers={"X-CSRF-Token": "synthetic"})
    assert response.status_code == 400
    prepare.assert_not_awaited()


@pytest.mark.asyncio
async def test_explicit_operation_model_cannot_be_replaced_by_global_chain(monkeypatch):
    variant = {"name": "Маг", "orientation": "Прямая", "label": "Маг (Прямая)", "context": "synthetic"}
    seen = []

    async def execute(models, history, **kwargs):
        seen.append(models)
        kwargs["on_model_used"](models[0])
        return "Синтетический расклад"

    router = SimpleNamespace(execute_gemini_model_plan=execute, get_response=AsyncMock())
    monkeypatch.setattr(tarot, "get_provider_router", lambda: router)
    monkeypatch.setattr(tarot, "iter_daily_card_variants", lambda: [variant])
    monkeypatch.setattr(tarot, "count_gemini_keys", AsyncMock(return_value=4))
    monkeypatch.setattr(tarot, "_acquire_redis_lock", AsyncMock(return_value=None))
    monkeypatch.setattr(
        tarot,
        "resolve_process",
        AsyncMock(return_value=ResolvedPolicy(("gemini-global-primary", "gemini-global-reserve"), explicit=True)),
    )
    monkeypatch.setattr("app.repos.models_repo.get_models", AsyncMock(return_value=[MODEL]))
    write = AsyncMock()
    monkeypatch.setattr(tarot, "upsert_prepared_daily_reading", write)
    result = await tarot.prepare_daily_readings(target_date=DAY, force=True, model=MODEL, sleep=AsyncMock())
    assert result.generated == 1
    assert seen == [(MODEL,)]
    assert write.await_args.kwargs["model_name"] == MODEL


@pytest.mark.asyncio
@pytest.mark.parametrize("force,generated,skipped", [(False, 0, 156), (True, 156, 0)])
async def test_all_variants_missing_vs_replace_and_actual_model(monkeypatch, force, generated, skipped):
    actual = "gemini-3.5-flash"

    async def generate(**kwargs):
        kwargs["on_model_used"](actual)
        return "Синтетический расклад", 10

    router = SimpleNamespace(get_response=AsyncMock(side_effect=generate))
    monkeypatch.setattr(tarot, "get_provider_router", lambda: router)
    monkeypatch.setattr(tarot, "_acquire_redis_lock", AsyncMock(return_value=None))
    monkeypatch.setattr(tarot, "count_gemini_keys", AsyncMock(return_value=4))
    monkeypatch.setattr(tarot, "resolve_process", AsyncMock(return_value=ResolvedPolicy((MODEL,))))
    monkeypatch.setattr(tarot, "get_prepared_daily_reading", AsyncMock(return_value=object()))
    write = AsyncMock()
    monkeypatch.setattr(tarot, "upsert_prepared_daily_reading", write)
    result = await tarot.prepare_daily_readings(target_date=DAY, force=force, sleep=AsyncMock())
    assert (result.generated, result.skipped, result.failed, result.remaining) == (generated, skipped, 0, 0)
    assert router.get_response.await_count == generated
    assert write.await_count == generated
    assert all(call.kwargs["model_name"] == actual for call in write.await_args_list)


@pytest.mark.asyncio
@pytest.mark.parametrize("model", ["gemini-missing", "gemini-3.5-flash-image"])
async def test_unknown_or_wrong_capability_model_is_rejected_before_generation(monkeypatch, model):
    factory = AsyncMock()
    monkeypatch.setattr(tarot, "get_provider_router", factory)
    monkeypatch.setattr("app.repos.models_repo.get_models", AsyncMock(return_value=[MODEL, "gemini-3.5-flash-image"]))
    with pytest.raises(ValueError):
        await tarot.prepare_daily_readings(target_date=DAY, model=model)
    factory.assert_not_called()


@pytest.mark.asyncio
async def test_concurrent_preparation_reports_locked_without_waiting_for_provider(monkeypatch):
    entered, release = asyncio.Event(), asyncio.Event()

    async def generate(**kwargs):
        entered.set()
        await release.wait()
        return "Синтетический расклад", 3

    monkeypatch.setattr(tarot, "get_provider_router", lambda: SimpleNamespace(get_response=generate))
    monkeypatch.setattr(
        tarot,
        "iter_daily_card_variants",
        lambda: [{"name": "Маг", "orientation": "Прямая", "label": "Маг", "context": "synthetic"}],
    )
    monkeypatch.setattr(tarot, "_acquire_redis_lock", AsyncMock(return_value=None))
    monkeypatch.setattr(tarot, "count_gemini_keys", AsyncMock(return_value=4))
    monkeypatch.setattr(tarot, "resolve_process", AsyncMock(return_value=ResolvedPolicy((MODEL,))))
    monkeypatch.setattr(tarot, "upsert_prepared_daily_reading", AsyncMock())
    owner = asyncio.create_task(tarot.prepare_daily_readings(target_date=DAY, force=True))
    try:
        await asyncio.wait_for(entered.wait(), 5)
        conflict = await asyncio.wait_for(tarot.prepare_daily_readings(target_date=DAY, force=True), 1)
        assert conflict.locked and conflict.generated == 0
    finally:
        release.set()
        await owner


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["transient", "permanent"])
async def test_router_reports_actual_fallback_model_without_changing_chain(monkeypatch, failure):
    from app.errors import ErrorCode, tag_error
    from app.providers.router import ProviderRouter
    from tests.test_provider_router import FakeAgentRequestUseCase, FakeKeyStatusManager

    fallback = "gemini-3.5-flash"
    code = ErrorCode.TIMEOUT if failure == "transient" else ErrorCode.INVALID_KEY
    use_case = FakeAgentRequestUseCase(
        [
            ({"api_key": "fake-primary", "key_hash": "primary"}, MODEL, None),
            ({"api_key": "fake-fallback", "key_hash": "fallback"}, fallback, None),
        ],
        [(tag_error(code, "synthetic failure"), None), ("synthetic answer", 9)],
    )
    monkeypatch.setattr("app.agent_use_cases.AgentRequestUseCase", lambda: use_case)
    monkeypatch.setattr("app.repos.keys.get_key_status_manager", lambda: FakeKeyStatusManager())
    monkeypatch.setattr("app.providers.router._ordered_gemini_fallback_models", lambda _model: [fallback])
    router = ProviderRouter()
    monkeypatch.setattr(router, "_pick_transient_fallback_model", lambda *_args: fallback)
    seen = []
    text, _ = await router.get_response(
        MODEL, [{"role": "user", "parts": ["synthetic"]}], max_key_retries=1, on_model_used=seen.append
    )
    assert text == "synthetic answer"
    assert seen == [fallback]
