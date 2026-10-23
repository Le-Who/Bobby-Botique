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
    const initialConnectionMode = payload.terminal && payload.terminal !== 'model_unavailable'
      ? 'vertex_internet' : 'standard';
    let releaseSettings;
    const settingsGate = new Promise(resolve => { releaseSettings = resolve; });
    if (!payload.defer_settings) releaseSettings();
    await page.route('**/*', async route => {
      const url = new URL(route.request().url());
      if (url.hostname === 'live.test' && url.pathname === '/webapp/live') {
        return route.fulfill({ body: payload.html, contentType: 'text/html' });
      }
      if (url.pathname.endsWith('/live-settings')) {
        await settingsGate;
        return route.fulfill({ json: { live_settings: { live_connection_mode: initialConnectionMode }, connection_modes: [
          { id: 'standard', label: 'Standard' }, { id: 'vertex_internet', label: 'Internet' },
        ], voices: [], thinking_presets: [] } });
      }
      return route.fulfill({ body: '', contentType: 'application/javascript' });
    });
    await page.addInitScript(({ scenario }) => {
      window.metrics = { streams: [], contexts: [], worklets: [], permissionCalls: 0, sockets: [], playbackResumes: 0 };
      if (scenario === 'manual_stop') localStorage.setItem('liveTurnMode', 'manual');
      if (scenario === 'rejected_resume') sessionStorage.setItem('liveResumptionToken', 'expired-handle');
      window.pendingPermissions = [];
      window.pendingModules = [];
      class Socket {
        static OPEN = 1; static CONNECTING = 0;
        readyState = 1; sent = []; closeCount = 0;
        constructor(url) { this.url = url; metrics.sockets.push(this); queueMicrotask(() => this.onopen?.()); }
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
        constructor(options) {
          if (options?.sampleRate === 24000) {
            this.state = 'suspended';
            this.resume = () => { metrics.playbackResumes++; this.state = 'running'; return Promise.resolve(); };
            return;
          }
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
    if (payload.defer_settings) {
      assert.equal(await page.locator('[data-live-connection-mode]').count(), 0,
        'the late-settings case must reach the page before settings are applied');
      releaseSettings();
    }
    // Wait for the rendered API settings before choosing the transport.
    await page.locator(`[data-live-connection-mode="${initialConnectionMode}"].active`).waitFor({ state: 'attached' });
    if (payload.scenario === 'rejected_resume') {
      await page.locator('#mic-btn').click();
      const attempts = await page.evaluate(() => {
        const first = metrics.sockets.at(-1);
        first.receive({ type: 'error', reason: 'connect_failed', message: 'Synthetic stale handle' });
        const fresh = metrics.sockets.at(-1);
        const result = { old: first.url, fresh: fresh.url, count: metrics.sockets.length };
        fresh.receive({ type: 'connected' });
        return result;
      });
      assert.equal(attempts.count, 2);
      assert.ok(attempts.old.includes('resumptionToken=expired-handle'));
      assert.ok(!attempts.fresh.includes('resumptionToken='));
      await page.waitForFunction(() => document.getElementById('mic-btn').classList.contains('recording'));
      await page.locator('#end-btn').click();
      assert.deepEqual(errors, []);
      return;
    }
    await start();
    if (['permission_denied', 'context_failure', 'worklet_failure'].includes(payload.scenario)) {
      await page.waitForFunction(() => document.getElementById('error-bar').classList.contains('show'));
      const value = await state();
      assert.equal(value.label, 'Начать запись');
      assert.deepEqual(value.stops, payload.scenario === 'permission_denied' ? [] : [1]);
      assert.deepEqual(value.closes, payload.scenario === 'worklet_failure' ? [1] : []);
      assert.deepEqual(value.events, []);
    } else if (payload.scenario === 'automatic_turns') {
      await page.waitForFunction(() => document.getElementById('mic-btn').classList.contains('recording'));
      const result = await page.evaluate(() => {
        const socket = metrics.sockets.at(-1);
        const sendAudio = () => metrics.worklets.at(-1).port.onmessage({ data: new Int16Array(1600).buffer });
        sendAudio();
        socket.receive({ type: 'input_transcript', text: 'Первая реплика', finished: true });
        socket.receive({ type: 'output_transcript', text: 'Первый ответ' });
        socket.receive({ type: 'input_transcript', text: 'Вторая ', finished: false });
        socket.receive({ type: 'turn_complete' });
        socket.receive({ type: 'input_transcript', text: 'Вторая реплика', finished: true });
        sendAudio();
        socket.receive({ type: 'output_transcript', text: 'Второй ответ' });
        socket.receive({ type: 'turn_complete' });
        plannedReconnect = true;
        socket.close();
        sendAudio(); // OPEN is not provider setupComplete.
        const beforeSetup = metrics.sockets.at(-1).sent.length;
        metrics.sockets.at(-1).receive({ type: 'connected' });
        sendAudio();
        return { beforeSetup, events: metrics.sockets.flatMap(s => s.sent.map(m => m.type)),
          isRecording: recording, resumes: metrics.playbackResumes,
          userRows: [...document.querySelectorAll('.t-user')].map(el => el.textContent) };
      });
      assert.equal(result.beforeSetup, 0);
      assert.equal(result.isRecording, true);
      assert.equal(result.resumes, 1, 'playback must be unlocked during the original user gesture');
      assert.deepEqual(result.events, ['realtime_input', 'realtime_input', 'realtime_input']);
      assert.deepEqual(result.userRows, ['Вы: Первая реплика', 'Вы: Вторая реплика']);
      await page.locator('#mic-btn').click();
    } else if (payload.scenario === 'normal_stop' || payload.scenario === 'manual_stop') {
      await page.waitForFunction(() => document.getElementById('mic-btn').classList.contains('recording'));
      await page.locator('#mic-btn').click();
      await page.evaluate(() => stopRecording()); // idempotent cleanup
      const value = await state();
      assert.equal(value.label, 'Начать запись');
      assert.deepEqual(value.stops, [1]); assert.deepEqual(value.closes, [1]);
      assert.deepEqual(value.workletDisconnects, [1]);
      assert.deepEqual(value.socketClosed, [0]);
      assert.deepEqual(value.events, payload.scenario === 'manual_stop' ? ['activity_start', 'activity_end'] : ['audio_stream_end']);
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
      assert.deepEqual(value.events, []);
      await page.locator('#mic-btn').click();
      assert.deepEqual((await state()).stops, [1, 1]);
    } else if (payload.terminal) {
      const isWorklet = payload.scenario === 'terminal_pending_worklet';
      if (isWorklet) await page.waitForFunction(() => pendingModules.length === 1);
      const fallsBack = payload.terminal !== 'model_unavailable';
      const terminalTarget = await page.evaluate(({ reason, fallsBack }) => {
        if (fallsBack) {
          // A proactive reconnect can begin while the previous mic request is pending.
          plannedReconnect = true;
          metrics.sockets.at(-1).close();
        }
        const socket = metrics.sockets.at(-1);
        const target = { path: new URL(socket.url).pathname, established: liveSessionEstablished };
        socket.receive({
          type: reason === 'connect_failed' ? 'error' : 'fatal',
          reason,
          message: 'Synthetic terminal event',
        });
        return target;
      }, { reason: payload.terminal, fallsBack });
      assert.equal(terminalTarget.path, fallsBack ? '/webapp/live-vertex/ws' : '/webapp/live/ws',
        'the terminal event must target the configured transport');
      if (fallsBack) assert.equal(terminalTarget.established, false,
        'fallback must be exercised before the replacement connection is established');
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
