import pytest

pytestmark = pytest.mark.integration
"""Integration tests for Quart web dashboard security boundaries (TC-01).

Ensures unauthenticated requests to protected endpoints are rejected or redirected.
"""

import pytest

from app.web import quart_app

pytestmark = pytest.mark.integration


@pytest.fixture
def test_client():
    """Provides a Quart test client for the web application."""
    quart_app.config["TESTING"] = True
    return quart_app.test_client()


@pytest.mark.asyncio
async def test_unauthenticated_dashboard_redirects_to_login(test_client):
    """
    Risk Covered: Unauthorized access to the main dashboard.
    Level: Integration.
    """
    # Act
    response = await test_client.get("/")

    # Assert
    assert response.status_code == 302
    assert "/login" in response.headers.get("Location", "")


@pytest.mark.asyncio
async def test_unauthenticated_api_endpoints_return_401(test_client):
    """
    Risk Covered: Unauthorized access to system telemetry data via API.
    Level: Integration.
    """
    endpoints = [
        "/api/overview",
        "/api/keys",
        "/api/errors",
        "/api/cache",
        "/api/queue",
        "/api/database",
        "/api/circuit-breakers",
        "/api/memory",
    ]

    for endpoint in endpoints:
        # Act
        response = await test_client.get(endpoint)

        # Assert (API should return 401 Unauthorized, not 302 redirect for data)
        assert response.status_code == 401, f"Endpoint {endpoint} failed to block unauthenticated request"


@pytest.mark.asyncio
async def test_invalid_auth_token_rejected(test_client):
    """
    Risk Covered: Accepting spoofed or invalid JWT/Cookies.
    Level: Integration.
    """
    # Arrange
    headers = {"X-Auth-Token": "invalid_or_expired_token", "Cookie": "session=invalid_session_data"}

    # Act
    response = await test_client.get("/api/overview", headers=headers)

    # Assert
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_health_and_metrics_are_public(test_client):
    """
    Risk Covered: Health checks failing because they require auth.
    Level: Integration.
    """
    # Act
    health_response = await test_client.get("/health")
    metrics_response = await test_client.get("/metrics")

    # Assert
    assert health_response.status_code in (200, 503)  # Depends on DB mock state, but not 401
    assert metrics_response.status_code == 200
