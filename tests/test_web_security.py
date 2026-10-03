import re
from collections import defaultdict, deque
from unittest.mock import AsyncMock, patch

import pytest


@pytest.fixture
def client(monkeypatch):
    from app import web, web_miniapp

    monkeypatch.setattr(web, "settings", web.settings.model_copy(update={"ADMIN_SECRET": "test_token"}))
    monkeypatch.setitem(web.quart_app.config, "TESTING", True)
    for limiter in (web._login_limiter, web._api_limiter, web_miniapp._reader_limiter):
        monkeypatch.setattr(limiter, "_requests", defaultdict(deque))
        monkeypatch.setattr(limiter, "_call_count", 0)
    with patch.object(web.database, "is_database_connected", return_value=True):
        yield web.quart_app.test_client()


@pytest.mark.asyncio
async def test_unauthorized_page_redirects_to_login(client):
    """Test that unauthorized page access redirects to /login."""
    response = await client.get("/")
    assert response.status_code == 302
    assert "/login" in response.headers.get("Location", "")


@pytest.mark.asyncio
async def test_unauthorized_api_returns_401(client):
    """Test that unauthorized API access returns 401 JSON."""
    endpoints = ["/api/overview", "/api/keys", "/api/cache", "/api/queue"]
    for endpoint in endpoints:
        response = await client.get(endpoint)
        assert response.status_code == 401


@pytest.mark.asyncio
async def test_login_page_accessible(client):
    """Test that login page is publicly accessible."""
    response = await client.get("/login")
    assert response.status_code == 200
    data = await response.get_data()
    assert b"password" in data.lower()


@pytest.mark.asyncio
async def test_login_with_correct_password(client):
    """Test that logging in with correct password creates session."""
    # First GET to obtain CSRF token
    get_response = await client.get("/login")
    data = (await get_response.get_data()).decode()
    m = re.search(r'name="csrf_token" value="([a-f0-9]+)"', data)
    assert m, "CSRF token not found in login page"
    csrf_token = m.group(1)

    response = await client.post(
        "/login",
        form={"password": "test_token", "csrf_token": csrf_token},
        follow_redirects=False,
    )
    assert response.status_code == 302
    assert "/" in response.headers.get("Location", "")


@pytest.mark.asyncio
async def test_login_with_wrong_password(client):
    """Test that wrong password shows error."""
    # First GET to obtain CSRF token
    get_response = await client.get("/login")
    data = (await get_response.get_data()).decode()
    m = re.search(r'name="csrf_token" value="([a-f0-9]+)"', data)
    csrf_token = m.group(1)

    response = await client.post(
        "/login",
        form={"password": "wrong_password", "csrf_token": csrf_token},
    )
    assert response.status_code == 200
    data = await response.get_data()
    assert b"Invalid password" in data


@pytest.mark.asyncio
async def test_header_auth_still_works(client):
    """Test that X-Auth-Token header auth still works (backward compat)."""
    headers = {"X-Auth-Token": "test_token"}
    response = await client.get("/", headers=headers)
    # Should not redirect — auth passed
    assert response.status_code == 200


@pytest.mark.asyncio
async def test_invalid_header_token_redirects(client):
    """Test that invalid token falls through to redirect."""
    headers = {"X-Auth-Token": "wrong_token"}
    with patch("app.web.render_template", new=AsyncMock(return_value="dashboard")) as render:
        response = await client.get("/", headers=headers)
        assert response.status_code == 302
        assert "/login" in response.headers["Location"]
        render.assert_not_awaited()
    response = await client.get("/api/overview", headers=headers)
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_public_health_endpoint(client):
    """Test that /health is public."""
    response = await client.get("/health")
    assert response.status_code != 401
    assert response.status_code != 302


@pytest.mark.asyncio
async def test_security_headers_present(client):
    """Verify that security headers are present."""
    response = await client.get("/health")

    assert response.headers.get("X-Content-Type-Options") == "nosniff"
    assert response.headers.get("X-Frame-Options") == "DENY"
    assert response.headers.get("Referrer-Policy") == "strict-origin-when-cross-origin"

    csp = response.headers.get("Content-Security-Policy")
    assert "default-src 'self'" in csp
    # Nonce-based CSP instead of 'unsafe-inline'
    assert "'nonce-" in csp
    assert "'unsafe-inline'" not in csp
    assert "https://fonts.googleapis.com" in csp


@pytest.mark.asyncio
async def test_logout_clears_session(client):
    """Test that logout clears session and redirects to login."""
    # First GET to obtain CSRF token
    get_response = await client.get("/login")
    data = (await get_response.get_data()).decode()
    m = re.search(r'name="csrf_token" value="([a-f0-9]+)"', data)
    csrf_token = m.group(1)

    # Login
    await client.post("/login", form={"password": "test_token", "csrf_token": csrf_token})
    # Then logout
    response = await client.get("/logout", follow_redirects=False)
    assert response.status_code == 302
    assert "/login" in response.headers.get("Location", "")


@pytest.mark.asyncio
async def test_generate_csp_nonce_format():
    """Test that generated CSP nonce has correct format and length directly."""
    from quart import g

    from app.web import generate_csp_nonce, quart_app

    async with quart_app.test_request_context("/"):
        await generate_csp_nonce()
        nonce = g.csp_nonce

        # A 16-byte random sequence encoded with base64url should be exactly 22 chars long
        assert len(nonce) == 22
        # Check that it's URL safe (alphanumeric, -, _)
        assert re.match(r"^[A-Za-z0-9_-]+$", nonce) is not None


@pytest.mark.asyncio
async def test_generate_csp_nonce_uniqueness():
    """Test that generated CSP nonces are unique per request."""
    from quart import g

    from app.web import generate_csp_nonce, quart_app

    async with quart_app.test_request_context("/"):
        await generate_csp_nonce()
        nonce1 = g.csp_nonce

    async with quart_app.test_request_context("/"):
        await generate_csp_nonce()
        nonce2 = g.csp_nonce

    assert nonce1 != nonce2, "Consecutive requests received the same CSP nonce"


@pytest.mark.asyncio
async def test_error_leakage_prevented(client):
    """Verify that exceptions do NOT leak internal details."""
    secret_message = "SecretDatabaseConnectionString"
    headers = {"X-Auth-Token": "test_token"}

    # Patch render_template in app.web
    with patch("app.web.render_template", side_effect=Exception(secret_message)):
        response = await client.get("/", headers=headers)
        assert response.status_code == 500

        response_text = (await response.get_data()).decode()
        assert secret_message not in response_text


@pytest.mark.asyncio
async def test_reader_rate_limiting(client):
    """Verify that the public reader page has rate limiting."""
    from app.web_miniapp import _reader_limiter

    # Lower the limit for testing
    original_max = _reader_limiter.max_requests
    _reader_limiter.max_requests = 2
    try:
        # Request 1 - OK
        response = await client.get("/webapp/reader?id=00000000-0000-0000-0000-000000000000")
        assert response.status_code == 200

        # Request 2 - OK
        response = await client.get("/webapp/reader?id=00000000-0000-0000-0000-000000000000")
        assert response.status_code == 200

        # Request 3 - Rate Limited
        response = await client.get("/webapp/reader?id=00000000-0000-0000-0000-000000000000")
        assert response.status_code == 429
        data = await response.get_data()
        assert b"too many requests" in data.lower() or "пожалуйста, подождите".encode() in data.lower()
    finally:
        _reader_limiter.max_requests = original_max
