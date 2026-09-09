"""Execute the actual admin JavaScript with a minimal DOM boundary double."""

import shutil
import subprocess
from pathlib import Path

import pytest


def test_quota_control_loads_saves_and_reports_rejected_values():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js is needed for admin JavaScript tests")
    template = (Path(__file__).parents[1] / "app/templates/admin_daily.html").read_text(encoding="utf-8")
    source = template[
        template.index("  async function loadImageQuota()") : template.index("  function renderDayStatus(data)")
    ]
    script = (
        """
const assert = require('node:assert/strict');
const input = {value:'', disabled:true};
const button = {disabled:true};
const status = {textContent:''};
const document = {getElementById(id) {
  return id === 'croc-image-quota' ? input : id === 'croc-image-quota-save' ? button : status;
}};
const apiHeaders = {}, getHeaders = {};
let stored = 6, writes = 0, fail = false;
const fetch = async (url, options) => {
  assert.equal(url, '/api/admin/dailycroc/image-quota');
  if (options.method === 'POST') {
    writes++;
    if (fail) return {ok:false, json:async()=>({error:'unavailable'})};
    stored = JSON.parse(options.body).limit;
  }
  return {ok:true, json:async()=>({limit:stored})};
};
"""
        + source
        + """
(async () => {
  await loadImageQuota();
  assert.equal(Number(input.value), 6);
  assert.equal(input.disabled, false);
  input.value = '0';
  await saveImageQuota();
  assert.equal(stored, 0);
  assert.match(status.textContent, /сохран/);
  for (const invalid of ['', '-1', '2.5', '1001']) {
    input.value = invalid;
    await saveImageQuota();
  }
  assert.equal(writes, 1);
  fail = true; input.value = '12';
  await saveImageQuota();
  assert.equal(stored, 0);
  assert.match(status.textContent, /unavailable/);
  assert.equal(button.disabled, false);
})().catch(error => { console.error(error); process.exitCode = 1; });
"""
    )
    result = subprocess.run([node, "-"], input=script, encoding="utf-8", capture_output=True)
    assert result.returncode == 0, result.stderr


def test_regenerate_reads_card_image_model_not_unrelated_text_model():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js is needed for admin JavaScript tests")
    template = (Path(__file__).parents[1] / "app/templates/admin_daily.html").read_text(encoding="utf-8")
    source = template[
        template.index("  async function regenerate(date, diff)") : template.index(
            "  async function savePrompt(date, diff)"
        )
    ]
    script = (
        """
const assert = require('node:assert/strict');
const card = {value:'qwen/qwen-image-3'};
const document = {getElementById(id) {
  if (id === 'croc-model-2026-09-08-easy') return card;
  if (id === 'global-regen-model') return {value:'gemini-unrelated'};
  return {};
}};
const showConfirm = async () => true;
const apiHeaders = {};
let payload;
const fetch = async (url, options) => {payload = JSON.parse(options.body); return {ok:true,json:async()=>({success:true})}};
const showToast = () => {};
const loadPuzzles = async () => {};
"""
        + source
        + "\nregenerate('2026-09-08','easy').then(()=>assert.equal(payload.model, 'qwen/qwen-image-3'));"
    )
    result = subprocess.run([node, "-"], input=script, encoding="utf-8", capture_output=True)
    assert result.returncode == 0, result.stderr
