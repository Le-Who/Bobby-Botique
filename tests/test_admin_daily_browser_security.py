"""Stored user fields must remain text in the administrator's browser."""

import pytest

from tests.test_natal_web_report import _run_browser_regression


@pytest.mark.asyncio
@pytest.mark.browser
async def test_offer_history_renders_untrusted_display_name_as_text():
    from app.web import quart_app

    client = quart_app.test_client()
    async with client.session_transaction() as session:
        session["authenticated"] = True
    response = await client.get("/admin_daily")
    assert response.status_code == 200
    name = '<img id="untrusted-display-name" src="/health">Synthetic Name'

    _run_browser_regression(
        {
            "html": (await response.get_data()).decode("utf-8"),
            "headers": dict(response.headers),
            "display_name": name,
        },
        r"""
    await page.route('**/api/**', route => {
      const body = route.request().url().includes('/offer-history')
        ? { total: 1, rows: [{
            user_id: 71001, display_name: payload.display_name,
            channel: 'horoscope', channel_emoji: '⭐', is_subscribed: false
          }] }
        : {};
      return route.fulfill({ body: JSON.stringify(body), contentType: 'application/json' });
    });
    await page.goto('http://natal.test/admin_daily');
    await page.locator('#btn-load-offers').click();
    await page.waitForFunction(() => document.querySelector('#offer-tbody .cell-user-name'));
    assert.equal(await page.locator('#offer-tbody .cell-user-name').textContent(), payload.display_name);
    assert.equal(await page.locator('#untrusted-display-name').count(), 0);
""",
    )


@pytest.mark.asyncio
@pytest.mark.browser
async def test_croc_word_topic_prompt_and_model_render_as_literal_text():
    from app.web import quart_app

    client = quart_app.test_client()
    async with client.session_transaction() as session:
        session["authenticated"] = True
    response = await client.get("/admin_daily")
    _run_browser_regression(
        {"html": (await response.get_data()).decode("utf-8"), "headers": dict(response.headers)},
        r"""
    page.setDefaultTimeout(2500);
    const attack='<img id="croc-injected" src=x data-owned="true">';
    const prompt='</textarea><input id="prompt-injected" autofocus> & literal';
    const model='flux"><img id="model-injected" src=x>';
    await page.route('**/api/**',route=>{
      const path=new URL(route.request().url()).pathname;
      const body=path==='/api/admin/dailycroc'?{puzzles:[{date:'2026-10-28',difficulty:'easy',target_word:attack,topic:attack,image_prompt:prompt,image_model:model,image_file_id:'',content_revision:1,played:false}]}:path==='/api/admin/dailycroc/models'?{settings_revision:0,text_models:[],text_processes:[],image_models:[],default_image_model:model}:{};
      return route.fulfill({json:body});
    });
    await page.goto('http://natal.test/admin_daily#croc');
    await page.waitForSelector('#croc-card-2026-10-28-easy');
    assert.equal(await page.locator('#croc-card-2026-10-28-easy .puzzle-word').textContent(),attack);
    assert.equal(await page.locator('#croc-card-2026-10-28-easy .puzzle-topic').textContent(),attack);
    assert.equal(await page.locator('#croc-prompt-input-2026-10-28-easy').inputValue(),prompt);
    assert.equal(await page.locator('#croc-prompt-text-2026-10-28-easy').textContent(),prompt);
    assert.equal(await page.locator('#croc-model-2026-10-28-easy').inputValue(),model);
    assert.equal(await page.locator('#croc-injected,#prompt-injected,#model-injected').count(),0);
""",
    )
