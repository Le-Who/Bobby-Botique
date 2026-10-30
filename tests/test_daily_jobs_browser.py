"""Daily controls enqueue once and reconcile durable status with tab-local drafts."""

import pytest

from app.web import quart_app
from tests.test_natal_web_report import _run_browser_regression


async def _payload(**extra):
    client = quart_app.test_client()
    async with client.session_transaction() as session:
        session["authenticated"] = True
    response = await client.get("/admin_daily")
    return {
        "html": (await response.get_data()).decode("utf-8"),
        "headers": dict(response.headers),
        "job_defaults": {
            "lane": "daily",
            "revision": 1,
            "attempts_started": 1,
            "max_attempts": 3,
            "submitted_at": 1,
            "started_at": 2,
            "finished_at": None,
            "not_before": 0,
            "deadline_at": 1801,
            "heartbeat_at": 3,
            "reason_code": None,
            "storage_mode": "local",
            "retry_eligible": False,
            "retry_payload_available": True,
            "parent_task_id": None,
        },
        **extra,
    }


@pytest.mark.browser
@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["succeeded", "revision_conflict", "newer_server_revision", "admission_unknown"])
async def test_queued_trivia_validation_preserves_drafts_and_never_reposts(outcome):
    _run_browser_regression(
        await _payload(outcome=outcome),
        r"""
    page.setDefaultTimeout(2500);
    const q=(id,text)=>({id,topic:'Наука',question:text,options:['A','B','C','D'],correct_index:0,explanation:'Text'});
    let puzzle={date:'2026-10-28',revision:1,questions:Array.from({length:5},(_,i)=>q(i+1,'Original main')),super_questions:Array.from({length:3},(_,i)=>q(i+1,'Original super'))};
    let job={...payload.job_defaults,task_id:'trivia-job',kind:'trivia.validate_save',state:'running',result_summary:{date:puzzle.date,generated:0,completed:0,total:5}};
    const enqueues=[],previews=[],legacy=[];
    let statusReads=0;
    await page.route('**/api/**',route=>{
      const path=new URL(route.request().url()).pathname;
      if(path==='/api/admin/dailytrivia')return route.fulfill({json:{puzzles:[puzzle],today:puzzle.date}});
      if(path==='/api/admin/daily/jobs/preview'){previews.push(route.request().postDataJSON());return route.fulfill({json:{work_items:5,effective_policy:{models:['gemini-synthetic'],explicit:false}}});}
      if(path==='/api/admin/daily/jobs'&&route.request().method()==='GET')return route.fulfill({json:{items:enqueues.map(command=>({operation_id:command.operation_id,available:true,job}))}});
      if(path==='/api/admin/daily/jobs'){
        enqueues.push(route.request().postDataJSON());
        if(payload.outcome==='admission_unknown')return route.abort('failed');
        return route.fulfill({status:202,json:{job_id:job.task_id,state:'accepted'}});
      }
      if(path.includes('/daily/jobs/trivia-job')||path.includes('/daily/jobs/by-operation/')){statusReads++;return route.fulfill({json:job});}
      if(path.endsWith('/save')||path.endsWith('/save_super')||path.endsWith('/regenerate'))legacy.push(path);
      return route.fulfill({json:{days:[]}});
    });
    await page.goto('http://natal.test/admin_daily#trivia');
    await page.locator('#trivia-puzzles-list [data-date="2026-10-28"]').click();
    await page.locator('.t-q-text[data-idx="0"]').fill('SUBMITTED MAIN');
    await page.locator('.t-sq-text[data-idx="0"]').fill('PRIVATE UNSAVED SUPER');
    await page.locator('#trivia-btn-save').click();
    await page.waitForTimeout(100);
    assert.equal(await page.locator('#confirm-modal').isVisible(),true,'Validation must use shared queue preflight and confirmation');
    assert.match(await page.locator('#modal-message').textContent(),/часть: main.*режим: replace/);
    await page.locator('#modal-confirm').click();
    await page.waitForFunction(()=>document.getElementById('daily-jobs-list').textContent.includes('running'));
    assert.equal(enqueues.length,1);
    assert.equal(enqueues[0].kind,'trivia.validate_save');
    assert.equal(enqueues[0].part,'main');
    assert.equal(enqueues[0].mode,'replace','Validation saves replace only the selected stored section');
    assert.equal(enqueues[0].expected_revision,1);
    assert.equal(enqueues[0].questions.length,5);
    assert.equal(Object.hasOwn(enqueues[0],'super_questions'),false);
    assert.equal(typeof enqueues[0].operation_id,'string');
    await page.locator('.t-q-text[data-idx="0"]').fill('NEW MAIN TYPING');
    await page.locator('#trivia-btn-save').click();
    await page.waitForTimeout(50);
    assert.equal(enqueues.length,1,'An already tracked running scope cannot issue another enqueue');
    assert.equal(previews.length,1);
    assert.deepEqual(legacy,[]);
    const stored=await page.evaluate(()=>sessionStorage.getItem('admin-daily-jobs-v1'));
    assert.equal(stored.includes('PRIVATE UNSAVED SUPER')||stored.includes('SUBMITTED MAIN'),false);
    const conflict=['revision_conflict','newer_server_revision','admission_unknown'].includes(payload.outcome);
    puzzle={...puzzle,revision:payload.outcome==='newer_server_revision'?3:2,questions:enqueues[0].questions};
    job={...job,revision:2,state:payload.outcome==='revision_conflict'?'failed':'succeeded',reason_code:payload.outcome==='revision_conflict'?'revision_conflict':null,result_summary:{...job.result_summary,completed:5,generated:5,puzzle_revision:2}};
    await page.locator('[data-tab="jobs"]').click();
    await page.locator('#confirm-modal').waitFor({state:'visible'});
    await page.locator('#modal-confirm').click();
    await page.getByRole('button',{name:'Проверить статус',exact:true}).click();
    await page.waitForFunction(()=>document.getElementById('daily-jobs-list').textContent.includes('succeeded')||document.getElementById('daily-jobs-list').textContent.includes('failed'));
    if(conflict)await page.waitForFunction(()=>!document.getElementById('trivia-editor-conflict').hidden);
    else await page.waitForFunction(()=>document.getElementById('trivia-editor-title').textContent.includes('ревизия 2'));
    await page.locator('[data-tab="trivia"]').click();
    assert.equal(await page.locator('.t-q-text[data-idx="0"]').inputValue(),'NEW MAIN TYPING');
    assert.equal(await page.locator('.t-sq-text[data-idx="0"]').inputValue(),'PRIVATE UNSAVED SUPER');
    assert.match(await page.locator('#trivia-editor-title').textContent(),conflict?/ревизия 1/:/ревизия 2/);
    assert.equal(enqueues.length,1);
    assert.ok(statusReads>=1);
""",
    )


@pytest.mark.browser
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "action,kind", [("prepare", "croc.prepare_day"), ("regen", "croc.regenerate_image"), ("reset", "croc.reset_word")]
)
async def test_croc_long_actions_use_queue_preflight_and_selected_revision(action, kind):
    _run_browser_regression(
        await _payload(action=action, kind=kind),
        r"""
    page.setDefaultTimeout(2500);
    const previews=[],writes=[],legacy=[];
    await page.route('**/api/**',route=>{
      const path=new URL(route.request().url()).pathname;
      if(path==='/api/admin/dailycroc')return route.fulfill({json:{puzzles:[{date:'2026-10-28',difficulty:'easy',target_word:'кот',topic:'Животные',image_prompt:'cat',image_model:'flux',image_file_id:'',content_revision:4,played:false}]}});
      if(path==='/api/admin/dailycroc/models')return route.fulfill({json:{settings_revision:0,text_models:[],text_processes:[],image_models:[{id:'flux'}],default_image_model:'flux'}});
      if(path==='/api/admin/daily/jobs/preview'){previews.push(route.request().postDataJSON());return route.fulfill({json:{work_items:1,effective_policy:{models:['flux']}}});}
      if(path==='/api/admin/daily/jobs'&&route.request().method()==='GET')return route.fulfill({json:{items:[]}});
      if(path==='/api/admin/daily/jobs'){writes.push(route.request().postDataJSON());return route.fulfill({status:202,json:{job_id:'croc-job',state:'accepted'}});}
      if(path==='/api/admin/daily/jobs/croc-job')return route.fulfill({json:{...payload.job_defaults,task_id:'croc-job',kind:payload.kind,state:'running',result_summary:{date:'2026-10-28'}}});
      if(path.endsWith('/regenerate')||path.endsWith('/reset-word')||path.endsWith('/day/prepare'))legacy.push(path);
      return route.fulfill({json:{days:[]}});
    });
    await page.goto('http://natal.test/admin_daily#croc');
    await page.waitForSelector('#croc-regen-2026-10-28-easy');
    if(payload.action==='prepare'){
      await page.locator('#croc-day-date').fill('2026-10-28');
      await page.locator('#croc-day-prepare').click();
    }else await page.locator(`#croc-${payload.action}-2026-10-28-easy`).click();
    await page.waitForTimeout(100);
    assert.equal(previews.length,1,'The shared read-only preflight must precede confirmation');
    if(payload.action!=='prepare')assert.match(await page.locator('#modal-message').textContent(),/сложность: easy/);
    await page.locator('#modal-confirm').click();
    await page.waitForFunction(()=>document.getElementById('daily-jobs-list').textContent.includes('running'));
    assert.equal(writes.length,1);assert.equal(writes[0].kind,payload.kind);assert.equal(writes[0].date,'2026-10-28');
    if(payload.action!=='prepare'){assert.equal(writes[0].expected_revision,4);assert.equal(writes[0].difficulty,'easy');}
    if(payload.action==='regen')assert.equal(writes[0].model,'flux');
    assert.deepEqual(legacy,[]);
""",
    )


@pytest.mark.browser
@pytest.mark.asyncio
@pytest.mark.parametrize("late_method", ["saved", "receive"])
async def test_late_job_or_refresh_snapshot_cannot_regress_clean_editor_revision(late_method):
    _run_browser_regression(
        await _payload(late_method=late_method),
        r"""
    page.setDefaultTimeout(2500);
    const q=(id)=>({id,topic:'Наука',question:`Question ${id}`,options:['A','B','C','D'],correct_index:0,explanation:'Text'});
    const puzzle={date:'2026-10-28',revision:1,questions:Array.from({length:5},(_,i)=>q(i+1)),super_questions:Array.from({length:3},(_,i)=>q(i+1))};
    await page.route('**/api/**',route=>route.fulfill({json:{puzzles:[puzzle],today:puzzle.date,days:[]}}));
    await page.goto('http://natal.test/admin_daily#trivia');
    await page.locator('#trivia-puzzles-list [data-date="2026-10-28"]').click();
    await page.evaluate(({puzzle,method})=>{
      const intent=window.TriviaModule.capture('main');
      window.TriviaModule.receive({...puzzle,revision:3});
      if(method==='saved')window.TriviaModule.saved(puzzle.date,'main',intent.submitted,{...puzzle,revision:2});
      else window.TriviaModule.receive({...puzzle,revision:2});
    },{puzzle,method:payload.late_method});
    assert.match(await page.locator('#trivia-editor-title').textContent(),/ревизия 3/);
""",
    )


@pytest.mark.browser
@pytest.mark.asyncio
async def test_restored_job_uses_batched_reads_pauses_hidden_offline_and_never_reenqueues():
    _run_browser_regression(
        await _payload(),
        r"""
    page.setDefaultTimeout(2500);
    const operation='restored-operation-0001';
    let reads=0,writes=0,state='running';
    await page.addInitScript(({operation})=>sessionStorage.setItem('admin-daily-jobs-v1',JSON.stringify([{operationId:operation,jobId:'restored-job',kind:'croc.prepare_day',date:'2026-10-28'}])),{operation});
    await page.route('**/api/**',route=>{
      const url=new URL(route.request().url());
      const job={...payload.job_defaults,revision:state==='running'?1:2,task_id:'restored-job',kind:'croc.prepare_day',state,result_summary:{date:'2026-10-28',generated:2,completed:2,total:8}};
      if(url.pathname==='/api/admin/daily/jobs'&&route.request().method()==='GET'){
        reads++;assert.equal(url.searchParams.get('operations'),operation);
        return route.fulfill({json:{items:[{operation_id:operation,available:true,job}]}});
      }
      if(url.pathname.startsWith('/api/admin/daily/jobs/')&&route.request().method()==='GET'){reads++;return route.fulfill({json:job});}
      if(route.request().method()==='POST')writes++;
      return route.fulfill({json:{days:[],puzzles:[]}});
    });
    await page.goto('http://natal.test/admin_daily#jobs');
    await page.waitForFunction(()=>document.getElementById('daily-jobs-list').textContent.includes('running'));
    assert.equal(await page.locator('#daily-jobs-list progress').getAttribute('value'),'2');
    assert.equal(writes,0);
    await page.evaluate(()=>{Object.defineProperty(document,'hidden',{configurable:true,get:()=>true});document.dispatchEvent(new Event('visibilitychange'));});
    const hiddenReads=reads;
    await page.waitForTimeout(2900);
    assert.equal(reads,hiddenReads);
    await page.evaluate(()=>{Object.defineProperty(navigator,'onLine',{configurable:true,get:()=>false});Object.defineProperty(document,'hidden',{configurable:true,get:()=>false});document.dispatchEvent(new Event('visibilitychange'));});
    await page.waitForTimeout(2100);
    assert.equal(reads,hiddenReads);
    await page.evaluate(()=>{Object.defineProperty(navigator,'onLine',{configurable:true,get:()=>true});dispatchEvent(new Event('online'));});
    while(reads===hiddenReads)await new Promise(resolve=>setTimeout(resolve,5));
    await page.waitForTimeout(50);
    const beforeClose=reads;
    await page.evaluate(()=>dispatchEvent(new Event('pagehide')));
    await page.waitForTimeout(3000);
    assert.equal(reads,beforeClose);
    state='succeeded';
    await page.reload();
    await page.waitForFunction(()=>document.getElementById('daily-jobs-list').textContent.includes('succeeded'));
    assert.equal(writes,0,'Reload/status recovery may never replay the admission POST');
""",
    )


@pytest.mark.browser
@pytest.mark.asyncio
async def test_queued_neighbor_regeneration_uses_one_confirmation_and_preserves_dirty_main():
    _run_browser_regression(
        await _payload(),
        r"""
    page.setDefaultTimeout(2500);
    const q=(id,text)=>({id,topic:'Наука',question:text,options:['A','B','C','D'],correct_index:0,explanation:'Text'});
    let puzzle={date:'2026-10-28',revision:1,questions:Array.from({length:5},(_,i)=>q(i+1,'Main')),super_questions:Array.from({length:3},(_,i)=>q(i+1,'Super'))};
    const writes=[];
    await page.route('**/api/**',route=>{
      const url=new URL(route.request().url());
      if(url.pathname==='/api/admin/dailytrivia')return route.fulfill({json:{puzzles:[puzzle],today:puzzle.date}});
      if(url.pathname.endsWith('/jobs/preview'))return route.fulfill({json:{work_items:3,effective_policy:{models:['gemini-synthetic']}}});
      if(url.pathname==='/api/admin/daily/jobs'&&route.request().method()==='POST'){
        const command=route.request().postDataJSON();writes.push(command);puzzle={...puzzle,revision:2,super_questions:Array.from({length:3},(_,i)=>q(i+1,'GENERATED SUPER'))};
        return route.fulfill({status:202,json:{job_id:'regen-job',state:'accepted'}});
      }
      if(url.pathname.endsWith('/jobs/regen-job'))return route.fulfill({json:{...payload.job_defaults,revision:2,task_id:'regen-job',kind:'trivia.regenerate',state:'succeeded',result_summary:{date:puzzle.date,puzzle_revision:2,generated:3}}});
      return route.fulfill({json:{days:[],items:[]}});
    });
    await page.goto('http://natal.test/admin_daily#trivia');
    await page.locator('#trivia-puzzles-list [data-date="2026-10-28"]').click();
    await page.locator('.t-q-text[data-idx="0"]').fill('PRIVATE MAIN DRAFT');
    await page.locator('.t-sq-text[data-idx="0"]').fill('REPLACE THIS SUPER DRAFT');
    await page.locator('#trivia-regen-main').uncheck();await page.locator('#trivia-regen-super').check();
    await page.locator('#trivia-btn-regen').click();
    await page.locator('#confirm-modal').waitFor({state:'visible'});
    assert.match(await page.locator('#modal-message').textContent(),/Несохранённые изменения.*Соседний черновик/);
    assert.match(await page.locator('#modal-message').textContent(),/часть: super.*режим: replace/);
    await page.locator('#modal-confirm').click();
    await page.waitForFunction(()=>document.querySelector('.t-sq-text[data-idx="0"]').value==='GENERATED SUPER');
    assert.equal(await page.locator('.t-q-text[data-idx="0"]').inputValue(),'PRIVATE MAIN DRAFT');
    assert.equal(writes.length,1);assert.equal(writes[0].kind,'trivia.regenerate');assert.equal(writes[0].part,'super');
    assert.equal(writes[0].mode,'replace');assert.equal(writes[0].expected_revision,1);assert.equal(Object.hasOwn(writes[0],'questions'),false);
    assert.equal(await page.locator('#confirm-modal').isVisible(),false);
""",
    )


@pytest.mark.browser
@pytest.mark.asyncio
@pytest.mark.parametrize("lost_second_reply", [False, True])
async def test_coalesced_other_section_job_never_claims_new_draft_as_saved(lost_second_reply):
    _run_browser_regression(
        await _payload(lost_second_reply=lost_second_reply),
        r"""
    page.setDefaultTimeout(2500);
    const q=(id,text)=>({id,topic:'Science',question:text,options:['A','B','C','D'],correct_index:0,explanation:'Text'});
    let puzzle={date:'2026-10-28',revision:1,questions:Array.from({length:5},(_,i)=>q(i+1,'Original main')),super_questions:Array.from({length:3},(_,i)=>q(i+1,'Original super'))};
    const commands=[];
    let state='running';
    const job=()=>({...payload.job_defaults,revision:state==='running'?1:2,task_id:'coalesced-job',kind:'trivia.validate_save',state,result_summary:{date:puzzle.date,puzzle_revision:2,generated:5}});
    await page.route('**/api/**',route=>{
      const url=new URL(route.request().url());
      if(url.pathname==='/api/admin/dailytrivia')return route.fulfill({json:{puzzles:[puzzle],today:puzzle.date}});
      if(url.pathname.endsWith('/jobs/preview'))return route.fulfill({json:{work_items:5,effective_policy:{models:['synthetic']}}});
      if(url.pathname==='/api/admin/daily/jobs'&&route.request().method()==='POST'){
        commands.push(route.request().postDataJSON());
        if(commands.length===2&&payload.lost_second_reply)return route.abort('failed');
        return route.fulfill({status:202,json:{job_id:'coalesced-job',state:commands.length===1?'accepted':'existing',reason_code:commands.length===1?null:'conflict_active'}});
      }
      if(url.pathname==='/api/admin/daily/jobs')return route.fulfill({json:{items:commands.map(command=>({operation_id:command.operation_id,available:true,job:job()}))}});
      if(url.pathname.endsWith('/jobs/coalesced-job')||url.pathname.includes('/jobs/by-operation/'))return route.fulfill({json:job()});
      return route.fulfill({json:{days:[]}});
    });
    await page.goto('http://natal.test/admin_daily#trivia');
    await page.locator('#trivia-puzzles-list [data-date="2026-10-28"]').click();
    await page.locator('.t-q-text[data-idx="0"]').fill('SUBMITTED MAIN');
    await page.locator('.t-sq-text[data-idx="0"]').fill('PRIVATE UNSAVED SUPER');
    await page.locator('#trivia-btn-save').click();await page.locator('#confirm-modal').waitFor({state:'visible'});await page.locator('#modal-confirm').click();
    await page.waitForFunction(()=>document.querySelectorAll('#daily-jobs-list li').length===1);
    await page.waitForFunction(()=>!document.getElementById('trivia-btn-save').disabled);
    await page.locator('#trivia-btn-save-super').click();await page.locator('#confirm-modal').waitFor({state:'visible'});await page.locator('#modal-confirm').click();
    await page.waitForFunction(()=>document.querySelectorAll('#daily-jobs-list li').length===2);
    assert.equal(commands.length,2);assert.equal(commands[0].part,'main');assert.equal(commands[1].part,'super');
    assert.notEqual(commands[0].operation_id,commands[1].operation_id);
    puzzle={...puzzle,revision:2,questions:commands[0].questions};state='succeeded';
    await page.evaluate(()=>dispatchEvent(new Event('online')));
    await page.waitForFunction(()=>[...document.querySelectorAll('#daily-jobs-list li')].every(row=>row.textContent.includes('succeeded')));
    await page.waitForTimeout(100);
    assert.equal(await page.locator('.t-sq-text[data-idx="0"]').inputValue(),'PRIVATE UNSAVED SUPER','An existing/coalesced main job never applied the newly submitted super draft');
    assert.match(await page.locator('#trivia-editor-status').textContent(),/супер несохранены/);
    assert.equal(commands.length,2,'Status reconciliation cannot retry either enqueue');
""",
    )


@pytest.mark.browser
@pytest.mark.asyncio
@pytest.mark.parametrize("latest_state", ["succeeded", "running"])
async def test_late_batch_status_cannot_regress_manual_terminal_or_progress(latest_state):
    _run_browser_regression(
        await _payload(latest_state=latest_state),
        r"""
    page.setDefaultTimeout(2500);
    const operation='status-operation-0001';
    let pendingBatch;
    const current={...payload.job_defaults,task_id:'status-job',kind:'tarot.prepare',revision:3,state:payload.latest_state,result_summary:{date:'2026-10-28',completed:payload.latest_state==='succeeded'?8:6,total:8}};
    const stale={...current,revision:payload.latest_state==='succeeded'?2:3,state:'running',result_summary:{...current.result_summary,completed:2}};
    await page.addInitScript(({operation})=>sessionStorage.setItem('admin-daily-jobs-v1',JSON.stringify([{operationId:operation,jobId:'status-job',kind:'tarot.prepare',date:'2026-10-28'}])),{operation});
    await page.route('**/api/**',route=>{
      const url=new URL(route.request().url());
      if(url.pathname==='/api/admin/daily/jobs'){pendingBatch=route;return;}
      if(url.pathname.endsWith('/daily/jobs/status-job'))return route.fulfill({json:current});
      return route.fulfill({json:{days:[]}});
    });
    await page.goto('http://natal.test/admin_daily#jobs');
    while(!pendingBatch)await new Promise(resolve=>setTimeout(resolve,5));
    await page.getByRole('button',{name:'Проверить статус',exact:true}).click();
    await page.waitForFunction(expected=>document.querySelector('#daily-jobs-list progress')?.value===expected,current.result_summary.completed);
    await pendingBatch.fulfill({json:{items:[{operation_id:operation,available:true,job:stale}]}});
    await page.waitForTimeout(100);
    assert.equal(await page.locator('#daily-jobs-list progress').getAttribute('value'),String(current.result_summary.completed));
    assert.match(await page.locator('#daily-jobs-list strong').textContent(),new RegExp(payload.latest_state));
""",
    )


@pytest.mark.browser
@pytest.mark.asyncio
async def test_delayed_admission_preserves_terminal_status_and_only_then_confirms_callback():
    _run_browser_regression(
        await _payload(),
        r"""
    page.setDefaultTimeout(2500);
    let admission,operation;
    const finished={...payload.job_defaults,task_id:'finished-job',kind:'tarot.prepare',revision:3,state:'succeeded',result_summary:{completed:8,total:8,generated:8}};
    await page.route('**/api/**',route=>{
      const url=new URL(route.request().url());
      if(url.pathname.endsWith('/jobs/preview'))return route.fulfill({json:{work_items:8,effective_policy:{models:['synthetic']}}});
      if(url.pathname==='/api/admin/daily/jobs'&&route.request().method()==='POST'){admission=route;operation=route.request().postDataJSON().operation_id;return;}
      if(url.pathname==='/api/admin/daily/jobs')return route.fulfill({json:{items:[{operation_id:operation,available:true,job:finished}]}});
      if(url.pathname.endsWith('/jobs/finished-job'))return;
      return route.fulfill({json:{days:[]}});
    });
    await page.goto('http://natal.test/admin_daily#jobs');
    await page.evaluate(()=>{
      window.callbackCount=0;
      window.pendingRun=window.DailyJobs.run({kind:'tarot.prepare',date:'2026-10-28',mode:'missing'},{onTerminal:()=>{window.callbackCount++;}});
      window.pendingRun.catch(()=>{});
    });
    await page.locator('#confirm-modal').waitFor({state:'visible'});await page.locator('#modal-confirm').click();
    while(!admission)await new Promise(resolve=>setTimeout(resolve,5));
    await page.evaluate(()=>dispatchEvent(new Event('online')));
    await page.waitForFunction(()=>document.getElementById('daily-jobs-list').textContent.includes('succeeded'));
    assert.equal(await page.evaluate(()=>window.callbackCount),0,'No content-owning callback exists while admission is unconfirmed');
    await admission.fulfill({status:202,json:{job_id:'finished-job',state:'accepted'}});
    await page.waitForFunction(()=>window.callbackCount===1);
    assert.match(await page.locator('#daily-jobs-list strong').textContent(),/succeeded/);
    assert.equal(await page.locator('#daily-jobs-list progress').getAttribute('value'),'8');
    assert.equal(await page.evaluate(()=>window.callbackCount),1);
""",
    )
