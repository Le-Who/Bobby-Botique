"""Trivia drafts survive independent writes and explicit navigation choices."""

import pytest

from app.web import quart_app
from tests.test_natal_web_report import _run_browser_regression


def _run_state_browser(payload, scenario):
    """Keep draft-state probes independent of the shared queue transport contract.

    D6's test_daily_jobs_browser exercises the real queue controller. Here only
    the external job boundary maps onto each scenario's synthetic provider/API.
    """
    _run_browser_regression(
        payload,
        r"""
    await page.addInitScript(()=>document.addEventListener('DOMContentLoaded',()=>{
      const core=window.DailyJobs;
      const original=core.run;
      core.run=async(command,{onTerminal,confirmationNote}={})=>{
        if(!command.kind.startsWith('trivia.'))return original(command,{onTerminal,confirmationNote});
        if(command.kind==='trivia.regenerate'&&!await window.showConfirm('Регенерация',confirmationNote || 'Генерировать?'))return null;
        const isSave=command.kind==='trivia.validate_save';
        const url=isSave?(command.part==='main'?'/api/admin/dailytrivia/save':'/api/admin/dailytrivia/save_super'):'/api/admin/dailytrivia/regenerate';
        const response=await window.fetch(url,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({date:command.date,revision:command.expected_revision,mode:command.part,
          ...(isSave?(command.part==='main'?{questions:command.questions}:{super_questions:command.questions}):{})})});
        const data=await response.json();
        await onTerminal({kind:command.kind,state:response.ok?'succeeded':'failed',reason_code:response.status===409?'revision_conflict':null,
          result_summary:{date:command.date,puzzle_revision:data.puzzle?.revision}});
        return {jobId:'synthetic-state-transport'};
      };
    },{once:true}));
"""
        + scenario,
    )


@pytest.mark.browser
@pytest.mark.asyncio
@pytest.mark.parametrize("saved_section", ["main", "super"])
async def test_saving_one_section_preserves_dirty_sibling_and_its_next_revision(saved_section):
    client = quart_app.test_client()
    async with client.session_transaction() as session:
        session["authenticated"] = True
    response = await client.get("/admin_daily")
    _run_state_browser(
        {
            "html": (await response.get_data()).decode("utf-8"),
            "headers": dict(response.headers),
            "saved_section": saved_section,
        },
        r"""
    page.setDefaultTimeout(2500);
    const question=(id,prefix)=>({id,topic:'Наука',question:`${prefix} ${id}`,options:['Да','Нет','Три','Четыре'],correct_index:0,explanation:'Объяснение'});
    let puzzle={date:'2026-10-28',revision:1,status:'ready',questions:Array.from({length:5},(_,i)=>question(i+1,'Main')),super_questions:Array.from({length:3},(_,i)=>question(i+1,'Super'))};
    const writes=[];
    await page.route('**/api/**',route=>{
      const path=new URL(route.request().url()).pathname;
      if(path==='/api/admin/dailytrivia')return route.fulfill({json:{puzzles:[puzzle],today:puzzle.date}});
      if(path.endsWith('/save')||path.endsWith('/save_super')){
        const data=route.request().postDataJSON();writes.push(data);
        puzzle={...puzzle,revision:puzzle.revision+1,...(data.questions?{questions:data.questions}:{super_questions:data.super_questions})};
        return route.fulfill({json:{success:true,puzzle}});
      }
      return route.fulfill({json:{}});
    });
    await page.goto('http://natal.test/admin_daily#trivia');
    await page.locator('#trivia-puzzles-list [data-date="2026-10-28"]').click();
    const sibling=payload.saved_section==='main'?'.t-sq-text[data-idx="0"]':'.t-q-text[data-idx="0"]';
    const own=payload.saved_section==='main'?'.t-q-text[data-idx="0"]':'.t-sq-text[data-idx="0"]';
    await page.locator(sibling).fill('UNSAVED SIBLING');
    await page.locator(own).fill('SAVED SECTION');
    const response=page.waitForResponse(res=>res.url().endsWith(payload.saved_section==='main'?'/save':'/save_super'));
    await page.locator(payload.saved_section==='main'?'#trivia-btn-save':'#trivia-btn-save-super').click();
    await response;
    await page.waitForTimeout(100);
    assert.equal(await page.locator(sibling).inputValue(),'UNSAVED SIBLING');
    assert.equal(writes[0].revision,1);
    assert.equal(Object.hasOwn(writes[0],payload.saved_section==='main'?'super_questions':'questions'),false);
    assert.match(await page.locator('#trivia-editor-status').textContent(),/несохран/);
    await page.locator(payload.saved_section==='main'?'#trivia-btn-save-super':'#trivia-btn-save').click();
    await page.waitForFunction(()=>document.getElementById('trivia-editor-title').textContent.includes('3'));
    assert.equal(writes[1].revision,2);
    assert.equal((writes[1].questions||writes[1].super_questions)[0].question,'UNSAVED SIBLING');
""",
    )


@pytest.mark.browser
@pytest.mark.asyncio
async def test_dirty_close_can_cancel_without_losing_selection_or_focus():
    client = quart_app.test_client()
    async with client.session_transaction() as session:
        session["authenticated"] = True
    response = await client.get("/admin_daily")
    _run_state_browser(
        {"html": (await response.get_data()).decode("utf-8"), "headers": dict(response.headers)},
        r"""
    page.setDefaultTimeout(2500);
    const q={id:1,topic:'Наука',question:'Original',options:['A','B','C','D'],correct_index:0,explanation:'Text'};
    await page.route('**/api/**',route=>route.fulfill({json:{puzzles:[{date:'2026-10-28',revision:1,questions:[q],super_questions:[q]}],today:'2026-10-28'}}));
    await page.goto('http://natal.test/admin_daily#trivia');
    await page.locator('#trivia-puzzles-list [data-date="2026-10-28"]').click();
    await page.locator('.t-q-text[data-idx="0"]').fill('DIRTY MAIN');
    await page.locator('.t-sq-text[data-idx="0"]').fill('DIRTY SUPER');
    await page.locator('.t-sq-text[data-idx="0"]').focus();
    await page.locator('#trivia-btn-close').click();
    assert.equal(await page.locator('#confirm-modal').isVisible(),true,'Dirty navigation must offer an explicit choice');
    await page.locator('#modal-cancel').click();
    assert.equal(await page.locator('#trivia-editor-pane').isVisible(),true);
    assert.equal(await page.locator('.t-q-text[data-idx="0"]').inputValue(),'DIRTY MAIN');
    assert.equal(await page.locator('.t-sq-text[data-idx="0"]').inputValue(),'DIRTY SUPER');
    assert.equal(await page.locator('.t-sq-text[data-idx="0"]').evaluate(node=>node===document.activeElement),true);
""",
    )


@pytest.mark.browser
@pytest.mark.asyncio
async def test_conflict_list_reload_and_explicit_server_choice_preserve_then_replace_drafts():
    client = quart_app.test_client()
    async with client.session_transaction() as session:
        session["authenticated"] = True
    response = await client.get("/admin_daily")
    _run_state_browser(
        {"html": (await response.get_data()).decode("utf-8"), "headers": dict(response.headers)},
        r"""
    page.setDefaultTimeout(2500);
    const q=question=>({id:1,topic:'Наука',question,options:['A','B','C','D'],correct_index:0,explanation:'Text'});
    let puzzle={date:'2026-10-28',revision:1,questions:[q('Original main')],super_questions:[q('Original super')]};
    let writes=0;
    await page.route('**/api/**',route=>{
      const path=new URL(route.request().url()).pathname;
      if(path==='/api/admin/dailytrivia')return route.fulfill({json:{puzzles:[puzzle],today:puzzle.date}});
      if(path.endsWith('/save')){writes++;puzzle={...puzzle,revision:2,questions:[q('SERVER MAIN')],super_questions:[q('SERVER SUPER')]};return route.fulfill({status:409,json:{current_revision:2,puzzle}});}
      return route.fulfill({json:{}});
    });
    await page.goto('http://natal.test/admin_daily#trivia');
    await page.locator('#trivia-puzzles-list [data-date="2026-10-28"]').click();
    await page.locator('.t-q-text[data-idx="0"]').fill('LOCAL MAIN');
    await page.locator('.t-sq-text[data-idx="0"]').fill('LOCAL SUPER');
    await page.locator('#trivia-btn-save').click();
    await page.waitForFunction(()=>!document.getElementById('trivia-editor-conflict').hidden);
    assert.equal(await page.locator('.t-q-text[data-idx="0"]').inputValue(),'LOCAL MAIN');
    assert.equal(await page.locator('.t-sq-text[data-idx="0"]').inputValue(),'LOCAL SUPER');
    assert.match(await page.locator('#trivia-editor-title').textContent(),/ревизия 1/);
    assert.match(await page.locator('#trivia-editor-comparison').textContent(),/SERVER MAIN.*SERVER SUPER/);
    await page.locator('#trivia-editor-continue').click();
    assert.equal(await page.locator('.t-sq-text[data-idx="0"]').evaluate(node=>node===document.activeElement),true);
    await page.locator('#trivia-btn-save-super').click();
    assert.equal(writes,1,'Conflict cannot automatically merge, rebase or POST again');
    const reload=page.waitForResponse(response=>new URL(response.url()).pathname==='/api/admin/dailytrivia');
    await page.locator('#trivia-editor-reload').click();
    await reload;
    await page.waitForTimeout(50);
    assert.equal(await page.locator('.t-q-text[data-idx="0"]').inputValue(),'SERVER MAIN');
    assert.equal(await page.locator('.t-sq-text[data-idx="0"]').inputValue(),'SERVER SUPER');
    assert.match(await page.locator('#trivia-editor-title').textContent(),/ревизия 2/);
""",
    )


@pytest.mark.browser
@pytest.mark.asyncio
async def test_save_response_retains_typing_started_after_submission():
    client = quart_app.test_client()
    async with client.session_transaction() as session:
        session["authenticated"] = True
    response = await client.get("/admin_daily")
    _run_state_browser(
        {"html": (await response.get_data()).decode("utf-8"), "headers": dict(response.headers)},
        r"""
    page.setDefaultTimeout(2500);
    const q={id:1,topic:'Наука',question:'Original',options:['A','B','C','D'],correct_index:0,explanation:'Text'};
    let puzzle={date:'2026-10-28',revision:1,questions:[q],super_questions:[q]};
    let pending,sentMain;
    await page.route('**/api/**',route=>{
      const path=new URL(route.request().url()).pathname;
      if(path==='/api/admin/dailytrivia')return route.fulfill({json:{puzzles:[puzzle],today:puzzle.date}});
      if(path.endsWith('/save')){sentMain=route.request().postDataJSON();pending=route;return;}
      return route.fulfill({json:{}});
    });
    await page.goto('http://natal.test/admin_daily#trivia');
    await page.locator('#trivia-puzzles-list [data-date="2026-10-28"]').click();
    await page.locator('.t-q-text[data-idx="0"]').fill('SUBMITTED MAIN');
    await page.locator('.t-sq-text[data-idx="0"]').fill('UNSAVED SUPER');
    await page.locator('#trivia-btn-save').click();
    while(!pending)await new Promise(resolve=>setTimeout(resolve,5));
    await page.locator('.t-q-text[data-idx="0"]').fill('NEW TYPING');
    puzzle={...puzzle,revision:2,questions:sentMain.questions};
    await pending.fulfill({json:{success:true,puzzle}});
    await page.waitForFunction(()=>document.getElementById('trivia-editor-title').textContent.includes('ревизия 2'));
    assert.equal(await page.locator('.t-q-text[data-idx="0"]').inputValue(),'NEW TYPING');
    assert.equal(await page.locator('.t-sq-text[data-idx="0"]').inputValue(),'UNSAVED SUPER');
    assert.match(await page.locator('#trivia-editor-status').textContent(),/обычные несохранены.*супер несохранены/);
""",
    )


@pytest.mark.browser
@pytest.mark.asyncio
async def test_dirty_drafts_survive_date_navigation_and_neighbor_regeneration():
    client = quart_app.test_client()
    async with client.session_transaction() as session:
        session["authenticated"] = True
    response = await client.get("/admin_daily")
    _run_state_browser(
        {"html": (await response.get_data()).decode("utf-8"), "headers": dict(response.headers)},
        r"""
    page.setDefaultTimeout(2500);
    const q=question=>({id:1,topic:'Наука',question,options:['A','B','C','D'],correct_index:0,explanation:'Text'});
    let first={date:'2026-10-28',revision:1,questions:[q('Main')],super_questions:[q('Super')]};
    const second={...first,date:'2026-10-29'};
    let writes=0;
    await page.route('**/api/**',route=>{
      const path=new URL(route.request().url()).pathname;
      if(path==='/api/admin/dailytrivia')return route.fulfill({json:{puzzles:[first,second],today:first.date}});
      if(path.endsWith('/regenerate')){writes++;assert.equal(route.request().postDataJSON().mode,'super');first={...first,revision:2,super_questions:[q('GENERATED SUPER')]};return route.fulfill({json:{success:true,puzzle:first}});}
      return route.fulfill({json:{}});
    });
    await page.goto('http://natal.test/admin_daily#trivia');
    await page.locator('#trivia-puzzles-list [data-date="2026-10-28"]').click();
    await page.locator('.t-q-text[data-idx="0"]').fill('DIRTY MAIN');
    await page.locator('.t-sq-text[data-idx="0"]').fill('DIRTY SUPER');
    await page.locator('#trivia-regen-main').uncheck();
    await page.locator('#trivia-regen-super').check();
    await page.locator('#trivia-btn-regen').click();
    assert.match(await page.locator('#modal-message').textContent(),/Несохранённые изменения/);
    await page.locator('#modal-cancel').click();
    assert.equal(writes,0);
    assert.equal(await page.locator('.t-sq-text[data-idx="0"]').inputValue(),'DIRTY SUPER');
    await page.locator('#trivia-btn-regen').click();
    await page.locator('#modal-confirm').click();
    await page.waitForFunction(()=>document.querySelector('.t-sq-text[data-idx="0"]').value==='GENERATED SUPER');
    assert.equal(await page.locator('.t-q-text[data-idx="0"]').inputValue(),'DIRTY MAIN');
    await page.locator('#trivia-btn-close').click();
    await page.locator('#modal-confirm').click();
    assert.equal(await page.evaluate(()=>window.TriviaModule.hasDirtyDrafts()),true);
    await page.locator('#trivia-refresh-list').click();
    await page.locator('#trivia-puzzles-list [data-date="2026-10-29"]').click();
    await page.locator('#trivia-btn-close').click();
    await page.locator('#trivia-puzzles-list [data-date="2026-10-28"]').click();
    assert.equal(await page.locator('.t-q-text[data-idx="0"]').inputValue(),'DIRTY MAIN');
    assert.equal(await page.locator('.t-sq-text[data-idx="0"]').inputValue(),'GENERATED SUPER');
""",
    )
