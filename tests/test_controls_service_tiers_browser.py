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
  let revision=1,conflict=false,provider='openai',loads=0,catalog=[luna,mini,terra];const writes=[];
  const routes={chat:{models:[luna],strategy:'sequential',inherit_user_model:false,service_tier:'default'},search:{models:[luna],strategy:'sequential',inherit_user_model:false,service_tier:'default'},'chat.complex':{models:[terra],strategy:'sequential',inherit_user_model:false,service_tier:'flex',reasoning_effort:'low'},'roles.generate':{models:[terra],strategy:'sequential',inherit_user_model:false}};
  const badLuna='openai-direct/gpt-6.1-luna';
  const modelError='Эта модель не поддерживается прямым OpenAI API. Выберите openai-direct/gpt-6-luna.';
  const allEfforts=['none','low','medium','high'];
  let savedOpenAI;
  const snapshot=()=>({revision,degraded:false,prompts:[],limits:[],audio:[],history:[],commands:{available:false,commands:[]},
   catalogs:[{provider:'openai',models:catalog,supported_models:[luna,terra,sol,mini],flex_models:[luna,terra,sol],
    reasoning_efforts_by_model:{[luna]:allEfforts,[terra]:allEfforts,[sol]:['low','medium','high'],[mini]:[]}}],
   processes:[...Object.entries(routes).map(([id,value])=>({id,title:id==='chat'?'Диалог':'Ответ с поиском',group:'Общение',editable:true,user_model_allowed:false,service_tier_explicit:true,service_tiers:['default','flex'],...value,
    ...(id==='roles.generate'?{title:'Роли: создание инструкции',user_model_allowed:true,service_tier_explicit:false,openai_model_choices:[sol,terra,luna],reasoning_efforts:allEfforts,flex_standard_recovery:false}:{}),
   })),
    {id:'research',title:'Agentic research',models:['gemini-test'],editable:true,openai_model_choices:[],reasoning_efforts:[],service_tiers:['default']}],
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
    if(body.value.models?.includes(badLuna))return route.fulfill({status:400,json:{error:modelError}});
    if(body.section==='process')routes[body.id]=structuredClone(body.value);else if(body.section==='catalog')catalog=body.value;
    revision++;return route.fulfill({json:{revision}});
   }
   if(path==='/api/admin/controls/preview'){
    assert.deepEqual(route.request().postDataJSON().value.models,[badLuna]);
    return route.fulfill({status:400,json:{error:modelError}});
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
  assert.equal(await reasoning.locator('option[value=""]').textContent(),'По умолчанию модели');
  const complex=page.locator('article[data-section="process"][data-id="chat.complex"]');
  assert.equal(await complex.getByLabel('Тариф обработки',{exact:true}).inputValue(),'flex');
  assert.equal(await complex.getByLabel('Reasoning effort',{exact:true}).inputValue(),'low');
  await picker.selectOption(terra);await tier.selectOption('flex');await reasoning.selectOption('none');await chat.getByRole('button',{name:'Сохранить маршрут',exact:true}).click();
  await page.waitForFunction(()=>document.querySelector('#revision').textContent.includes('2'));
  assert.equal(routes.chat.models[0],terra);assert.equal(routes.chat.service_tier,'flex');assert.equal(routes.chat.reasoning_effort,'none');
  await picker.selectOption(sol);await chat.getByRole('button',{name:'Сохранить маршрут',exact:true}).click();
  assert.equal(writes.length,1);assert.equal(await reasoning.inputValue(),'none');
  assert.equal(await reasoning.locator('option[value="none"]').evaluate(option=>option.disabled),true);
  assert.match(await chat.locator('.feedback').textContent(),/Sol поддерживает уровни Low, Medium и High/);
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
  assert.equal(await reasoning.isVisible(),false);
  assert.equal(await tier.isVisible(),false);
  await profile.getByRole('button',{name:'Включить OpenAI напрямую',exact:true}).click();
  await page.waitForFunction(()=>document.querySelector('#revision').textContent.includes('7'));
  assert.equal(await reasoning.isVisible(),true);
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
  await page.selectOption('#section-select','processes');
  const role=page.locator('article[data-id="roles.generate"]');
  const search=page.locator('#process-search');
  await role.waitFor();
  await search.fill('/gpt-6.1-terra ');
  const roleModel=role.getByRole('textbox',{name:'Модель 1',exact:true});
  await roleModel.fill(badLuna);
  const roleError=role.locator('.feedback.error').filter({hasText:modelError});
  await role.getByRole('button',{name:'Проверить маршрут',exact:true}).click();
  await roleError.waitFor();
  const roleSave=role.getByRole('button',{name:'Сохранить маршрут',exact:true});
  await roleSave.click();
  await roleError.waitFor();
  assert.equal(await roleModel.inputValue(),badLuna);
  assert.equal(writes.at(-1).id,'roles.generate');
  assert.equal(revision,8);
  await page.getByRole('button',{name:'Обновить данные',exact:true}).click();
  await page.locator('#notice').filter({hasText:'Данные обновлены · ревизия 8'}).waitFor();
  assert.equal(await role.isVisible(),true);
  assert.equal(await roleModel.inputValue(),badLuna);
  const roleTier=role.getByLabel('Тариф обработки',{exact:true});
  const roleFlex=roleTier.locator('option[value="flex"]');
  assert.equal(await roleFlex.evaluate(option=>option.disabled),true);
  const rolePicker=role.getByLabel('Выбрать модель OpenAI 1');
  assert.deepEqual(await rolePicker.locator('option').evaluateAll(options=>options.map(option=>option.value)),['',luna,terra,sol]);
  await rolePicker.selectOption(luna);
  const inherit=role.getByRole('checkbox',{name:'Учитывать модель пользователя'});
  await inherit.check();
  assert.equal(await roleFlex.evaluate(option=>option.disabled),true);
  await inherit.uncheck();
  await roleTier.selectOption('flex');
  assert.ok(await role.getByText('Маршрут сохраняет выбранный тариф при переходе к резерву.',{exact:false}).isVisible());
  await role.getByLabel('Reasoning effort',{exact:true}).selectOption('high');
  await roleSave.click();
  await page.waitForFunction(()=>document.querySelector('#revision').textContent.includes('9'));
  assert.equal(await search.inputValue(),'/gpt-6.1-terra ');
  assert.equal(await role.isVisible(),false);
  await search.clear();
  assert.equal(await roleModel.inputValue(),luna);
  assert.equal(await roleTier.inputValue(),'flex');
  assert.equal(await role.getByLabel('Reasoning effort',{exact:true}).inputValue(),'high');
  const research=page.locator('article[data-id="research"]');
  assert.equal(await research.locator('.model-picker').count(),0);
  assert.equal(await research.getByLabel('Тариф обработки',{exact:true}).isVisible(),false);
  assert.equal(await research.getByLabel('Reasoning effort',{exact:true}).isVisible(),false);
  assert.deepEqual(errors,[]);
  process.stdout.write('Model/tier save, incompatible combination, CAS conflict, profile round-trip, catalog addition and mobile passed.\n');
 }finally{await browser.close();}
});
"""
    result = subprocess.run(
        [node, "-e", script], input=json.dumps(payload), text=True, encoding="utf-8", capture_output=True, timeout=40
    )
    assert result.returncode == 0, result.stdout + result.stderr
