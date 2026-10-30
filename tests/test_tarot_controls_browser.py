"""Tarot operation controls with the application's real CSP and offline transport."""

import pytest

from tests.test_natal_web_report import _run_browser_regression


@pytest.mark.asyncio
@pytest.mark.browser
async def test_tarot_modes_model_confirmation_and_partial_result():
    from app.web import quart_app

    client = quart_app.test_client()
    async with client.session_transaction() as session:
        session["authenticated"] = True
    response = await client.get("/admin_daily")
    assert response.status_code == 200
    _run_browser_regression(
        {"html": (await response.get_data()).decode("utf-8"), "headers": dict(response.headers)},
        r"""
    page.setDefaultTimeout(2500);
    const writes = [];
    const results = new Map();
    let outcome = { generated: 0, skipped: 156, failed: 0, remaining: 0, locked: false };
    await page.route('**/api/**', route => {
      const path = new URL(route.request().url()).pathname;
      let body = {};
      if (path.endsWith('/tarot/models')) body = { models: ['gemini-test'], effective_models: ['primary', 'reserve'], variants: 156 };
      if (path.endsWith('/tarot/status')) body = { subscribers: 0, today: { cards: [], total: 156 }, tomorrow: { cards: [], total: 156 } };
      if (path.endsWith('/daily/jobs/preview')) {
        const command = route.request().postDataJSON();
        body = {work_items:156,readiness:{total:156},effective_policy:{models:command.model ? [command.model] : ['primary','reserve'],explicit:!!command.model}};
      }
      if (path.endsWith('/daily/jobs')) {
        writes.push(route.request().postDataJSON()); const id=`job-${writes.length}`;
        results.set(id,{task_id:id,state:outcome.locked || outcome.failed ? 'failed' : 'succeeded',reason_code:outcome.locked ? 'generation_busy' : null,result_summary:{...outcome}});
        return route.fulfill({status:202,json:{job_id:id,state:'accepted'}});
      }
      if (path.includes('/daily/jobs/job-')) body=results.get(path.split('/').pop());
      return route.fulfill({ json: body });
    });
    await page.goto('http://natal.test/admin_daily#tarot');
    await page.locator('#tarot-regen-date').fill('2026-10-28');
    await page.locator('#tarot-missing-btn').click();
    await page.locator('#confirm-modal').waitFor({state:'visible'});
    assert.match(await page.locator('#modal-message').textContent(), /2026-10-28.*156.*primary.*reserve.*missing/);
    await page.locator('#modal-confirm').click();
    await page.waitForFunction(() => document.getElementById('tarot-result').textContent.includes('Пропущено: 156'))
      .catch(async () => {throw new Error(await page.locator('#tarot-result').textContent());});
    assert.deepEqual({...writes[0],operation_id:undefined}, { kind:'tarot.prepare', date: '2026-10-28', mode: 'missing', model: null, language: 'ru', operation_id:undefined });
    assert.match(await page.locator('#tarot-result').textContent(), /Создано: 0/);
    await page.locator('#tarot-model').selectOption('gemini-test');
    outcome = { generated: 3, skipped: 0, failed: 2, remaining: 151, locked: false };
    await page.locator('#tarot-replace-btn').click();
    await page.locator('#confirm-modal').waitFor({state:'visible'});
    assert.match(await page.locator('#modal-message').textContent(), /gemini-test.*replace/);
    await page.locator('#modal-confirm').click();
    await page.waitForFunction(() => document.getElementById('tarot-result').textContent.includes('Ошибок: 2'));
    assert.equal(writes[1].mode, 'replace');
    assert.equal(writes[1].model, 'gemini-test');
    assert.match(await page.locator('#tarot-result').textContent(), /Осталось: 151/);
    outcome = { generated: 0, skipped: 0, failed: 0, remaining: 156, locked: true };
    await page.locator('#tarot-missing-btn').click();
    await page.locator('#confirm-modal').waitFor({state:'visible'});
    await page.locator('#modal-confirm').click();
    await page.waitForFunction(() => document.getElementById('tarot-result').textContent.includes('Уже выполняется'));
    assert.equal(writes.length, 3, 'No automatic retry of a locked or partial operation');
""",
    )
