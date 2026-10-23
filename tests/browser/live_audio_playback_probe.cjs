const assert = require('node:assert/strict');
const fs = require('node:fs');
const { chromium } = require('playwright');
const payload = JSON.parse(fs.readFileSync(0, 'utf8'));
(async () => {
  const browser = await chromium.launch({ headless: true });
  try {
    const page = await browser.newPage();
    await page.route('**/*', route => route.fulfill(route.request().url().endsWith('/live')
      ? { body: payload.html, contentType: 'text/html' }
      : { json: { live_settings: {}, voices: [], connection_modes: [], thinking_presets: [] } }));
    await page.goto('http://live.test/live');
    const report = await page.evaluate(async () => {
      playbackCtx = new OfflineAudioContext(1, 24000, 24000);
      const pcm = btoa(String.fromCharCode(...new Uint8Array(new Int16Array(2400).fill(8192).buffer)));
      for (let i = 0; i < 4; i++) enqueueAudio(pcm);
      const start = nextPlaybackTime - .4;
      const rendered = await playbackCtx.startRendering();
      const samples = rendered.getChannelData(0);
      const boundary = Math.round((start + .2) * 24000);
      let error = 0;
      for (let i = boundary - 12; i <= boundary + 12; i++) {
        error = Math.max(error, Math.abs(samples[i] - .25));
      }
      return { error, queuedDuration: nextPlaybackTime - start };
    });
    assert.ok(report.error < .00001, `continuous PCM must stay continuous across packets: ${JSON.stringify(report)}`);
    assert.ok(Math.abs(report.queuedDuration - .4) < 1 / 24000);
    const stalled = await page.evaluate(async () => {
      playbackCtx = new OfflineAudioContext(1, 24000, 24000);
      scheduledSources = []; nextPlaybackTime = 0; playbackTurnEnded = true;
      const pcm = btoa(String.fromCharCode(...new Uint8Array(new Int16Array(2400).fill(8192).buffer)));
      enqueueAudio(pcm);
      const paused = playbackCtx.suspend(.25);
      const rendering = playbackCtx.startRendering();
      await paused;
      // Offline rendering runs faster than wall time. Resume only after the
      // packet's automation is scheduled, keeping this timing oracle stable.
      const resume = playbackCtx.resume.bind(playbackCtx);
      playbackCtx.resume = () => Promise.resolve();
      enqueueAudio(pcm); // the next packet arrived after the queue drained
      finishPlaybackTurn();
      await resume();
      const samples = (await rendering).getChannelData(0);
      let jump = 0, jumpIndex = 0;
      for (let i = 1; i < samples.length; i++) {
        const delta = Math.abs(samples[i] - samples[i - 1]);
        if (delta > jump) { jump = delta; jumpIndex = i; }
      }
      return { jump, jumpTime: jumpIndex / 24000, before: samples[jumpIndex - 1], after: samples[jumpIndex] };
    });
    assert.ok(stalled.jump < .004, `network underruns must fade rather than click: ${JSON.stringify(stalled)}`);
    const stopped = await page.evaluate(async () => {
      playbackCtx = new OfflineAudioContext(1, 24000, 24000);
      scheduledSources = []; nextPlaybackTime = 0; playbackTurnEnded = true;
      const pcm = btoa(String.fromCharCode(...new Uint8Array(new Int16Array(2400).fill(8192).buffer)));
      enqueueAudio(pcm);
      const paused = playbackCtx.suspend(.218);
      const rendering = playbackCtx.startRendering();
      await paused;
      flushPlayback(); // Stop while the queue-tail fade is already in progress.
      await playbackCtx.resume();
      const samples = (await rendering).getChannelData(0);
      let jump = 0;
      for (let i = 1; i < samples.length; i++) jump = Math.max(jump, Math.abs(samples[i] - samples[i - 1]));
      return { jump };
    });
    assert.ok(stopped.jump < .004, `stopping during a fade must preserve instantaneous gain: ${JSON.stringify(stopped)}`);
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
