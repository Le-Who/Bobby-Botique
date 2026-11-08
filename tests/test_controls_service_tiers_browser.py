"""Real browser checks for model/tier selection without silently changing either."""

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
def test_openai_model_tier_save_conflict_catalog_and_mobile(tmp_path):
    node = shutil.which("node")
    module = ROOT / "tests/browser/node_modules/playwright"
    if not node or not module.is_dir():
        if os.environ.get("GEMAIBOT_REQUIRE_BROWSER_TESTS") == "1":
            pytest.fail("Required browser dependencies missing")
        pytest.skip("Node.js and locked Playwright unavailable")
    templates = Environment(loader=FileSystemLoader(ROOT / "app/templates"), autoescape=True)
    payload = {
        "html": templates.get_template("controls.html").render(
            csrf_token="tier-csrf",
            g=SimpleNamespace(csp_nonce="offline"),
            url_for=lambda endpoint, **values: f"/static/{values['filename']}" if endpoint == "static" else "/logout",
        ),
        "scripts": {
            f"/static/js/{name}": (ROOT / "app/static/js" / name).read_text(encoding="utf-8")
            for name in ("admin-api.js", "controls.js", "controls-navigation.js")
        },
        "css": (ROOT / "app/static/css/controls.css").read_text(encoding="utf-8"),
        "module": module.as_posix(),
        "desktop": (tmp_path / "tiers-desktop.png").as_posix(),
        "mobile": (tmp_path / "tiers-mobile.png").as_posix(),
    }
    script = r"""
const assert=require('node:assert/strict');let input='';
process.stdin.setEncoding('utf8');process.stdin.on('data',s=>input+=s);
process.stdin.on('end',async()=>{
 const data=JSON.parse(input),{chromium}=require(data.module),browser=await chromium.launch({headless:true});
 try{
  const page=await browser.newPage({viewport:{width:1366,height:900}});page.setDefaultTimeout(3500);
  const errors=[];page.on('pageerror',e=>errors.push(e.message));page.on('dialog',d=>d.accept());
  const luna='openai-direct/gpt-6-luna',terra='openai-direct/gpt-6.1-terra',sol='openai-direct/gpt-6.1-sol',mini='openai-direct/gpt-4o-mini';
  let revision=1,conflict=false,provider='openai',loads=0,catalog=[luna,terra];const writes=[];
  const routes={chat:{models:[luna],strategy:'sequential',inherit_user_model:false,service_tier:'default'},search:{models:[luna],strategy:'sequential',inherit_user_model:false,service_tier:'default'},'chat.complex':{models:[terra],strategy:'sequential',inherit_user_model:false,service_tier:'flex',reasoning_effort:'low'}};
  let savedOpenAI;
  const snapshot=()=>({revision,degraded:false,prompts:[],limits:[],audio:[],history:[],commands:{available:false,commands:[]},
   catalogs:[{provider:'openai',models:catalog,supported_models:[luna,terra,sol,mini],flex_models:[luna,terra,sol]}],
   processes:Object.entries(routes).map(([id,value])=>({id,title:id==='chat'?'Диалог':'Ответ с поиском',group:'Общение',editable:true,user_model_allowed:false,service_tier_explicit:true,service_tiers:['default','flex'],...value})),
   live_routes:{provider,process_ids:['chat','search'],readiness:{openai:{configured:true},gemini:{configured:true}},can_restore_baseline:true,openai_defaults:{chat:luna,search:luna},note:'Только наличие ключей.'}});
  await page.route('**/*',async route=>{
   const path=new URL(route.request().url()).pathname;
   if(path==='/controls'){loads++;return route.fulfill({contentType:'text/html',body:data.html});}
   if(data.scripts[path])return route.fulfill({contentType:'application/javascript',body:data.scripts[path]});
   if(path.endsWith('.css'))return route.fulfill({contentType:'text/css',body:data.css});
   if(path==='/api/admin/controls'&&route.request().method()==='GET')return route.fulfill({json:snapshot()});
   if(path==='/api/admin/controls'&&route.request().method()==='POST'){
    const body=route.request().postDataJSON();writes.push(body);assert.equal(route.request().headers()['x-csrf-token'],'tier-csrf');
    if(conflict){conflict=false;revision++;return route.fulfill({status:409,json:{error:'changed'}});}
    assert.equal(body.expected_revision,revision);
    if(body.section==='process')routes[body.id]=structuredClone(body.value);else if(body.section==='catalog')catalog=body.value;
    revision++;return route.fulfill({json:{revision}});
   }
   if(path==='/api/admin/controls/live-provider'){
    const body=route.request().postDataJSON();assert.equal(body.expected_revision,revision);
    if(body.provider==='gemini'){savedOpenAI=structuredClone(routes);for(const key of Object.keys(routes))routes[key]={...routes[key],models:['gemini-test'],service_tier:'default',reasoning_effort:null};}
    else Object.assign(routes,savedOpenAI);
    provider=body.provider;revision++;return route.fulfill({json:{revision}});
   }
   return route.fulfill({json:{}});
  });
  await page.goto('http://tiers.test/controls');
  const chat=page.locator('article[data-section="process"][data-id="chat"]');
  const picker=chat.getByLabel('Выбрать модель OpenAI 1'),tier=chat.getByLabel('Тариф обработки',{exact:true});
  const reasoning=chat.getByLabel('Reasoning effort',{exact:true});
  const complex=page.locator('article[data-section="process"][data-id="chat.complex"]');
  assert.equal(await complex.getByLabel('Тариф обработки',{exact:true}).inputValue(),'flex');
  assert.equal(await complex.getByLabel('Reasoning effort',{exact:true}).inputValue(),'low');
  await picker.selectOption(terra);await tier.selectOption('flex');await reasoning.selectOption('none');await chat.getByRole('button',{name:'Сохранить маршрут',exact:true}).click();
  await page.waitForFunction(()=>document.querySelector('#revision').textContent.includes('2'));
  assert.equal(routes.chat.models[0],terra);assert.equal(routes.chat.service_tier,'flex');assert.equal(routes.chat.reasoning_effort,'none');
  await picker.selectOption(sol);await chat.getByRole('button',{name:'Сохранить маршрут',exact:true}).click();
  assert.equal(writes.length,1);assert.equal(await reasoning.inputValue(),'none');
  assert.match(await chat.locator('.feedback').textContent(),/Sol не поддерживает reasoning none/);
  await picker.selectOption(mini);assert.equal(await tier.inputValue(),'flex');
  await chat.getByRole('button',{name:'Сохранить маршрут',exact:true}).click();assert.equal(writes.length,1);
  assert.match(await chat.locator('.feedback').textContent(),/Flex требует/);
  await tier.selectOption('default');await chat.getByRole('button',{name:'Сохранить маршрут',exact:true}).click();
  assert.equal(writes.length,1);assert.equal(await reasoning.inputValue(),'none');
  assert.match(await chat.locator('.feedback').textContent(),/Reasoning override требует/);
  await reasoning.selectOption('');await chat.getByRole('button',{name:'Сохранить маршрут',exact:true}).click();
  await page.waitForFunction(()=>document.querySelector('#revision').textContent.includes('3'));
  assert.equal(routes.chat.models[0],mini);assert.equal(routes.chat.service_tier,'default');assert.equal(routes.chat.reasoning_effort,null);
  await picker.selectOption(luna);await tier.selectOption('flex');await reasoning.selectOption('low');conflict=true;
  await chat.getByRole('button',{name:'Сохранить маршрут',exact:true}).click();
  await page.waitForFunction(()=>document.querySelector('article[data-id="chat"] .feedback').classList.contains('error'));
  assert.equal(await tier.inputValue(),'flex');assert.equal(await chat.locator('.model-input').inputValue(),luna);assert.equal(await reasoning.inputValue(),'low');
  await page.getByRole('button',{name:'Обновить данные',exact:true}).click();
  await chat.getByRole('button',{name:'Перенести черновик на текущую ревизию',exact:true}).click();
  await chat.getByRole('button',{name:'Сохранить маршрут',exact:true}).click();
  await page.waitForFunction(()=>document.querySelector('#revision').textContent.includes('5'));
  const profile=page.locator('#live-route-profile');
  await profile.getByRole('button',{name:'Включить Gemini',exact:true}).click();
  await page.waitForFunction(()=>document.querySelector('#revision').textContent.includes('6'));
  await profile.getByRole('button',{name:'Включить OpenAI напрямую',exact:true}).click();
  await page.waitForFunction(()=>document.querySelector('#revision').textContent.includes('7'));
  assert.equal(await tier.inputValue(),'flex');assert.equal(await chat.locator('.model-input').inputValue(),luna);assert.equal(await reasoning.inputValue(),'low');
  await page.evaluate(()=>window.scrollTo(0,0));await page.evaluate(()=>new Promise(requestAnimationFrame));
  await page.screenshot({path:data.desktop,fullPage:true});
  await page.setViewportSize({width:390,height:844});
  assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=window.innerWidth),true);
  await page.screenshot({path:data.mobile,fullPage:true});
  await page.selectOption('#section-select','catalogs');
  const catalogCard=page.locator('article[data-section="catalog"][data-id="openai"]');
  await catalogCard.getByRole('button',{name:'Добавить ID модели',exact:true}).click();
  await catalogCard.locator('.model-input').last().fill(sol);
  await catalogCard.getByRole('button',{name:'Сохранить каталог',exact:true}).click();
  await page.waitForFunction(()=>document.querySelector('#revision').textContent.includes('8'));
  assert.ok(catalog.includes(sol));assert.equal(loads,1);assert.deepEqual(errors,[]);
  process.stdout.write('Model/tier save, incompatible combination, CAS conflict, profile round-trip, catalog addition and mobile passed.\n');
 }finally{await browser.close();}
});
"""
    result = subprocess.run(
        [node, "-e", script], input=json.dumps(payload), text=True, encoding="utf-8", capture_output=True, timeout=40
    )
    assert result.returncode == 0, result.stdout + result.stderr
