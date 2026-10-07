"""Offline browser regression for command discovery, navigation and refresh."""

import json
import os
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
from telegram.ext import CommandHandler

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.asyncio
@pytest.mark.browser
async def test_command_search_navigation_and_refresh_in_browser(tmp_path):
    node = shutil.which("node")
    browser_root = ROOT / "tests/browser"
    required = os.environ.get("GEMAIBOT_REQUIRE_BROWSER_TESTS") == "1"
    if not node or not (browser_root / "node_modules/playwright").is_dir():
        if required:
            pytest.fail("Required browser regression needs the existing tests/browser dependencies and Node.js")
        pytest.skip("Node.js and tests/browser Playwright dependencies are unavailable")

    from app.command_inventory import command_inventory
    from app.handlers import commands, memory_commands, messages
    from app.web import quart_app

    class RecordingApplication:
        def __init__(self):
            self.handlers = {}

        def add_handler(self, handler, group=0):
            self.handlers.setdefault(group, []).append(handler)

    application = RecordingApplication()
    commands.register(application)
    memory_commands.register(application)
    messages.register(application)

    async def future(update, context):
        """<img src=x onerror=alert(1)>"""

    replacement = SimpleNamespace(handlers={0: [CommandHandler(["future", "later"], future)]})
    client = quart_app.test_client()
    async with client.session_transaction() as session:
        session["authenticated"] = True
    response = await client.get("/controls")
    assert response.status_code == 200
    payload = {
        "html": await response.get_data(as_text=True),
        "scripts": {
            "/static/js/controls.js": (ROOT / "app/static/js/controls.js").read_text(encoding="utf-8"),
            "/static/js/controls-navigation.js": (ROOT / "app/static/js/controls-navigation.js").read_text(
                encoding="utf-8"
            ),
        },
        "css": (ROOT / "app/static/css/controls.css").read_text(encoding="utf-8"),
        "commands": command_inventory(application),
        "replacement": command_inventory(replacement),
        "playwright_module": (browser_root / "node_modules/playwright").as_posix(),
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
    const errors = [];
    page.on('pageerror', error => errors.push(error.message));
    let commandReads = 0;
    await page.route('**/*', async route => {
      const path = new URL(route.request().url()).pathname;
      if (path === '/api/admin/controls/commands') {
        commandReads += 1;
        return route.fulfill({ json: commandReads === 1 ? data.commands : data.replacement });
      }
      if (path === '/api/admin/controls') return route.fulfill({ json: {
        revision: 1, degraded: false, processes: [], prompts: [], catalogs: [], limits: [], history: [],
      }});
      if (path === '/static/css/controls.css') return route.fulfill({ contentType: 'text/css', body: data.css });
      if (data.scripts[path]) return route.fulfill({ contentType: 'text/javascript', body: data.scripts[path] });
      if (path === '/controls') return route.fulfill({ contentType: 'text/html', body: data.html });
      return route.abort();
    });
    await page.goto('http://gemaibot.test/controls#commands');
    await page.locator('#command-count').filter({ hasText: /^[0-9]+$/ }).waitFor();
    assert.equal(await page.locator('#commands').isVisible(), true);
    await page.locator('#command-search').fill('натальная');
    await page.locator('#command-kind').selectOption('public');
    const natal = page.locator('#command-list article:visible');
    assert.equal(await natal.count(), 1);
    assert.equal((await natal.innerText()).includes('/natal'), true);
    await page.locator('.sidebar a[href="#prompts"]').click();
    assert.equal(await page.locator('#prompts').isVisible(), true);
    await page.locator('.sidebar a[href="#commands"]').click();
    assert.equal(await page.locator('#command-search').inputValue(), 'натальная');
    assert.equal(new URL(page.url()).searchParams.get('command'), 'натальная');
    await page.locator('#command-search').fill('/таро');
    assert.equal((await page.locator('#command-list article:visible').innerText()).includes('/tarot'), true);
    await page.locator('#command-search').fill('');
    await page.locator('#command-kind').selectOption('');
    await page.locator('#refresh').click();
    await page.locator('#command-count').filter({ hasText: /^1$/ }).waitFor();
    const refreshed = await page.locator('#command-list').innerText();
    assert.equal(refreshed.includes('/future'), true);
    assert.equal(refreshed.includes('/later'), true);
    assert.equal(refreshed.includes('/tarot'), false);
    assert.equal(refreshed.includes('<img src=x onerror=alert(1)>'), true);
    assert.equal(await page.locator('#command-list img').count(), 0);
    await page.setViewportSize({ width: 390, height: 844 });
    assert.equal(await page.locator('#section-select').isVisible(), true);
    assert.equal(await page.locator('#section-select').inputValue(), 'commands');
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth), true);
    assert.deepEqual(errors, []);
    process.stdout.write('Command search, aliases, refresh, escaping and mobile layout passed.\n');
  } finally {
    await browser.close();
  }
}).on('error', error => { console.error(error); process.exitCode = 1; });
"""
    script_path = tmp_path / "controls-commands.cjs"
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


@pytest.mark.asyncio
@pytest.mark.browser
async def test_alias_editor_save_conflict_refresh_reset_and_mobile(tmp_path):
    node = shutil.which("node")
    browser_root = ROOT / "tests/browser"
    if not node or not (browser_root / "node_modules/playwright").is_dir():
        if os.environ.get("GEMAIBOT_REQUIRE_BROWSER_TESTS") == "1":
            pytest.fail("Required alias editor regression needs Node.js and tests/browser dependencies")
        pytest.skip("Node.js and tests/browser Playwright dependencies are unavailable")

    from app.command_aliases import command_alias_inventory
    from app.runtime_settings.store import SettingsSnapshot
    from app.web import quart_app

    async def draw(update, context):
        pass

    application = SimpleNamespace(handlers={0: [CommandHandler(["draw", "img"], draw)]})
    client = quart_app.test_client()
    async with client.session_transaction() as session:
        session["authenticated"] = True
    response = await client.get("/controls")
    payload = {
        "html": await response.get_data(as_text=True),
        "scripts": {
            f"/static/js/{name}": (ROOT / "app/static/js" / name).read_text(encoding="utf-8")
            for name in ("controls.js", "controls-navigation.js")
        },
        "css": (ROOT / "app/static/css/controls.css").read_text(encoding="utf-8"),
        "commands": command_alias_inventory(application, SettingsSnapshot(1, {})),
        "playwright_module": (browser_root / "node_modules/playwright").as_posix(),
        "screenshot": (tmp_path / "alias-editor-mobile.png").as_posix(),
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
    const errors = [];
    page.on('pageerror', error => errors.push(error.message));
    let revision = 1;
    page.setDefaultTimeout(3000);
    let aliases = ['/img'];
    let failRefresh = false;
    let holdSave = false;
    let releaseSave;
    const writes = [];
    const catalog = () => ({ ...data.commands, revision, commands: data.commands.commands.map(row => ({
      ...row, aliases: [...aliases], source: aliases.join() === '/img' ? 'default' : 'override',
    })) });
    await page.route('**/*', async route => {
      const path = new URL(route.request().url()).pathname;
      if (path === '/api/admin/controls/preview') {
        const body = route.request().postDataJSON();
        assert.equal(body.section, 'command');
        assert.equal(body.id, 'draw');
        return route.fulfill({ json: { valid: true, notes: ['Формат и конфликты проверены.'] } });
      }
      if (path === '/api/admin/controls/commands') return route.fulfill({ json: catalog() });
      if (path === '/api/admin/controls') {
        if (route.request().method() === 'POST') {
          const body = route.request().postDataJSON();
          writes.push(body);
          assert.equal(body.section, 'command');
          assert.equal(body.id, 'draw');
          assert.ok(route.request().headers()['x-csrf-token']);
          if (body.expected_revision !== revision) return route.fulfill({ status: 409, json: { error: 'changed' } });
          if (holdSave) await new Promise(resolve => { releaseSave = resolve; });
          aliases = body.reset ? ['/img'] : body.value;
          revision += 1;
          return route.fulfill({ json: { revision } });
        }
        if (failRefresh) {
          failRefresh = false;
          return route.fulfill({ status: 503, json: { error: 'Refresh unavailable' } });
        }
        return route.fulfill({ json: { revision, degraded: false, processes: [], prompts: [], catalogs: [],
          limits: [], history: [], commands: catalog() } });
      }
      if (path === '/static/css/controls.css') return route.fulfill({ contentType: 'text/css', body: data.css });
      if (data.scripts[path]) return route.fulfill({ contentType: 'text/javascript', body: data.scripts[path] });
      if (path === '/controls') return route.fulfill({ contentType: 'text/html', body: data.html });
      return route.abort();
    });
    await page.goto('http://gemaibot.test/controls#commands');
    const card = page.locator('article[data-section="command"][data-id="draw"]');
    const editor = card.locator('textarea');
    await editor.waitFor({ timeout: 3000 });
    assert.equal(await editor.inputValue(), '/img');
    await editor.fill('/paint\n/рисуй\nнарисуй');
    await page.locator('#command-search').fill('/paint');
    assert.equal(await card.isVisible(), true);
    await card.getByRole('button', { name: 'Проверить алиасы', exact: true }).click();
    await card.locator('.feedback').filter({ hasText: 'Формат и конфликты проверены' }).waitFor();

    holdSave = true;
    await card.getByRole('button', { name: 'Сохранить алиасы', exact: true }).click();
    await page.waitForFunction(() => document.querySelector('#notice') &&
      document.querySelector('article[data-id="draw"] .feedback').textContent.includes('Сохраняем'));
    await editor.fill('/paint\n/рисуй\nнарисуй\n/during');
    assert.deepEqual(writes[0].value, ['/paint', '/рисуй', 'нарисуй']);
    holdSave = false;
    releaseSave();
    await page.locator('#revision').filter({ hasText: 'Ревизия 2' }).waitFor();
    assert.equal(await editor.inputValue(), '/paint\n/рисуй\nнарисуй\n/during');
    await card.getByRole('button', { name: 'Сохранить алиасы', exact: true }).click();
    await page.locator('#revision').filter({ hasText: 'Ревизия 3' }).waitFor();
    assert.equal(writes[1].expected_revision, 2);
    assert.equal(await card.evaluate(node => node.classList.contains('dirty')), false);

    await editor.fill('/paint\n/retry');
    revision = 4; // Another administrator has saved since this draft started.
    aliases = ['/elsewhere'];
    await card.getByRole('button', { name: 'Сохранить алиасы', exact: true }).click();
    await card.locator('.feedback').filter({ hasText: 'Конфигурация изменилась' }).waitFor();
    assert.equal(await editor.inputValue(), '/paint\n/retry');
    await page.locator('#refresh').click();
    await card.locator('.stale-draft').waitFor();
    assert.equal(await editor.inputValue(), '/paint\n/retry');
    await card.getByRole('button', { name: 'Перенести черновик на текущую ревизию' }).click();
    failRefresh = true;
    await card.getByRole('button', { name: 'Сохранить алиасы', exact: true }).click();
    await page.locator('#notice.error').filter({ hasText: 'на сервере' }).waitFor();
    assert.equal(await card.getByRole('button', { name: 'Сохранить алиасы', exact: true }).isDisabled(), true);
    assert.equal(await editor.inputValue(), '/paint\n/retry');
    await page.locator('#refresh').click();
    await page.locator('#revision').filter({ hasText: 'Ревизия 5' }).waitFor();
    assert.equal(await editor.inputValue(), '/paint\n/retry');
    assert.equal(await card.evaluate(node => node.classList.contains('dirty')), false);

    await editor.fill('/discard');
    await page.locator('#command-search').fill('');
    await card.getByRole('button', { name: 'Отменить черновик', exact: true }).click();
    assert.equal(await editor.inputValue(), '/paint\n/retry');
    await card.getByRole('button', { name: 'Вернуть исходные алиасы', exact: true }).click();
    await page.locator('#revision').filter({ hasText: 'Ревизия 6' }).waitFor();
    assert.equal(await editor.inputValue(), '/img');
    await editor.fill('');
    await card.getByRole('button', { name: 'Сохранить алиасы', exact: true }).click();
    await page.locator('#revision').filter({ hasText: 'Ревизия 7' }).waitFor();
    assert.deepEqual(writes.at(-1).value, []);
    assert.equal(await editor.inputValue(), '');

    await page.setViewportSize({ width: 390, height: 844 });
    assert.equal(await editor.isVisible(), true);
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth), true);
    assert.deepEqual(errors, []);
    await page.screenshot({ path: data.screenshot, fullPage: true });
    process.stdout.write('Alias editor drafts, save, conflicts, recovery, reset and mobile layout passed.\n');
  } finally { await browser.close(); }
}).on('error', error => { console.error(error); process.exitCode = 1; });
"""
    script_path = tmp_path / "controls-alias-editor.cjs"
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
