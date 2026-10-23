"""Offline browser regression for editable TTS catalogs and revision safety."""

import json
import os
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
from jinja2 import Environment, FileSystemLoader

from tests.test_web_audio_controls import ELEVENLABS, GEMINI

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.asyncio
@pytest.mark.browser
async def test_audio_catalog_edit_order_default_save_conflict_reset_and_mobile(tmp_path):
    node = shutil.which("node")
    browser_root = ROOT / "tests/browser"
    if not node or not (browser_root / "node_modules/playwright").is_dir():
        if os.environ.get("GEMAIBOT_REQUIRE_BROWSER_TESTS") == "1":
            pytest.fail("Required TTS editor regression needs Node.js and tests/browser dependencies")
        pytest.skip("Node.js and tests/browser Playwright dependencies are unavailable")

    templates = Environment(loader=FileSystemLoader(ROOT / "app/templates"), autoescape=True)
    html = templates.get_template("controls.html").render(
        csrf_token="test-csrf",
        g=SimpleNamespace(csp_nonce="offline"),
        url_for=lambda endpoint, **values: f"/static/{values['filename']}" if endpoint == "static" else "/logout",
    )
    payload = {
        "html": html,
        "scripts": {
            f"/static/js/{name}": (ROOT / "app/static/js" / name).read_text(encoding="utf-8")
            for name in ("controls.js", "controls-navigation.js")
        },
        "css": (ROOT / "app/static/css/controls.css").read_text(encoding="utf-8"),
        "baseline": {"gemini": GEMINI, "elevenlabs": ELEVENLABS},
        "playwright_module": (browser_root / "node_modules/playwright").as_posix(),
        "screenshot": (tmp_path / "tts-editor-mobile.png").as_posix(),
    }
    script = r"""
const assert = require('node:assert/strict');
let input = '';
process.stdin.setEncoding('utf8');
process.stdin.on('data', chunk => { input += chunk; });
process.stdin.on('end', async () => {
  const data = JSON.parse(input);
  const { chromium } = require(data.playwright_module);
  const browser = await chromium.launch({ headless: true });
  try {
    const page = await browser.newPage({ viewport: { width: 1280, height: 900 } });
    page.setDefaultTimeout(2500);
    const errors = [];
    page.on('pageerror', error => errors.push(error.message));
    let revision = 1;
    let holdSave = false;
    let saveGate;
    let releaseSave;
    const values = structuredClone(data.baseline);
    const writes = [];
    await page.route('**/*', async route => {
      const path = new URL(route.request().url()).pathname;
      if (path === '/api/admin/controls') {
        if (route.request().method() === 'POST') {
          const body = route.request().postDataJSON();
          writes.push(body);
          assert.equal(body.section, 'audio');
          assert.ok(route.request().headers()['x-csrf-token']);
          if (body.expected_revision !== revision) return route.fulfill({ status: 409, json: { error: 'changed' } });
          if (holdSave) { saveGate(); await new Promise(resolve => { releaseSave = resolve; }); }
          values[body.id] = body.reset ? structuredClone(data.baseline[body.id]) : body.value;
          revision += 1;
          return route.fulfill({ json: { revision } });
        }
        return route.fulfill({ json: {
          revision, degraded: false, processes: [], prompts: [], catalogs: [], limits: [], history: [],
          commands: { available: false, commands: [], revision },
          audio: Object.entries(values).map(([id, value]) => ({
            id, title: id, value, baseline: data.baseline[id], source: 'override', apply: 'next_request',
          })),
        }});
      }
      if (path === '/static/css/controls.css') return route.fulfill({ contentType: 'text/css', body: data.css });
      if (data.scripts[path]) return route.fulfill({ contentType: 'text/javascript', body: data.scripts[path] });
      if (path === '/controls') return route.fulfill({ contentType: 'text/html', body: data.html });
      return route.abort();
    });
    await page.goto('http://gemaibot.test/controls#audio');
    const gemini = page.locator('article[data-section="audio"][data-id="gemini"]');
    const eleven = page.locator('article[data-section="audio"][data-id="elevenlabs"]');
    await gemini.waitFor();
    assert.equal(await page.locator('#audio').isVisible(), true);
    assert.equal(await page.locator('#section-select').inputValue(), 'audio');
    assert.equal(await gemini.locator('.voice-row').count(), 2);
    assert.equal(await eleven.getByLabel('Промпт голоса', { exact: true }).count(), 0);
    assert.equal(await page.locator('#audio a[href*="#processes"]').count() > 0, true);
    assert.equal(await page.locator('#audio a[href*="#prompts"]').count() > 0, true);

    const first = gemini.locator('.voice-row').nth(0);
    await first.getByLabel('ID голоса', { exact: true }).fill('Charon');
    await first.getByLabel('Название голоса', { exact: true }).fill('<img src=x onerror=alert(1)>');
    await first.getByLabel('Промпт голоса', { exact: true }).fill('Custom delivery.');
    await first.getByLabel('Стиль / legacy temperature', { exact: true }).fill('0.7');
    await gemini.getByLabel('Температура', { exact: true }).fill('1.1');
    await gemini.getByLabel('Максимум выходных токенов', { exact: true }).fill('8192');
    await gemini.getByRole('button', { name: 'Добавить голос', exact: true }).click();
    const added = gemini.locator('.voice-row').nth(2);
    await added.getByLabel('ID голоса', { exact: true }).fill('Aoede');
    await added.getByLabel('Название голоса', { exact: true }).fill('Новый голос');
    await added.getByLabel('Промпт голоса', { exact: true }).fill('Read gently.');
    await added.getByLabel('Голос по умолчанию', { exact: true }).check();
    await added.getByRole('button', { name: /Выше/ }).click();
    assert.equal(await gemini.locator('.voice-row').nth(1).getByLabel('ID голоса', { exact: true }).inputValue(), 'Aoede');
    await gemini.locator('.voice-row').nth(2).getByRole('button', { name: /Удалить голос/ }).click();
    await gemini.getByRole('button', { name: 'Сохранить голоса и TTS', exact: true }).click();
    await page.locator('#notice').filter({ hasText: 'Сохранено' }).waitFor();
    assert.deepEqual(writes[0].value, {
      default_voice: 'Aoede', voices: [
        { id: 'Charon', name: '<img src=x onerror=alert(1)>', prompt: 'Custom delivery.', temperature: 0.7 },
        { id: 'Aoede', name: 'Новый голос', prompt: 'Read gently.' },
      ], temperature: 1.1, max_output_tokens: 8192,
    });
    assert.equal(await gemini.locator('img').count(), 0);
    assert.equal(await gemini.locator('.voice-row').nth(1).getByLabel('Голос по умолчанию', { exact: true }).isChecked(), true);
    assert.equal(await gemini.locator('.voice-row').nth(0).getByLabel('Стиль / legacy temperature', { exact: true }).inputValue(), '0.7');
    assert.equal(await gemini.locator('.voice-row').nth(1).getByLabel('Стиль / legacy temperature', { exact: true }).inputValue(), '');

    await eleven.getByLabel('Стабильность', { exact: true }).fill('0.6');
    await eleven.getByLabel('Сходство с голосом', { exact: true }).fill('0.8');
    await eleven.getByLabel('Выразительность', { exact: true }).fill('0.3');
    await eleven.getByLabel('Усиление сходства с диктором', { exact: true }).uncheck();
    await eleven.getByLabel('Нормализация текста', { exact: true }).selectOption('off');
    revision += 1;
    await eleven.getByRole('button', { name: 'Сохранить голоса и TTS', exact: true }).click();
    await eleven.locator('.feedback.error').waitFor();
    assert.equal(await eleven.getByLabel('Стабильность', { exact: true }).inputValue(), '0.6');
    await page.locator('#refresh').click();
    await eleven.locator('.stale-draft').waitFor();
    await eleven.getByRole('button', { name: 'Перенести черновик на текущую ревизию', exact: true }).click();
    await eleven.getByRole('button', { name: 'Сохранить голоса и TTS', exact: true }).click();
    await page.locator('#notice').filter({ hasText: 'Сохранено' }).waitFor();
    assert.equal(writes[2].expected_revision, 3);
    assert.deepEqual(writes[2].value.voice_settings, {
      stability: 0.6, similarity_boost: 0.8, style: 0.3, use_speaker_boost: false,
    });
    assert.equal(writes[2].value.apply_text_normalization, 'off');
    await eleven.getByRole('button', { name: 'Сбросить к базовому', exact: true }).click();
    await page.locator('#notice').filter({ hasText: 'ревизия 5' }).waitFor();
    assert.equal(writes[3].reset, true);
    assert.equal(await eleven.getByLabel('Стабильность', { exact: true }).inputValue(), '0.4');

    await gemini.locator('.voice-row').nth(0).getByLabel('Название голоса', { exact: true }).fill('Queued name');
    holdSave = true;
    const saveStarted = new Promise(resolve => { saveGate = resolve; });
    await gemini.getByRole('button', { name: 'Сохранить голоса и TTS', exact: true }).click();
    await saveStarted;
    await gemini.locator('.voice-row').nth(0).getByLabel('Промпт голоса', { exact: true }).fill('Typed during save.');
    holdSave = false;
    releaseSave();
    await page.locator('#notice').filter({ hasText: 'ревизия 6' }).waitFor();
    assert.equal(writes[4].value.voices[0].prompt, 'Custom delivery.');
    assert.equal(await gemini.locator('.voice-row').nth(0).getByLabel('Промпт голоса', { exact: true }).inputValue(), 'Typed during save.');
    await gemini.getByRole('button', { name: 'Отменить черновик', exact: true }).click();
    assert.equal(await gemini.locator('.voice-row').nth(0).getByLabel('Промпт голоса', { exact: true }).inputValue(), 'Custom delivery.');

    await gemini.locator('.voice-row').nth(1).getByLabel('ID голоса', { exact: true }).fill('Leda');
    await gemini.locator('.voice-row').nth(0).getByRole('button', { name: /Ниже/ }).click();
    assert.equal(await gemini.locator('.voice-row').nth(0).getByLabel('Голос по умолчанию', { exact: true }).isChecked(), true);
    await gemini.locator('.voice-row').nth(0).getByRole('button', { name: /Удалить голос/ }).click();
    assert.equal(await gemini.locator('.voice-row').nth(0).getByLabel('Голос по умолчанию', { exact: true }).isChecked(), true);
    assert.equal(await gemini.locator('.voice-row').nth(0).getByRole('button', { name: /Удалить голос/ }).isDisabled(), true);
    await gemini.getByRole('button', { name: 'Сохранить голоса и TTS', exact: true }).click();
    await page.locator('#notice').filter({ hasText: 'ревизия 7' }).waitFor();
    assert.equal(writes[5].value.default_voice, 'Charon');
    assert.equal(writes[5].value.voices[0].temperature, 0.7);
    await gemini.locator('.voice-row').nth(0).getByLabel('Стиль / legacy temperature', { exact: true }).fill('');
    await gemini.getByRole('button', { name: 'Сохранить голоса и TTS', exact: true }).click();
    await page.locator('#notice').filter({ hasText: 'ревизия 8' }).waitFor();
    assert.equal('temperature' in writes[6].value.voices[0], false);

    await page.setViewportSize({ width: 390, height: 844 });
    assert.equal(await page.locator('#section-select').isVisible(), true);
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth), true);
    await page.evaluate(() => window.scrollTo({ top: 0, behavior: 'instant' }));
    await page.screenshot({ path: data.screenshot, fullPage: true });
    assert.deepEqual(errors, []);
    process.stdout.write('TTS catalogs, prompts, order, defaults, revision safety and mobile passed.\n');
  } finally {
    await browser.close();
  }
}).on('error', error => { console.error(error); process.exitCode = 1; });
"""
    script_path = tmp_path / "controls-audio.cjs"
    script_path.write_text(script, encoding="utf-8")
    result = subprocess.run(
        [node, str(script_path)],
        cwd=browser_root,
        input=json.dumps(payload, ensure_ascii=False),
        encoding="utf-8",
        capture_output=True,
        timeout=25,
    )
    assert result.returncode == 0, result.stdout + result.stderr
