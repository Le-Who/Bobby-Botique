const assert = require('node:assert/strict');
const fs = require('node:fs');
const { chromium } = require('playwright');
const payload = JSON.parse(fs.readFileSync(0, 'utf8'));
(async () => {
  const browser = await chromium.launch({ headless: true });
  try {
    const context = await browser.newContext();
    const page = await context.newPage();
    page.setDefaultTimeout(4000);
    const errors = [];
    page.on('pageerror', error => errors.push(error.message));
    await context.addInitScript(() => {
      const noop = () => {};
      window.Telegram = { WebApp: { initData: 'synthetic-test-init', ready: noop, expand: noop,
        setHeaderColor: noop, setBackgroundColor: noop, onEvent: noop,
        BackButton: { onClick: noop, show: noop, hide: noop },
        MainButton: { onClick: noop, setText: noop, show: noop, hide: noop },
        HapticFeedback: { selectionChanged: noop, impactOccurred: noop, notificationOccurred: noop },
      } };
      window.deletes = [];
      window.speechCalls = [];
      window.cancelCalls = 0;
      const recordSpeech = type => {
        const records = JSON.parse(localStorage.getItem('speech-boundary-records') || '[]');
        records.push({ type, phase: window.teardownPhase || 'active', url: location.href });
        localStorage.setItem('speech-boundary-records', JSON.stringify(records));
      };
      ['beforeunload', 'pagehide', 'visibilitychange'].forEach(type => {
        window.addEventListener(type, () => { window.teardownPhase = type; }, { capture: true });
      });
      Object.defineProperty(window, 'speechSynthesis', { value: {
        speak(value) { speechCalls.push(value); recordSpeech('speak'); },
        cancel() { cancelCalls++; recordSpeech('cancel'); },
      } });
      window.SpeechSynthesisUtterance = class { constructor(text) { this.text = text; } };
      Object.defineProperty(navigator, 'clipboard', { value: {
        writeText(text) { window.clipboardText = text; return Promise.reject(new Error('permission denied')); },
      } });
      const nativeFetch = window.fetch.bind(window);
      window.fetch = (url, options) => {
        if (options?.method === 'DELETE') {
          deletes.push({ url, options });
          const records = JSON.parse(sessionStorage.getItem('delete-dispatch-records') || '[]');
          records.push({ url, options });
          sessionStorage.setItem('delete-dispatch-records', JSON.stringify(records));
        }
        return nativeFetch(url, options);
      };
    });
    const deleteReceipts = [];
    const memoryReads = [];
    let receiptReady;
    const receivedDelete = new Promise(resolve => { receiptReady = resolve; });
    let storedMemories = [{ id: 731, content: 'Synthetic memory',
      source_type: 'manual', created_at: '2026-10-09T09:00:00Z' }];
    await context.route('**/*', route => {
      const url = new URL(route.request().url());
      if (url.hostname === 'ui.test' && url.pathname === '/page') {
        return route.fulfill({ body: payload.html, contentType: 'text/html' });
      }
      if (url.hostname === 'ui.test' && url.pathname === '/departed') {
        return route.fulfill({ body: '<html><body>Controlled destination</body></html>', contentType: 'text/html' });
      }
      if (url.pathname === '/webapp/api/memories/stats') {
        assert.equal(route.request().headers().authorization, 'tma synthetic-test-init');
        return route.fulfill({ json: { total_memories: storedMemories.length, limit: 500 } });
      }
      if (url.pathname === '/webapp/api/memories') {
        assert.equal(route.request().headers().authorization, 'tma synthetic-test-init');
        memoryReads.push(storedMemories.map(memory => memory.id));
        return route.fulfill({ json: { memories: storedMemories } });
      }
      if (url.pathname === '/webapp/api/memories/731') {
        assert.equal(route.request().method(), 'DELETE');
        assert.equal(route.request().headers().authorization, 'tma synthetic-test-init');
        const failed = ['delete_failure', 'reload_failure'].includes(payload.scenario);
        deleteReceipts.push({ path: url.pathname, method: route.request().method(),
          authorization: route.request().headers().authorization, status: failed ? 500 : 200 });
        if (!failed) storedMemories = [];
        receiptReady(deleteReceipts.at(-1));
        return route.fulfill({ status: failed ? 500 : 200, json: { ok: !failed } });
      }
      return route.fulfill({ body: '', contentType: url.pathname.endsWith('.css') ? 'text/css' : 'application/javascript' });
    });
    if (payload.pageName === 'miniapp') await page.clock.install();
    await page.goto('http://ui.test/page?tab=memories');
    if (payload.pageName === 'miniapp') {
      await page.waitForSelector('#mem-731');
      await page.locator('#mem-731').hover();
      await page.locator('#mem-731 .memory-delete-hover').click();
      await page.clock.runFor(200);
      assert.equal(await page.evaluate(() => deletes.length), 0);
      assert.equal(await page.locator('#mem-731').evaluate(el => el.style.height), '0px');
      if (payload.scenario === 'undo' || payload.scenario === 'reload_undo') {
        await page.locator('.toast-undo').click();
        if (payload.scenario === 'reload_undo') {
          await page.reload();
          await page.waitForSelector('#mem-731');
        }
        await page.clock.runFor(6000);
        assert.equal(await page.evaluate(() => deletes.length), 0);
        assert.equal(await page.locator('#mem-731').evaluate(el => el.style.height), '');
        const restoredOpacity = payload.scenario === 'reload_undo' ? '' : '1';
        assert.equal(await page.locator('#mem-731').evaluate(el => el.style.opacity), restoredOpacity);
        assert.equal(await page.locator('#mem-731 .memory-card-inner').evaluate(el => el.style.opacity), restoredOpacity);
        assert.equal(await page.locator('.timeline-date').evaluate(el => el.style.height), '');
        if (payload.scenario === 'reload_undo') {
          assert.deepEqual(deleteReceipts, []);
          assert.deepEqual(await page.evaluate(() => JSON.parse(sessionStorage.getItem('delete-dispatch-records') || '[]')), []);
          assert.deepEqual(memoryReads, [[731], [731]]);
          assert.equal(await page.locator('#mem-stats-text').textContent(), '1 / 500');
        }
      } else if (['reload_delete', 'navigate_delete', 'reload_failure'].includes(payload.scenario)) {
        // Observe actual HTTP delivery independently from old-page fetch dispatch.
        if (payload.scenario === 'navigate_delete') {
          await page.goto('http://ui.test/departed');
          assert.equal(page.url(), 'http://ui.test/departed');
        } else await page.reload();
        let receiptTimeout;
        const receipt = await Promise.race([receivedDelete, new Promise((_, reject) => {
          receiptTimeout = setTimeout(() => reject(new Error('No DELETE delivered to controlled API after navigation')), 4000);
        })]).finally(() => clearTimeout(receiptTimeout));
        assert.equal(receipt.status, payload.scenario === 'reload_failure' ? 500 : 200);
        assert.deepEqual(deleteReceipts, [{ path: '/webapp/api/memories/731', method: 'DELETE',
          authorization: 'tma synthetic-test-init', status: receipt.status }]);
        const dispatched = await page.evaluate(() => JSON.parse(sessionStorage.getItem('delete-dispatch-records') || '[]'));
        assert.equal(dispatched.length, 1);
        assert.equal(dispatched[0].url, '/webapp/api/memories/731');
        assert.equal(dispatched[0].options.method, 'DELETE');
        assert.equal(dispatched[0].options.headers.Authorization, 'tma synthetic-test-init');
        assert.equal(dispatched[0].options.keepalive, true);
        if (payload.scenario === 'navigate_delete') await page.goto('http://ui.test/page?tab=memories');
        await page.waitForFunction(() => document.getElementById('mem-stats-text').textContent.includes('/ 500'));
        await page.clock.runFor(6000);
        if (payload.scenario === 'reload_failure') {
          assert.equal(await page.locator('#mem-731').count(), 1);
          assert.equal(await page.locator('#mem-731 .memory-card-inner').evaluate(el => el.style.opacity), '');
          assert.equal(await page.locator('#mem-stats-text').textContent(), '1 / 500');
          assert.deepEqual(storedMemories.map(memory => memory.id), [731]);
          assert.deepEqual(memoryReads, [[731], [731]]);
        } else {
          assert.equal(await page.locator('#mem-731').count(), 0);
          assert.equal(await page.locator('#mem-stats-text').textContent(), '0 / 500');
          assert.deepEqual(storedMemories, []);
          assert.deepEqual(memoryReads, [[731], []]);
        }
        assert.equal(deleteReceipts.length, 1);
      } else {
        if (payload.scenario === 'unload_flush') {
          await page.evaluate(() => window.dispatchEvent(new Event('beforeunload')));
          await page.clock.runFor(6000);
        } else await page.clock.runFor(5000);
        await page.waitForFunction(() => deletes.length === 1);
        const requests = await page.evaluate(() => deletes);
        assert.equal(requests[0].url, '/webapp/api/memories/731');
        assert.equal(requests[0].options.method, 'DELETE');
        assert.equal(requests[0].options.headers.Authorization, 'tma synthetic-test-init');
        assert.equal(Boolean(requests[0].options.keepalive), payload.scenario === 'unload_flush');
        if (payload.scenario === 'delete_failure') {
          await page.waitForFunction(() => document.getElementById('mem-731').style.opacity === '1');
          assert.equal(await page.locator('#mem-731').evaluate(el => el.style.height), '');
          assert.equal(await page.locator('#mem-731 .memory-card-inner').evaluate(el => el.style.opacity), '1');
          assert.equal(await page.locator('.timeline-date').evaluate(el => el.style.height), '');
          assert.equal(await page.locator('#mem-stats-text').textContent(), '1 / 500');
          assert.ok((await page.locator('#toast-container').textContent()).includes('Ошибка удаления'));
        } else if (payload.scenario === 'timer_delete') {
          await page.waitForFunction(() => !document.getElementById('mem-731'));
          assert.equal(await page.locator('.timeline-date').count(), 0);
          assert.equal(await page.locator('#mem-stats-text').textContent(), '0 / 500');
        }
      }
    } else if (payload.scenario === 'toc') {
      assert.deepEqual(await page.locator('#toc-list a').allTextContents(), ['Overview', 'Details']);
      await page.locator('#toc-fab').click();
      assert.equal(await page.locator('#toc-sheet').evaluate(el => el.classList.contains('open')), true);
      await page.locator('#toc-list a').nth(1).click();
      assert.equal(await page.locator('#toc-sheet').evaluate(el => el.classList.contains('open')), false);
      await page.waitForFunction(() => window.scrollY > 500);
    } else if (payload.scenario === 'clipboard_error') {
      await page.evaluate(() => copyAll());
      await page.waitForFunction(() => document.getElementById('toast').textContent === 'Не удалось скопировать');
      assert.ok((await page.evaluate(() => clipboardText)).includes('Readable answer.'));
    } else {
      await page.locator('#btn-tts').click();
      assert.equal(await page.locator('#btn-tts').textContent(), '⏹ Стоп');
      assert.equal(await page.evaluate(() => speechCalls.length), 1);
      assert.equal(await page.evaluate(() => speechCalls[0].lang), 'ru-RU');
      assert.ok((await page.evaluate(() => speechCalls[0].text)).includes('Readable answer.'));
      if (['tts_reload', 'tts_navigate', 'tts_close'].includes(payload.scenario)) {
        if (payload.scenario === 'tts_reload') await page.reload();
        else if (payload.scenario === 'tts_navigate') {
          await page.goto('http://ui.test/departed');
          assert.equal(page.url(), 'http://ui.test/departed');
        } else {
          const closed = page.waitForEvent('close');
          await page.close({ runBeforeUnload: true });
          await closed;
          assert.equal(page.isClosed(), true);
          const observer = await context.newPage();
          await observer.goto('http://ui.test/departed');
          const records = await observer.evaluate(() => JSON.parse(localStorage.getItem('speech-boundary-records') || '[]'));
          assert.deepEqual(records.map(record => record.type), ['speak', 'cancel']);
          assert.ok(['beforeunload', 'pagehide', 'visibilitychange'].includes(records[1].phase), JSON.stringify(records));
          assert.equal(records[1].url, 'http://ui.test/page?tab=memories');
          assert.deepEqual(errors, []);
          return;
        }
        const records = await page.evaluate(() => JSON.parse(localStorage.getItem('speech-boundary-records') || '[]'));
        assert.deepEqual(records.map(record => record.type), ['speak', 'cancel']);
        assert.ok(['beforeunload', 'pagehide', 'visibilitychange'].includes(records[1].phase), JSON.stringify(records));
        assert.equal(records[1].url, 'http://ui.test/page?tab=memories');
        if (payload.scenario === 'tts_reload') {
          assert.equal((await page.locator('#btn-tts').textContent()).trim(), '🔊 Вслух');
          assert.equal(await page.locator('#btn-tts').evaluate(el => el.classList.contains('active')), false);
          assert.equal(await page.evaluate(() => speechCalls.length), 0);
        }
        assert.deepEqual(errors, []);
        return;
      } else if (payload.scenario === 'tts_hidden') {
        await page.evaluate(() => {
          Object.defineProperty(document, 'hidden', { value: true });
          document.dispatchEvent(new Event('visibilitychange'));
        });
      } else if (payload.scenario === 'tts_error') await page.evaluate(() => speechCalls[0].onerror());
      else await page.locator('#btn-tts').click();
      assert.equal(await page.locator('#btn-tts').textContent(), '🔊 Вслух');
      assert.equal(await page.locator('#btn-tts').evaluate(el => el.classList.contains('active')), false);
      assert.equal(await page.evaluate(() => cancelCalls), payload.scenario === 'tts_error' ? 0 : 1);
    }
    assert.deepEqual(errors, []);
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
