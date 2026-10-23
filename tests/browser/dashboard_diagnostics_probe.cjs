/* Real templates and scripts; all HTTP responses below are synthetic and offline. */
const assert = require('node:assert/strict');
const path = require('node:path');
let input = '';
process.stdin.setEncoding('utf8');
process.stdin.on('data', chunk => { input += chunk; });
process.stdin.on('end', async () => {
  const data = JSON.parse(input);
  const { chromium } = require(data.playwright_module);
  const browser = await chromium.launch({ headless: true });
  try {
    const page = await browser.newPage({ viewport: { width: 1366, height: 900 }, reducedMotion: 'reduce' });
    page.setDefaultTimeout(3000);
    const errors = [];
    page.on('pageerror', error => errors.push(error.message));
    // Deliver a synthetic SSE response through the browser's native EventSource
    // only after the canonical failed-probe snapshot has rendered.
    let releaseEvents;
    const eventsReady = new Promise(resolve => { releaseEvents = resolve; });
    let dashboardReads = 0, failDashboard = false;
    const usage = {
      available: true, rows: [
        { model_name: 'gemini-fixture', key_hash: 'hash-one', api_key_preview: 'one***', request_count: 75,
          daily_limit: 100, admission_limit: 80, is_available: true, usage_date: '2026-10-23',
          settings_revision: 4, settings_degraded: false, usage_timezone: 'America/Los_Angeles' },
        { model_name: 'gemini-fixture', key_hash: 'hash-two', api_key_preview: 'two***', request_count: 80,
          daily_limit: 100, admission_limit: 80, is_available: false },
        { model_name: 'gemini-unlimited', key_hash: 'hash-one', api_key_preview: 'one***', request_count: 0,
          daily_limit: null, admission_limit: null, is_available: true },
        { model_name: '<img src=x onerror=alert(1)>', key_hash: 'hash-one', request_count: 1,
          daily_limit: 1, admission_limit: 1, is_available: false },
      ],
    };
    const snapshot = {
      overview: { system: { cpu_percent: 22, memory_percent: 33, disk_percent: 44 },
        services: { database: 'connected', redis: 'connected', bot: 'running' },
        metrics: { total_requests: 12, average_response_time_ms: 150, error_count: 0,
          error_rate_percent: 0, cache_hit_rate_percent: 25 }, summarization: {} },
      providers: { gemini: usage, tavily: { available: true, rows: [] },
        circuit_breakers: { available: true, items: {} }, key_health: { available: true, truncated: false, rows: [
          { key_hash: 'hash-one', key: 'one***', model: 'gemini-fixture', status: 'suspended',
            failures: 2, suspended_until: '2099-01-01T00:00:00Z', last_error: 'Synthetic rate limit' },
        ] } },
      infrastructure: { database: { available: true, status: 'connected', pool_size: 5,
        active_connections: 2, free_size: 3, response_time_ms: 7 }, queue: {},
        cache: { available: true, memory_items: 12, memory_capacity: 1200, memory_utilization_percent: 1, redis_keys: 9 } },
      errors: { available: true, count: 0, recent: [] }, generated_at: new Date().toISOString(),
    };
    const controls = { revision: 4, degraded: false, processes: [
      { id: 'chat', title: 'Общение', group: 'Общение', editable: true, models: ['gemini-fixture', 'gemini-unlimited'], strategies: ['sequential'] },
      { id: 'search', title: 'Поиск', group: 'Поиск', editable: true, models: ['gemini-fixture'], strategies: ['sequential'] },
      { id: 'tts.delivery', title: 'Провайдеры TTS', editable: true, capabilities: ['provider_order'], models: ['gemini', 'elevenlabs'] },
    ], prompts: [], audio: [], catalogs: [{ provider: 'gemini', models: ['gemini-catalog-only'] }],
    limits: [], history: [], commands: { available: true, commands: [], revision: 4 } };
    await page.route('**/*', async route => {
      const url = new URL(route.request().url());
      if (url.pathname === '/api/events') {
        await eventsReady;
        return route.fulfill({ contentType: 'text/event-stream', body: 'retry: 60000\ndata: ' + JSON.stringify({
          services: { database: 'connected' }, metrics: { total_requests: 19 }, queue: { pending: 3, processing: 2 },
        }) + '\n\n' });
      }
      if (url.pathname === '/api/dashboard') {
        dashboardReads += 1;
        return failDashboard ? route.fulfill({ status: 503, json: { error: 'Synthetic unavailable source' } })
          : route.fulfill({ json: snapshot });
      }
      if (url.pathname === '/api/admin/controls') return route.fulfill({ json: controls });
      if (data.assets[url.pathname]) return route.fulfill({
        contentType: url.pathname.endsWith('.css') ? 'text/css' : 'text/javascript', body: data.assets[url.pathname],
      });
      if (url.pathname === '/' || url.pathname === '/controls') return route.fulfill({ contentType: 'text/html', body: data.html });
      return route.abort();
    });
    if (data.surface === 'dashboard') {
      await page.goto('http://gemaibot.test/');
      await page.locator('#live-status').filter({ hasText: 'LIVE' }).waitFor();
      assert.equal(await page.locator('#db-pool').textContent(), '5');
      snapshot.infrastructure.database = { available: false };
      snapshot.infrastructure.cache = { available: false };
      snapshot.overview.system = {};
      snapshot.overview.services.database = 'unknown';
      await page.evaluate(() => refreshAll());
      assert.equal(await page.locator('#db-pool').textContent(), '—', 'Lost source must clear earlier pool values');
      assert.equal(await page.locator('#c-redis-hits').textContent(), '—', 'Lost cache source must clear earlier capacity');
      assert.equal(await page.evaluate(() => cpuHistory.length), 1, 'Missing CPU readings must never become zero samples');
      assert.equal(await page.locator('#svc-db').evaluate(element => element.classList.contains('dot-warn')), true);
      releaseEvents();
      await page.locator('#req-count').filter({ hasText: /^19$/ }).waitFor();
      assert.equal(await page.locator('#req-count').textContent(), '19', 'SSE still updates real-time counters');
      assert.equal(await page.locator('#q-running').textContent(), '2');
      assert.equal(await page.locator('#svc-db').evaluate(element => element.classList.contains('dot-warn')), true,
        'Pool-only SSE messages must not overwrite canonical probed database health');
      await page.locator('#tab-providers').click();
      const limited = page.locator('#key-tbody tr').filter({ hasText: 'two***' });
      assert.match(await limited.innerText(), /80/);
      assert.doesNotMatch(await limited.innerText(), /Available|Exhausted/);
      assert.match(await limited.innerText(), /Blocked|threshold/i);
      const unlimited = page.locator('#key-tbody tr').filter({ hasText: 'gemini-unlimited' });
      assert.match(await unlimited.innerText(), /No local limit/i);
      assert.equal(await page.locator('#key-tbody img').count(), 0);
      await page.locator('#tab-errors').click();
      assert.doesNotMatch(await page.locator('#error-log').innerText(), /all systems normal/i);
      failDashboard = true;
      await page.evaluate(() => refreshAll());
      assert.equal(await page.locator('#live-status').textContent(), 'STALE');
      await page.locator('#tab-providers').click();
      await page.evaluate(() => window.scrollTo({ top: 0, behavior: 'instant' }));
      await page.screenshot({ path: path.join(data.screenshots, 'dashboard-desktop.png'), fullPage: true });
    } else {
      await page.goto('http://gemaibot.test/controls#diagnostics');
      await page.locator('#diagnostics-list article').first().waitFor();
      assert.equal(dashboardReads, 1, 'Diagnostics activation performs one shared snapshot read');
      const model = page.locator('#diagnostics-list article[data-model="gemini-fixture"]');
      assert.match(await model.innerText(), /155/);
      assert.match(await model.innerText(), /2 маршрута/);
      assert.match(await model.innerText(), /1 из 2/);
      assert.match(await model.innerText(), /не проверялся/i);
      await model.locator('details').first().locator('summary').click();
      assert.match(await model.innerText(), /80/);
      assert.match(await model.innerText(), /Synthetic rate limit/);
      assert.match(await model.innerText(), /приостановлен/i);
      const catalogOnly = page.locator('#diagnostics-list article[data-model="gemini-catalog-only"]');
      assert.match(await catalogOnly.innerText(), /Нет данных RPD/);
      assert.doesNotMatch(await catalogOnly.innerText(), /0 запросов/);
      assert.equal(await page.locator('#diagnostics-list article[data-model="gemini"]').count(), 0,
        'Provider-order tokens must never become model assignments');
      assert.equal(await page.locator('#diagnostics-list img').count(), 0);
      await page.locator('#diagnostics-search').fill('gemini-fixture');
      assert.equal(await page.locator('#diagnostics-list article:visible').count(), 1);
      await model.getByRole('link', { name: /Общение/ }).click();
      assert.equal(await page.locator('#processes').isVisible(), true);
      assert.equal(await page.locator('#process-list article:visible').count(), 1);
      await page.locator('.sidebar a[href="#diagnostics"]').click();
      assert.equal(dashboardReads, 1, 'Section navigation reuses the loaded snapshot');
      await page.locator('#diagnostics-search').fill('');
      await page.locator('#diagnostics-filter').selectOption('attention');
      assert.equal(await page.locator('#diagnostics-list article:visible').count(), 2);
      await page.locator('#diagnostics-filter').selectOption('unmeasured');
      assert.equal(await page.locator('#diagnostics-list article:visible').count(), 1);
      await page.locator('#diagnostics-filter').selectOption('');
      await page.locator('#diagnostics-search').fill('gemini-fixture');
      failDashboard = true;
      await page.locator('#diagnostics-refresh').click();
      await page.locator('#diagnostics-status').filter({ hasText: /устарели/i }).waitFor();
      assert.match(await model.innerText(), /155/, 'A failed refresh preserves explicitly stale evidence');
      await page.evaluate(() => window.scrollTo({ top: 0, behavior: 'instant' }));
      await page.screenshot({ path: path.join(data.screenshots, 'diagnostics-desktop.png'), fullPage: true });
      failDashboard = false;
      snapshot.providers.key_health = { available: true, rows: [], summary: { truncated: true, total_key_model_pairs: 21 } };
      usage.rows[0].settings_revision = 3;
      usage.rows[0].settings_degraded = true;
      await page.locator('#diagnostics-refresh').click();
      await page.locator('#diagnostics-summary').filter({ hasText: /0 из 21/ }).waitFor();
      assert.match(await model.innerText(), /Состояние ключа неизвестно/);
      assert.match(await page.locator('#diagnostics-summary').innerText(), /ревизия 3/);
      assert.match(await page.locator('#diagnostics-status').innerText(), /хранилище настроек недоступно/);
    }
    await page.setViewportSize({ width: 390, height: 844 });
    await page.evaluate(() => window.scrollTo({ top: 0, behavior: 'instant' }));
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth), true);
    await page.screenshot({ path: path.join(data.screenshots, `${data.surface}-mobile.png`), fullPage: true });
    assert.deepEqual(errors, []);
    process.stdout.write(`Offline ${data.surface} evidence and responsive interactions passed.\n`);
  } finally { await browser.close(); }
}).on('error', error => { console.error(error); process.exitCode = 1; });
