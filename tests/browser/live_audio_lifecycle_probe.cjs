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
    page.on('pageerror', error => errors.push(error.message));
    await page.route('**/*', route => {
      const url = new URL(route.request().url());
      if (url.hostname === 'live.test' && url.pathname === '/webapp/live') {
        return route.fulfill({ body: payload.html, contentType: 'text/html' });
      }
      if (url.pathname.endsWith('/live-settings')) {
        return route.fulfill({ json: { live_settings: {}, connection_modes: [], voices: [], thinking_levels: [] } });
      }
      return route.fulfill({ body: '', contentType: 'application/javascript' });
    });
    await page.addInitScript(({ scenario }) => {
      window.metrics = { streams: [], contexts: [], worklets: [], permissionCalls: 0, sockets: [] };
      window.pendingPermissions = [];
      window.pendingModules = [];
      class Socket {
        static OPEN = 1; static CONNECTING = 0;
        readyState = 1; sent = []; closeCount = 0;
        constructor() { metrics.sockets.push(this); queueMicrotask(() => this.onopen?.()); }
        send(data) { this.sent.push(JSON.parse(data)); }
        close() { this.readyState = 3; this.closeCount++; this.onclose?.({ code: 1000 }); }
        receive(data) { this.onmessage?.({ data: JSON.stringify(data) }); }
      }
      window.WebSocket = Socket;
      const stream = () => {
        const track = { stopCount: 0, stop() { this.stopCount++; } };
        const value = { getTracks: () => [track], track };
        metrics.streams.push(value);
        return value;
      };
      Object.defineProperty(navigator, 'mediaDevices', { value: {
        getUserMedia() {
          metrics.permissionCalls++;
          if (scenario === 'permission_denied') return Promise.reject(new Error('denied'));
          if (scenario.includes('permission') || scenario === 'superseded_start') {
            return new Promise(resolve => pendingPermissions.push(() => resolve(stream())));
          }
          return Promise.resolve(stream());
        }
      } });
      const node = () => ({ connect() {}, disconnectCount: 0, disconnect() { this.disconnectCount++; } });
      window.AudioContext = class {
        closeCount = 0; destination = {};
        constructor() {
          if (scenario === 'context_failure') throw new Error('unsupported sample rate');
          metrics.contexts.push(this);
          this.audioWorklet = { addModule() {
            if (scenario === 'worklet_failure') return Promise.reject(new Error('module rejected'));
            if (scenario === 'stop_pending_worklet' || scenario === 'terminal_pending_worklet') {
              return new Promise(resolve => pendingModules.push(resolve));
            }
            return Promise.resolve();
          } };
        }
        createMediaStreamSource() { return node(); }
        createAnalyser() { return { ...node(), frequencyBinCount: 128, getByteFrequencyData() {} }; }
        createGain() { return { ...node(), gain: { value: 1 } }; }
        close() { this.closeCount++; return Promise.resolve(); }
      };
      window.AudioWorkletNode = class {
        port = {}; disconnectCount = 0;
        constructor() { metrics.worklets.push(this); }
        connect() {}
        disconnect() { this.disconnectCount++; }
      };
    }, { scenario: payload.scenario });
    await page.goto('http://live.test/webapp/live');
    const start = async () => {
      await page.locator('#mic-btn').click();
      await page.evaluate(() => metrics.sockets.at(-1).receive({ type: 'connected' }));
      await page.waitForFunction(() => metrics.permissionCalls > 0);
    };
    const state = () => page.evaluate(() => ({
      label: document.getElementById('mic-btn').getAttribute('aria-label'),
      status: document.getElementById('status-label').textContent,
      stops: metrics.streams.map(s => s.track.stopCount),
      closes: metrics.contexts.map(c => c.closeCount),
      workletDisconnects: metrics.worklets.map(w => w.disconnectCount),
      socketClosed: metrics.sockets.map(s => s.closeCount),
      events: metrics.sockets.flatMap(s => s.sent.map(m => m.type)),
    }));
    if (payload.terminal && payload.terminal !== 'model_unavailable') {
      await page.evaluate(() => { liveSettings.live_connection_mode = 'vertex_internet'; });
    }
    await start();
    if (['permission_denied', 'context_failure', 'worklet_failure'].includes(payload.scenario)) {
      await page.waitForFunction(() => document.getElementById('error-bar').classList.contains('show'));
      const value = await state();
      assert.equal(value.label, 'Начать запись');
      assert.deepEqual(value.stops, payload.scenario === 'permission_denied' ? [] : [1]);
      assert.deepEqual(value.closes, payload.scenario === 'worklet_failure' ? [1] : []);
      assert.deepEqual(value.events, []);
    } else if (payload.scenario === 'normal_stop') {
      await page.waitForFunction(() => document.getElementById('mic-btn').classList.contains('recording'));
      await page.locator('#mic-btn').click();
      await page.evaluate(() => stopRecording()); // idempotent cleanup
      const value = await state();
      assert.equal(value.label, 'Начать запись');
      assert.deepEqual(value.stops, [1]); assert.deepEqual(value.closes, [1]);
      assert.deepEqual(value.workletDisconnects, [1]);
      assert.deepEqual(value.socketClosed, [0]);
      assert.deepEqual(value.events, ['activity_start', 'activity_end']);
    } else if (payload.scenario === 'superseded_start') {
      await page.locator('#mic-btn').click();
      await page.locator('#mic-btn').click();
      await page.waitForFunction(() => pendingPermissions.length === 2);
      await page.evaluate(() => pendingPermissions[1]());
      await page.waitForFunction(() => document.getElementById('mic-btn').classList.contains('recording'));
      await page.evaluate(() => pendingPermissions[0]());
      await page.waitForFunction(() => metrics.streams.length === 2);
      const value = await state();
      assert.deepEqual(value.stops, [0, 1], 'a superseded request may only stop its own returned stream');
      assert.equal(value.label, 'Остановить запись');
      assert.deepEqual(value.events, ['activity_start']);
      await page.locator('#mic-btn').click();
      assert.deepEqual((await state()).stops, [1, 1]);
    } else if (payload.terminal) {
      const isWorklet = payload.scenario === 'terminal_pending_worklet';
      if (isWorklet) await page.waitForFunction(() => pendingModules.length === 1);
      const fallsBack = payload.terminal !== 'model_unavailable';
      await page.evaluate(({ reason, fallsBack }) => {
        if (fallsBack) {
          // A proactive reconnect can begin while the previous mic request is pending.
          plannedReconnect = true;
          metrics.sockets.at(-1).close();
        }
        metrics.sockets.at(-1).receive({
          type: reason === 'connect_failed' ? 'error' : 'fatal',
          reason,
          message: 'Synthetic terminal event',
        });
      }, { reason: payload.terminal, fallsBack });
      let value = await state();
      assert.equal(value.label, 'Начать запись', 'terminal event must invalidate the pending owner');
      if (fallsBack) {
        await page.evaluate(() => metrics.sockets.at(-1).receive({ type: 'connected' }));
      }
      await page.evaluate(isWorklet => {
        if (isWorklet) pendingModules[0]();
        else pendingPermissions[0]();
      }, isWorklet);
      await page.waitForFunction(() => metrics.streams.length === 1 && metrics.streams[0].track.stopCount === 1);
      value = await state();
      assert.equal(value.label, 'Начать запись');
      assert.ok(!value.status.includes('Слушаю'), 'late resolution must not overwrite terminal/fallback UI');
      assert.deepEqual(value.stops, [1]);
      assert.deepEqual(value.closes, isWorklet ? [1] : []);
      assert.deepEqual(value.workletDisconnects, []);
      assert.deepEqual(value.socketClosed, fallsBack ? [1, 1, 0] : [1]);
      assert.deepEqual(value.events, [], 'a cancelled owner must not open an activity in the new session');
    } else {
      if (payload.scenario === 'stop_pending_worklet') await page.waitForFunction(() => pendingModules.length === 1);
      await page.locator(payload.scenario === 'end_pending_permission' ? '#end-btn' : '#mic-btn').click();
      await page.evaluate(mode => {
        if (mode === 'stop_pending_worklet') pendingModules[0]();
        else pendingPermissions[0]();
      }, payload.scenario);
      await page.waitForFunction(() => metrics.streams.length === 1);
      const value = await state();
      assert.equal(value.label, 'Начать запись');
      assert.deepEqual(value.stops, [1]);
      assert.ok(value.closes.every(count => count === 1));
      assert.deepEqual(value.events, [], 'cancelled start must never open an activity boundary');
    }
    assert.deepEqual(errors, []);
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
