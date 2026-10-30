"""Daily browser model writes retain the revision the administrator edited."""

import copy
from datetime import date
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app import web
from app.runtime_settings import legacy_models, processes, store

PRIMARY = "gemini-2.5-pro"
RESERVE = "gemini-2.5-flash"


@pytest.fixture
def runtime_transport(monkeypatch):
    """Exercise the real versioned store, replacing only its SQL transport."""
    persisted = {"raw": None}

    async def query(sql, *params):
        if sql.startswith("SELECT"):
            return [{"value_data": persisted["raw"]}] if persisted["raw"] is not None else []
        if sql.startswith("INSERT"):
            if persisted["raw"] is not None:
                return []
        else:
            assert sql.startswith("UPDATE")
            if persisted["raw"] != params[2]:
                return []
        persisted["raw"] = params[1]
        return [{"value_data": params[1]}]

    monkeypatch.setattr(store, "_query", query)
    backend = store.RuntimeSettingsStore()
    for name in ("get_snapshot", "set_value", "update_values"):
        monkeypatch.setattr(store, name, getattr(backend, name))
    monkeypatch.setattr("app.runtime_settings.lifecycle.refresh_runtime_settings", AsyncMock())
    monkeypatch.setattr(
        "app.repos.settings_repo.get_global_setting", AsyncMock(side_effect=lambda key, default="": default)
    )
    monkeypatch.setattr("app.repos.settings_repo.set_global_setting", AsyncMock())
    monkeypatch.setattr(
        "app.providers.pollinations.fetch_models", AsyncMock(return_value=[{"id": "flux", "aliases": []}])
    )
    monkeypatch.setattr(web, "_is_authenticated", lambda: True)
    monkeypatch.setattr(web.settings, "AVAILABLE_MODELS", [PRIMARY, RESERVE])
    from app.providers.gemini import GeminiModelValidationStatus

    monkeypatch.setattr(
        "app.providers.gemini.validate_gemini_chat_model_capability",
        AsyncMock(return_value=GeminiModelValidationStatus.SUPPORTED),
    )
    return backend


@pytest.mark.asyncio
@pytest.mark.parametrize("process", ["", *legacy_models.CROC_ROLES])
async def test_stale_croc_tab_cannot_clear_new_primary_and_reserve(runtime_transport, process):
    backend = runtime_transport
    key = f"process:crocodile.{process or 'judge'}"
    installed = {"models": [PRIMARY, RESERVE], "strategy": "sequential", "inherit_user_model": False}
    await backend.set_value(key, installed, expected_revision=0, actor="other-tab")
    response = await web.quart_app.test_client().post(
        "/api/admin/dailycroc/text-model", json={"process": process, "model": "", "expected_revision": 0}
    )
    body = await response.get_json()
    assert response.status_code == 409 and body["reason"] == "revision_conflict"
    assert body["settings_revision"] == 1
    assert dict((await backend.get_snapshot(force=True)).values[key]) == {**installed, "models": (PRIMARY, RESERVE)}
    assert (await backend.get_snapshot()).revision == 1


@pytest.mark.asyncio
async def test_stale_trivia_tab_cannot_rebase_over_current_chain(runtime_transport):
    backend = runtime_transport
    await backend.set_value(
        "process:daily.trivia",
        {"models": [PRIMARY, RESERVE], "strategy": "sequential"},
        expected_revision=0,
        actor="other-tab",
    )
    response = await web.quart_app.test_client().post(
        "/api/admin/dailytrivia/settings", json={"llm_model": RESERVE, "expected_revision": 0}
    )
    body = await response.get_json()
    assert response.status_code == 409 and body["reason"] == "revision_conflict"
    assert body["settings"]["effective_models"] == [PRIMARY, RESERVE]
    assert (await backend.get_snapshot(force=True)).values["process:daily.trivia"]["models"] == (PRIMARY, RESERVE)


@pytest.mark.asyncio
@pytest.mark.parametrize("process", ["", *legacy_models.CROC_ROLES])
async def test_browser_model_write_requires_client_revision(runtime_transport, process):
    response = await web.quart_app.test_client().post(
        "/api/admin/dailycroc/text-model", json={"process": process, "model": PRIMARY}
    )
    assert response.status_code == 400
    assert (await runtime_transport.get_snapshot(force=True)).revision == 0


@pytest.mark.asyncio
async def test_browser_trivia_write_requires_client_revision_before_capability_call(runtime_transport, monkeypatch):
    validation = AsyncMock(side_effect=AssertionError("Missing revision must reject before provider validation"))
    monkeypatch.setattr("app.providers.gemini.validate_gemini_chat_model_capability", validation)
    response = await web.quart_app.test_client().post("/api/admin/dailytrivia/settings", json={"llm_model": PRIMARY})
    assert response.status_code == 400 and (await runtime_transport.get_snapshot(force=True)).revision == 0
    validation.assert_not_awaited()


@pytest.mark.asyncio
async def test_at_revision_core_retains_reserves_and_rejects_stale_auto(runtime_transport):
    initial = await runtime_transport.set_value(
        "process:crocodile.judge",
        {"models": [PRIMARY, RESERVE], "strategy": "sequential"},
        expected_revision=0,
        actor="other-tab",
    )
    with pytest.raises(store.RevisionConflict):
        await legacy_models.save_croc_model_at_revision("judge", "", expected_revision=0, actor="tab-a")
    saved = await processes.save_primary_model_at_revision(
        "crocodile.judge", RESERVE, expected_revision=initial.revision, actor="tab-b"
    )
    assert saved.revision == 2 and saved.values["process:crocodile.judge"]["models"] == (RESERVE,)


@pytest.mark.asyncio
async def test_models_get_reports_revision_effective_chain_and_source(runtime_transport):
    await runtime_transport.set_value(
        "process:crocodile.judge",
        {"models": [PRIMARY, RESERVE], "strategy": "sequential"},
        expected_revision=0,
        actor="other-tab",
    )
    response = await web.quart_app.test_client().get("/api/admin/dailycroc/models")
    body = await response.get_json()
    judge = next(item for item in body["text_processes"] if item["id"] == "judge")
    assert body["settings_revision"] == 1 and judge["effective_models"] == [PRIMARY, RESERVE]
    assert judge["source"] == "process"


@pytest.mark.asyncio
async def test_global_image_selector_uses_same_revision_and_reader_boundary(runtime_transport):
    from app.games.crocodile_daily import get_daily_image_model

    client = web.quart_app.test_client()
    first = await client.post(
        "/api/admin/dailycroc/image-default", json={"model": "fta-gpt-image-2", "expected_revision": 0}
    )
    assert first.status_code == 200 and (await first.get_json())["settings_revision"] == 1
    assert await get_daily_image_model() == "fta-gpt-image-2"
    stale = await client.post("/api/admin/dailycroc/image-default", json={"model": "flux", "expected_revision": 0})
    assert stale.status_code == 409
    assert (await stale.get_json())["settings"]["default_image_model"] == "fta-gpt-image-2"
    assert await get_daily_image_model() == "fta-gpt-image-2"
    assert (await runtime_transport.get_snapshot(force=True)).revision == 1


@pytest.mark.asyncio
async def test_immediate_image_command_uses_fresh_revision_and_keeps_operation_snapshot(runtime_transport):
    from app.games.crocodile_daily import get_daily_image_model
    from app.handlers.cmd_admin import set_dailycroc_model_command
    from app.runtime_settings.lifecycle import runtime_settings_scope

    first = await legacy_models.save_croc_image_model_at_revision("fta-gpt-image-2", expected_revision=0, actor="web")
    update = SimpleNamespace(message=SimpleNamespace(reply_text=AsyncMock()), effective_user=SimpleNamespace(id=1))
    async with runtime_settings_scope(first):
        await set_dailycroc_model_command.__wrapped__(update, SimpleNamespace(args=["pollinations"]))
        assert await get_daily_image_model() == "fta-gpt-image-2"
    assert await get_daily_image_model() == "qwen-image"
    assert (await runtime_transport.get_snapshot(force=True)).revision == 2
    assert "переключена" in update.message.reply_text.await_args.args[0]


@pytest.mark.asyncio
async def test_prompt_only_generation_setter_preserves_nondefault_image_model():
    from app.repos import crocodile_daily as repo
    from tests.test_daily_croc_generation_ownership import PreparationDatabase

    day = date(2027, 2, 5)
    database = PreparationDatabase(repo.DailyPuzzle(day, "кот", "Животные", "ru", image_model="flux"))
    ticket = await repo.begin_preparation(day, "easy", 1, conn=database)
    assert await repo.set_puzzle_image_prompt(ticket, "cat", conn=database)
    assert database.row["image_model"] == "flux" and database.row["image_prompt"] == "cat"


@pytest.mark.asyncio
@pytest.mark.parametrize("model,status", [(None, 200), ("fta-gpt-image-2", 200), ("not-a-model", 400)])
async def test_manual_prompt_model_validation_is_atomic(runtime_transport, monkeypatch, model, status):
    from contextlib import asynccontextmanager

    from app.repos import crocodile_daily as repo
    from tests.test_daily_croc_generation_ownership import PreparationDatabase

    day = date(2027, 2, 5)
    database = PreparationDatabase(repo.DailyPuzzle(day, "кот", "Животные", "ru", image_model="flux"))
    await repo.begin_preparation(day, "easy", 1, conn=database)

    @asynccontextmanager
    async def acquire():
        yield database

    monkeypatch.setattr(repo.db.db_manager, "pool", SimpleNamespace(acquire=acquire))
    before = copy.deepcopy(database.row)
    response = await web.quart_app.test_client().post(
        "/api/admin/dailycroc/prompt",
        json={
            "date": day.isoformat(),
            "difficulty": "easy",
            "prompt": "new prompt",
            "image_model": model,
            "expected_revision": 1,
        },
    )
    assert response.status_code == status
    if status == 200:
        assert database.row["image_model"] == (model or "flux")
        assert database.row["content_revision"] == 2 and database.row["generation_token"] is None
        assert database.row["image_prompt"] == "new prompt"
    else:
        assert database.row == before and database.writes == 0


@pytest.mark.browser
@pytest.mark.asyncio
async def test_model_conflicts_keep_local_selection_until_explicit_editor_refresh():
    from tests.test_natal_web_report import _run_browser_regression

    client = web.quart_app.test_client()
    async with client.session_transaction() as session:
        session["authenticated"] = True
    response = await client.get("/admin_daily")
    _run_browser_regression(
        {"html": (await response.get_data()).decode("utf-8"), "headers": dict(response.headers)},
        r"""
    page.setDefaultTimeout(2500);
    const old='gemini-2.5-flash', primary='gemini-2.5-pro';
    const posts=[];
    let revision=4;
    const state=()=>({settings_revision:revision,text_model:'',text_processes:[{id:'judge',label:'Проверка',model:revision===4?old:primary,effective_models:revision===4?[old]:[primary,old],source:'process'}]});
    await page.route('**/api/**', async route=>{
      const path=new URL(route.request().url()).pathname;
      if(path==='/api/admin/dailycroc/models') return route.fulfill({json:{...state(),text_models:[{id:'',title:'Auto'},{id:old},{id:primary}],image_models:[],default_image_model:'flux'}});
      if(path==='/api/admin/dailytrivia/stats') return route.fulfill({json:{settings_revision:4,llm_model:old,effective_models:[old],llm_models:[old,primary]}});
      if(path==='/api/admin/dailycroc/text-model'){
        const body=route.request().postDataJSON(); posts.push(body);
        if(posts.length===1){revision=5;return route.fulfill({status:409,json:{reason:'revision_conflict',settings_revision:5,settings:state()}});}
        revision=6;return route.fulfill({json:{success:true,model:body.model,process:body.process,settings_revision:6,settings:state()}});
      }
      if(path==='/api/admin/dailytrivia/settings'){
        posts.push(route.request().postDataJSON());
        return route.fulfill({status:409,json:{reason:'revision_conflict',settings_revision:7,settings:{llm_model:old,effective_models:[old,primary]}}});
      }
      return route.fulfill({json:{puzzles:[]}});
    });
    await page.goto('http://natal.test/admin_daily#trivia');
    await page.waitForFunction(()=>document.getElementById('trivia-model-input').value==='gemini-2.5-flash');
    await page.locator('[data-tab="croc"]').click();
    await page.waitForSelector('#croc-process-judge');
    const conflict=page.waitForResponse(response=>response.url().endsWith('/dailycroc/text-model'));
    await page.locator('#croc-process-judge').selectOption('');
    await conflict;
    await page.waitForTimeout(50);
    assert.equal(await page.locator('#croc-process-judge').inputValue(),'');
    assert.equal(posts[0].expected_revision,4);
    assert.match(await page.locator('#croc-model-status').textContent(),/gemini-2.5-pro.*gemini-2.5-flash/);
    await page.locator('[data-croc-action="loadPuzzles"]').first().click();
    await page.waitForTimeout(100);
    assert.equal(await page.locator('#croc-process-judge').inputValue(),'');
    assert.equal(posts.length,1,'Conflict/list refresh cannot retry or rebase a pending selection');
    await page.locator('#croc-settings-reload').click();
    await page.waitForFunction(()=>document.getElementById('croc-process-judge').value==='gemini-2.5-pro');
    await page.locator('#croc-process-judge').selectOption(old);
    await page.waitForFunction(()=>document.getElementById('croc-model-status').textContent.includes('r6'));
    assert.equal(posts[1].expected_revision,5);
    await page.locator('[data-tab="trivia"]').click();
    await page.locator('#trivia-model-input').fill(primary);
    await page.locator('#trivia-model-save').click();
    await page.waitForFunction(()=>document.getElementById('trivia-model-status').textContent.includes('Конфликт'));
    assert.equal(posts[2].expected_revision,6,'Confirmed same-tab save updates related forms');
    assert.equal(await page.locator('#trivia-model-input').inputValue(),primary);
    assert.match(await page.locator('#trivia-model-status').textContent(),/gemini-2.5-flash.*gemini-2.5-pro/);
    assert.equal(posts.length,3);
""",
    )


@pytest.mark.browser
@pytest.mark.asyncio
async def test_image_default_conflict_preserves_choice_and_requires_explicit_reload():
    from tests.test_natal_web_report import _run_browser_regression

    client = web.quart_app.test_client()
    async with client.session_transaction() as session:
        session["authenticated"] = True
    response = await client.get("/admin_daily")
    _run_browser_regression(
        {"html": (await response.get_data()).decode("utf-8"), "headers": dict(response.headers)},
        r"""
    page.setDefaultTimeout(2500);
    const writes=[];
    await page.route('**/api/**',route=>{
      const path=new URL(route.request().url()).pathname;
      if(path.endsWith('/dailycroc/models')) return route.fulfill({json:{settings_revision:4,text_models:[],text_processes:[],image_models:[{id:'flux'},{id:'fta-gpt-image-2'}],default_image_model:'flux'}});
      if(path.endsWith('/dailycroc/image-default')){writes.push(route.request().postDataJSON());return route.fulfill({status:409,json:{reason:'revision_conflict',settings_revision:5,settings:{default_image_model:'flux'}}});}
      return route.fulfill({json:{puzzles:[]}});
    });
    await page.goto('http://natal.test/admin_daily#croc');
    await page.waitForFunction(()=>!document.getElementById('croc-default-image-model').disabled);
    await page.locator('#croc-default-image-model').selectOption('fta-gpt-image-2');
    await page.waitForFunction(()=>document.getElementById('croc-model-status').textContent.includes('Конфликт'));
    assert.equal(await page.locator('#croc-default-image-model').inputValue(),'fta-gpt-image-2');
    assert.equal(writes[0].expected_revision,4);
    assert.match(await page.locator('#croc-model-status').textContent(),/flux/);
    await page.locator('#croc-settings-reload').click();
    await page.waitForFunction(()=>document.getElementById('croc-default-image-model').value==='flux');
    assert.equal(writes.length,1);
""",
    )
