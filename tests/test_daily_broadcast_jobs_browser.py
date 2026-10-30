"""Broadcast UI observes journaled jobs without inventing delivery or retrying unknown sends."""

import pytest

from app.web import quart_app
from tests.test_natal_web_report import _run_browser_regression

BATCH = "11111111-1111-4111-8111-111111111111"
CHILD = "22222222-2222-4222-8222-222222222222"


async def _payload(**extra):
    client = quart_app.test_client()
    async with client.session_transaction() as session:
        session["authenticated"] = True
    response = await client.get("/admin_daily")
    return {
        "html": (await response.get_data()).decode("utf-8"),
        "headers": dict(response.headers),
        "batch_id": BATCH,
        "child_id": CHILD,
        "batch": {
            "batch_id": BATCH,
            "parent_batch_id": None,
            "operation_id": None,
            "task_id": None,
            "channel": "horoscope",
            "mode": "offer",
            "effective_date": "2026-10-28",
            "variant": "",
            "created_at": "2026-10-28T10:00:00+00:00",
            "finished_at": None,
            "state": "queued",
            "total": 1,
            "pending": 1,
            "started": 0,
            "sent": 0,
            "failed": 0,
            "skipped": 0,
            "unknown_delivery": 0,
            "retryable_failed": 0,
            "settings_revision": 7,
            "coverage_started_at": "2026-10-28T00:00:00+00:00",
            "admission_state": "accepted",
            "admission_reason": None,
            "job": None,
        },
        **extra,
    }


@pytest.mark.browser
@pytest.mark.asyncio
@pytest.mark.parametrize("admission", ["accepted", "unknown", "rejected"])
async def test_single_offer_uses_preview_and_tracks_unknown_or_rejection_without_false_sent(admission):
    _run_browser_regression(
        await _payload(admission=admission),
        r"""
    page.setDefaultTimeout(2500);
    let batch={...payload.batch},postCount=0,previews=0,operation;
    await page.route('**/api/**',route=>{
      const url=new URL(route.request().url());
      if(url.pathname==='/api/admin/broadcast/preview'){
        previews++;assert.deepEqual(route.request().postDataJSON(),{channel:'horoscope',user_ids:[123],force:false});
        return route.fulfill({json:{channel:'horoscope',requested:1,eligible:1,excluded:{},recipient_ids:[123]}});
      }
      if(url.pathname==='/api/admin/broadcast/send-offer'){
        const command=route.request().postDataJSON();postCount++;operation=command.operation_id;
        assert.equal(command.user_id,123);assert.equal(command.force,false);assert.match(operation,/^[0-9a-f-]{36}$/);
        batch.operation_id=operation;
        if(payload.admission==='unknown')return route.abort('failed');
        if(payload.admission==='rejected')batch={...batch,state:'finished',pending:0,failed:1,retryable_failed:1,admission_state:'rejected',admission_reason:'queue_capacity',finished_at:'2026-10-28T10:00:01+00:00'};
        return route.fulfill({status:payload.admission==='rejected'?429:202,json:{batch,admission:{state:payload.admission,batch_id:batch.batch_id,task_id:null,reason_code:batch.admission_reason,storage_mode:'local'}}});
      }
      if(url.pathname==='/api/admin/broadcast/jobs')return route.fulfill({json:{items:[{operation_id:operation,available:true,batch}]}});
      if(url.pathname.startsWith('/api/admin/broadcast/jobs/'))return route.fulfill({json:batch});
      return route.fulfill({json:{channels:[],rows:[],total:0,days:[]}});
    });
    await page.goto('http://natal.test/admin_daily#broadcast');
    await page.locator('#offer-quick-uid').fill('123');await page.locator('#offer-quick-channel').selectOption('horoscope');
    await page.locator('#btn-offer-quick-send').click();
    await page.locator('#confirm-modal').waitFor({state:'visible'});
    assert.equal(previews,1,'The confirmation uses actual read-only recipient eligibility');
    assert.match(await page.locator('#modal-message').textContent(),/1.*force.*нет/i);
    await page.locator('#modal-confirm').click();
    await page.waitForSelector('#broadcast-jobs-list [data-batch-operation]');
    assert.equal(postCount,1);
    assert.equal((await page.locator('#toast-container').textContent()).includes('Оффер отправлен'),false);
    const stored=await page.evaluate(()=>sessionStorage.getItem('admin-broadcast-batches-v1'));
    const entries=JSON.parse(stored).operations;assert.equal(entries.length,1);
    assert.deepEqual(Object.keys(entries[0]).sort(),['batch_id','operation_id']);
    if(payload.admission!=='rejected'){
      await page.waitForFunction(()=>!document.getElementById('btn-offer-quick-send').disabled);
      await page.locator('#btn-offer-quick-send').click();await page.waitForTimeout(50);
      assert.equal(postCount,1,'The same in-flight command is observed, never reposted');
      batch={...batch,state:'finished',pending:0,unknown_delivery:1,finished_at:'2026-10-28T10:00:02+00:00'};
      await page.evaluate(()=>dispatchEvent(new Event('online')));
      await page.waitForFunction(()=>document.getElementById('broadcast-jobs-list').textContent.includes('Исход неизвестен: 1'));
      assert.equal(await page.locator('#broadcast-jobs-list [data-broadcast-retry]').isDisabled(),true);
    }else{
      assert.match(await page.locator('#broadcast-jobs-list').textContent(),/Отклон|отклон/);
      assert.equal(await page.locator('#broadcast-jobs-list [data-broadcast-retry]').isDisabled(),false);
    }
    assert.equal(postCount,1);
""",
    )


@pytest.mark.browser
@pytest.mark.asyncio
async def test_batch_preview_exclusions_and_admission_never_set_offer_timestamp():
    _run_browser_regression(
        await _payload(),
        r"""
    page.setDefaultTimeout(2500);
    let posted;
    await page.route('**/api/**',route=>{
      const url=new URL(route.request().url());
      if(url.pathname.endsWith('/offer-history'))return route.fulfill({json:{total:2,rows:[123,456].map(user_id=>({user_id,channel:'horoscope',channel_emoji:'⭐',display_name:'Synthetic',is_subscribed:user_id===456,offer_sent_at:null}))}});
      if(url.pathname.endsWith('/preview'))return route.fulfill({json:{requested:2,eligible:1,excluded:{already_subscribed:1},recipient_ids:[123]}});
      if(url.pathname.endsWith('/send-offer-batch')){posted=route.request().postDataJSON();return route.fulfill({status:202,json:{batch:{...payload.batch,total:2,pending:1,skipped:1,operation_id:posted.operation_id},admission:{state:'accepted',batch_id:payload.batch_id,storage_mode:'local'}}});}
      if(url.pathname.includes('/broadcast/jobs'))return route.fulfill({json:{...payload.batch,total:2,pending:1,skipped:1}});
      return route.fulfill({json:{channels:[],rows:[],days:[]}});
    });
    await page.goto('http://natal.test/admin_daily#broadcast');await page.locator('#btn-load-offers').click();
    await page.waitForSelector('.offer-row-chk');await page.locator('#offer-select-all').check();
    await page.locator('#offer-batch-channel').selectOption('horoscope');
    await page.locator('#btn-offer-batch-send').click();await page.locator('#confirm-modal').waitFor({state:'visible'});
    assert.match(await page.locator('#modal-message').textContent(),/2.*1.*already_subscribed.*1/s);
    await page.locator('#modal-confirm').click();await page.waitForSelector('#broadcast-jobs-list [data-batch-operation]');
    assert.deepEqual(posted.user_ids,[123,456]);assert.equal(posted.force,false);
    assert.equal(await page.locator('#offer-tbody .cell-date-val').count(),0,'Queue admission is not a Telegram receipt');
    assert.match(await page.locator('#offer-tbody').textContent(),/никогда/);
""",
    )


@pytest.mark.browser
@pytest.mark.asyncio
async def test_journal_filters_paginates_literal_fields_and_explains_coverage():
    _run_browser_regression(
        await _payload(),
        r"""
    page.setDefaultTimeout(2500);
    const seen=[];
    const cursor='33333333-3333-4333-8333-333333333333';
    const attack='<img id="journal-injected" src=x>';
    await page.route('**/api/**',route=>{
      const url=new URL(route.request().url());
      if(url.pathname.endsWith('/errors')){
        seen.push(Object.fromEntries(url.searchParams));
        return route.fulfill({json:{coverage_started_at:'2026-10-28T00:00:00+00:00',retention_days:30,next_cursor:url.searchParams.has('cursor')?null:cursor,
          events:[{attempt_id:cursor,batch_id:payload.batch_id,recipient_id:123,channel:'tarot',mode:'delivery',outcome:'unknown_delivery',reason_code:attack,started_at:'2026-10-28T10:00:00+00:00',body:'PRIVATE FULL TEXT MUST NOT RENDER'}]}});
      }
      return route.fulfill({json:{channels:[],rows:[],days:[]}});
    });
    await page.goto('http://natal.test/admin_daily#broadcast');
    await page.locator('#broadcast-journal-channel').selectOption('tarot');
    await page.locator('#broadcast-journal-date').fill('2026-10-28');
    await page.locator('#broadcast-journal-state').selectOption('unknown_delivery');
    await page.locator('#broadcast-journal-reason').fill('telegram_timeout');
    await page.locator('#broadcast-journal-apply').click();
    await page.waitForFunction(()=>document.getElementById('broadcast-journal-body').textContent.includes('telegram_timeout')||document.getElementById('broadcast-journal-body').textContent.includes('journal-injected'));
    assert.equal(await page.locator('#journal-injected').count(),0);
    assert.equal((await page.locator('#broadcast-journal-body').textContent()).includes('PRIVATE FULL TEXT'),false);
    assert.match(await page.locator('#broadcast-journal-coverage').textContent(),/2026.*30/);
    await page.locator('#broadcast-journal-next').click();await page.waitForTimeout(50);
    assert.equal(seen.at(-1).cursor,cursor);assert.equal(seen.at(-1).channel,'tarot');assert.equal(seen.at(-1).state,'unknown_delivery');
    assert.equal(seen.at(-1).reason,'telegram_timeout');assert.equal(seen.at(-1).date,'2026-10-28');
    assert.equal(await page.locator('#broadcast-journal-next').isDisabled(),true);
""",
    )


@pytest.mark.browser
@pytest.mark.asyncio
async def test_reload_batches_poll_together_pause_offline_hidden_and_keep_retry_child_fenced():
    _run_browser_regression(
        await _payload(),
        r"""
    page.setDefaultTimeout(2500);
    const parentOp='44444444-4444-4444-8444-444444444444',childOp='55555555-5555-4555-8555-555555555555';
    let reads=0,posts=0;
    const parent={...payload.batch,operation_id:parentOp,state:'finished',pending:0,failed:1,retryable_failed:1};
    const child={...payload.batch,batch_id:payload.child_id,parent_batch_id:payload.batch_id,operation_id:childOp};
    await page.addInitScript(({parentOp,childOp,batchId,childId})=>{
      sessionStorage.setItem('admin-broadcast-batches-v1',JSON.stringify({operations:[{operation_id:parentOp,batch_id:batchId},{operation_id:childOp,batch_id:childId}],filters:{channel:'tarot'}}));
      window.__hidden=false;window.__offline=false;
      Object.defineProperty(document,'hidden',{get:()=>window.__hidden});
      Object.defineProperty(navigator,'onLine',{get:()=>!window.__offline});
    },{parentOp,childOp,batchId:payload.batch_id,childId:payload.child_id});
    await page.route('**/api/**',route=>{
      const url=new URL(route.request().url());
      if(route.request().method()==='POST'){posts++;return route.fulfill({status:500,json:{}});}
      if(url.pathname==='/api/admin/broadcast/jobs'){
        reads++;const ids=url.searchParams.get('operations').split(',');assert.ok(ids.length<=10);
        return route.fulfill({json:{items:ids.map(operation_id=>({operation_id,available:true,batch:operation_id===parentOp?parent:child}))}});
      }
      return route.fulfill({json:{channels:[],rows:[],events:[]}});
    });
    await page.goto('http://natal.test/admin_daily#broadcast');
    await page.waitForFunction(()=>document.getElementById('broadcast-jobs-list').textContent.includes('Ошибки: 1'));
    assert.equal(await page.locator('[data-batch-operation="'+parentOp+'"] [data-broadcast-retry]').isDisabled(),true,'An existing child fences a second parent retry after reload');
    assert.equal(await page.locator('#broadcast-journal-channel').inputValue(),'tarot');
    assert.equal(posts,0,'Restoring operation IDs only reads status');
    await page.evaluate(()=>{window.__hidden=true;document.dispatchEvent(new Event('visibilitychange'));});
    const paused=reads;await page.waitForTimeout(2150);assert.equal(reads,paused);
    await page.evaluate(()=>{window.__hidden=false;document.dispatchEvent(new Event('visibilitychange'));});
    await page.waitForTimeout(80);assert.ok(reads>paused);
    await page.evaluate(()=>{window.__offline=true;dispatchEvent(new Event('offline'));});
    const offlineReads=reads;await page.waitForTimeout(2150);assert.equal(reads,offlineReads);
    await page.evaluate(()=>{window.__offline=false;dispatchEvent(new Event('online'));});
    await page.waitForTimeout(80);assert.ok(reads>offlineReads);
    await page.evaluate(()=>dispatchEvent(new Event('pagehide')));
    const left=reads;await page.waitForTimeout(2150);assert.equal(reads,left);
    assert.equal(posts,0);
""",
    )


@pytest.mark.browser
@pytest.mark.asyncio
async def test_retry_targets_only_definite_failures_new_operation_and_survives_lost_reply():
    _run_browser_regression(
        await _payload(),
        r"""
    page.setDefaultTimeout(2500);
    let operation,retryOperation,posts=0;
    const parent={...payload.batch,state:'finished',pending:0,total:4,sent:1,failed:1,retryable_failed:1,unknown_delivery:1,skipped:1};
    await page.route('**/api/**',async route=>{
      const url=new URL(route.request().url());
      if(url.pathname.endsWith('/preview'))return route.fulfill({json:{requested:1,eligible:1,excluded:{}}});
      if(url.pathname.endsWith('/send-offer')){
        posts++;operation=route.request().postDataJSON().operation_id;
        const stored=await page.evaluate(()=>JSON.parse(sessionStorage.getItem('admin-broadcast-batches-v1')));
        assert.equal(stored.operations[0].operation_id,operation,'Operation identity exists before POST');
        return route.fulfill({status:202,json:{batch:{...parent,operation_id:operation},admission:{state:'accepted'}}});
      }
      if(url.pathname.endsWith('/retry')){
        posts++;retryOperation=route.request().postDataJSON().operation_id;
        assert.notEqual(retryOperation,operation);assert.deepEqual(Object.keys(route.request().postDataJSON()),['operation_id']);
        return route.abort('failed');
      }
      if(url.pathname.includes('/by-operation/'))return route.fulfill({json:{...payload.batch,batch_id:payload.child_id,parent_batch_id:payload.batch_id,operation_id:retryOperation}});
      if(url.pathname==='/api/admin/broadcast/jobs')return route.fulfill({json:{items:[{operation_id:retryOperation,available:true,batch:{...payload.batch,batch_id:payload.child_id,parent_batch_id:payload.batch_id,operation_id:retryOperation}}]}});
      if(url.pathname.endsWith('/jobs/'+payload.batch_id))return route.fulfill({json:parent});
      return route.fulfill({json:{channels:[],rows:[],events:[]}});
    });
    await page.goto('http://natal.test/admin_daily#broadcast');await page.locator('#offer-quick-uid').fill('123');
    await page.locator('#btn-offer-quick-send').click();await page.locator('#modal-confirm').click();
    const retry=page.locator('[data-batch-operation] [data-broadcast-retry]').first();
    await retry.waitFor();await retry.click();
    assert.match(await page.locator('#modal-message').textContent(),/retryable failed: 1.*sent.*unknown/s);
    await page.locator('#modal-confirm').click();
    await page.waitForFunction(()=>document.querySelectorAll('[data-batch-operation]').length===2);
    await page.waitForTimeout(100);
    assert.equal(await page.locator('[data-batch-operation="'+operation+'"] [data-broadcast-retry]').isDisabled(),true);
    assert.equal(posts,2,'A lost retry acknowledgment never repeats the POST');
""",
    )


@pytest.mark.browser
@pytest.mark.asyncio
async def test_journal_filter_changes_reset_pagination_instead_of_reusing_another_query_cursor():
    _run_browser_regression(
        await _payload(),
        r"""
    page.setDefaultTimeout(2500);const seen=[];
    await page.route('**/api/**',route=>{
      const url=new URL(route.request().url());
      if(url.pathname.endsWith('/errors')){seen.push(Object.fromEntries(url.searchParams));return route.fulfill({json:{events:[],next_cursor:payload.child_id,retention_days:30,coverage_started_at:null}});}
      return route.fulfill({json:{channels:[],rows:[],events:[]}});
    });
    await page.goto('http://natal.test/admin_daily#broadcast');await page.waitForFunction(()=>!document.getElementById('broadcast-journal-next').disabled);
    await page.locator('#broadcast-journal-channel').selectOption('tarot');
    await page.locator('#broadcast-journal-next').click();await page.waitForTimeout(80);
    assert.equal(seen.at(-1).channel,'tarot');assert.equal(seen.at(-1).cursor,undefined,'A cursor belongs to the exact applied filters');
""",
    )


@pytest.mark.browser
@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["proxy_500", "nested_unknown", "outcome_unknown"])
async def test_uncertain_http_admission_keeps_operation_and_only_recovers_by_read(failure):
    _run_browser_regression(
        await _payload(failure=failure),
        r"""
    page.setDefaultTimeout(2500);let posts=0,lookups=0,operation;
    await page.route('**/api/**',route=>{
      const url=new URL(route.request().url());
      if(url.pathname.endsWith('/preview'))return route.fulfill({json:{requested:1,eligible:1,excluded:{}}});
      if(url.pathname.endsWith('/send-offer')){
        posts++;operation=route.request().postDataJSON().operation_id;
        if(payload.failure==='proxy_500')return route.fulfill({status:500,contentType:'text/html',body:'Proxy unavailable'});
        return route.fulfill({status:503,json:payload.failure==='nested_unknown'?{admission:{state:'unknown',reason_code:'admission_unconfirmed'}}:{error:'unconfirmed',outcome_unknown:true}});
      }
      if(url.pathname.includes('/jobs/by-operation/')){lookups++;assert.ok(url.pathname.endsWith(operation));return route.fulfill({json:{...payload.batch,operation_id:operation}});}
      if(url.pathname==='/api/admin/broadcast/jobs')return route.fulfill({json:{items:[{operation_id:operation,available:false,batch:null}]}});
      return route.fulfill({json:{channels:[],rows:[],events:[]}});
    });
    await page.goto('http://natal.test/admin_daily#broadcast');await page.locator('#offer-quick-uid').fill('123');
    await page.locator('#btn-offer-quick-send').click();await page.locator('#modal-confirm').click();
    await page.waitForFunction(()=>!document.getElementById('btn-offer-quick-send').disabled);
    assert.equal(posts,1);assert.equal(lookups,1);
    assert.equal(await page.locator('[data-batch-operation]').count(),1);
    await page.locator('#btn-offer-quick-send').click();await page.waitForTimeout(50);assert.equal(posts,1);
    assert.equal(await page.locator('#confirm-modal').isVisible(),false);
    const saved=await page.evaluate(()=>JSON.parse(sessionStorage.getItem('admin-broadcast-batches-v1')));
    assert.equal(saved.operations[0].operation_id,operation);
""",
    )


@pytest.mark.browser
@pytest.mark.asyncio
@pytest.mark.parametrize("available", [True, False])
async def test_tarot_overview_uses_observed_delivery_only_and_marks_unavailable(available):
    _run_browser_regression(
        await _payload(available=available),
        r"""
    page.setDefaultTimeout(2500);
    await page.route('**/api/**',route=>{
      if(new URL(route.request().url()).pathname.endsWith('/overview'))return route.fulfill({json:{channels:[{id:'tarot',name:'Tarot',emoji:'🔮',subscribers:3,sent_today:99,delivery_journal:payload.available?{available:true,mode:'delivery',sent:2,unknown_delivery:1,coverage_started_at:'2026-10-28'}:{available:false}}]}});
      return route.fulfill({json:{channels:[],rows:[],events:[]}});
    });
    await page.goto('http://natal.test/admin_daily#broadcast');await page.waitForFunction(()=>document.getElementById('channel-cards').textContent.includes('Tarot'));
    const text=await page.locator('#channel-cards').textContent();
    assert.equal(text.includes('99'),false,'Offer counters cannot become readings');
    if(payload.available){assert.match(text,/Раскладов доставлено\s*2/);assert.match(text,/Исход неизвестен\s*1/);assert.match(text,/2026-10-28/);}
    else {assert.match(text,/нет наблюдаемых данных/);assert.equal(text.includes('Раскладов доставлено'),false);}
""",
    )


@pytest.mark.browser
@pytest.mark.asyncio
async def test_late_admission_after_pagehide_preserves_operation_without_starting_background_reads():
    _run_browser_regression(
        await _payload(),
        r"""
    page.setDefaultTimeout(2500);let held,posts=0,reads=0;
    await page.route('**/api/**',route=>{
      const url=new URL(route.request().url());
      if(url.pathname.endsWith('/preview'))return route.fulfill({json:{requested:1,eligible:1,excluded:{}}});
      if(url.pathname.endsWith('/send-offer')){held=route;posts++;return;}
      if(url.pathname.includes('/broadcast/jobs')){reads++;return route.fulfill({json:payload.batch});}
      return route.fulfill({json:{channels:[],rows:[],events:[]}});
    });
    await page.goto('http://natal.test/admin_daily#broadcast');await page.locator('#offer-quick-uid').fill('123');
    await page.locator('#btn-offer-quick-send').click();await page.locator('#modal-confirm').click();
    while(!held)await page.waitForTimeout(10);
    await page.evaluate(()=>dispatchEvent(new Event('pagehide')));const before=reads;
    await held.fulfill({status:202,json:{batch:payload.batch,admission:{state:'accepted'}}});
    await page.waitForTimeout(150);assert.equal(reads,before,'A late POST response cannot restart status reads after pagehide');
    assert.equal(posts,1);assert.ok(await page.evaluate(()=>JSON.parse(sessionStorage.getItem('admin-broadcast-batches-v1')).operations.length===1));
""",
    )
