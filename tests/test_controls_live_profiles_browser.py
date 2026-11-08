"""Browser coverage of the bulk provider switch and retained route drafts."""

import json
import os
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
from jinja2 import Environment, FileSystemLoader

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.browser
def test_live_profile_switch_conflict_drafts_and_mobile(tmp_path):
    node = shutil.which("node")
    browser_root = ROOT / "tests/browser"
    if not node or not (browser_root / "node_modules/playwright").is_dir():
        if os.environ.get("GEMAIBOT_REQUIRE_BROWSER_TESTS") == "1":
            pytest.fail("Required profile regression needs Node.js and tests/browser dependencies")
        pytest.skip("Node.js and tests/browser Playwright dependencies are unavailable")
    templates = Environment(loader=FileSystemLoader(ROOT / "app/templates"), autoescape=True)
    payload = {
        "html": templates.get_template("controls.html").render(
            csrf_token="test-profile-csrf",
            g=SimpleNamespace(csp_nonce="offline"),
            url_for=lambda endpoint, **values: f"/static/{values['filename']}" if endpoint == "static" else "/logout",
        ),
        "scripts": {
            f"/static/js/{name}": (ROOT / "app/static/js" / name).read_text(encoding="utf-8")
            for name in ("admin-api.js", "controls.js", "controls-navigation.js")
        },
        "css": (ROOT / "app/static/css/controls.css").read_text(encoding="utf-8"),
        "playwright_module": (browser_root / "node_modules/playwright").as_posix(),
        "screenshot": (tmp_path / "live-profile-mobile.png").as_posix(),
        "desktop_screenshot": (tmp_path / "live-profile-desktop.png").as_posix(),
    }
    script = r"""
const assert = require('node:assert/strict');
let input = '';
process.stdin.setEncoding('utf8');
process.stdin.on('data', chunk => { input += chunk; });
process.stdin.on('end', async () => {
 const data = JSON.parse(input);
 const { chromium } = require(data.playwright_module);
 const browser = await chromium.launch({headless:true});
 try {
  const page = await browser.newPage({viewport:{width:1280,height:900}});
  page.setDefaultTimeout(3000);
  const errors = []; page.on('pageerror', error => errors.push(error.message));
  page.on('dialog', dialog => dialog.accept());
  let revision=1, provider='baseline', conflict=false, documentLoads=0;
  const writes=[];
  const snapshot=()=>({revision,degraded:false,prompts:[],catalogs:[],limits:[],history:[],audio:[],
   commands:{available:false,commands:[]},
   processes:[{id:'chat',title:'Диалог',group:'Общение',models:[provider==='openai'?'openai-direct/gpt-6.1-terra':'gemini-test'],editable:true,strategy:'sequential',inherit_user_model:false,user_model_allowed:provider==='baseline'}],
   live_routes:{provider,process_ids:['chat'],readiness:{openai:{configured:true},gemini:{configured:true}},
    can_restore_baseline:provider!=='baseline',openai_defaults:{chat:'openai-direct/gpt-6.1-terra'},note:'Только наличие ключей.'}});
  await page.route('**/*', async route=>{
   const path=new URL(route.request().url()).pathname;
   if(path==='/controls'){documentLoads++;return route.fulfill({contentType:'text/html',body:data.html});}
   if(data.scripts[path])return route.fulfill({contentType:'application/javascript',body:data.scripts[path]});
   if(path.endsWith('.css'))return route.fulfill({contentType:'text/css',body:data.css});
   if(path==='/api/admin/controls')return route.fulfill({json:snapshot()});
   if(path==='/api/admin/controls/live-provider'){
    assert.equal(route.request().headers()['x-csrf-token'],'test-profile-csrf');
    const body=route.request().postDataJSON();writes.push(body);
    if(conflict){revision++;return route.fulfill({status:409,json:{error:'changed'}});}
    assert.equal(body.expected_revision,revision); provider=body.provider;revision++;
    return route.fulfill({json:{revision,live_routes:snapshot().live_routes}});
   }
   return route.fulfill({json:{}});
  });
  await page.goto('http://profiles.test/controls');
  const profile=page.locator('#live-route-profile');
  await profile.getByRole('button',{name:'Включить OpenAI напрямую',exact:true}).click();
  await page.waitForFunction(()=>document.querySelector('#live-route-profile').textContent.includes('Выбранный профиль: OpenAI напрямую'));
  assert.equal(provider,'openai'); assert.equal(writes.length,1);
  await page.screenshot({path:data.desktop_screenshot,fullPage:true});
  const chat=page.locator('article[data-section="process"][data-id="chat"]');
  assert.equal(await chat.locator('input[type="checkbox"]').isDisabled(),true);
  await chat.locator('.model-input').fill('openai-direct/gpt-6.1-sol');
  await profile.getByRole('button',{name:'Включить Gemini',exact:true}).click();
  await page.waitForFunction(()=>document.querySelector('#live-route-profile').textContent.includes('Выбранный профиль: Gemini'));
  assert.equal(await chat.locator('.model-input').inputValue(),'openai-direct/gpt-6.1-sol');
  assert.equal(await chat.locator('.stale-draft').count(),1);
  conflict=true;
  await profile.getByRole('button',{name:'Включить OpenAI напрямую',exact:true}).click();
  await page.waitForFunction(()=>document.querySelector('#notice').classList.contains('error'));
  assert.equal(provider,'gemini');assert.equal(writes.length,3);
  assert.equal(await chat.locator('.model-input').inputValue(),'openai-direct/gpt-6.1-sol');
  await page.setViewportSize({width:390,height:844});
  await page.screenshot({path:data.screenshot,fullPage:true});
  assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=window.innerWidth),true);
  assert.equal(documentLoads,1);assert.deepEqual(errors,[]);
  process.stdout.write('Provider switch, CSRF/revision, retained draft, conflict, mobile and no reload passed.\n');
 } finally {await browser.close();}
});
"""
    result = subprocess.run(
        [node, "-e", script], input=json.dumps(payload), text=True, encoding="utf-8", capture_output=True, timeout=30
    )
    assert result.returncode == 0, result.stdout + result.stderr
