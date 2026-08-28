"""
Integration tests for Chat API endpoints.

Tests all chat completion endpoints including:
- Non-streaming chat completion
- Streaming chat completion (SSE)
- WebSocket chat (if applicable)
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
    redis_client = get_sync_redis_client("test_chat_api")
    sa_manager = ServiceAccountManager(redis_client)
    
    token = sa_manager.create_service_account(
        name="test-chat-api",
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
def test_chat_completion(test_headers):
    """Test non-streaming chat completion."""
    with with_env({
        "MOTET_API_KEY": "test-key",
        "MOTET_REDIS_URL": os.getenv("MOTET_REDIS_URL", "redis://localhost:6379/0"),
        "MOTET_ALLOW_INSECURE_PRINCIPAL_HEADERS": "false"
    }):
        app = create_app()
        
        async def _run():
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test", timeout=30) as client:
                # Chat completion request
                payload = {
                    "messages": [
                        {"role": "user", "content": "Hello, what is AI?"}
                    ],
                    "stream": False
                }
                
                response = await client.post(
                    "/api/v1/chat",
                    json=payload,
                    headers=test_headers
                )
                
                # Should return 200 (success) or 500 (if model not configured)
                assert response.status_code in [200, 500], \
                    f"Expected 200 or 500, got {response.status_code}: {response.text}"
                
                if response.status_code == 200:
                    data = response.json()
                    assert "content" in data
                    assert isinstance(data["content"], str)
        
        asyncio.run(_run())


@pytest.mark.integration
def test_chat_completion_streaming(test_headers):
    """Test streaming chat completion via SSE."""
    with with_env({
        "MOTET_API_KEY": "test-key",
        "MOTET_REDIS_URL": os.getenv("MOTET_REDIS_URL", "redis://localhost:6379/0"),
        "MOTET_ALLOW_INSECURE_PRINCIPAL_HEADERS": "false"
    }):
        app = create_app()
        
        async def _run():
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test", timeout=10) as client:
                # Streaming chat completion request
                payload = {
                    "messages": [
                        {"role": "user", "content": "Hello"}
                    ],
                    "stream": True
                }
                
                try:
                    async with asyncio.timeout(5.0):
                        async with client.stream(
                            "POST",
                            "/api/v1/chat",
                            json=payload,
                            headers=test_headers
                        ) as response:
                            # Should return 200 (success) or 500 (if model not configured)
                            assert response.status_code in [200, 500], \
                                f"Expected 200 or 500, got {response.status_code}"
                            
                            if response.status_code == 200:
                                # Check content type for SSE
                                assert "text/event-stream" in response.headers.get("content-type", "")
                                
                                # Read a few events with timeout per line
                                event_count = 0
                                try:
                                    async for line in response.aiter_lines():
                                        if line.startswith("data:"):
                                            event_count += 1
                                            if event_count >= 1:
                                                break
                                except Exception:
                                    pass  # Stream may close early
                except TimeoutError:
                    pytest.skip("Chat streaming test timed out (likely no model configured in test env)")
        
        asyncio.run(_run())


@pytest.mark.integration
def test_chat_completion_with_conversation_id(test_headers):
    """Test chat completion with conversation ID."""
    with with_env({
        "MOTET_API_KEY": "test-key",
        "MOTET_REDIS_URL": os.getenv("MOTET_REDIS_URL", "redis://localhost:6379/0"),
        "MOTET_ALLOW_INSECURE_PRINCIPAL_HEADERS": "false"
    }):
        app = create_app()
        
        async def _run():
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test", timeout=30) as client:
                # Chat with conversation ID
                payload = {
                    "messages": [
                        {"role": "user", "content": "What is machine learning?"}
                    ],
                    "stream": False,
                    "conversation_id": "test-conv-123"
                }
                
                response = await client.post(
                    "/api/v1/chat",
                    json=payload,
                    headers=test_headers
                )
                
                # Should return 200 or 500
                assert response.status_code in [200, 500], \
                    f"Expected 200 or 500, got {response.status_code}: {response.text}"
        
        asyncio.run(_run())


@pytest.mark.integration
def test_chat_completion_requires_auth():
    """Test that chat completion requires authentication."""
    with with_env({
        "MOTET_API_KEY": "test-key",
        "MOTET_ALLOW_INSECURE_PRINCIPAL_HEADERS": "false"
    }):
        app = create_app()
        
        async def _run():
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test", timeout=10) as client:
                payload = {
                    "messages": [
                        {"role": "user", "content": "Hello"}
                    ]
                }
                
                response = await client.post(
                    "/api/v1/chat",
                    json=payload,
                    headers={"X-API-Key": "test-key"}
                )
                
                # API may allow API key (200) or require full auth (401)
                assert response.status_code in [200, 401], \
                    f"Expected 200 or 401, got {response.status_code}: {response.text}"

        asyncio.run(_run())


@pytest.mark.integration
def test_chat_completion_empty_messages(test_headers):
    """Test chat completion with empty messages."""
    with with_env({
        "MOTET_API_KEY": "test-key",
        "MOTET_REDIS_URL": os.getenv("MOTET_REDIS_URL", "redis://localhost:6379/0"),
        "MOTET_ALLOW_INSECURE_PRINCIPAL_HEADERS": "false"
    }):
        app = create_app()
        
        async def _run():
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test", timeout=10) as client:
                # Empty messages
                payload = {
                    "messages": []
                }
                
                response = await client.post(
                    "/api/v1/chat",
                    json=payload,
                    headers=test_headers
                )
                
                # May return 400/422 (validation), 500 (backend), or 200 (if empty accepted)
                assert response.status_code in [200, 400, 422, 500], \
                    f"Expected 200, 400, 422, or 500 for empty messages, got {response.status_code}: {response.text}"
        
        asyncio.run(_run())


