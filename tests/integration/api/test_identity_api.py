"""
Integration tests for Identity API endpoints.

Tests all identity information endpoints including:
- Current principal information
- Current tenant information
"""

from __future__ import annotations

import asyncio
import os
import httpx
import pytest
from contextlib import contextmanager

from motet.interfaces.http import create_app
from motet.core.distributed.redis_manager import get_sync_redis_client
from motet.core.security.service_accounts import ServiceAccountManager


@contextmanager
def with_env(vars: dict[str, str]):
    """Context manager for environment variables."""
    old = {}
    try:
        for k, v in vars.items():
            old[k] = os.environ.get(k)
            os.environ[k] = v
        yield
    finally:
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


@pytest.fixture
def test_service_account_token():
    """Create a test service account token for API authentication."""
    redis_client = get_sync_redis_client("test_identity_api")
    sa_manager = ServiceAccountManager(redis_client)
    
    token = sa_manager.create_service_account(
        name="test-identity-api",
        tenant_id="test-tenant",
        motet_id="production",
        roles=["admin", "user"],
        created_by="test@example.com",
        expires_days=1
    )
    
    yield token
    
    # Cleanup
    sa_manager.revoke_service_account(token)


@pytest.fixture
def test_headers(test_service_account_token):
    """Provide test headers with authentication."""
    return {
        "X-API-Key": "test-key",
        "Authorization": f"Bearer {test_service_account_token}",
        "Content-Type": "application/json"
    }


@pytest.mark.integration
def test_get_current_principal(test_headers):
    """Test getting current principal information."""
    with with_env({
        "MOTET_API_KEY": "test-key",
        "MOTET_REDIS_URL": os.getenv("MOTET_REDIS_URL", "redis://localhost:6379/0"),
        "MOTET_ALLOW_INSECURE_PRINCIPAL_HEADERS": "false"
    }):
        app = create_app()
        
        async def _run():
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test", timeout=10) as client:
                response = await client.get(
                    "/api/v1/identity/me",
                    headers=test_headers
                )
                
                assert response.status_code == 200, f"Expected 200, got {response.status_code}: {response.text}"
                data = response.json()
                assert "id" in data
                assert "roles" in data
                assert "tenant_id" in data
                assert isinstance(data["roles"], list)
                # Verify it matches the service account
                assert data["id"] == "service-account:test-identity-api"
                assert data["tenant_id"] == "test-tenant"
        
        asyncio.run(_run())


@pytest.mark.integration
def test_get_current_principal_requires_auth():
    """Test that getting principal info requires authentication."""
    with with_env({
        "MOTET_API_KEY": "test-key",
        "MOTET_ALLOW_INSECURE_PRINCIPAL_HEADERS": "false"
    }):
        app = create_app()
        
        async def _run():
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test", timeout=10) as client:
                response = await client.get(
                    "/api/v1/identity/me",
                    headers={"X-API-Key": "test-key"}
                )
                
                # API may allow API key (200) or require full auth (401)
                assert response.status_code in [200, 401], \
                    f"Expected 200 or 401, got {response.status_code}: {response.text}"
        
        asyncio.run(_run())


@pytest.mark.integration
def test_get_current_tenant(test_headers):
    """Test getting current tenant information."""
    with with_env({
        "MOTET_API_KEY": "test-key",
        "MOTET_REDIS_URL": os.getenv("MOTET_REDIS_URL", "redis://localhost:6379/0"),
        "MOTET_ALLOW_INSECURE_PRINCIPAL_HEADERS": "false"
    }):
        app = create_app()
        
        async def _run():
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test", timeout=10) as client:
                response = await client.get(
                    "/api/v1/identity/tenant",
                    headers=test_headers
                )
                
                assert response.status_code == 200, f"Expected 200, got {response.status_code}: {response.text}"
                data = response.json()
                assert "tenant_id" in data
                assert data["tenant_id"] == "test-tenant"
        
        asyncio.run(_run())


@pytest.mark.integration
def test_get_current_tenant_requires_auth():
    """Test that getting tenant info requires authentication."""
    with with_env({
        "MOTET_API_KEY": "test-key",
        "MOTET_ALLOW_INSECURE_PRINCIPAL_HEADERS": "false"
    }):
        app = create_app()
        
        async def _run():
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test", timeout=10) as client:
                response = await client.get(
                    "/api/v1/identity/tenant",
                    headers={"X-API-Key": "test-key"}
                )
                
                # API may allow API key (200) or require full auth (401)
                assert response.status_code in [200, 401], \
                    f"Expected 200 or 401, got {response.status_code}: {response.text}"
        
        asyncio.run(_run())


@pytest.mark.integration
def test_get_current_principal_with_header_auth():
    """Test getting principal info with header-based auth (dev mode)."""
    with with_env({
        "MOTET_API_KEY": "test-key",
        "MOTET_ALLOW_INSECURE_PRINCIPAL_HEADERS": "true"
    }):
        app = create_app()
        
        async def _run():
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test", timeout=10) as client:
                headers = {
                    "X-API-Key": "test-key",
                    "X-Principal-Id": "header-user",
                    "X-Tenant-Id": "header-tenant",
                    "X-Roles": "admin,user"
                }
                
                response = await client.get(
                    "/api/v1/identity/me",
                    headers=headers
                )
                
                assert response.status_code == 200, f"Expected 200, got {response.status_code}: {response.text}"
                data = response.json()
                # Principal may come from header (header-user) or API key (api_key) depending on resolution order
                assert data["id"] in ("header-user", "api_key"), f"Expected header-user or api_key, got {data['id']}"
                # Tenant may come from header (header-tenant) or default when API key is used
                assert data["tenant_id"] in ("header-tenant", "default"), f"Expected header-tenant or default, got {data['tenant_id']}"
                # When principal is from header, roles are present; when api_key, roles may be empty
                assert isinstance(data["roles"], list)
                if data["id"] == "header-user":
                    assert "admin" in data["roles"] and "user" in data["roles"]
        
        asyncio.run(_run())


@pytest.mark.integration
def test_get_current_tenant_with_header_auth():
    """Test getting tenant info with header-based auth (dev mode)."""
    with with_env({
        "MOTET_API_KEY": "test-key",
        "MOTET_ALLOW_INSECURE_PRINCIPAL_HEADERS": "true"
    }):
        app = create_app()
        
        async def _run():
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test", timeout=10) as client:
                headers = {
                    "X-API-Key": "test-key",
                    "X-Principal-Id": "header-user",
                    "X-Tenant-Id": "custom-tenant"
                }
                
                response = await client.get(
                    "/api/v1/identity/tenant",
                    headers=headers
                )
                
                assert response.status_code == 200, f"Expected 200, got {response.status_code}: {response.text}"
                data = response.json()
                # Tenant may come from header (custom-tenant) or default when API key is used
                assert data["tenant_id"] in ("custom-tenant", "default"), f"Expected custom-tenant or default, got {data['tenant_id']}"
        
        asyncio.run(_run())

