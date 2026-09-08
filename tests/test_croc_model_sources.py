import shutil
import subprocess
from pathlib import Path
from unittest.mock import AsyncMock

import pytest


@pytest.mark.asyncio
async def test_image_catalog_identifies_source_not_model_publisher(monkeypatch):
    from app import web
    from app.providers import pollinations
    from app.repos import settings_repo

    monkeypatch.setattr(web.settings, "ADMIN_SECRET", "test-token")
    monkeypatch.setattr(settings_repo, "get_global_setting", AsyncMock(return_value=""))
    monkeypatch.setattr(
        pollinations,
        "fetch_models",
        AsyncMock(
            return_value=[
                {
                    "id": "openai/gpt-image-2",
                    "title": "GPT Image 2",
                    "publisher": "OpenAI",
                    "aliases": ["gptimage"],
                    "source": "untrusted-publisher-field",
                }
            ]
        ),
    )
    response = await web.quart_app.test_client().get(
        "/api/admin/dailycroc/models", headers={"X-Auth-Token": "test-token"}
    )
    assert response.status_code == 200
    models = (await response.get_json())["image_models"]
    assert models[0]["source"] == "pollinations"
    assert models[0]["publisher"] == "OpenAI"
    assert models[0]["aliases"] == ["gptimage"]
    assert models[1]["id"] == "fta-gpt-image-2"
    assert models[1]["source"] == "fta"


def test_grouped_options_preserve_aliases_unavailable_selection_and_flat_text_models():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js unavailable")
    template = (Path(__file__).parents[1] / "app/templates/admin_daily.html").read_text(encoding="utf-8")
    source = template[template.index("  function fillModels(") : template.index("  async function loadModels()")]
    script = (
        """
const assert = require('node:assert/strict');
class Element {
  constructor(tagName) {this.tagName=tagName;this.children=[];}
  append(child) {this.children.push(child);}
  add(child) {this.append(child);}
  replaceChildren() {this.children=[];}
}
const document = {createElement: name => new Element(name)};
class Option {
  constructor(text,value,defaultSelected,selected) {
    Object.assign(this,{tagName:'option',text,value,defaultSelected,selected});
  }
}
const options = parent => parent.children.flatMap(child => child.tagName==='optgroup' ? options(child) : [child]);
const models = [
  {id:'openai/gpt-image-2',title:'GPT Image 2',source:'pollinations',aliases:['gptimage']},
  {id:'qwen/qwen-image',title:'Qwen',source:'pollinations'},
  {id:'fta-gpt-image-2',title:'GPT Image 2',source:'fta',aliases:['vhr/gpt_image_2']},
];
"""
        + source
        + """
for (const [saved,expected] of [['gptimage','openai/gpt-image-2'],['vhr/gpt_image_2','fta-gpt-image-2']]) {
  const select = new Element('select');
  fillModels(select,models,saved);
  assert.deepEqual(select.children.map(child=>[child.tagName,child.label]), [['optgroup','Pollinations'],['optgroup','FTA']]);
  assert.deepEqual(select.children[0].children.map(child=>child.value),['openai/gpt-image-2','qwen/qwen-image']);
  assert.deepEqual(select.children[1].children.map(child=>child.value),['fta-gpt-image-2']);
  assert.deepEqual(options(select).filter(option=>option.selected).map(option=>option.value),[expected]);
}
const select = new Element('select');
fillModels(select,models,'retired/model');
assert.deepEqual(options(select).filter(option=>option.selected).map(option=>option.value),['retired/model']);
fillModels(select,[{id:'',title:'Auto'},{id:'gemini-2.5-flash',title:'Gemini'}],'gemini-2.5-flash');
assert.deepEqual(select.children.map(child=>child.tagName),['option','option']);
assert.deepEqual(options(select).filter(option=>option.selected).map(option=>option.value),['gemini-2.5-flash']);
"""
    )
    result = subprocess.run([node, "-"], input=script, encoding="utf-8", capture_output=True)
    assert result.returncode == 0, result.stderr
