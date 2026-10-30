"""Execute the shared transport with delayed, aborted and ambiguous requests."""

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def test_deadlines_no_post_retry_status_codes_latest_get_and_kyiv_midnight():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node unavailable")
    script = r"""
const assert = require('node:assert/strict');
const {adminRequest, createLatestRequest, kyivDate} = require('./app/static/js/admin-api.js');
(async () => {
  let calls = 0;
  global.fetch = () => { calls += 1; return new Promise(() => {}); };
  await assert.rejects(adminRequest('/save', {method:'POST',body:{revision:1},timeoutMs:10}), e => e.code === 'timeout' && e.outcomeUnknown);
  assert.equal(calls, 1);
  await assert.rejects(adminRequest('/read', {timeoutMs:10}), e => e.code === 'timeout' && !e.outcomeUnknown);
  for (const [status,code] of [[401,'session_expired'],[403,'denied'],[409,'conflict'],[429,'temporary_unavailable'],[503,'temporary_unavailable']]) {
    global.fetch = async () => new Response(JSON.stringify({current_revision:2}), {status,headers:{'Retry-After':'3'}});
    await assert.rejects(adminRequest('/save', {method:'POST'}), e => e.status === status && e.code === code && e.retryAfter === '3' && e.data.current_revision === 2);
  }
  for (const body of [{error:'storage_unavailable'}, {admission:{state:'unknown'}}, {outcome_unknown:true}]) {
    global.fetch = async () => new Response(JSON.stringify(body), {status:503});
    await assert.rejects(adminRequest('/save', {method:'POST',body:{}}), e => e.outcomeUnknown === true);
    await assert.rejects(adminRequest('/read'), e => e.outcomeUnknown === false);
  }
  global.fetch = async () => new Response(JSON.stringify({admission:{state:'rejected',reason_code:'job_capacity'}}), {status:503});
  await assert.rejects(adminRequest('/save', {method:'POST',body:{}}), e => e.outcomeUnknown === false);
  global.fetch = async () => new Response('synthetic proxy failure', {status:502});
  await assert.rejects(adminRequest('/save', {method:'POST',body:{}}), e => e.outcomeUnknown === true);
  global.document = {getElementById:() => ({textContent:'{}'}), querySelector:() => null};
  require('./app/static/js/daily-chrome.js');
  for (const [status,body,unknown] of [[503,{admission:{state:'unknown'}},true], [500,{error:'write_reply_lost'},true], [503,{admission:{state:'rejected'}},false]]) {
    global.fetch = async () => new Response(JSON.stringify(body), {status});
    await assert.rejects(DailyChrome.fetch('/save',{method:'POST'}), e => e.outcomeUnknown === unknown);
    await assert.rejects(DailyChrome.fetch('/read'), e => e.outcomeUnknown === false);
  }
  global.fetch = async () => new Response('synthetic proxy failure', {status:502});
  await assert.rejects(DailyChrome.fetch('/save',{method:'POST'}), e => e.outcomeUnknown === true);
  const controller = new AbortController();
  global.fetch = () => new Promise(() => {});
  const cancelled = adminRequest('/old', {signal:controller.signal});
  controller.abort();
  await assert.rejects(cancelled, e => e.code === 'aborted' && !e.outcomeUnknown);
  let releaseOld;
  global.fetch = url => url === '/old' ? new Promise(resolve => {releaseOld=resolve;}) : Promise.resolve(new Response('{"date":"new"}'));
  const latest = createLatestRequest();
  const old = latest.request('/old').catch(error => error.code);
  assert.deepEqual(await latest.request('/new'), {date:'new'});
  releaseOld(new Response('{"date":"old"}'));
  assert.equal(await old, 'aborted');
  assert.equal(kyivDate(new Date('2026-10-27T22:30:00Z')), '2026-10-28');
})().catch(error => {console.error(error); process.exitCode=1;});
"""
    result = subprocess.run([node, "-e", script], cwd=ROOT, encoding="utf-8", capture_output=True, timeout=10)
    assert result.returncode == 0, result.stdout + result.stderr
