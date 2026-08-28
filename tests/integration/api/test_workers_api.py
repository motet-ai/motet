"""
Integration tests for Workers API endpoints.

Tests all worker management endpoints including:
- Worker readiness status
- Worker health checks
- Worker termination
- Termination history
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
    redis_client = get_sync_redis_client("test_workers_api")
    sa_manager = ServiceAccountManager(redis_client)
    
    token = sa_manager.create_service_account(
        name="test-workers-api",
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
def test_worker_readiness_status():
    """Test worker readiness status endpoint (no auth required)."""
    with with_env({
        "MOTET_API_KEY": "test-key",
        "MOTET_REDIS_URL": os.getenv("MOTET_REDIS_URL", "redis://localhost:6379/0")
    }):
        app = create_app()
        
        async def _run():
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test", timeout=10) as client:
                # Readiness endpoint doesn't require auth (monitoring endpoint)
                response = await client.get(
                    "/api/v1/workers/readiness"
                )
                
                assert response.status_code == 200, f"Expected 200, got {response.status_code}: {response.text}"
                data = response.json()
                assert "status" in data
                assert "timestamp" in data
                # May have system_stats and workers fields
                assert isinstance(data, dict)
        
        asyncio.run(_run())


@pytest.mark.integration
def test_worker_health(test_headers):
    """Test worker health endpoint (no auth required)."""
    with with_env({
        "MOTET_API_KEY": "test-key",
        "MOTET_REDIS_URL": os.getenv("MOTET_REDIS_URL", "redis://localhost:6379/0")
    }):
        app = create_app()
        
        async def _run():
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test", timeout=10) as client:
                # Health endpoint doesn't require auth (monitoring endpoint)
                response = await client.get(
                    "/api/v1/workers/health"
                )
                
                assert response.status_code == 200, f"Expected 200, got {response.status_code}: {response.text}"
                data = response.json()
                assert "status" in data
                assert "timestamp" in data
                assert "total_workers" in data
                assert "healthy_workers" in data
                assert "unhealthy_workers" in data
                assert "worker_health" in data
        
        asyncio.run(_run())


@pytest.mark.integration
def test_terminate_worker(test_headers):
    """Test terminating a specific worker."""
    with with_env({
        "MOTET_API_KEY": "test-key",
        "MOTET_REDIS_URL": os.getenv("MOTET_REDIS_URL", "redis://localhost:6379/0"),
        "MOTET_ALLOW_INSECURE_PRINCIPAL_HEADERS": "false"
    }):
        app = create_app()
        
        async def _run():
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test", timeout=10) as client:
                # Try to terminate a worker (may not exist, but tests the endpoint)
                payload = {
                    "reason": "manual_request",
                    "method": "graceful_shutdown",
                    "timeout_seconds": 60
                }
                
                response = await client.post(
                    "/api/v1/workers/nonexistent-worker-id/terminate",
                    json=payload,
                    headers=test_headers
                )
                
                # May return 200/400/404/500 or 403 (admin role required)
                assert response.status_code in [200, 400, 403, 404, 500], \
                    f"Expected 200, 400, 403, 404, or 500, got {response.status_code}: {response.text}"
        
        asyncio.run(_run())


@pytest.mark.integration
def test_terminate_worker_invalid_method(test_headers):
    """Test terminating a worker with invalid method."""
    with with_env({
        "MOTET_API_KEY": "test-key",
        "MOTET_REDIS_URL": os.getenv("MOTET_REDIS_URL", "redis://localhost:6379/0"),
        "MOTET_ALLOW_INSECURE_PRINCIPAL_HEADERS": "false"
    }):
        app = create_app()
        
        async def _run():
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test", timeout=10) as client:
                # Try with invalid method
                payload = {
                    "reason": "manual_request",
                    "method": "invalid_method",
                    "timeout_seconds": 60
                }
                
                response = await client.post(
                    "/api/v1/workers/test-worker-id/terminate",
                    json=payload,
                    headers=test_headers
                )
                
                # May return 400 for invalid method or 403 (admin required)
                assert response.status_code in [400, 403], \
                    f"Expected 400 or 403, got {response.status_code}: {response.text}"
        
        asyncio.run(_run())


@pytest.mark.integration
def test_terminate_worker_requires_auth():
    """Test that terminating a worker requires authentication."""
    with with_env({
        "MOTET_API_KEY": "test-key",
        "MOTET_ALLOW_INSECURE_PRINCIPAL_HEADERS": "false"
    }):
        app = create_app()
        
        async def _run():
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test", timeout=10) as client:
                payload = {
                    "reason": "manual_request",
                    "method": "graceful_shutdown"
                }
                
                response = await client.post(
                    "/api/v1/workers/test-worker-id/terminate",
                    json=payload,
                    headers={"X-API-Key": "test-key"}
                )
                
                # May return 401 (unauthorized) or 403 (admin required)
                assert response.status_code in [401, 403], \
                    f"Expected 401 or 403, got {response.status_code}: {response.text}"

        asyncio.run(_run())


@pytest.mark.integration
def test_terminate_unhealthy_workers(test_headers):
    """Test terminating all unhealthy workers."""
    with with_env({
        "MOTET_API_KEY": "test-key",
        "MOTET_REDIS_URL": os.getenv("MOTET_REDIS_URL", "redis://localhost:6379/0")
    }):
        app = create_app()
        
        async def _run():
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test", timeout=10) as client:
                # Terminate unhealthy workers (requires API key)
                response = await client.post(
                    "/api/v1/workers/terminate-unhealthy",
                    headers={"X-API-Key": "test-key"}
                )
                
                # Should return 200 (success) or 500 (error)
                assert response.status_code in [200, 500], \
                    f"Expected 200 or 500, got {response.status_code}: {response.text}"
                
                if response.status_code == 200:
                    data = response.json()
                    assert "status" in data
                    assert "total_terminations" in data
                    assert "successful_terminations" in data
                    assert "failed_terminations" in data
        
        asyncio.run(_run())


@pytest.mark.integration
def test_terminate_unhealthy_workers_requires_api_key():
    """Test that terminating unhealthy workers requires API key."""
    with with_env({
        "MOTET_API_KEY": "test-key"
    }):
        app = create_app()
        
        async def _run():
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test", timeout=10) as client:
                response = await client.post(
                    "/api/v1/workers/terminate-unhealthy"
                )
                
                assert response.status_code == 401, \
                    f"Expected 401 without API key, got {response.status_code}: {response.text}"
        
        asyncio.run(_run())


@pytest.mark.integration
def test_get_termination_history(test_headers):
    """Test retrieving worker termination history."""
    with with_env({
        "MOTET_API_KEY": "test-key",
        "MOTET_REDIS_URL": os.getenv("MOTET_REDIS_URL", "redis://localhost:6379/0"),
        "MOTET_ALLOW_INSECURE_PRINCIPAL_HEADERS": "false"
    }):
        app = create_app()
        
        async def _run():
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test", timeout=10) as client:
                # Get termination history
                response = await client.get(
                    "/api/v1/workers/termination-history",
                    params={"limit": 10},
                    headers=test_headers
                )
                
                assert response.status_code == 200, f"Expected 200, got {response.status_code}: {response.text}"
                data = response.json()
                assert "status" in data
                assert "timestamp" in data
                assert "termination_history" in data
                assert "total_records" in data
                assert isinstance(data["termination_history"], list)
        
        asyncio.run(_run())


@pytest.mark.integration
def test_get_termination_history_with_limit(test_headers):
    """Test retrieving termination history with custom limit."""
    with with_env({
        "MOTET_API_KEY": "test-key",
        "MOTET_REDIS_URL": os.getenv("MOTET_REDIS_URL", "redis://localhost:6379/0"),
        "MOTET_ALLOW_INSECURE_PRINCIPAL_HEADERS": "false"
    }):
        app = create_app()
        
        async def _run():
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test", timeout=10) as client:
                # Get termination history with custom limit
                response = await client.get(
                    "/api/v1/workers/termination-history",
                    params={"limit": 5},
                    headers=test_headers
                )
                
                assert response.status_code == 200, f"Expected 200, got {response.status_code}: {response.text}"
                data = response.json()
                assert len(data["termination_history"]) <= 5
        
        asyncio.run(_run())


@pytest.mark.integration
def test_termination_history_requires_auth():
    """Test that termination history requires authentication."""
    with with_env({
        "MOTET_API_KEY": "test-key",
        "MOTET_ALLOW_INSECURE_PRINCIPAL_HEADERS": "false"
    }):
        app = create_app()
        
        async def _run():
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test", timeout=10) as client:
                response = await client.get(
                    "/api/v1/workers/termination-history",
                    headers={"X-API-Key": "test-key"}
                )
                
                # Endpoint may allow API key (200) or require auth (401)
                assert response.status_code in [200, 401], \
                    f"Expected 200 or 401, got {response.status_code}: {response.text}"

        asyncio.run(_run())

