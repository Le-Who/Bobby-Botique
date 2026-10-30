from unittest.mock import patch

import pytest


@pytest.mark.asyncio
async def test_admin_dailycroc_csp_allows_blob_image_previews():
    """Daily Crocodile admin previews use URL.createObjectURL(blob)."""
    from app.web import quart_app

    mock_settings = type(
        "Settings",
        (),
        {
            "ADMIN_ID": 123,
            "ADMIN_SECRET": "test_token",
            "TELEGRAM_BOT_TOKEN": "test_bot_token",
        },
    )()
    quart_app.config["TESTING"] = True

    with patch("app.web.settings", mock_settings):
        client = quart_app.test_client()
        headers = {"X-Auth-Token": "test_token"}

        response = await client.get("/admin_daily", headers=headers)
        assert response.status_code == 200
        csp = response.headers.get("Content-Security-Policy", "")
        assert "img-src" in csp
        assert "blob:" in csp

        croc_redirect = await client.get("/admin_dailycroc", headers=headers)
        assert croc_redirect.status_code == 301
        assert croc_redirect.headers["Location"].endswith("/admin_daily#croc")

        puzzle_redirect = await client.get("/admin_daily2048", headers=headers)
        assert puzzle_redirect.status_code == 301
        assert puzzle_redirect.headers["Location"].endswith("/admin_daily#2048")


@pytest.mark.browser
@pytest.mark.asyncio
async def test_extracted_croc_editor_keeps_csp_and_only_fetches_picture_through_proxy():
    from app.web import quart_app
    from tests.test_natal_web_report import _run_browser_regression

    client = quart_app.test_client()
    async with client.session_transaction() as session:
        session["authenticated"] = True
    response = await client.get("/admin_daily")
    csp = response.headers["Content-Security-Policy"]
    script_policy = next(part for part in csp.split(";") if part.strip().startswith("script-src "))
    assert "'unsafe-inline'" not in script_policy
    _run_browser_regression(
        {"html": (await response.get_data()).decode("utf-8"), "headers": dict(response.headers)},
        r"""
    page.setDefaultTimeout(2500);
    const file='https://untrusted.invalid/picture?x="<img>';
    const pictureRequests=[];
    await page.route('**/api/**',route=>{
      const url=new URL(route.request().url());
      if(url.pathname==='/api/admin/dailycroc/image'){
        pictureRequests.push(url.searchParams.get('file_id'));
        return route.fulfill({contentType:'image/png',body:Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+a57sAAAAASUVORK5CYII=','base64')});
      }
      const data=url.pathname==='/api/admin/dailycroc'?{puzzles:[{date:'2026-10-28',difficulty:'easy',target_word:'кот',topic:'Животные',image_prompt:'cat',image_file_id:file,image_model:'flux',content_revision:1},{date:'2026-10-29',difficulty:'easy',target_word:''}]}:url.pathname==='/api/admin/dailycroc/models'?{settings_revision:0,text_models:[],image_models:[],text_processes:[],default_image_model:'flux'}:{};
      return route.fulfill({json:data});
    });
    await page.goto('http://natal.test/admin_daily#croc');
    await page.waitForFunction(()=>document.querySelector('#croc-card-2026-10-28-easy img')?.src.startsWith('blob:'));
    assert.deepEqual(pictureRequests,[file]);
    assert.equal(await page.evaluate(()=>typeof window.CrocModule.init),'function');
    assert.equal(await page.locator('script[src="/static/js/daily-croc-editor.js"]').count(),1);
    assert.equal(await page.locator('#croc-card-2026-10-28-easy [src^="https:"]').count(),0);
    assert.equal(await page.locator('#croc-card-2026-10-28-easy .badge-missing').isVisible(),false);
    assert.equal(await page.locator('#croc-card-2026-10-29-easy .image-container').isVisible(),false);
""",
    )
