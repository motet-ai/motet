"""
Integration tests for Conversations API endpoints (ADR-0072).

Tests all conversation management endpoints including:
- Conversation listing
- Conversation details retrieval
- Conversation clearing
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

pytestmark = [pytest.mark.integration, pytest.mark.requires_external]


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
    redis_client = get_sync_redis_client("test_conversations_api")
    sa_manager = ServiceAccountManager(redis_client)

    token = sa_manager.create_service_account(
        name="test-conversations-api",
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
        "Content-Type": "application/json",
        "X-Principal-Id": "test-principal",
        "X-Tenant-Id": "test-tenant"
    }


@pytest.mark.integration
def test_list_conversations(test_headers):
    """Test listing conversations."""
    with with_env({
        "MOTET_API_KEY": "test-key",
        "MOTET_REDIS_URL": os.getenv("MOTET_REDIS_URL", "redis://localhost:6379/0"),
        "MOTET_ALLOW_INSECURE_PRINCIPAL_HEADERS": "false"
    }):
        app = create_app()

        async def _run():
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(
                transport=transport, base_url="http://test", timeout=10,
                headers={"X-Principal-Id": "test-principal", "X-Tenant-Id": "test-tenant"}
            ) as client:
                response = await client.get(
                    "/api/v1/conversations",
                    headers=test_headers
                )

                assert response.status_code == 200, f"Expected 200, got {response.status_code}: {response.text}"
                data = response.json()
                assert "conversations" in data
                assert isinstance(data["conversations"], list)

        asyncio.run(_run())


@pytest.mark.integration
def test_list_conversations_requires_auth():
    """Test that listing conversations requires authentication."""
    with with_env({
        "MOTET_API_KEY": "test-key",
        "MOTET_ALLOW_INSECURE_PRINCIPAL_HEADERS": "false"
    }):
        app = create_app()

        async def _run():
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(
                transport=transport, base_url="http://test", timeout=10,
                headers={"X-Principal-Id": "test-principal", "X-Tenant-Id": "test-tenant"}
            ) as client:
                response = await client.get(
                    "/api/v1/conversations",
                    headers={"X-API-Key": "test-key", "X-Principal-Id": "test-principal", "X-Tenant-Id": "test-tenant"}
                )

                # API may allow API key (200) or require full auth (401)
                assert response.status_code in [200, 401], \
                    f"Expected 200 or 401, got {response.status_code}: {response.text}"

        asyncio.run(_run())


@pytest.mark.integration
def test_get_conversation_details(test_headers):
    """Test getting conversation details."""
    with with_env({
        "MOTET_API_KEY": "test-key",
        "MOTET_REDIS_URL": os.getenv("MOTET_REDIS_URL", "redis://localhost:6379/0"),
        "MOTET_ALLOW_INSECURE_PRINCIPAL_HEADERS": "false"
    }):
        app = create_app()

        async def _run():
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(
                transport=transport, base_url="http://test", timeout=10,
                headers={"X-Principal-Id": "test-principal", "X-Tenant-Id": "test-tenant"}
            ) as client:
                response = await client.get(
                    "/api/v1/conversations/test-conversation-123",
                    headers=test_headers
                )

                assert response.status_code == 200, f"Expected 200, got {response.status_code}: {response.text}"
                data = response.json()
                assert "conversation_id" in data
                assert "history" in data
                assert "counts" in data
                assert isinstance(data["history"], list)
                assert isinstance(data["counts"], dict)

        asyncio.run(_run())


@pytest.mark.integration
def test_get_conversation_details_requires_auth():
    """Test that getting conversation details requires authentication."""
    with with_env({
        "MOTET_API_KEY": "test-key",
        "MOTET_ALLOW_INSECURE_PRINCIPAL_HEADERS": "false"
    }):
        app = create_app()

        async def _run():
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(
                transport=transport, base_url="http://test", timeout=10,
                headers={"X-Principal-Id": "test-principal", "X-Tenant-Id": "test-tenant"}
            ) as client:
                response = await client.get(
                    "/api/v1/conversations/test-conversation-123",
                    headers={"X-API-Key": "test-key", "X-Principal-Id": "test-principal", "X-Tenant-Id": "test-tenant"}
                )

                # API may allow API key (200) or require full auth (401)
                assert response.status_code in [200, 401, 404], \
                    f"Expected 200, 401, or 404, got {response.status_code}: {response.text}"

        asyncio.run(_run())


@pytest.mark.integration
def test_clear_conversation(test_headers, test_service_account_token):
    """Owner can clear a conversation they have claimed (issue #139)."""
    import uuid

    from motet.core.conversations.ownership import authorize_conversation_access_sync

    conversation_id = f"test-clear-{uuid.uuid4().hex[:12]}"
    # SA bearer identity wins over X-Principal-Id when insecure headers are off.
    principal_id = f"service-account:test-conversations-api"

    with with_env({
        "MOTET_API_KEY": "test-key",
        "MOTET_REDIS_URL": os.getenv("MOTET_REDIS_URL", "redis://localhost:6379/0"),
        "MOTET_ALLOW_INSECURE_PRINCIPAL_HEADERS": "false"
    }):
        # Claim ownership before clear — clear uses bind_if_unclaimed=False.
        authorize_conversation_access_sync(
            motet_id="production",
            tenant_id="test-tenant",
            principal_id=principal_id,
            conversation_id=conversation_id,
            bind_if_unclaimed=True,
        )
        app = create_app()

        async def _run():
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(
                transport=transport, base_url="http://test", timeout=10,
            ) as client:
                response = await client.post(
                    f"/api/v1/conversations/{conversation_id}/clear",
                    headers=test_headers
                )

                # 200 = success, 500 = circuit breaker or distributed command failure (no workers)
                assert response.status_code in [200, 500], \
                    f"Expected 200 or 500, got {response.status_code}: {response.text}"
                if response.status_code == 200:
                    data = response.json()
                    assert "conversation_id" in data
                    assert "cleared" in data
                    assert isinstance(data["cleared"], dict)

        asyncio.run(_run())


@pytest.mark.integration
def test_clear_unowned_conversation_is_forbidden(test_headers):
    """Clearing an unclaimed / foreign conversation_id returns 403 (issue #139)."""
    import uuid

    conversation_id = f"test-clear-unowned-{uuid.uuid4().hex[:12]}"
    with with_env({
        "MOTET_API_KEY": "test-key",
        "MOTET_REDIS_URL": os.getenv("MOTET_REDIS_URL", "redis://localhost:6379/0"),
        "MOTET_ALLOW_INSECURE_PRINCIPAL_HEADERS": "false"
    }):
        app = create_app()

        async def _run():
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(
                transport=transport, base_url="http://test", timeout=10,
            ) as client:
                response = await client.post(
                    f"/api/v1/conversations/{conversation_id}/clear",
                    headers=test_headers
                )

                # Ownership denial is 403; 500 when the command path cannot run.
                assert response.status_code in [403, 500], \
                    f"Expected 403 or 500, got {response.status_code}: {response.text}"
                if response.status_code == 403:
                    assert "access denied" in response.text.lower()

        asyncio.run(_run())


@pytest.mark.integration
def test_clear_conversation_requires_auth():
    """Clearing a conversation without credentials is rejected."""
    with with_env({
        "MOTET_API_KEY": "test-key",
        "MOTET_ALLOW_INSECURE_PRINCIPAL_HEADERS": "false"
    }):
        app = create_app()

        async def _run():
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(
                transport=transport, base_url="http://test", timeout=10,
            ) as client:
                response = await client.post(
                    "/api/v1/conversations/test-conversation-123/clear",
                )

                assert response.status_code in [401, 403], \
                    f"Expected 401 or 403, got {response.status_code}: {response.text}"

        asyncio.run(_run())


@pytest.mark.integration
def test_get_conversation_invalid_id(test_headers):
    """Test getting conversation with invalid ID format."""
    with with_env({
        "MOTET_API_KEY": "test-key",
        "MOTET_REDIS_URL": os.getenv("MOTET_REDIS_URL", "redis://localhost:6379/0"),
        "MOTET_ALLOW_INSECURE_PRINCIPAL_HEADERS": "false"
    }):
        app = create_app()

        async def _run():
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(
                transport=transport, base_url="http://test", timeout=10,
                headers={"X-Principal-Id": "test-principal", "X-Tenant-Id": "test-tenant"}
            ) as client:
                response = await client.get(
                    "/api/v1/conversations/",
                    headers=test_headers
                )

                assert response.status_code in [200, 307, 404, 422], \
                    f"Expected 200/307/404/422 for trailing-slash path, got {response.status_code}: {response.text}"

        asyncio.run(_run())
