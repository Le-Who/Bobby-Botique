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
async def test_rpd_rpm_save_disable_reset_and_discard_are_independent(tmp_path):
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
            for name in ("admin-api.js", "controls.js", "controls-navigation.js")
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
  const page = await browser.newPage();
  page.setDefaultTimeout(2500);
  let revision = 1, limit = 100, rpm_limit = null;
  const writes = [];
  await page.route('**/*', async route => {
   const path = new URL(route.request().url()).pathname;
   if (path === '/api/admin/controls') {
    if (route.request().method() === 'POST') {
     const body = route.request().postDataJSON(); writes.push(body);
     if (body.expected_revision !== revision) return route.fulfill({status:409,json:{error:'changed'}});
     if(body.section === 'rpm') rpm_limit = body.reset ? null : body.value;
     else if(body.section === 'limit') limit = body.reset ? 100 : body.value;
     else throw Error('Unexpected mutation');
     return route.fulfill({json:{revision:++revision}});
    }
    return route.fulfill({json:{revision,degraded:false,processes:[],prompts:[],catalogs:[],audio:[],history:[],limits:[{model:'gemini-test',limit,source:'admin',rpm_limit,rpm_source:'admin'},{model:'gemini-test-image',limit:null,rpm_limit:null,rpd_supported:false}],commands:{available:false,commands:[]}}});
   }
   if(data.scripts[path]) return route.fulfill({contentType:'text/javascript',body:data.scripts[path]});
   if(path === '/static/css/controls.css') return route.fulfill({contentType:'text/css',body:data.css});
   if(path === '/controls') return route.fulfill({contentType:'text/html',body:data.html});
   return route.abort();
  });
  await page.goto('http://gemaibot.test/controls#limits');
  const card = page.locator('article[data-section="limit"][data-id="gemini-test"]');
  const rpm = card.getByLabel('RPM · запросов на ключ за 60 секунд', {exact:true});
  const rpd = card.getByLabel('RPD · запросов на ключ в сутки', {exact:true});
  const image = page.locator('article[data-id="gemini-test-image"]');
  assert.equal(await image.getByLabel('RPM · запросов на ключ за 60 секунд', {exact:true}).count(), 1);
  assert.equal(await image.getByRole('button',{name:'Сохранить RPD',exact:true}).count(), 0);
  await rpm.fill('7');
  await card.getByRole('button',{name:'Сохранить RPM',exact:true}).click();
  await page.waitForFunction(() => document.querySelector('#revision').textContent.includes('2'));
  assert.equal(await rpd.inputValue(),'100');
  assert.equal(writes[0].section,'rpm'); assert.equal(writes[0].value,7);
  await rpm.fill('');
  await card.getByRole('button',{name:'Сохранить RPM',exact:true}).click();
  await page.waitForFunction(() => document.querySelector('#revision').textContent.includes('3'));
  assert.equal(writes[1].value,null);
  await rpd.fill('90');
  await rpm.fill('8');
  const rpmGroup = rpm.locator('..').locator('..');
  await rpmGroup.getByRole('button',{name:'Отменить черновик',exact:true}).click();
  assert.equal(await rpd.inputValue(),'90'); assert.equal(await rpm.inputValue(),'');
  await card.getByRole('button',{name:'Сохранить RPD',exact:true}).click();
  await page.waitForFunction(() => document.querySelector('#revision').textContent.includes('4'));
  await card.getByRole('button',{name:'Сбросить RPM',exact:true}).click();
  await page.waitForFunction(() => document.querySelector('#revision').textContent.includes('5'));
  assert.equal(writes[3].section,'rpm'); assert.equal(writes[3].reset,true);
  assert.equal(await rpd.inputValue(),'90');
 } finally { await browser.close(); }
}).on('error', error => { console.error(error); process.exitCode=1; });
"""
    script_path = tmp_path / "controls-limits.cjs"
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
