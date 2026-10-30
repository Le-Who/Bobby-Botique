"""Offline operator keyboard, local-date and ambiguous-save regressions."""

from pathlib import Path

import pytest

from app.web import quart_app
from tests.test_natal_web_report import _run_browser_regression


async def _page(path):
    client = quart_app.test_client()
    async with client.session_transaction() as session:
        session["authenticated"] = True
    response = await client.get(path)
    assert response.status_code == 200
    static = Path(__file__).resolve().parents[1] / "app/static"
    assets = {
        f"/static/{path.relative_to(static).as_posix()}": path.read_text(encoding="utf-8")
        for path in [*static.glob("js/controls*.js"), static / "css/controls.css", static / "css/daily-admin.css"]
    }
    return {"html": await response.get_data(as_text=True), "headers": dict(response.headers), "assets": assets}


@pytest.mark.browser
@pytest.mark.asyncio
async def test_daily_keyboard_dialog_labels_server_day_and_responsive_layout(tmp_path):
    payload = await _page("/admin_daily")
    payload["screenshots"] = tmp_path.as_posix()
    _run_browser_regression(
        payload,
        r"""
    page.setDefaultTimeout(2500);
    await page.route('**/api/**',route=>{
      const path=new URL(route.request().url()).pathname;
      const puzzles=path==='/api/admin/dailycroc'?[{date:'2026-10-28',difficulty:'easy',target_word:'кот',topic:'Животные',language:'ru',image_prompt:'A cat',content_revision:1}]:[];
      return route.fulfill({json:{puzzles,channels:[],rows:[]}});
    });
    await page.clock.install({time:new Date('2026-10-27T22:30:00Z')});
    await page.goto('http://natal.test/admin_daily#2048');
    const bootstrap=await page.locator('#daily-bootstrap').textContent();
    assert.equal(JSON.parse(bootstrap).timezone,'Europe/Kyiv');
    assert.equal(await page.locator('#s2048-edit-date').inputValue(),JSON.parse(bootstrap).today);
    assert.equal(await page.evaluate(()=>AdminApi.kyivDate(new Date('2026-10-27T22:30:00Z'))),'2026-10-28');
    await page.evaluate(()=>DailyChrome.updateCalendar({today:'2026-10-29',timezone:'Europe/Kyiv'}));
    assert.equal(await page.evaluate(()=>getKyivDateString()),'2026-10-29');
    assert.equal(await page.locator('#s2048-edit-date').inputValue(),JSON.parse(bootstrap).today,'Readiness updates defaults without replacing an existing field');
    const tabs=page.getByRole('tablist');
    assert.equal(await tabs.count(),1);
    await page.getByRole('tab',{name:/2048/}).focus();
    await page.keyboard.press('ArrowRight');
    assert.equal(await page.getByRole('tab',{name:/Trivia/}).getAttribute('aria-selected'),'true');
    await page.keyboard.press('End');
    assert.equal(await page.getByRole('tab',{name:/Задания/}).getAttribute('aria-selected'),'true');
    await page.keyboard.press('Home');
    const origin=page.getByRole('tab',{name:/Рассылки/});
    await page.evaluate(()=>{window.confirmResult=null;showConfirm('Проверка','Сохранить изменения?').then(value=>window.confirmResult=value);});
    const dialog=page.getByRole('dialog',{name:'Проверка'});
    assert.equal(await dialog.getAttribute('aria-modal'),'true');
    assert.equal(await page.locator('#modal-cancel').evaluate(node=>node===document.activeElement),true);
    await page.keyboard.press('Tab');
    assert.equal(await page.locator('#modal-confirm').evaluate(node=>node===document.activeElement),true);
    await page.keyboard.press('Shift+Tab');
    assert.equal(await page.locator('#modal-cancel').evaluate(node=>node===document.activeElement),true);
    await page.keyboard.press('Escape');
    assert.equal(await page.evaluate(()=>window.confirmResult),false);
    assert.equal(await origin.evaluate(node=>node===document.activeElement),true);
    assert.equal(await page.locator('#toast-container').getAttribute('aria-live'),'polite');
    const missingLabels=await page.evaluate(()=>[...document.querySelectorAll('input:not([type=hidden]),select,textarea')].filter(node=>!node.labels?.length&&!node.getAttribute('aria-label')&&!node.getAttribute('aria-labelledby')).map(node=>node.id));
    assert.deepEqual(missingLabels,[]);
    for(const width of [320,375,768,1440]){
      await page.setViewportSize({width,height:900});
      for(const name of ['croc','2048','trivia','tarot','jobs']){
        await page.locator(`[data-tab="${name}"]`).click();
        assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true,`${name} at ${width}`);
        if(name==='croc'&&[320,1440].includes(width)){
          assert.equal(await page.locator('#tab-croc .stat-card label').evaluate(label=>label.getBoundingClientRect().right<=label.closest('.stat-card').getBoundingClientRect().right),true,'Banner upload action is not clipped inside a stat card');
          await page.evaluate(()=>scrollTo(0,0));await page.clock.runFor(30);
          await page.screenshot({path:`${payload.screenshots}/daily-${width}.png`});
          await page.locator('#croc-card-2026-10-28-easy').scrollIntoViewIfNeeded();await page.clock.runFor(30);
          await page.screenshot({path:`${payload.screenshots}/daily-card-${width}.png`});
        }
      }
    }
    // 200% CSS zoom in Chromium preserves a local table scroller.
    await page.setViewportSize({width:768,height:900});
    await page.evaluate(()=>document.documentElement.style.zoom='2');
    for(const name of ['croc','2048','trivia','tarot','jobs']){
      await page.locator(`[data-tab="${name}"]`).click();
      assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true,`${name} at 200%`);
    }
    assert.equal(await page.locator('.tab-nav').evaluate(node=>Math.abs(node.getBoundingClientRect().top-document.querySelector('.header').getBoundingClientRect().bottom)<2),true,'Sticky tabs follow measured header');
""",
    )


@pytest.mark.browser
@pytest.mark.asyncio
@pytest.mark.parametrize("matches", [True, False])
async def test_controls_ambiguous_save_reconciles_only_equal_section_without_duplicate_post(matches):
    payload = await _page("/controls")
    payload["matches"] = matches
    _run_browser_regression(
        payload,
        r"""
    page.setDefaultTimeout(2500);
    let revision=1,text='server',writes=0;
    const snapshot=()=>({revision,degraded:false,processes:[],prompts:[{id:'system',title:'System',editable:true,text,baseline:'server'}],catalogs:[],limits:[],history:[],commands:{revision,available:true,commands:[]}});
    await page.route('**/api/**',async route=>{
      if(route.request().method()==='POST'){
        writes++;const body=route.request().postDataJSON();text=payload.matches?body.value:'other administrator';revision=2;
        await new Promise(resolve=>setTimeout(resolve,200));
        return route.fulfill({json:{revision}}).catch(()=>{});
      }
      return route.fulfill({json:snapshot()});
    });
    await page.goto('http://natal.test/controls#prompts');
    await page.waitForSelector('#prompt-list textarea');
    await page.evaluate(()=>{const original=AdminApi.adminRequest;AdminApi.adminRequest=(url,options={})=>original(url,{...options,timeoutMs:40});});
    await page.locator('#prompt-list textarea').fill('confirmed draft');
    await page.locator('#prompt-list button.save').click();
    await page.waitForFunction(()=>!document.getElementById('refresh').disabled);
    assert.equal(writes,1,'No automatic POST retry');
    assert.equal(await page.locator('#prompt-list textarea').inputValue(),'confirmed draft');
    assert.match(await page.locator('#notice').textContent(),payload.matches?/подтвержден|Сохранено/:/пока неизвестен/);
    assert.equal(await page.locator('#prompt-list article').getAttribute('class').then(value=>value.includes('dirty')),!payload.matches);
    assert.equal(await page.locator('#prompt-list button.save').isDisabled(),!payload.matches);
    assert.equal(await page.locator('#prompt-list').getAttribute('aria-busy'),'false');
""",
    )


@pytest.mark.browser
@pytest.mark.asyncio
async def test_2048_short_save_timeout_reconciles_and_preserves_later_typing():
    _run_browser_regression(
        await _page("/admin_daily"),
        r"""
    page.setDefaultTimeout(2500);
    let writes=0;
    let puzzle={date:'2026-10-28',revision:1,board:[[2,2,0,0],[0,0,0,0],[0,0,0,0],[0,0,0,0]],goal_type:'tile',goal_value:256,spawn_sequence:[],seed:'original',par_moves:72,target_seconds:240,status:'draft',solution_moves:''};
    await page.route('**/api/**',async route=>{
      if(route.request().method()==='POST'){
        writes++;puzzle={...puzzle,...route.request().postDataJSON(),revision:puzzle.revision+1};
        await new Promise(resolve=>setTimeout(resolve,300));
        return route.fulfill({json:{success:true,puzzle}}).catch(()=>{});
      }
      return route.fulfill({json:{puzzles:[puzzle]}});
    });
    await page.goto('http://natal.test/admin_daily#2048');
    await page.waitForFunction(()=>document.getElementById('s2048-edit-seed').value==='original');
    await page.evaluate(()=>{const request=AdminApi.adminRequest;AdminApi.adminRequest=(url,options={})=>request(url,{...options,timeoutMs:80});});
    await page.locator('#s2048-edit-seed').fill('submitted');
    await page.locator('#s2048-save').click();
    await page.locator('#s2048-edit-seed').fill('later typing');
    await page.waitForFunction(()=>document.getElementById('s2048-save-status').textContent.includes('подтвержден'));
    assert.equal(writes,1);
    assert.equal(await page.locator('#s2048-edit-seed').inputValue(),'later typing');
    assert.equal(await page.locator('#s2048-save').isDisabled(),false);
    assert.equal(await page.evaluate(()=>S2048Module.hasDirtyDrafts()),true);
    await page.locator('[data-tab="croc"]').click();
    assert.equal(await page.locator('#confirm-modal').isVisible(),true);
    await page.keyboard.press('Escape');
    assert.equal(await page.locator('[data-tab="2048"]').getAttribute('aria-selected'),'true');
    assert.equal(await page.locator('#s2048-edit-seed').inputValue(),'later typing');
    await page.locator('#s2048-edit-status').selectOption('ready');
    await page.locator('#s2048-status-save').click();
    await page.waitForFunction(()=>document.getElementById('s2048-edit-revision').textContent==='3');
    assert.equal(await page.locator('#s2048-edit-seed').inputValue(),'later typing','Status-only save retains unsaved gameplay fields');
    assert.equal(writes,2);
""",
    )


@pytest.mark.browser
@pytest.mark.asyncio
async def test_queue_latest_filter_and_pending_user_selection_win_old_refresh():
    _run_browser_regression(
        await _page("/controls"),
        r"""
    page.setDefaultTimeout(2500);
    const job=id=>({task_id:id,revision:1,state:'queued',kind:'daily.test',lane:id==='A'?'deferred':'daily',result_summary:{},capabilities:{}});
    let hold=false,oldOverview,oldJobs,pendingB;
    await page.route('**/api/**',route=>{
      const url=new URL(route.request().url());
      if(url.pathname==='/api/admin/controls') return route.fulfill({json:{revision:1,processes:[],prompts:[],catalogs:[],limits:[],history:[],commands:{commands:[]}}});
      if(url.pathname==='/api/controls/queues'){
        if(hold&&!oldOverview){oldOverview=route;return;}
        return route.fulfill({json:{queue:{lanes:{broadcast:{available:false}}},load:{}}});
      }
      if(url.pathname==='/api/controls/jobs'){
        if(hold&&!url.searchParams.has('lane')&&!oldJobs){oldJobs=route;return;}
        return route.fulfill({json:{items:url.searchParams.has('lane')?[job('B')]:[job('A'),job('B')]}});
      }
      if(url.pathname==='/api/controls/jobs/B'&&hold){pendingB=route;return;}
      return route.fulfill({json:{job:job('A')}});
    });
    await page.goto('http://natal.test/controls#queues');
    await page.locator('#queue-lane-filter option[value="broadcast"]').waitFor({state:'attached'});
    await page.locator('[data-job="A"]').click();
    await page.waitForFunction(()=>document.getElementById('queue-job-detail').textContent.includes('A'));
    hold=true;await page.locator('#queue-refresh').click();
    while(!oldOverview||!oldJobs)await page.waitForTimeout(10);
    await page.locator('[data-job="B"]').click();
    while(!pendingB)await page.waitForTimeout(10);
    await page.locator('#queue-lane-filter').selectOption('daily');
    await page.waitForFunction(()=>!document.querySelector('[data-job="A"]'));
    await oldOverview.fulfill({json:{queue:{lanes:{}},load:{}}});
    await oldJobs.fulfill({json:{items:[job('A'),job('B')]}});
    await pendingB.fulfill({json:{job:job('B')}});
    await page.waitForFunction(()=>document.getElementById('queue-job-detail').querySelector('h3')?.textContent==='B');
    await page.waitForTimeout(50);
    assert.equal(await page.locator('[data-job="A"]').count(),0,'Old filter result cannot repaint newest filter');
    assert.equal(await page.locator('#queue-job-detail h3').textContent(),'B','Refresh cannot supersede pending user selection');
""",
    )


@pytest.mark.browser
@pytest.mark.asyncio
@pytest.mark.parametrize("matches", [True, False])
async def test_croc_prompt_refresh_and_unknown_save_keep_draft_focus_and_held_revision(matches):
    payload = await _page("/admin_daily")
    payload["matches"] = matches
    _run_browser_regression(
        payload,
        r"""
    page.setDefaultTimeout(2500);
    let writes=[],failRead=false;
    let puzzle={date:'2026-10-28',difficulty:'easy',language:'ru',target_word:'cat',topic:'animals',image_prompt:'original',image_file_id:'synthetic',content_revision:1};
    await page.addInitScript(()=>{
      window.blobs={created:0,revoked:0};const create=URL.createObjectURL,revoke=URL.revokeObjectURL;
      URL.createObjectURL=blob=>{window.blobs.created++;return create(blob);};
      URL.revokeObjectURL=url=>{window.blobs.revoked++;return revoke(url);};
    });
    await page.route('**/api/**',async route=>{
      const path=new URL(route.request().url()).pathname;
      if(path==='/api/admin/dailycroc/image')return route.fulfill({contentType:'image/png',body:Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aF1UAAAAASUVORK5CYII=','base64')});
      if(path==='/api/admin/dailycroc/models')return route.fulfill({json:{settings_revision:1,text_models:[],text_processes:[],image_models:[],default_image_model:'flux'}});
      if(path==='/api/admin/dailycroc/prompt'){
        const body=route.request().postDataJSON();writes.push(body);
        if(payload.matches)puzzle={...puzzle,image_prompt:body.prompt,content_revision:puzzle.content_revision+1};
        if(writes.length===1)await new Promise(resolve=>setTimeout(resolve,250));
        return route.fulfill({json:{success:true,puzzle}}).catch(()=>{});
      }
      if(path==='/api/admin/dailycroc')return route.fulfill(failRead?{status:503,json:{error:'unavailable'}}:{json:{puzzles:[puzzle]}});
      return route.fulfill({json:{}});
    });
    await page.goto('http://natal.test/admin_daily#croc');
    const input=page.locator('#croc-prompt-input-2026-10-28-easy');
    await page.locator('[data-croc-action="editPrompt"]').click();
    await input.fill('submitted');
    failRead=true;await page.locator('[data-croc-action="loadPuzzles"]').first().click();
    await page.waitForTimeout(50);assert.equal(await input.inputValue(),'submitted');
    failRead=false;await input.focus();
    await page.evaluate(()=>{const request=AdminApi.adminRequest;AdminApi.adminRequest=(url,options={})=>request(url,{...options,timeoutMs:80});});
    await page.locator('[data-croc-action="savePrompt"]').click();
    await input.fill('later typing');
    if(!payload.matches){
      await page.waitForFunction(()=>document.querySelector('.prompt-save-status').textContent.includes('неизвестен'));
      assert.equal(await input.inputValue(),'later typing');
      assert.equal(await page.locator('[data-croc-action="savePrompt"]').isDisabled(),true);
      await page.locator('[data-croc-action="reloadPrompt"]').click();
      await page.locator('#modal-confirm').click();
      await page.waitForFunction(()=>document.getElementById('croc-prompt-input-2026-10-28-easy').value==='original');
      assert.equal(writes.length,1);return;
    }
    await page.waitForFunction(()=>document.querySelector('.prompt-save-status').textContent.includes('подтвержден'));
    assert.equal(await input.inputValue(),'later typing');
    assert.equal(await input.evaluate(node=>node===document.activeElement),true);
    assert.equal(writes.length,1);
    await page.locator('[data-croc-action="savePrompt"]').click();
    await page.waitForFunction(()=>!CrocModule.hasDirtyDrafts());
    assert.equal(writes[1].expected_revision,2);
    assert.equal(writes[1].prompt,'later typing');
    await page.waitForFunction(()=>window.blobs.created>0&&window.blobs.revoked>=window.blobs.created);
""",
    )


@pytest.mark.browser
@pytest.mark.asyncio
@pytest.mark.parametrize("game", ["croc", "trivia"])
async def test_unknown_model_cas_reads_server_without_rebasing_local_choice(game):
    payload = await _page("/admin_daily")
    payload["game"] = game
    _run_browser_regression(
        payload,
        r"""
    page.setDefaultTimeout(2500);
    let writes=0,revision=1;
    await page.route('**/api/**',async route=>{
      const path=new URL(route.request().url()).pathname;
      if(route.request().method()==='POST'){
        writes++;revision=2;await new Promise(resolve=>setTimeout(resolve,250));
        return route.fulfill({json:{settings_revision:2,model:'new',llm_model:'new'}}).catch(()=>{});
      }
      if(path.endsWith('/models'))return route.fulfill({json:{settings_revision:revision,text_model:'old',text_models:[{id:'old'},{id:'new'},{id:'other'}],text_processes:[],image_models:[],default_image_model:'flux'}});
      if(path.endsWith('/stats'))return route.fulfill({json:{settings_revision:revision,llm_model:'old',effective_models:['old'],llm_models:['old','new','other']}});
      return route.fulfill({json:{puzzles:[]}});
    });
    await page.goto('http://natal.test/admin_daily#'+payload.game);
    const input=page.locator(payload.game==='croc'?'#croc-text-model':'#trivia-model-input');
    await page.waitForFunction(game=>document.getElementById(game==='croc'?'croc-text-model':'trivia-model-input').value==='old',payload.game);
    await page.evaluate(()=>{
      const request=AdminApi.adminRequest,fetch=AdminApi.adminFetch;
      AdminApi.adminRequest=(url,options={})=>request(url,{...options,timeoutMs:50});
      AdminApi.adminFetch=(url,options={})=>fetch(url,{...options,timeoutMs:50});
    });
    if(payload.game==='croc')await input.selectOption('new');
    else {await input.fill('new');await page.locator('#trivia-model-save').click();}
    const status=page.locator(`#${payload.game}-model-status`);
    await page.waitForFunction(game=>document.getElementById(game+'-model-status').textContent.includes('неизвестен'),payload.game);
    assert.equal(await input.inputValue(),'new');
    assert.equal(await page.evaluate(game=>window[game==='croc'?'CrocModule':'TriviaModule'].hasDirtyDrafts(),payload.game),true);
    if(payload.game==='croc')await input.selectOption('other');
    else {await input.fill('other');await page.locator('#trivia-model-save').click();}
    await page.waitForTimeout(80);assert.equal(writes,1,'Unconfirmed CAS cannot be rebased or repeated');
    await page.locator(`#${payload.game}-settings-reload`).click();
    await page.waitForFunction(game=>document.getElementById(game==='croc'?'croc-text-model':'trivia-model-input').value==='old',payload.game);
    assert.equal(writes,1);
    assert.match(await status.textContent(),/r2/);
    assert.equal(await page.evaluate(game=>window[game==='croc'?'CrocModule':'TriviaModule'].hasDirtyDrafts(),payload.game),false);
""",
    )


@pytest.mark.browser
@pytest.mark.asyncio
async def test_trivia_old_leaderboard_filter_cannot_repaint_new_selection():
    _run_browser_regression(
        await _page("/admin_daily"),
        r"""
    page.setDefaultTimeout(2500);
    let old;
    await page.route('**/api/**',route=>{
      const url=new URL(route.request().url());
      if(url.pathname==='/api/admin/dailytrivia')return route.fulfill({json:{puzzles:[],today:'2026-10-28'}});
      if(url.pathname.endsWith('/leaderboard')){
        if(url.searchParams.get('type')==='monthly'){old=route;return;}
        return route.fulfill({json:{items:[{name:'NEW DATE',score:1,correct:1}]}});
      }
      return route.fulfill({json:{}});
    });
    await page.goto('http://natal.test/admin_daily#trivia');
    await page.locator('#trivia-lb-btn-monthly').click();
    while(!old)await page.waitForTimeout(10);
    await page.locator('#trivia-lb-btn-daily').click();
    await page.waitForFunction(()=>document.getElementById('trivia-lb-title').textContent.includes('сегодня'));
    await old.fulfill({json:{items:[{name:'OLD DATE',score:9,correct:5}]}});
    await page.waitForTimeout(50);
    assert.match(await page.locator('#trivia-leaderboard-container').textContent(),/NEW DATE/);
    assert.doesNotMatch(await page.locator('#trivia-leaderboard-container').textContent(),/OLD DATE/);
""",
    )


@pytest.mark.browser
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status,expected",
    [(401, "Сессия истекла"), (403, "Доступ запрещён"), (429, "Временно недоступно"), (503, "Временно недоступно")],
)
async def test_daily_read_errors_explain_session_denial_and_temporary_status(status, expected):
    payload = await _page("/admin_daily")
    payload.update(status=status, expected=expected)
    _run_browser_regression(
        payload,
        r"""
    await page.route('**/api/**',route=>{
      const path=new URL(route.request().url()).pathname;
      return route.fulfill(path.endsWith('/models')?{status:payload.status,headers:{'Retry-After':'3'},json:{error:'safe'}}:{json:{puzzles:[]}});
    });
    await page.goto('http://natal.test/admin_daily#croc');
    await page.waitForFunction(expected=>document.getElementById('croc-model-status').textContent.includes(expected),payload.expected);
    if([429,503].includes(payload.status))assert.match(await page.locator('#croc-model-status').textContent(),/3 с/);
""",
    )


@pytest.mark.browser
@pytest.mark.asyncio
async def test_puzzle_image_model_unknown_result_keeps_choice_until_explicit_reload():
    _run_browser_regression(
        await _page("/admin_daily"),
        r"""
    page.setDefaultTimeout(2500);
    let writes=0;
    const puzzle={date:'2026-10-28',difficulty:'easy',target_word:'cat',image_prompt:'original',image_model:'flux',content_revision:1};
    await page.route('**/api/**',async route=>{
      const path=new URL(route.request().url()).pathname;
      if(path.endsWith('/models'))return route.fulfill({json:{settings_revision:1,text_models:[],text_processes:[],image_models:[{id:'flux'},{id:'new'}],default_image_model:'flux'}});
      if(path.endsWith('/model')){writes++;await new Promise(resolve=>setTimeout(resolve,200));return route.fulfill({json:{puzzle}}).catch(()=>{});}
      return route.fulfill({json:{puzzles:[puzzle]}});
    });
    await page.goto('http://natal.test/admin_daily#croc');
    await page.evaluate(()=>{const request=AdminApi.adminRequest;AdminApi.adminRequest=(url,options={})=>request(url,{...options,timeoutMs:50});});
    const select=page.locator('#croc-model-2026-10-28-easy');
    await select.selectOption('new');
    await page.waitForFunction(()=>document.querySelector('.image-model-status')?.textContent.includes('неизвестен'));
    assert.equal(await select.inputValue(),'new');assert.equal(await select.isDisabled(),true);assert.equal(writes,1);
    await page.locator('[data-croc-action="loadPuzzles"]').first().click();
    await page.waitForTimeout(50);assert.equal(await select.inputValue(),'new');
    await page.locator('[data-croc-action="reloadModel"]').click();
    await page.locator('#modal-confirm').click();
    await page.waitForFunction(()=>document.getElementById('croc-model-2026-10-28-easy').value==='flux');
    assert.equal(writes,1);
""",
    )


@pytest.mark.browser
@pytest.mark.asyncio
async def test_unconfirmed_restore_blocks_new_writes_and_reads_revision_without_retry():
    _run_browser_regression(
        await _page("/controls"),
        r"""
    page.setDefaultTimeout(2500);page.on('dialog',dialog=>dialog.accept());
    let revision=1,writes=0,reads=0;
    await page.route('**/api/**',async route=>{
      if(route.request().method()==='POST'){
        writes++;revision=2;await new Promise(resolve=>setTimeout(resolve,250));return route.fulfill({json:{revision}}).catch(()=>{});
      }
      reads++;return route.fulfill({json:{revision,processes:[],prompts:[],catalogs:[],limits:[],history:[{revision:0,created_at:'2026-10-28T00:00:00Z'}],commands:{commands:[]}}});
    });
    await page.goto('http://natal.test/controls#history');
    await page.waitForSelector('#history-list button');
    await page.evaluate(()=>{const request=AdminApi.adminRequest;AdminApi.adminRequest=(url,options={})=>request(url,{...options,timeoutMs:50});});
    await page.locator('#history-list button').click();
    await page.waitForFunction(()=>!document.getElementById('refresh').disabled);
    assert.equal(writes,1);assert.ok(reads>=2,'Read server revision after timeout');
    assert.equal(await page.locator('#history-list button').isDisabled(),true);
    assert.match(await page.locator('#notice').textContent(),/пока неизвестен.*2/);
    assert.equal(await page.evaluate(()=>{const event=new Event('beforeunload',{cancelable:true});window.dispatchEvent(event);return event.defaultPrevented;}),false,'Uncertainty alone is not an unsaved draft');
""",
    )
