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
