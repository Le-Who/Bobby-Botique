"""Execute the actual admin JavaScript with a minimal DOM boundary double."""

import shutil
import subprocess
from pathlib import Path

import pytest


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
