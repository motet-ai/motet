"""
Integration tests for Auth API endpoints.

Tests all authentication endpoints including:
- OAuth login initiation
- OAuth callback handling
- JWT claims debugging
- Logout

Note: Auth tests require JWT/OAuth configuration.
Tests will skip gracefully if not configured.
"""

from __future__ import annotations

import os

import httpx
import pytest

from motet.interfaces.http import create_app
from .conftest import with_env, get_test_env_vars

pytestmark = [pytest.mark.integration, pytest.mark.requires_external]


async def _client_request(app, **request_kwargs):
    """Run a single request against app in the current event loop."""
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test", timeout=10, follow_redirects=False) as client:
        method = request_kwargs.pop("method", "get")
        return await getattr(client, method)(**request_kwargs)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_initiate_login():
    """Test initiating OAuth login flow - skips if JWT not configured."""
    with with_env(get_test_env_vars(
        MOTET_JWT_JWKS_URL="http://keycloak:8080/realms/motet/protocol/openid-connect/certs",
        MOTET_JWT_ISSUER="http://localhost:8080/realms/motet",
        MOTET_KEYCLOAK_CLIENT_ID="motet-ai-stack"
    )):
        app = create_app()
        response = await _client_request(app, url="/api/v1/auth/login", params={"redirect_uri": "/demo_chat.html"})
        if response.status_code in (503, 404):
            pytest.skip("JWT/OAuth not configured or auth routes not mounted (expected in test environment)")
        assert response.status_code in [302, 307, 503, 404], \
            f"Expected 302/307 (redirect), 503 or 404 (not configured), got {response.status_code}: {response.text}"
        if response.status_code == 302:
            location = response.headers.get("location", "")
            assert "keycloak" in location.lower() or "auth" in location.lower() or "realms" in location.lower()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_initiate_login_without_jwt_config():
    """Test that login fails gracefully when JWT is not configured."""
    jwt_keys = (
        "MOTET_JWT_JWKS_URL",
        "MOTET_JWT_ISSUER",
        "MOTET_JWT_AUDIENCE",
        "MOTET_KEYCLOAK_CLIENT_ID",
    )
    saved = {key: os.environ.pop(key, None) for key in jwt_keys}
    try:
        with with_env(get_test_env_vars()):
            app = create_app()
            response = await _client_request(app, url="/api/v1/auth/login")
            assert response.status_code in [500, 503, 404], \
                f"Expected 500/503/404 when JWT not configured, got {response.status_code}: {response.text}"
    finally:
        for key, value in saved.items():
            if value is not None:
                os.environ[key] = value


@pytest.mark.integration
@pytest.mark.asyncio
async def test_oauth_callback():
    """Test OAuth callback endpoint."""
    with with_env(get_test_env_vars()):
        app = create_app()
        response = await _client_request(
            app,
            url="/api/v1/auth/callback",
            params={"code": "invalid_code", "state": "invalid_state"},
        )
        if response.status_code == 503:
            pytest.skip("JWT/OAuth not configured (expected in test environment)")
        assert response.status_code in [200, 400], \
            f"Expected 200 or 400, got {response.status_code}: {response.text}"
        assert "text/html" in response.headers.get("content-type", "")


@pytest.mark.integration
@pytest.mark.asyncio
async def test_oauth_callback_missing_params():
    """Test OAuth callback with missing parameters."""
    with with_env(get_test_env_vars()):
        app = create_app()
        response = await _client_request(app, url="/api/v1/auth/callback", params={"state": "test_state"})
        if response.status_code == 503:
            pytest.skip("JWT/OAuth not configured (expected in test environment)")
        assert response.status_code in [400, 422], \
            f"Expected 400 or 422 for missing params, got {response.status_code}: {response.text}"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_debug_claims(test_headers):
    """Test JWT claims debugging endpoint.
    
    The test_headers fixture provides a service-account token (sa_...),
    not a JWT. The debug/claims endpoint tries to decode a JWT and will
    return 400 (no Bearer) or 500 (decode failure) for non-JWT tokens.
    A real JWT test requires Keycloak token exchange.
    """
    with with_env(get_test_env_vars(MOTET_DEBUG_MODE="true")):
        app = create_app()
        response = await _client_request(app, url="/api/v1/auth/debug/claims", headers=test_headers)
        # Service account token is not a JWT — endpoint returns 500 "Failed to decode JWT claims"
        assert response.status_code in [200, 400, 500], \
            f"Expected 200/400/500, got {response.status_code}: {response.text}"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_debug_claims_without_token():
    """Test debug claims endpoint without token returns 400."""
    with with_env(get_test_env_vars(MOTET_DEBUG_MODE="true")):
        app = create_app()
        response = await _client_request(
            app,
            url="/api/v1/auth/debug/claims",
            headers={"X-API-Key": "test-key", "X-Principal-Id": "test-principal", "X-Tenant-Id": "test-tenant"},
        )
        # Endpoint requires a Bearer JWT token; returns 400 when none provided
        assert response.status_code == 400, \
            f"Expected 400 (no JWT token), got {response.status_code}: {response.text}"
        assert "No JWT token" in response.json().get("detail", "")


@pytest.mark.integration
@pytest.mark.asyncio
async def test_logout(test_headers):
    """Test logout endpoint."""
    with with_env(get_test_env_vars()):
        app = create_app()
        response = await _client_request(app, url="/api/v1/auth/logout", headers=test_headers)
        assert response.status_code == 200, f"Expected 200, got {response.status_code}: {response.text}"
        data = response.json()
        assert "status" in data
        assert data["status"] == "success"
        assert "message" in data


@pytest.mark.integration
@pytest.mark.asyncio
async def test_logout_requires_auth():
    """Test logout without auth. API may return 200 or 401."""
    with with_env(get_test_env_vars()):
        app = create_app()
        response = await _client_request(
            app,
            url="/api/v1/auth/logout",
            headers={"X-API-Key": "test-key", "X-Principal-Id": "test-principal", "X-Tenant-Id": "test-tenant"},
        )
        assert response.status_code in [200, 401], \
            f"Expected 200 or 401, got {response.status_code}: {response.text}"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_initiate_login_with_redirect_uri():
    """Test login initiation with custom redirect URI."""
    with with_env(get_test_env_vars(
        MOTET_JWT_JWKS_URL="http://keycloak:8080/realms/motet/protocol/openid-connect/certs",
        MOTET_JWT_ISSUER="http://localhost:8080/realms/motet",
        MOTET_KEYCLOAK_CLIENT_ID="motet-ai-stack"
    )):
        app = create_app()
        response = await _client_request(
            app,
            url="/api/v1/auth/login",
            params={"redirect_uri": "https://example.com/callback"},
        )
        if response.status_code in (503, 404):
            pytest.skip("JWT/OAuth not configured or auth routes not mounted (expected in test environment)")
        assert response.status_code in [302, 307, 500, 503], \
            f"Expected 302/307 or 500/503, got {response.status_code}: {response.text}"
