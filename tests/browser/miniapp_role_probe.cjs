const assert = require('node:assert/strict');
const fs = require('node:fs');
const { chromium } = require('playwright');
const payload = JSON.parse(fs.readFileSync(0, 'utf8'));
function deferred() {
  let resolve;
  const promise = new Promise(done => { resolve = done; });
  return {promise, resolve};
}
(async () => {
  const browser = await chromium.launch({ headless: true });
  try {
    const page = await browser.newPage();
    page.setDefaultTimeout(4000);
    const errors = [];
    const writes = [];
    const deletes = [];
    const patchStarted = deferred(), secondPatchStarted = deferred(), releasePatch = deferred();
    const deleteStarted = deferred(), releaseDelete = deferred(), releaseSettings = deferred();
    const deleteScenario = payload.scenario.startsWith('delete_');
    if (deleteScenario) await page.clock.install();
    page.on('pageerror', error => errors.push(error.message));
    await page.addInitScript(() => {
      const noop = () => {};
      window.roleInjected = false;
      window.readerInjected = () => { window.roleInjected = true; return 0; };
      window.Telegram = { WebApp: { initData: 'synthetic', ready: noop, expand: noop,
        setHeaderColor: noop, setBackgroundColor: noop, onEvent: noop,
        BackButton: { onClick: noop, show: noop, hide: noop },
        MainButton: { onClick: cb => { window.saveCallback = cb; }, setText: noop,
          show: () => {window.saveVisible = true;}, hide: () => {window.saveVisible = false;},
          showProgress: noop, hideProgress: noop },
        CloudStorage: {getItem: (_key, cb) => {window.cachedSettingsCallback = cb;}, setItem: noop},
        HapticFeedback: { selectionChanged: noop, impactOccurred: noop, notificationOccurred: noop },
      } };
    });
    const prompt = payload.scenario === 'role_active_text'
      ? "'-window.readerInjected(1)-'"
      : "You're a tutor. Keep 'quoted' text intact.";
    const model = "provider/tutor's-model";
    const settings = { model: 'initial-model', system_prompt: 'Initial saved prompt',
      ltm_enabled: false, search_enabled: false, temperature: 0.7, tts_temperature: 1,
      thinking_level: 'off', voice_id: null };
    const reasoningScenario = payload.scenario.startsWith('reasoning_');
    const readonly = reasoningScenario && payload.scenario !== 'reasoning_gemini';
    if (reasoningScenario) {
      settings.model = readonly ? 'openai-direct/gpt-6-luna' : 'gemini-3.5-flash-lite';
      settings.thinking_level = 'high';
    }
    await page.route('**/*', async route => {
      const request = route.request();
      const url = new URL(request.url());
      if (url.hostname === 'ui.test' && url.pathname === '/page') {
        return route.fulfill({ body: payload.html, contentType: 'text/html' });
      }
      if (url.pathname === '/webapp/api/settings') {
        assert.equal(request.headers().authorization, 'tma synthetic');
        if (request.method() === 'PATCH') {
          writes.push(request.postDataJSON());
          patchStarted.resolve();
          if (writes.length === 2) secondPatchStarted.resolve();
          if (payload.scenario.startsWith('settings_')) await releasePatch.promise;
          return route.fulfill({ json: { ok: true } });
        }
        if (payload.scenario.startsWith('cloud_server_after_')) await releaseSettings.promise;
        return route.fulfill({ json: { settings, available_models: [model],
          ...(reasoningScenario ? {thinking_editable:!readonly,model_editable:!readonly,
            effective_reasoning:{effort:readonly ? payload.scenario === 'reasoning_default' ? null : 'none' : 'high', source:readonly ? 'admin' : 'user', model:settings.model}} : {}),
          grouped_models: payload.scenario === 'model_grouped'
            ? [{ provider: "Tutor's provider", icon: 'P', models: [model] }] : [] } });
      }
      if (url.pathname === '/webapp/api/roles') {
        return route.fulfill({ json: { roles: [{ id: 23, title: 'Saved <tutor>', prompt }] } });
      }
      if (url.pathname === '/webapp/api/voices') return route.fulfill({ json: { voices: [] } });
      if (url.pathname === '/webapp/api/memories/stats') return route.fulfill({json:{total_memories:1,limit:500}});
      if (url.pathname === '/webapp/api/memories') return route.fulfill({json:{memories:[
        {id:17,content:'Synthetic memory',created_at:'2026-11-11T12:00:00Z',source_type:'manual'},
      ]}});
      if (url.pathname === '/webapp/api/memories/17' && request.method() === 'DELETE') {
        deletes.push(request);
        deleteStarted.resolve();
        await releaseDelete.promise;
        return route.fulfill({status:payload.scenario === 'delete_flush_error' ? 503 : 200,json:{ok:true}});
      }
      return route.fulfill({ body: '', contentType: 'application/javascript' });
    });
    await page.goto('http://ui.test/page?tab=' + (deleteScenario ? 'memories' : 'settings'));
    if (deleteScenario) {
      await page.locator('#mem-17').waitFor();
      await page.locator('#mem-17').hover();
      await page.locator('#mem-17 .memory-delete-hover').click();
      await page.clock.runFor(201);
      await page.evaluate(() => { window.cachedUndo = document.querySelector('.toast-undo').onclick; });
      if (payload.scenario === 'delete_undo_before_send') {
        await page.locator('.toast-undo').click();
        await page.clock.runFor(5001);
        assert.equal(deletes.length,0);
        assert.equal(await page.locator('#mem-17').evaluate(el=>el.style.opacity),'1');
      } else {
        if (payload.scenario.startsWith('delete_flush')) {
          await page.evaluate(() => { window.flushPromise = flushPendingDeletions(); });
        } else await page.clock.runFor(5000);
        await deleteStarted.promise;
        await page.evaluate(() => window.cachedUndo());
        assert.equal(await page.locator('#mem-17').evaluate(el=>el.style.opacity),'0',
          'An undo callback cannot restore a card after its DELETE starts');
        await page.evaluate(() => { flushPendingDeletions(); });
        assert.equal(deletes.length,1);
        releaseDelete.resolve();
        if (payload.scenario === 'delete_flush_error') {
          await page.waitForFunction(() => document.getElementById('mem-17')?.style.opacity === '1');
          assert.match(await page.locator('#mem-stats-text').textContent(),/^1\s*\//);
        } else await page.locator('#mem-17').waitFor({state:'detached'});
        await page.evaluate(() => window.cachedUndo());
        assert.equal(await page.evaluate(() => Object.keys(pendingDeletions).length),0);
      }
      assert.deepEqual(errors,[]);
      return;
    }
    const cached = {d:{settings:{...settings,system_prompt:'Old cached prompt',model:'old-cache-model'},
      available_models:['old-cache-model']},r:{roles:[{id:23,title:'Saved <tutor>',prompt}]},v:{voices:[]}};
    if (payload.scenario.startsWith('cloud_server_after_')) {
      await page.evaluate(value=>window.cachedSettingsCallback(null,JSON.stringify(value)),cached);
      await page.locator('#s-roles-wrap .chip').nth(1).click();
      const saved = payload.scenario === 'cloud_server_after_saved_edit';
      if (saved) await page.evaluate(()=>window.saveCallback());
      releaseSettings.resolve();
      await page.waitForFunction(value=>availableModels.includes(value),model);
      assert.equal(await page.locator('#s-prompt-area').inputValue(),prompt,
        'An authoritative load must retain edits made to the cached draft');
      assert.equal(await page.evaluate(()=>isDirty),!saved);
      assert.deepEqual(errors,[]);
      return;
    }
    await page.waitForSelector('#s-roles-wrap .chip:nth-child(2)');
    if (payload.scenario.startsWith('cloud_')) {
      const edited = payload.scenario === 'cloud_late_edit';
      if (edited) await page.locator('#s-roles-wrap .chip').nth(1).click();
      await page.evaluate(value=>window.cachedSettingsCallback(null,JSON.stringify(value)),cached);
      assert.equal(await page.locator('#s-prompt-area').inputValue(),edited ? prompt : settings.system_prompt,
        'A late CloudStorage callback must not replace the server state or newer edits');
      assert.equal(await page.evaluate(()=>settingsData.model),settings.model);
      assert.deepEqual(errors,[]);
      return;
    }
    if (payload.scenario.startsWith('settings_')) {
      await page.locator('#s-roles-wrap .chip').nth(1).click();
      await page.evaluate(() => {window.firstSave = window.saveCallback();});
      await patchStarted.promise;
      if (payload.scenario === 'settings_duplicate_save') {
        const duplicate = page.evaluate(() => window.saveCallback());
        const winner = await Promise.race([duplicate.then(()=>'returned'),secondPatchStarted.promise.then(()=>'second-request')]);
        assert.equal(winner,'returned','A duplicate save cannot issue another mutation while one is pending');
        assert.equal(writes.length,1);
      } else {
        await page.locator('#s-roles-wrap .chip').nth(0).click();
      }
      releasePatch.resolve();
      await page.evaluate(()=>window.firstSave);
      if (payload.scenario === 'settings_save_newer_edit') {
        assert.equal(await page.locator('#s-prompt-area').inputValue(),'');
        assert.equal(await page.evaluate(()=>settingsData.system_prompt),'');
        assert.equal(await page.evaluate(()=>isDirty),true,'ACK for an earlier draft cannot clear newer edits');
        assert.equal(await page.evaluate(()=>window.saveVisible),true);
        await page.evaluate(()=>window.saveCallback());
        assert.equal(writes.length,2);
        assert.equal(writes[1].system_prompt,'');
      } else assert.equal(await page.evaluate(()=>isDirty),false);
      assert.deepEqual(errors,[]);
      return;
    }
    if (reasoningScenario) {
      if (readonly) {
        const label = await page.locator('#val-thinking').textContent();
        assert.match(label,/Администратор/);
        assert.ok(label.includes(payload.scenario === 'reasoning_default' ? 'По умолчанию модели' : 'Выкл'));
        assert.ok(!label.includes('Высокий'));
        await page.locator('#setting-thinking').click();
        assert.equal(await page.locator('#sheet-overlay').evaluate(el=>el.classList.contains('show')),false);
        await page.evaluate(()=>setThinking('high'));
        assert.equal(await page.locator('#val-thinking').textContent(),label);
      } else {
        await page.locator('#setting-thinking').click();
        await page.locator('#sheet-thinking .sheet-row[data-val="low"]').click();
        assert.match(await page.locator('#val-thinking').textContent(),/Низкий/);
      }
      await page.locator('#s-search + .toggle-track').click();
      assert.equal(await page.locator('#s-search').isChecked(),true);
    } else if (payload.scenario.startsWith('role_')) {
      const standard = payload.scenario === 'role_standard';
      await page.locator('#s-roles-wrap .chip').nth(standard ? 0 : 1).click();
      assert.equal(await page.locator('#s-prompt-area').inputValue(), standard ? '' : prompt);
      assert.equal(await page.locator('#s-roles-wrap .chip.active').textContent(), standard ? 'Стандартная' : 'Saved <tutor>');
      assert.equal(await page.evaluate(() => window.roleInjected), false);
    } else {
      await page.evaluate(() => openSheet('sheet-models'));
      await page.locator('#sheet-models .sheet-row').click();
      assert.ok((await page.locator('#val-model').textContent()).includes("Tutor's model"));
      assert.equal(await page.locator('#sheet-overlay').evaluate(el => el.classList.contains('show')), false);
      assert.equal(await page.locator('#sheet-models .sheet-row.active').count(), 1);
    }
    await page.evaluate(() => window.saveCallback());
    assert.equal(writes.length, 1);
    if (reasoningScenario) {
      assert.equal(writes[0].search_enabled,true);
      if (readonly) {
        assert.equal(Object.hasOwn(writes[0],'thinking_level'),false);
        assert.equal(Object.hasOwn(writes[0],'model'),false);
      } else assert.equal(writes[0].thinking_level,'low');
    } else if (payload.scenario.startsWith('role_')) assert.equal(writes[0].system_prompt,
      payload.scenario === 'role_standard' ? '' : prompt);
    else assert.equal(writes[0].model, model);
    assert.deepEqual(errors, []);
    if (reasoningScenario && payload.mobile) {
      await page.setViewportSize({width:390,height:844});
      await page.locator('#setting-thinking').scrollIntoViewIfNeeded();
      assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=window.innerWidth),true);
      await page.screenshot({path:payload.mobile});
    }
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
