from unittest.mock import patch

import pytest


@pytest.fixture
def client(monkeypatch):
    from app import web

    monkeypatch.setattr(web, "settings", web.settings.model_copy(update={"ADMIN_SECRET": "test_token"}))
    monkeypatch.setitem(web.quart_app.config, "TESTING", True)
    with patch.object(web.database, "is_database_connected", return_value=True):
        yield web.quart_app.test_client()


@pytest.mark.asyncio
async def test_security_headers_present(client):
    """Test that security headers are present in responses"""
    # Test public endpoint
    response = await client.get("/health")
    assert response.status_code == 200

    headers = response.headers
    assert headers.get("X-Content-Type-Options") == "nosniff"
    assert headers.get("X-Frame-Options") == "DENY"
    assert headers.get("Referrer-Policy") == "strict-origin-when-cross-origin"

    csp = headers.get("Content-Security-Policy")
    assert csp is not None
    assert "default-src 'self'" in csp
    assert "frame-ancestors 'none'" in csp
    # Nonce-based CSP instead of 'unsafe-inline'
    assert "'nonce-" in csp
    assert "'unsafe-inline'" not in csp
    assert "https://fonts.googleapis.com" in csp
    assert "font-src 'self' https://fonts.gstatic.com" in csp


@pytest.mark.asyncio
async def test_security_headers_on_error(client):
    """Test that security headers are present even on error responses"""
    response = await client.get("/non-existent-endpoint")
    assert response.status_code == 404

    headers = response.headers
    assert headers.get("X-Content-Type-Options") == "nosniff"
    assert headers.get("X-Frame-Options") == "DENY"


@pytest.mark.asyncio
async def test_security_headers_on_auth_failure(client):
    """Test that security headers are present on auth redirect"""
    response = await client.get("/")
    # Pages now redirect to /login instead of returning 401
    assert response.status_code == 302

    headers = response.headers
    assert headers.get("X-Content-Type-Options") == "nosniff"
    assert headers.get("X-Frame-Options") == "DENY"

    # Also verify API endpoints return 401 with headers
    response = await client.get("/api/overview")
    assert response.status_code == 401
    assert response.headers.get("X-Content-Type-Options") == "nosniff"
    assert response.headers.get("X-Frame-Options") == "DENY"
    """Test that security headers are present even on error pages"""
    # Force an error by accessing a non-existent route or causing an exception
    # Since we are testing headers, a 404 is a good candidate
    response = await client.get("/non-existent-route")

    assert response.status_code == 404
    assert response.headers.get("X-Content-Type-Options") == "nosniff"
    assert response.headers.get("X-Frame-Options") == "DENY"
