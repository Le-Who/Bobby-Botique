import shutil
import subprocess
from pathlib import Path

import pytest


def test_process_selector_sends_its_model_and_process_not_global_value():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js unavailable")
    template = (Path(__file__).parents[1] / "app/templates/admin_daily.html").read_text(encoding="utf-8")
    source = template[template.index("  async function saveTextModel") : template.index("  async function loadStats")]
    script = (
        """
const assert = require('node:assert/strict');
let savedTextModel = 'gemini-2.5-flash';
let savedProcessModels = {judge: 'gemini-2.5-flash'};
const select = {value:'gemini-2.5-pro',dataset:{process:'judge'},disabled:false};
const document = {getElementById:()=>({value:'gemini-2.5-flash',dataset:{}})};
const apiHeaders = {};
const showToast = () => {};
let payload;
const fetch = async (url, options) => {
  payload = JSON.parse(options.body);
  return {ok:true,json:async()=>({success:true,model:payload.model,process:payload.process})};
};
"""
        + source
        + """
saveTextModel({target:select}).then(()=>{
  assert.equal(payload.process,'judge');
  assert.equal(payload.model,'gemini-2.5-pro');
  assert.equal(savedTextModel,'gemini-2.5-flash');
  assert.equal(savedProcessModels.judge,'gemini-2.5-pro');
  assert.equal(select.disabled,false);
});
"""
    )
    result = subprocess.run([node, "-"], input=script, encoding="utf-8", capture_output=True)
    assert result.returncode == 0, result.stderr
