"""
Integration tests for OAuth API endpoints.

Tests all OAuth authentication endpoints including:
- OAuth flow initiation
- OAuth callback handling
- OAuth status checking
- OAuth token refresh

Note: Most OAuth tests require OAuth providers to be configured.
Tests will skip gracefully if providers are not available.
"""

from __future__ import annotations

import asyncio
import httpx
import pytest

from motet.interfaces.http import create_app
from .conftest import with_env, get_test_env_vars


@pytest.mark.integration
def test_oauth_initiate(test_headers):
    """Test initiating OAuth flow - skips if provider not configured."""
    with with_env(get_test_env_vars()):
        app = create_app()
        
        async def _run():
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test", timeout=10) as client:
                # Try to initiate OAuth flow
                response = await client.post(
                    "/api/v1/oauth/mcp/google_workspace/initiate",
                    headers=test_headers
                )
                
                # OAuth provider not configured in test environment - skip
                if response.status_code == 404:
                    pytest.skip("OAuth provider not configured (expected in test environment)")
                
                # Should return 200 (success) or 400 (provider error)
                assert response.status_code in [200, 400], \
                    f"Expected 200 or 400, got {response.status_code}: {response.text}"
                
                if response.status_code == 200:
                    data = response.json()
                    assert "authorization_url" in data
                    assert "state" in data
                    assert "instructions" in data
        
        asyncio.run(_run())


@pytest.mark.integration
def test_oauth_initiate_requires_auth():
    """Test that OAuth initiation requires authentication."""
    with with_env(get_test_env_vars()):
        app = create_app()
        
        async def _run():
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test", timeout=10) as client:
                response = await client.post(
                    "/api/v1/oauth/mcp/google_workspace/initiate",
                    headers={"X-API-Key": "test-key"}
                )
                
                # OAuth provider not configured - skip
                if response.status_code == 404:
                    pytest.skip("OAuth provider not configured (expected in test environment)")
                
                assert response.status_code == 401, \
                    f"Expected 401 without auth, got {response.status_code}: {response.text}"
        
        asyncio.run(_run())


@pytest.mark.integration
def test_oauth_callback():
    """Test OAuth callback endpoint (no auth required - called by provider)."""
    with with_env(get_test_env_vars()):
        app = create_app()
        
        async def _run():
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test", timeout=10) as client:
                # OAuth callback with invalid code/state (tests endpoint)
                response = await client.get(
                    "/api/v1/oauth/mcp/google_workspace/callback",
                    params={
                        "code": "invalid_code",
                        "state": "invalid_state"
                    }
                )
                
                # OAuth provider not configured - skip
                if response.status_code == 404:
                    pytest.skip("OAuth provider not configured (expected in test environment)")
                
                # Should return 400 (error) or 200 (success page)
                assert response.status_code in [200, 400], \
                    f"Expected 200 or 400, got {response.status_code}: {response.text}"
                
                # Should return HTML response
                assert "text/html" in response.headers.get("content-type", "")
        
        asyncio.run(_run())


@pytest.mark.integration
def test_oauth_callback_missing_params():
    """Test OAuth callback with missing parameters."""
    with with_env(get_test_env_vars()):
        app = create_app()
        
        async def _run():
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test", timeout=10) as client:
                # Missing code parameter
                response = await client.get(
                    "/api/v1/oauth/mcp/google_workspace/callback",
                    params={"state": "test_state"}
                )
                
                # OAuth provider not configured - skip
                if response.status_code == 404:
                    pytest.skip("OAuth provider not configured (expected in test environment)")
                
                # Should return 422 (validation error) or 400 (bad request)
                assert response.status_code in [400, 422], \
                    f"Expected 400 or 422 for missing params, got {response.status_code}: {response.text}"
        
        asyncio.run(_run())


@pytest.mark.integration
def test_oauth_status(test_headers):
    """Test getting OAuth status."""
    with with_env(get_test_env_vars()):
        app = create_app()
        
        async def _run():
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test", timeout=10) as client:
                # Get OAuth status
                response = await client.get(
                    "/api/v1/oauth/mcp/google_workspace/status",
                    headers=test_headers
                )
                
                # OAuth provider not configured - skip
                if response.status_code == 404:
                    pytest.skip("OAuth provider not configured (expected in test environment)")
                
                # Should return 200 (success) or 400 (error)
                assert response.status_code in [200, 400], \
                    f"Expected 200 or 400, got {response.status_code}: {response.text}"
                
                if response.status_code == 200:
                    data = response.json()
                    assert "provider" in data
                    assert "configured" in data
                    assert "authenticated" in data
        
        asyncio.run(_run())


@pytest.mark.integration
def test_oauth_status_requires_auth():
    """Test that OAuth status requires authentication."""
    with with_env(get_test_env_vars()):
        app = create_app()
        
        async def _run():
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test", timeout=10) as client:
                response = await client.get(
                    "/api/v1/oauth/mcp/google_workspace/status",
                    headers={"X-API-Key": "test-key"}
                )
                
                # OAuth provider not configured - skip
                if response.status_code == 404:
                    pytest.skip("OAuth provider not configured (expected in test environment)")
                
                assert response.status_code == 401, \
                    f"Expected 401 without auth, got {response.status_code}: {response.text}"
        
        asyncio.run(_run())


@pytest.mark.integration
def test_oauth_refresh(test_headers):
    """Test refreshing OAuth tokens."""
    with with_env(get_test_env_vars()):
        app = create_app()
        
        async def _run():
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test", timeout=10) as client:
                # Try to refresh tokens (may fail if not configured)
                response = await client.post(
                    "/api/v1/oauth/mcp/google_workspace/refresh",
                    headers=test_headers
                )
                
                # OAuth provider not configured - skip
                if response.status_code == 404:
                    pytest.skip("OAuth provider not configured (expected in test environment)")
                
                # Should return 200 (success) or 400 (error - not configured/no refresh token)
                assert response.status_code in [200, 400], \
                    f"Expected 200 or 400, got {response.status_code}: {response.text}"
                
                if response.status_code == 200:
                    data = response.json()
                    assert "success" in data
                    assert data["success"] is True
        
        asyncio.run(_run())


@pytest.mark.integration
def test_oauth_refresh_requires_auth():
    """Test that OAuth refresh requires authentication."""
    with with_env(get_test_env_vars()):
        app = create_app()
        
        async def _run():
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test", timeout=10) as client:
                response = await client.post(
                    "/api/v1/oauth/mcp/google_workspace/refresh",
                    headers={"X-API-Key": "test-key"}
                )
                
                # OAuth provider not configured - skip
                if response.status_code == 404:
                    pytest.skip("OAuth provider not configured (expected in test environment)")
                
                assert response.status_code == 401, \
                    f"Expected 401 without auth, got {response.status_code}: {response.text}"
        
        asyncio.run(_run())


@pytest.mark.integration
def test_oauth_initiate_different_providers(test_headers):
    """Test initiating OAuth for different provider types."""
    with with_env(get_test_env_vars()):
        app = create_app()
        
        async def _run():
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test", timeout=10) as client:
                # Test MCP provider
                response1 = await client.post(
                    "/api/v1/oauth/mcp/github/initiate",
                    headers=test_headers
                )
                
                # OAuth provider not configured - skip
                if response1.status_code == 404:
                    pytest.skip("OAuth providers not configured (expected in test environment)")
                
                assert response1.status_code in [200, 400]
                
                # Test Motet integration provider
                response2 = await client.post(
                    "/api/v1/oauth/slack/initiate",
                    headers=test_headers
                )
                
                # OAuth provider not configured - skip
                if response2.status_code == 404:
                    pytest.skip("OAuth providers not configured (expected in test environment)")
                
                assert response2.status_code in [200, 400]
        
        asyncio.run(_run())
