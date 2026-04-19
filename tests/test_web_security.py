import importlib
import re
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Define keys but do not override them yet
_mock_keys = [
    "asyncpg",
    "asyncpg.pool",
    "google.genai",
    "google.genai.errors",
    "redis",
    "redis.exceptions",
    "telegram",
    "telegram.ext",
    "telegram.error",
    "hypercorn.config",
    "hypercorn.asyncio",
    "psutil",
    "app.database",
]
_original_modules = {}


def setup_module(module):
    global _original_modules
    for k in _mock_keys:
        if k in sys.modules:
            _original_modules[k] = sys.modules[k]
        sys.modules[k] = MagicMock()

    # Specialized mocks
    mock_psutil = MagicMock()
    mock_psutil.cpu_percent.return_value = 10.0
    mock_psutil.virtual_memory.return_value.percent = 20.0
    mock_psutil.virtual_memory.return_value.used = 512 * 1024 * 1024
    mock_psutil.virtual_memory.return_value.total = 1024 * 1024 * 1024
    mock_psutil.disk_usage.return_value.percent = 30.0
    sys.modules["psutil"] = mock_psutil

    mock_db = MagicMock()
    mock_db.is_database_connected.return_value = True
    mock_db.get_gemini_key_usage_stats = AsyncMock(return_value=[])
    mock_db.get_active_key_info = AsyncMock(return_value={})
    mock_db.get_supabase_metrics = AsyncMock(return_value={"status": "connected", "pool_size": 5})
    sys.modules["app.database"] = mock_db


def teardown_module(module):
    for k in _mock_keys:
        if k in sys.modules:
            del sys.modules[k]
    sys.modules.update(_original_modules)


# Mock app.config settings
@pytest.fixture
def mock_settings():
    with patch("app.config.settings") as mock:
        mock.TELEGRAM_BOT_TOKEN = "bot_token"
        mock.ADMIN_SECRET = "test_token"
        mock.ADMIN_ID = 123
        mock.DAILY_LIMITS = {}
        mock.PORT = 5000
        mock.AVAILABLE_MODELS = ["gemini-2.5-flash"]
        yield mock


@pytest.fixture
def client(mock_settings):
    # Force reload of app.web if it's already imported
    if "app.web" in sys.modules:
        importlib.reload(sys.modules["app.web"])

    # Explicitly patch settings in app.web to be sure
    with patch("app.web.settings", mock_settings):
        from app.web import quart_app

        quart_app.config["TESTING"] = True
        yield quart_app.test_client()


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
    assert response.status_code != 302


@pytest.mark.asyncio
async def test_invalid_header_token_redirects(client):
    """Test that invalid token falls through to redirect."""
    headers = {"X-Auth-Token": "wrong_token"}  # noqa: F841
    response = await client.get("/")
    assert response.status_code == 302


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
async def test_generate_csp_nonce_integration(client):
    """Test that generate_csp_nonce runs during a request and injects a unique nonce into CSP."""
    response1 = await client.get("/health")
    csp1 = response1.headers.get("Content-Security-Policy", "")
    m1 = re.search(r"'nonce-([A-Za-z0-9_-]+)'", csp1)
    assert m1 is not None, "Nonce not found in CSP header"
    nonce1 = m1.group(1)
    assert len(nonce1) == 22

    response2 = await client.get("/health")
    csp2 = response2.headers.get("Content-Security-Policy", "")
    m2 = re.search(r"'nonce-([A-Za-z0-9_-]+)'", csp2)
    assert m2 is not None, "Nonce not found in CSP header"
    nonce2 = m2.group(1)

    assert nonce1 != nonce2, "Nonce should be unique per request"
