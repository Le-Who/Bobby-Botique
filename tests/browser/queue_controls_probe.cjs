/* Real UI scripts and templates; every HTTP response is synthetic/offline. */
const assert = require('node:assert/strict');
const path = require('node:path');
let input = '';
process.stdin.setEncoding('utf8');
process.stdin.on('data', part => { input += part; });
process.stdin.on('end', async () => {
  const data = JSON.parse(input), { chromium } = require(data.playwright_module);
  const browser = await chromium.launch({ headless: true });
  try {
    const page = await browser.newPage({ viewport: { width: 1366, height: 900 }, reducedMotion: 'reduce' });
    page.setDefaultTimeout(3000);
    const errors = [], writes = [], confirmations = [];
    page.on('pageerror', error => errors.push(error.message));
    page.on('dialog', async dialog => { confirmations.push(dialog.message()); await dialog.accept(); });
    const now = Date.now() / 1000;
    const jobs = {
      unknown: { task_id: 'unknown-job', kind: 'deferred_ai_response', lane: 'deferred', state: 'unknown_delivery', revision: 3,
        attempts_started: 1, max_attempts: 3, submitted_at: now - 30, age_seconds: 30, scope: 'shared', storage_mode: 'redis',
        capabilities: { cancel: false, retry: true }, result_summary: {} },
      active: { task_id: 'active-job', kind: 'deferred_ai_response', lane: 'deferred', state: 'running', revision: 5,
        attempts_started: 1, max_attempts: 3, submitted_at: now - 20, age_seconds: 20, scope: 'process', storage_mode: 'local',
        capabilities: { cancel: true, retry: false }, result_summary: { actual_model: '<img src=x onerror=alert(1)>', settings_revision: 9 } },
      failed: { task_id: 'failed-job', kind: 'document_processing', lane: 'deferred', state: 'failed', revision: 4,
        attempts_started: 3, max_attempts: 3, submitted_at: now - 50, age_seconds: 50, scope: 'shared', storage_mode: 'redis',
        capabilities: { cancel: false, retry: true }, result_summary: {} },
    };
    let laneRevision = 7, retryUncertain = true, queueReads = 0;
    const queueData = () => ({ observed_at: now, queue: { available: false, scope: 'mixed', lanes: {
      deferred: { available: true, scope: 'process', observed_at: now, revision: laneRevision, admission_paused: false,
        claim_paused: false, queued: 2, delayed: 1, running: 1, payload_bytes: 200, oldest_age_seconds: 30,
        workers: 3, policy: { max_jobs: 100, max_payload_bytes: 262144, max_total_payload_bytes: 8388608,
          max_age_seconds: 900, max_attempts: 3 }, capabilities: { set_state: true } },
      daily: { available: false, scope: 'shared', observed_at: now, capabilities: { set_state: false } },
    } }, load: { ingress: { available: false, scope: 'process', observed_at: now },
      tts: { available: true, scope: 'process', observed_at: now, jobs: 3, bytes: 4096 },
      task_manager: { available: true, scope: 'process', observed_at: now, running: 1, outstanding: 2 } },
      capabilities: { queue_actions: true, process_load_actions: false, policy_edit: false } });
    const snapshot = { overview: { system: {}, services: { database: 'unknown', redis: 'unknown', bot: 'ready' }, metrics: {}, summarization: {} },
      providers: { gemini: { available: true, rows: [] }, tavily: { available: true, rows: [] }, circuit_breakers: { items: {} }, key_health: { rows: [] } },
      infrastructure: { database: { available: false }, queue: { available: false }, cache: { available: false } }, errors: { available: false },
      operations: { stages: [{ stage: 'total', workload: 'interactive', count: 3, p50_seconds: .02, p95_seconds: .08,
        p99_seconds: .095, window_seconds: 900, scope: 'process', available: true, observed_at: now, low_sample: true }],
        amplification: { available: true, accepted_generation_requests: 2, api_attempts: 5, ratio: 2.5, scope: 'process', window_seconds: 900,
          coverage: 'owned_routed_and_direct_generation_scopes' }, races: [],
        completed_generation_wait_seconds_per_window_second: { value: .25, scope: 'process', window_seconds: 900, approximate: true },
        load: { ingress: { available: false, scope: 'process', observed_at: now }, heavy: { available: true, waiters: 2,
          active: 1, capacity: 4, scope: 'process', observed_at: now }, tts: queueData().load.tts } } };
    await page.route('**/*', async route => {
      const url = new URL(route.request().url()), pathname = url.pathname;
      if (data.assets[pathname]) return route.fulfill({ contentType: pathname.endsWith('.css') ? 'text/css' : 'text/javascript', body: data.assets[pathname] });
      if (pathname === '/controls' || pathname === '/') return route.fulfill({ contentType: 'text/html', body: data.html });
      if (pathname === '/api/admin/controls') return route.fulfill({ json: { revision: 1, processes: [], prompts: [], audio: [],
        catalogs: [], limits: [], history: [], commands: { commands: [] } } });
      if (pathname === '/api/dashboard') return route.fulfill({ json: snapshot });
      if (pathname === '/api/controls/queues') { queueReads++; return route.fulfill({ json: queueData() }); }
      if (pathname === '/api/controls/jobs') return route.fulfill({ json: { available: true, scope: 'mixed', observed_at: now,
        items: Object.values(jobs), next_cursor: null } });
      const command = pathname.match(/^\/api\/controls\/jobs\/([^/]+)\/(cancel|retry)$/);
      if (command) {
        const body = route.request().postDataJSON(); writes.push({ action: command[2], ...body });
        assert.ok(route.request().headers()['x-csrf-token']);
        const job = Object.values(jobs).find(row => row.task_id === command[1]);
        if (body.expected_revision !== job.revision) return route.fulfill({ status: 409, json: { reason_code: 'revision_conflict', confirmed: false } });
        if (command[2] === 'retry') {
          if (retryUncertain) { retryUncertain = false; return route.fulfill({ status: 503, json: { reason_code: 'command_unconfirmed', confirmed: false } }); }
          job.capabilities.retry = false;
          return route.fulfill({ json: { confirmed: true, source_job: job, admission: { state: 'existing', task_id: 'retry-child' } } });
        }
        job.state = 'cancel_requested'; job.revision++; job.capabilities.cancel = false;
        return route.fulfill({ json: { confirmed: true, job } });
      }
      if (pathname === '/api/controls/queues/deferred/state') {
        const body = route.request().postDataJSON(); writes.push({ action: 'pause', ...body });
        return route.fulfill({ status: 409, json: { confirmed: false, reason_code: 'revision_conflict' } });
      }
      const detail = pathname.match(/^\/api\/controls\/jobs\/([^/]+)$/);
      if (detail) return route.fulfill({ json: { available: true, observed_at: now,
        job: Object.values(jobs).find(row => row.task_id === detail[1]) } });
      return route.abort();
    });
    if (data.surface === 'queues') {
      await page.goto('http://gemaibot.test/controls#queues');
      await page.locator('#queues').waitFor({ state: 'visible' });
      await page.locator('#queue-jobs button[data-job="unknown-job"]').click();
      assert.equal(await page.locator('#queue-job-detail button[data-action="retry"]').count(), 0, 'Unknown delivery cannot be retried even with an inconsistent capability hint');
      assert.match(await page.locator('#queue-lanes').textContent(), /daily[\s\S]*недоступ/i);
      assert.match(await page.locator('#queue-load').textContent(), /tts[\s\S]*4096/);
      await page.locator('#queue-jobs button[data-job="active-job"]').click();
      jobs.active.revision = 6; // Another owner has progressed after the client opened revision 5.
      await page.locator('#queue-job-detail button[data-action="cancel"]').click();
      await page.locator('#queue-status').filter({ hasText: /измени|ревизи/i }).waitFor();
      assert.equal(writes.at(-1).expected_revision, 5, 'Do not replace the held revision with a new server read');
      assert.match(await page.locator('#queue-job-detail').textContent(), /active-job/);
      await page.locator('#queue-refresh').click();
      await page.locator('#queue-job-detail').filter({ hasText: /Ревизия 6/ }).waitFor();
      await page.locator('#queue-job-detail button[data-action="cancel"]').click();
      await page.locator('#queue-job-detail').filter({ hasText: /cancel_requested/ }).waitFor();
      assert.equal(writes.at(-1).expected_revision, 6);
      assert.doesNotMatch(await page.locator('#queue-job-detail').textContent(), /Состояние: cancelled/);
      jobs.active.state = 'cancelled'; jobs.active.revision = 8;
      await page.locator('#queue-refresh').click();
      await page.locator('#queue-job-detail').filter({ hasText: /Состояние: cancelled/ }).waitFor();
      await page.locator('#queue-jobs button[data-job="failed-job"]').click();
      await page.locator('#queue-job-detail button[data-action="retry"]').click();
      await page.locator('#queue-reconcile').waitFor({ state: 'visible' });
      const operationId = writes.at(-1).operation_id;
      await page.locator('#queue-reconcile').click();
      await page.locator('#queue-status').filter({ hasText: /retry-child/ }).waitFor();
      assert.equal(writes.at(-1).operation_id, operationId, 'Reconciliation must reuse the exact command ID');
      laneRevision = 8;
      await page.locator('[data-lane="deferred"] input[data-pause="admission"]').check();
      await page.locator('[data-lane="deferred"] button[data-action="set-state"]').click();
      await page.locator('#queue-status').filter({ hasText: /измени|ревизи/i }).waitFor();
      assert.equal(writes.at(-1).expected_revision, 7, 'Pause uses the revision held by the rendered lane');
      assert.ok(confirmations.some(text => /deferred/.test(text) && /2/.test(text) && /1/.test(text) && /запущен/i.test(text)));
      assert.equal(await page.locator('#queues img').count(), 0, 'Model metadata is text, not markup');
      const readsBeforeAgeTick = queueReads;
      await page.evaluate(() => { const now = Date.now(); Date.now = () => now + 65000; });
      await page.locator('[data-lane="deferred"] .queue-source-age').filter({ hasText: /[6-9][0-9] с/ }).waitFor();
      assert.equal(queueReads, readsBeforeAgeTick, 'Age ticks must not refresh or rebase a held lane revision');
    } else {
      await page.goto('http://gemaibot.test/');
      await page.locator('#operations-stages tr').filter({ hasText: 'interactive' }).waitFor();
      const row = page.locator('#operations-stages tr').filter({ hasText: 'interactive' });
      assert.match(await row.innerText(), /0\.020[\s\S]*0\.080[\s\S]*0\.095/);
      assert.match(await row.innerText(), /3[\s\S]*low sample/i);
      assert.match(await row.innerText(), /900[\s\S]*process/);
      assert.match(await page.locator('#operations-amplification').textContent(), /2\.50[\s\S]*5[\s\S]*2/);
      assert.match(await page.locator('#operations-load [data-source="ingress"]').textContent(), /unavailable/i);
      assert.match(await page.locator('#operations-load [data-source="tts"]').textContent(), /4096/);
      snapshot.operations.load.heavy = { available: false, scope: 'process', observed_at: now };
      await page.evaluate(() => refreshAll());
      assert.match(await page.locator('#operations-load [data-source="heavy"]').textContent(), /unavailable/i);
    }
    await page.evaluate(() => window.scrollTo({ top: 0, behavior: 'instant' }));
    await page.screenshot({ path: path.join(data.screenshots, `${data.surface}-desktop.png`), fullPage: true });
    await page.setViewportSize({ width: 390, height: 844 });
    await page.evaluate(() => window.scrollTo({ top: 0, behavior: 'instant' }));
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth), true, 'Only the table may scroll horizontally');
    await page.screenshot({ path: path.join(data.screenshots, `${data.surface}-mobile.png`), fullPage: true });
    assert.deepEqual(errors, []);
  } finally { await browser.close(); }
}).on('error', error => { console.error(error); process.exitCode = 1; });
