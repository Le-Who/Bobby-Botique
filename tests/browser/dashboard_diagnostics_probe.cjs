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
    let releaseCanonicalEvents;
    const canonicalEventsReady = new Promise(resolve => { releaseCanonicalEvents = resolve; });
    let firstEvent = true;
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
      sources: {
        metrics: { available: true, stale: false, observed_at: Date.now() / 1000, scope: 'process' },
        queue: { available: true, stale: true, observed_at: Date.now() / 1000 - 25, scope: 'process' },
      },
      overview: { system: { cpu_percent: 22, memory_percent: 33, disk_percent: 44 },
        services: { database: 'connected', redis: 'connected', bot: 'running' },
        metrics: { total_requests: 12000, traffic: { timezone: 'Europe/Kyiv', periods: {
          today: { start: '2026-10-23T00:00:00+03:00', end: '2026-10-23T14:30:00+03:00', user_requests: 4, api_attempts: 11, available: true },
          last_7_days: { start: '2026-10-17T00:00:00+03:00', end: '2026-10-23T14:30:00+03:00', user_requests: 24, api_attempts: 61, available: true,
            complete: false, coverage: 'partial', observed_start: '2026-10-22T10:15:00+03:00', reason: 'API recording started during this period <img src=x onerror=alert(1)>' },
          last_30_days: { start: '2026-09-24T00:00:00+03:00', end: '2026-10-23T14:30:00+03:00', user_requests: 104, api_attempts: 311, available: true },
        } }, average_response_time_ms: 150, error_count: 0,
          error_rate_percent: 0, cache_hit_rate_percent: 25 }, summarization: {} },
      providers: { gemini: usage, tavily: { available: true, rows: [
        { key_hash: 'tavily-normal', api_key_preview: 'normal***', credit_usage: 0, credit_limit: 1000,
          admission_limit: 970, usage_percent: 0, is_available: true },
        { key_hash: 'tavily-threshold', api_key_preview: 'threshold***', credit_usage: 970, credit_limit: 1000,
          admission_limit: 970, usage_percent: 97, is_available: false },
        { key_hash: 'tavily-zero-threshold', api_key_preview: 'zero-threshold***', credit_usage: 0, credit_limit: 1000,
          admission_limit: 0, usage_percent: 0, is_available: false },
        { key_hash: 'tavily-exhausted', api_key_preview: 'exhausted***', credit_usage: 1000, credit_limit: 1000,
          admission_limit: 970, usage_percent: 100, is_available: false },
        { key_hash: 'tavily-unknown', api_key_preview: 'unknown***', credit_usage: 0, credit_limit: 1000, usage_percent: 0 },
        { key_hash: 'tavily-missing', api_key_preview: 'missing***', credit_usage: null, credit_limit: null,
          usage_percent: null, is_available: true },
        { key_hash: 'tavily-invalid', api_key_preview: 'invalid***', credit_usage: '<img src=x onerror=alert(1)>',
          credit_limit: 'invalid', usage_percent: 'not-a-number', is_available: true },
      ] },
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
        if (!firstEvent) {
          await canonicalEventsReady;
          return route.fulfill({ contentType: 'text/event-stream', body: 'retry: 60000\ndata: ' + JSON.stringify({
            metrics: { traffic: { ...snapshot.overview.metrics.traffic, periods: {
              ...snapshot.overview.metrics.traffic.periods,
              last_7_days: { ...snapshot.overview.metrics.traffic.periods.last_7_days, user_requests: 25, api_attempts: 62 },
            } } },
          }) + '\n\n' });
        }
        firstEvent = false;
        await eventsReady;
        return route.fulfill({ contentType: 'text/event-stream', body: 'retry: 10\ndata: ' + JSON.stringify({
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
      assert.match(await page.locator('[data-source="queue"]').textContent(), /stale/);
      assert.match(await page.locator('[data-source="metrics"]').textContent(), /current/);
      for (const state of ['starting', 'ready', 'draining', 'stopped', 'failed']) {
        // The explicit supervisor state must win over a legacy service value.
        snapshot.overview.lifecycle = { state, ready: state === 'ready' };
        await page.evaluate(() => refreshAll());
        assert.equal(await page.locator('#svc-bot').getAttribute('aria-label'), state);
        assert.equal(await page.locator('#svc-bot').evaluate(el => el.classList.contains('dot-ok')), state === 'ready');
        if (state === 'failed') assert.equal(await page.locator('#svc-bot').evaluate(el => el.classList.contains('dot-err')), true);
      }
      const staleTraffic = { available: true, stale: true, observed_at: Date.now() / 1000 - 25, scope: 'database' };
      const currentMemory = { available: true, stale: false, observed_at: Date.now() / 1000, scope: 'process' };
      const staleRedis = { available: true, stale: true, observed_at: Date.now() / 1000 - 25, scope: 'redis' };
      snapshot.sources.traffic = staleTraffic;
      snapshot.sources.memory_cache = currentMemory;
      snapshot.sources.redis_cache = staleRedis;
      snapshot.infrastructure.cache = { ...snapshot.infrastructure.cache, memory_available: true, redis_available: true,
        memory_source: currentMemory, redis_source: staleRedis };
      await page.evaluate(() => refreshAll());
      assert.equal(await page.locator('#req-count').textContent(), '4', 'Last-good traffic stays visible while marked stale');
      assert.match(await page.locator('#traffic-note').textContent(), /stale.*traffic/i);
      assert.match(await page.locator('#cache-source-status').textContent(), /memory: current.*redis: stale/i);
      snapshot.sources.traffic = { ...staleTraffic, available: false, observed_at: Date.now() / 1000 - 61 };
      snapshot.sources.redis_cache = { ...staleRedis, available: false, observed_at: Date.now() / 1000 - 61 };
      snapshot.infrastructure.cache = { ...snapshot.infrastructure.cache, redis_available: false, redis_keys: null,
        redis_source: snapshot.sources.redis_cache };
      await page.evaluate(() => refreshAll());
      assert.equal(await page.locator('#req-count').textContent(), 'Unavailable', 'Unavailable traffic must not show cached counts as current');
      assert.equal(await page.locator('#c-mem-hits').textContent(), '12', 'Healthy memory survives the Redis source outage');
      assert.equal(await page.locator('#c-mem-items').textContent(), 'Unavailable');
      assert.match(await page.locator('[data-source="redis_cache"]').textContent(), /unavailable/);
      assert.match(await page.locator('#cache-source-status').textContent(), /memory: current.*redis: unavailable/i);
      snapshot.sources.traffic = { ...staleTraffic, stale: false, available: true, observed_at: Date.now() / 1000 };
      snapshot.sources.redis_cache = { ...staleRedis, stale: false, available: true, observed_at: Date.now() / 1000 };
      snapshot.infrastructure.cache = { ...snapshot.infrastructure.cache, redis_available: true, redis_keys: 9,
        redis_source: snapshot.sources.redis_cache };
      await page.evaluate(() => refreshAll());
      const beforeHidden = dashboardReads;
      await page.evaluate(async () => {
        Object.defineProperty(document, 'hidden', { configurable: true, value: true });
        document.dispatchEvent(new Event('visibilitychange'));
        await refreshAll();
      });
      assert.equal(dashboardReads, beforeHidden, 'Hidden tab must stop HTTP polling');
      const visibleRefresh = page.waitForResponse(response => new URL(response.url()).pathname === '/api/dashboard');
      await page.evaluate(() => {
        Object.defineProperty(document, 'hidden', { configurable: true, value: false });
        for (let i = 0; i < 3; i++) document.dispatchEvent(new Event('visibilitychange'));
      });
      await visibleRefresh;
      assert.equal(dashboardReads, beforeHidden + 1, 'Visibility burst shares one HTTP refresh');
      assert.equal(await page.locator('#req-count').textContent(), '4', 'User requests come only from the selected calendar period');
      assert.equal(await page.locator('#api-attempt-count').textContent(), '11');
      assert.match(await page.locator('#traffic-range').textContent(), /23 Oct 2026.*Kyiv/);
      await page.getByLabel('Traffic period').selectOption('last_7_days');
      assert.equal(await page.locator('#req-count').textContent(), '24');
      assert.equal(await page.locator('#api-attempt-count').textContent(), '61');
      assert.match(await page.locator('#traffic-range').textContent(), /17 Oct 2026/);
      assert.match(await page.locator('#traffic-note').textContent(), /Partial API coverage.*22 Oct 2026.*10:15.*Kyiv/);
      assert.equal(await page.locator('#traffic-note img').count(), 0);
      await page.getByLabel('Traffic period').selectOption('last_30_days');
      assert.equal(await page.locator('#req-count').textContent(), '104');
      assert.equal(await page.locator('#api-attempt-count').textContent(), '311');
      await page.getByLabel('Traffic period').selectOption('last_7_days');
      assert.equal(await page.locator('#db-pool').textContent(), '5');
      const cpuSamplesBeforeFailure = await page.evaluate(() => cpuHistory.length);
      snapshot.infrastructure.database = { available: false };
      snapshot.infrastructure.cache = { available: false };
      snapshot.overview.system = {};
      snapshot.overview.services.database = 'unknown';
      snapshot.sources.queue = { available: false, stale: true, observed_at: Date.now() / 1000 - 61, scope: 'process' };
      await page.evaluate(() => refreshAll());
      assert.match(await page.locator('[data-source="queue"]').textContent(), /unavailable/);
      assert.match(await page.locator('[data-source="metrics"]').textContent(), /current/);
      assert.equal(await page.locator('#db-pool').textContent(), '—', 'Lost source must clear earlier pool values');
      assert.equal(await page.locator('#c-redis-hits').textContent(), '—', 'Lost cache source must clear earlier capacity');
      assert.equal(await page.evaluate(() => cpuHistory.length), cpuSamplesBeforeFailure, 'Missing CPU readings must never become zero samples');
      assert.equal(await page.locator('#svc-db').evaluate(element => element.classList.contains('dot-warn')), true);
      releaseEvents();
      await page.locator('#q-running').filter({ hasText: /^2$/ }).waitFor({ state: 'attached' });
      assert.equal(await page.locator('#req-count').textContent(), '24', 'Legacy SSE totals never overwrite period counters');
      assert.equal(await page.locator('#traffic-period').inputValue(), 'last_7_days');
      releaseCanonicalEvents();
      await page.locator('#req-count').filter({ hasText: /^25$/ }).waitFor();
      assert.equal(await page.locator('#api-attempt-count').textContent(), '62', 'Canonical SSE preserves selected period');
      assert.equal(await page.locator('#q-running').textContent(), '2');
      assert.equal(await page.locator('#svc-db').evaluate(element => element.classList.contains('dot-warn')), true,
        'Pool-only SSE messages must not overwrite canonical probed database health');
      await page.locator('#tab-providers').click();
      const groups = page.locator('#gemini-models details');
      assert.equal(await groups.count(), 3, 'Each exact model ID has one group');
      assert.equal(await page.locator('#gemini-models details[open]').count(), 0, 'Models start collapsed');
      const fixture = page.locator('#gemini-models details[data-model="gemini-fixture"]');
      assert.match(await fixture.locator('summary').innerText(), /2 keys/);
      assert.match(await fixture.locator('summary').innerText(), /155/);
      await fixture.locator('summary').focus();
      await page.keyboard.press('Enter');
      assert.equal(await fixture.getAttribute('open'), '');
      const limited = fixture.locator('tr').filter({ hasText: 'two***' });
      assert.match(await limited.innerText(), /80/);
      assert.doesNotMatch(await limited.innerText(), /Available|Exhausted/);
      assert.match(await limited.innerText(), /Blocked|threshold/i);
      const unlimited = page.locator('#gemini-models details[data-model="gemini-unlimited"]');
      await unlimited.locator('summary').click();
      assert.match(await unlimited.innerText(), /No local limit/i);
      assert.equal(await page.locator('#gemini-models img').count(), 0);
      await page.getByLabel('Search models or masked keys').fill('two***');
      assert.equal(await page.locator('#gemini-models details:visible').count(), 1);
      assert.match(await fixture.locator('summary').innerText(), /2 keys/, 'Search retains complete model totals');
      assert.equal(await fixture.locator('tbody tr:visible').count(), 1, 'Masked key search isolates matching key rows');
      await page.getByLabel('Sort models').selectOption('used');
      await page.evaluate(() => refreshAll());
      assert.equal(await page.getByLabel('Search models or masked keys').inputValue(), 'two***');
      assert.equal(await page.getByLabel('Sort models').inputValue(), 'used');
      assert.equal(await fixture.getAttribute('open'), '');
      await page.getByLabel('Search models or masked keys').fill('no-match');
      assert.match(await page.locator('#keys-empty').innerText(), /No models or masked keys match/i);
      await page.getByLabel('Search models or masked keys').fill('');
      assert.equal(await unlimited.getAttribute('open'), '', 'Filtered-out groups retain expansion state');
      assert.equal(await groups.first().getAttribute('data-model'), 'gemini-fixture', 'Usage sort is descending');
      await page.getByLabel('Sort models').selectOption('pressure');
      assert.equal(await groups.first().getAttribute('data-model'), '<img src=x onerror=alert(1)>', 'Pressure uses per-key admission limits');
      await page.getByRole('button', { name: 'Collapse all models' }).click();
      assert.equal(await page.locator('#gemini-models details[open]').count(), 0);
      await page.getByRole('button', { name: 'Expand all models' }).click();
      assert.equal(await page.locator('#gemini-models details[open]').count(), 3);
      usage.rows.push(
        { model_name: 'gemini-fixture-preview', api_key_preview: 'preview***', request_count: 7, daily_limit: null, admission_limit: null, is_available: true },
        { model_name: 'gemini-unknown', api_key_preview: '<img src=x onerror=alert(1)>', request_count: '<img src=x onerror=alert(1)>', daily_limit: 'invalid', is_available: true },
      );
      await page.evaluate(() => refreshAll());
      assert.equal(await groups.count(), 5, 'Related model IDs never merge');
      const unknown = page.locator('#gemini-models details[data-model="gemini-unknown"]');
      assert.equal(await unknown.getAttribute('open'), null, 'New models remain collapsed during refresh');
      await unknown.locator('summary').click();
      assert.match(await unknown.locator('summary').innerText(), /Usage unknown/, 'Missing usage never presents a zero total');
      assert.match(await unknown.innerText(), /Unknown/);
      assert.equal(await unknown.locator('tbody td').nth(1).textContent(), '—', 'Invalid usage never becomes zero or markup');
      assert.equal(await page.locator('#gemini-models img').count(), 0);
      snapshot.overview.metrics.traffic.periods.last_7_days.available = false;
      await page.evaluate(() => refreshAll());
      assert.equal(await page.locator('#req-count').textContent(), 'Unavailable');
      assert.equal(await page.locator('#api-attempt-count').textContent(), 'Unavailable');
      snapshot.overview.metrics.traffic.periods.last_7_days.available = true;
      snapshot.overview.metrics.traffic.periods.today.user_requests = 0;
      snapshot.overview.metrics.traffic.periods.today.api_attempts = 0;
      await page.evaluate(() => refreshAll());
      await page.locator('#tab-overview').click();
      await page.getByLabel('Traffic period').selectOption('today');
      assert.equal(await page.locator('#req-count').textContent(), '0', 'Measured zero is distinct from unavailable');
      await page.getByLabel('Traffic period').selectOption('last_7_days');
      await page.locator('#tab-providers').click();
      const tavilyRow = name => page.locator('#tavily-tbody tr').filter({
        has: page.getByRole('cell', { name: `${name}***`, exact: true }),
      });
      assert.match(await tavilyRow('zero-threshold').innerText(), /Blocked at local threshold/i);
      assert.doesNotMatch(await tavilyRow('zero-threshold').innerText(), /Near Limit|Available|Exhausted/i);
      assert.match(await tavilyRow('normal').innerText(), /Local credits admit/i);
      assert.match(await tavilyRow('threshold').innerText(), /Blocked at local threshold/i);
      assert.match(await tavilyRow('exhausted').innerText(), /Exhausted/i);
      assert.match(await tavilyRow('unknown').innerText(), /Unknown/i);
      for (const name of ['missing', 'invalid']) {
        const row = tavilyRow(name);
        assert.match(await row.innerText(), /Unknown/i);
        assert.equal(await row.locator('td').nth(1).innerText(), '—');
        assert.equal(await row.locator('td').nth(2).innerText(), '—');
        assert.equal(await row.locator('td').nth(3).innerText(), '—');
        assert.equal(await row.locator('img').count(), 0);
      }
      await page.locator('#tab-errors').click();
      assert.doesNotMatch(await page.locator('#error-log').innerText(), /all systems normal/i);
      failDashboard = true;
      await page.evaluate(() => refreshAll());
      assert.equal(await page.locator('#live-status').textContent(), 'STALE');
      failDashboard = false;
      snapshot.providers.gemini = { available: false, rows: [] };
      delete snapshot.overview.metrics.traffic;
      await page.evaluate(() => refreshAll());
      assert.equal(await page.locator('#req-count').textContent(), 'Unavailable', 'Missing traffic never falls back to legacy totals');
      assert.equal(await page.locator('#api-attempt-count').textContent(), 'Unavailable');
      await page.locator('#tab-providers').click();
      assert.match(await page.locator('#keys-empty').innerText(), /Gemini metrics unavailable/);
      snapshot.providers.gemini = { available: true, rows: [] };
      await page.evaluate(() => refreshAll());
      assert.match(await page.locator('#keys-empty').innerText(), /No Gemini keys configured/);
      snapshot.providers.gemini = usage;
      await page.evaluate(() => refreshAll());
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
    await page.setViewportSize({ width: 375, height: 844 });
    await page.evaluate(() => window.scrollTo({ top: 0, behavior: 'instant' }));
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth), true);
    await page.screenshot({ path: path.join(data.screenshots, `${data.surface}-mobile.png`), fullPage: true });
    assert.deepEqual(errors, []);
    process.stdout.write(`Offline ${data.surface} evidence and responsive interactions passed.\n`);
  } finally { await browser.close(); }
}).on('error', error => { console.error(error); process.exitCode = 1; });
