const assert = require('node:assert/strict');
const fs = require('node:fs');
const { chromium } = require('playwright');
const payload = JSON.parse(fs.readFileSync(0, 'utf8'));
(async () => {
  const browser = await chromium.launch({ headless: true });
  try {
    const page = await browser.newPage();
    page.setDefaultTimeout(4000);
    const errors = [];
    const writes = [];
    page.on('pageerror', error => errors.push(error.message));
    await page.addInitScript(() => {
      const noop = () => {};
      window.roleInjected = false;
      window.readerInjected = () => { window.roleInjected = true; return 0; };
      window.Telegram = { WebApp: { initData: 'synthetic', ready: noop, expand: noop,
        setHeaderColor: noop, setBackgroundColor: noop, onEvent: noop,
        BackButton: { onClick: noop, show: noop, hide: noop },
        MainButton: { onClick: cb => { window.saveCallback = cb; }, setText: noop,
          show: noop, hide: noop, showProgress: noop, hideProgress: noop },
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
    await page.route('**/*', route => {
      const request = route.request();
      const url = new URL(request.url());
      if (url.hostname === 'ui.test' && url.pathname === '/page') {
        return route.fulfill({ body: payload.html, contentType: 'text/html' });
      }
      if (url.pathname === '/webapp/api/settings') {
        assert.equal(request.headers().authorization, 'tma synthetic');
        if (request.method() === 'PATCH') {
          writes.push(request.postDataJSON());
          return route.fulfill({ json: { ok: true } });
        }
        return route.fulfill({ json: { settings, available_models: [model],
          grouped_models: payload.scenario === 'model_grouped'
            ? [{ provider: "Tutor's provider", icon: 'P', models: [model] }] : [] } });
      }
      if (url.pathname === '/webapp/api/roles') {
        return route.fulfill({ json: { roles: [{ id: 23, title: 'Saved <tutor>', prompt }] } });
      }
      if (url.pathname === '/webapp/api/voices') return route.fulfill({ json: { voices: [] } });
      return route.fulfill({ body: '', contentType: 'application/javascript' });
    });
    await page.goto('http://ui.test/page?tab=settings');
    await page.waitForSelector('#s-roles-wrap .chip:nth-child(2)');
    if (payload.scenario.startsWith('role_')) {
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
    if (payload.scenario.startsWith('role_')) assert.equal(writes[0].system_prompt,
      payload.scenario === 'role_standard' ? '' : prompt);
    else assert.equal(writes[0].model, model);
    assert.deepEqual(errors, []);
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
