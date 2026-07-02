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
