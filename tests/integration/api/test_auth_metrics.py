"""
Integration tests for authentication metrics export.

Verifies that Prometheus metrics for authentication are properly exported.
"""

from __future__ import annotations

import pytest
import asyncio
import httpx

from motet.interfaces.http import create_app
from motet.core.distributed.redis_manager import get_sync_redis_client
from motet.core.security.service_accounts import ServiceAccountManager


@pytest.mark.integration
def test_auth_metrics_exported():
    """Test that authentication metrics are exported to Prometheus."""
    import os
    from contextlib import contextmanager
    
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
    
    with with_env({
        "MOTET_API_KEY": "test-key",
        "MOTET_REDIS_URL": os.getenv("MOTET_REDIS_URL", "redis://localhost:6379/0"),
        "MOTET_ALLOW_INSECURE_PRINCIPAL_HEADERS": "false"
    }):
        app = create_app()
        
        async def _run():
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test", timeout=5) as client:
                # Make a request to trigger metrics
                headers = {"X-API-Key": "test-key"}
                await client.get("/api/v1/commands/list", headers=headers)
                
                # Fetch metrics endpoint
                response = await client.get("/metrics")
                assert response.status_code == 200
                
                metrics_text = response.text
                
                # Verify authentication metrics are present
                assert "motet_auth_attempts_total" in metrics_text, "Auth attempts metric not found"
                assert "motet_auth_latency_seconds" in metrics_text, "Auth latency metric not found"
                
                # Verify metrics have labels
                assert 'auth_type="none"' in metrics_text or 'auth_type="error"' in metrics_text, \
                    "Auth metrics should have auth_type label"
                assert 'status="failure"' in metrics_text or 'status="success"' in metrics_text, \
                    "Auth metrics should have status label"
        
        asyncio.run(_run())


@pytest.mark.integration
def test_auth_metrics_with_service_account():
    """Test that service account authentication is tracked in metrics."""
    import os
    from contextlib import contextmanager
    
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
    
    with with_env({
        "MOTET_API_KEY": "test-key",
        "MOTET_REDIS_URL": os.getenv("MOTET_REDIS_URL", "redis://localhost:6379/0"),
        "MOTET_ALLOW_INSECURE_PRINCIPAL_HEADERS": "false"
    }):
        app = create_app()
        
        # Create service account token
        redis_client = get_sync_redis_client("test_auth_metrics")
        sa_manager = ServiceAccountManager(redis_client)
        
        token = sa_manager.create_service_account(
            name="test-metrics",
            tenant_id="test-tenant",
            motet_id="production",
            roles=["admin"],
            created_by="test@example.com",
            expires_days=1
        )
        
        async def _run():
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test", timeout=5) as client:
                # Make authenticated request
                headers = {
                    "X-API-Key": "test-key",
                    "Authorization": f"Bearer {token}"
                }
                await client.get("/api/v1/commands/list", headers=headers)
                
                # Fetch metrics
                response = await client.get("/metrics")
                assert response.status_code == 200
                
                metrics_text = response.text
                
                # Verify service account auth is tracked
                assert 'auth_type="service_account"' in metrics_text, \
                    "Service account authentication should be tracked"
                assert 'status="success"' in metrics_text, \
                    "Successful authentication should be tracked"
        
        asyncio.run(_run())
        
        # Cleanup
        sa_manager.revoke_service_account(token)

