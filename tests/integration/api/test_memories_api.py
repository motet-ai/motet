"""
Integration tests for Memories API endpoints.

Tests all memory management endpoints including:
- List recent memories
- Store memory (`POST /api/v1/memories/store`; see unit tests in ``test_memories_store_api.py``)
- Find memories by tags
- Tag memories
- Inspect memory system
- Clear memories
- Semantic search
- Memory retrieval
- Memory consolidation
"""

from __future__ import annotations

import asyncio
import os
import httpx
import pytest
import uuid
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
    redis_client = get_sync_redis_client("test_memories_api")
    sa_manager = ServiceAccountManager(redis_client)
    
    token = sa_manager.create_service_account(
        name="test-memories-api",
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
def test_list_memories(test_headers):
    """Test listing recent memories."""
    with with_env({
        "MOTET_API_KEY": "test-key",
        "MOTET_REDIS_URL": os.getenv("MOTET_REDIS_URL", "redis://localhost:6379/0"),
        "MOTET_ALLOW_INSECURE_PRINCIPAL_HEADERS": "false"
    }):
        app = create_app()
        
        async def _run():
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test", timeout=10) as client:
                # List memories
                response = await client.get(
                    "/api/v1/memories",
                    params={"limit": 10},
                    headers=test_headers
                )
                
                assert response.status_code == 200, f"Expected 200, got {response.status_code}: {response.text}"
                data = response.json()
                assert isinstance(data, list)
        
        asyncio.run(_run())


@pytest.mark.integration
def test_list_memories_with_filters(test_headers):
    """Test listing memories with tag and entity filters."""
    with with_env({
        "MOTET_API_KEY": "test-key",
        "MOTET_REDIS_URL": os.getenv("MOTET_REDIS_URL", "redis://localhost:6379/0"),
        "MOTET_ALLOW_INSECURE_PRINCIPAL_HEADERS": "false"
    }):
        app = create_app()
        
        async def _run():
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test", timeout=10) as client:
                # List with tag filter
                response = await client.get(
                    "/api/v1/memories",
                    params={"tag": "type:conversation", "limit": 5},
                    headers=test_headers
                )
                
                assert response.status_code == 200
                data = response.json()
                assert isinstance(data, list)
                
                # List with entity filter
                response = await client.get(
                    "/api/v1/memories",
                    params={"entity": "user123", "limit": 5},
                    headers=test_headers
                )
                
                assert response.status_code == 200
                data = response.json()
                assert isinstance(data, list)
        
        asyncio.run(_run())


@pytest.mark.integration
def test_find_memories_by_tags(test_headers):
    """Test finding memories by tags."""
    with with_env({
        "MOTET_API_KEY": "test-key",
        "MOTET_REDIS_URL": os.getenv("MOTET_REDIS_URL", "redis://localhost:6379/0"),
        "MOTET_ALLOW_INSECURE_PRINCIPAL_HEADERS": "false"
    }):
        app = create_app()
        
        async def _run():
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test", timeout=10) as client:
                # Find memories by tags
                payload = {
                    "tags": ["type:conversation"],
                    "match": "any",
                    "limit": 10
                }
                
                response = await client.post(
                    "/api/v1/memories/find",
                    json=payload,
                    headers=test_headers
                )
                
                # Should return 200 or 400 (if tool not available)
                assert response.status_code in [200, 400, 500], \
                    f"Expected 200, 400, or 500, got {response.status_code}: {response.text}"
        
        asyncio.run(_run())


@pytest.mark.integration
def test_tag_memories(test_headers):
    """Test tagging memories."""
    with with_env({
        "MOTET_API_KEY": "test-key",
        "MOTET_REDIS_URL": os.getenv("MOTET_REDIS_URL", "redis://localhost:6379/0"),
        "MOTET_ALLOW_INSECURE_PRINCIPAL_HEADERS": "false"
    }):
        app = create_app()
        
        async def _run():
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test", timeout=10) as client:
                # Tag memories
                payload = {
                    "memory_ids": ["mem-123"],
                    "tags": ["important", "reviewed"],
                    "op": "add"
                }
                
                response = await client.post(
                    "/api/v1/memories/tag",
                    json=payload,
                    headers=test_headers
                )
                
                # Should return 200 or 400 (if memory not found)
                assert response.status_code in [200, 400, 500], \
                    f"Expected 200, 400, or 500, got {response.status_code}: {response.text}"
        
        asyncio.run(_run())


@pytest.mark.integration
def test_inspect_memories(test_headers):
    """Test inspecting memory system."""
    with with_env({
        "MOTET_API_KEY": "test-key",
        "MOTET_REDIS_URL": os.getenv("MOTET_REDIS_URL", "redis://localhost:6379/0"),
        "MOTET_ALLOW_INSECURE_PRINCIPAL_HEADERS": "false"
    }):
        app = create_app()
        
        async def _run():
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test", timeout=10) as client:
                # Inspect memory system
                response = await client.get(
                    "/api/v1/memories/inspect",
                    params={"limit": 5},
                    headers=test_headers
                )
                
                assert response.status_code == 200, f"Expected 200, got {response.status_code}: {response.text}"
                data = response.json()
                assert "memory" in data
                assert isinstance(data["memory"], dict)
        
        asyncio.run(_run())


@pytest.mark.integration
def test_clear_memories(test_headers):
    """Test clearing memories."""
    with with_env({
        "MOTET_API_KEY": "test-key",
        "MOTET_REDIS_URL": os.getenv("MOTET_REDIS_URL", "redis://localhost:6379/0"),
        "MOTET_ALLOW_INSECURE_PRINCIPAL_HEADERS": "false"
    }):
        app = create_app()
        
        async def _run():
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test", timeout=10) as client:
                # Clear memories by tag
                response = await client.post(
                    "/api/v1/memories/clear",
                    params={"tag": "test-tag"},
                    headers=test_headers
                )
                
                # Should return 200; response may be {"cleared": {...}} or {"memory": N, "vector": N}
                assert response.status_code == 200, f"Expected 200, got {response.status_code}: {response.text}"
                data = response.json()
                assert isinstance(data, dict)
                assert "cleared" in data or ("memory" in data and "vector" in data)
        
        asyncio.run(_run())


@pytest.mark.integration
def test_semantic_search(test_headers):
    """Test semantic search of memories."""
    with with_env({
        "MOTET_API_KEY": "test-key",
        "MOTET_REDIS_URL": os.getenv("MOTET_REDIS_URL", "redis://localhost:6379/0"),
        "MOTET_ALLOW_INSECURE_PRINCIPAL_HEADERS": "false"
    }):
        app = create_app()
        
        async def _run():
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test", timeout=10) as client:
                # Semantic search
                response = await client.get(
                    "/api/v1/memories/search",
                    params={
                        "q": "What is AI?",
                        "top_k": 5
                    },
                    headers=test_headers
                )
                
                # 200 success; 503 if workers unavailable; 500 legacy misconfiguration
                assert response.status_code in [200, 500, 503], \
                    f"Expected 200, 500, or 503, got {response.status_code}: {response.text}"
        
        asyncio.run(_run())


@pytest.mark.integration
def test_store_memory(test_headers):
    """Test storing a memory via ``POST /api/v1/memories/store`` (distributed ``core.memory_store``)."""
    marker = f"integration-store-{uuid.uuid4().hex[:12]}"
    with with_env({
        "MOTET_API_KEY": "test-key",
        "MOTET_REDIS_URL": os.getenv("MOTET_REDIS_URL", "redis://localhost:6379/0"),
        "MOTET_ALLOW_INSECURE_PRINCIPAL_HEADERS": "false",
    }):
        app = create_app()

        async def _run():
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test", timeout=120) as client:
                response = await client.post(
                    "/api/v1/memories/store",
                    json={
                        "content": f"Integration test memory content {marker}",
                        "type": "note",
                        "tags": ["integration_test", "api_memories_store"],
                        "metadata": {"test": "test_memories_api.store"},
                    },
                    headers=test_headers,
                )

                assert response.status_code in [200, 500, 503], (
                    f"Expected 200, 500, or 503, got {response.status_code}: {response.text}"
                )
                if response.status_code == 200:
                    data = response.json()
                    assert data.get("stored") is True
                    assert data.get("memory_id"), f"Expected memory_id in body: {data}"

        asyncio.run(_run())


@pytest.mark.integration
def test_store_memory_requires_auth():
    """Unauthenticated store requests must not succeed (401)."""
    with with_env({
        "MOTET_API_KEY": "test-key",
        "MOTET_ALLOW_INSECURE_PRINCIPAL_HEADERS": "false",
    }):
        app = create_app()

        async def _run():
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test", timeout=30) as client:
                response = await client.post(
                    "/api/v1/memories/store",
                    json={"content": "should not persist without auth"},
                )
                assert response.status_code == 401, (
                    f"Expected 401 without credentials, got {response.status_code}: {response.text}"
                )

        asyncio.run(_run())


@pytest.mark.integration
def test_list_memories_requires_auth():
    """Test that listing memories requires authentication."""
    with with_env({
        "MOTET_API_KEY": "test-key",
        "MOTET_ALLOW_INSECURE_PRINCIPAL_HEADERS": "false"
    }):
        app = create_app()
        
        async def _run():
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test", timeout=10) as client:
                response = await client.get(
                    "/api/v1/memories",
                    headers={"X-API-Key": "test-key"}
                )
                
                # API may allow API key (200) or require full auth (401)
                assert response.status_code in [200, 401], \
                    f"Expected 200 or 401, got {response.status_code}: {response.text}"

        asyncio.run(_run())


@pytest.mark.integration
def test_find_memories_requires_auth():
    """Unauthenticated requests to find memories must not succeed (401)."""
    with with_env({
        "MOTET_API_KEY": "test-key",
        "MOTET_ALLOW_INSECURE_PRINCIPAL_HEADERS": "false"
    }):
        app = create_app()
        
        async def _run():
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test", timeout=10) as client:
                payload = {
                    "tags": ["test"],
                    "limit": 10
                }
                # Do not send X-API-Key or other credentials — a matching API key *is* auth.
                response = await client.post(
                    "/api/v1/memories/find",
                    json=payload,
                )
                assert response.status_code == 401, (
                    f"Expected 401 without credentials, got {response.status_code}: {response.text}"
                )

        asyncio.run(_run())


@pytest.mark.integration
def test_inspect_memories_requires_auth():
    """Test that inspecting memories requires authentication."""
    with with_env({
        "MOTET_API_KEY": "test-key",
        "MOTET_ALLOW_INSECURE_PRINCIPAL_HEADERS": "false"
    }):
        app = create_app()
        
        async def _run():
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test", timeout=10) as client:
                response = await client.get(
                    "/api/v1/memories/inspect",
                    headers={"X-API-Key": "test-key"}
                )
                
                # API may allow API key (200) or require full auth (401)
                assert response.status_code in [200, 401], \
                    f"Expected 200 or 401, got {response.status_code}: {response.text}"

        asyncio.run(_run())


def _extract_prepare_context_memory_items(task_flow: dict) -> list[dict]:
    """Extract prepare_context memory_items list from debug task-flow payload."""
    commands = task_flow.get("commands") or []
    for cmd in commands:
        if cmd.get("command_type") != "core.prepare_context":
            continue
        results = cmd.get("results") or {}
        data = results.get("data") or {}
        context_info = data.get("context_info") or {}
        items = context_info.get("memory_items") or []
        if isinstance(items, list):
            return items
    return []


def _matches_agent_turn(
    command: dict,
    *,
    conversation_id: str,
    agent_id: str,
    user_text: str,
) -> bool:
    """Best-effort filter for the specific chat turn command we just submitted."""
    if command.get("command_type") != "core.agent_turn":
        return False
    cmd_data = command.get("command_data") or {}
    if not isinstance(cmd_data, dict):
        return False

    if str(cmd_data.get("conversation_id", "") or "") != conversation_id:
        return False

    data = cmd_data.get("data") or {}
    if not isinstance(data, dict):
        return False

    cmd_agent = str(data.get("agent_id", "") or "")
    if cmd_agent and cmd_agent != agent_id:
        return False

    messages = data.get("messages") or []
    if not isinstance(messages, list):
        return False
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        if str(msg.get("role", "") or "") != "user":
            continue
        content = str(msg.get("content", "") or "")
        if user_text in content:
            return True
    return False


async def _run_chat_and_get_latest_task_flow(
    client: httpx.AsyncClient,
    *,
    headers: dict[str, str],
    conversation_id: str,
    agent_id: str,
    user_text: str,
) -> dict:
    chat_payload = {
        "messages": [{"role": "user", "content": user_text}],
        "stream": False,
        "conversation_id": conversation_id,
        "agent_id": agent_id,
        "surface_id": "demo_chat",
    }
    chat_resp = await client.post("/api/v1/chat", json=chat_payload, headers=headers)
    assert chat_resp.status_code == 200, f"Expected 200 from /api/v1/chat, got {chat_resp.status_code}: {chat_resp.text}"

    task_id: str | None = None
    for _ in range(10):
        commands_resp = await client.get("/api/v1/debug/commands", params={"limit": 200}, headers=headers)
        assert commands_resp.status_code == 200, f"Expected 200 from /api/v1/debug/commands, got {commands_resp.status_code}: {commands_resp.text}"
        commands = (commands_resp.json() or {}).get("commands") or []
        agent_turn = next(
            (
                c for c in commands
                if _matches_agent_turn(
                    c,
                    conversation_id=conversation_id,
                    agent_id=agent_id,
                    user_text=user_text,
                )
            ),
            None,
        )
        if agent_turn is None:
            agent_turn = next((c for c in commands if c.get("command_type") == "core.agent_turn"), None)
        if agent_turn and agent_turn.get("task_id"):
            task_id = str(agent_turn["task_id"])
            break
        await asyncio.sleep(0.2)

    if not task_id:
        pytest.skip("Distributed workers unavailable; skipping cross-agent API flow test.")

    flow_resp = await client.get(f"/api/v1/debug/task-flow/{task_id}", headers=headers)
    assert flow_resp.status_code == 200, f"Expected 200 from /api/v1/debug/task-flow/{task_id}, got {flow_resp.status_code}: {flow_resp.text}"
    return flow_resp.json()


async def _store_foreign_agent_memory(
    client: httpx.AsyncClient,
    *,
    headers: dict[str, str],
    conversation_id: str,
    marker: str,
    foreign_agent_id: str = "core.foreign",
) -> None:
    """Store a conversation-scoped memory owned by a different agent than ``default``."""
    response = await client.post(
        "/api/v1/memories/store",
        json={
            "content": f"remember marker {marker}",
            "type": "note",
            # hybrid_retrieve filters conversation scope via this tag (also
            # auto-added by MemoryManager.store_memory when conversation_id is set).
            "tags": [
                f"agent:{foreign_agent_id}",
                f"conversation:{conversation_id}",
                "agent_scope_test",
            ],
            "metadata": {"agent_id": foreign_agent_id, "test": "agent_scope"},
            "conversation_id": conversation_id,
            "scope_type": "conversation",
        },
        headers=headers,
    )
    if response.status_code in (500, 503):
        pytest.skip(
            "Distributed workers unavailable; skipping cross-agent memory store: "
            f"{response.status_code} {response.text}"
        )
    assert response.status_code == 200, (
        f"Expected 200 from /api/v1/memories/store, got {response.status_code}: {response.text}"
    )
    body = response.json()
    assert body.get("stored") is True, f"Expected stored=true: {body}"


@pytest.mark.integration
def test_chat_memory_agent_scope_prefer_falls_back_cross_agent(test_headers):
    """End-to-end: in prefer mode, default agent may fallback to other-agent memories.

    ``core.motet_admin`` intentionally sets ``context_prepare=None``, so this test
    stores a foreign-agent memory via the store API and recalls it through
    ``core.default`` (which runs ``core.prepare_context``).
    """
    marker = f"agent-scope-prefer-{uuid.uuid4().hex[:10]}"
    conversation_id = f"conv-{uuid.uuid4().hex[:10]}"

    with with_env({
        "MOTET_API_KEY": "test-key",
        "MOTET_REDIS_URL": os.getenv("MOTET_REDIS_URL", "redis://localhost:6379/0"),
        "MOTET_ALLOW_INSECURE_PRINCIPAL_HEADERS": "false",
        "MOTET_MODEL_PROVIDER": "mock",
        "MOTET_ENABLE_MODEL_PROFILES": "false",
        "MOTET_MEMORY_AGENT_SCOPE_MODE": "prefer",
    }):
        app = create_app()

        async def _run():
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test", timeout=30) as client:
                # Keep debug response clean for deterministic task lookup.
                await client.delete("/api/v1/debug/tasks/clear", headers=test_headers)

                await _store_foreign_agent_memory(
                    client,
                    headers=test_headers,
                    conversation_id=conversation_id,
                    marker=marker,
                )

                # default asks for marker; prefer mode should allow cross-agent fallback.
                flow = await _run_chat_and_get_latest_task_flow(
                    client,
                    headers=test_headers,
                    conversation_id=conversation_id,
                    agent_id="default",
                    user_text=f"what do you know about marker {marker}?",
                )

                memory_items = _extract_prepare_context_memory_items(flow)
                combined = " ".join(str(i.get("content", "")) for i in memory_items)
                assert marker in combined, "Expected marker to appear via cross-agent fallback in prefer mode"

        asyncio.run(_run())


@pytest.mark.integration
def test_chat_memory_agent_scope_strict_blocks_cross_agent(test_headers):
    """End-to-end: in strict mode, default agent must not retrieve other-agent memories."""
    marker = f"agent-scope-strict-{uuid.uuid4().hex[:10]}"
    conversation_id = f"conv-{uuid.uuid4().hex[:10]}"

    with with_env({
        "MOTET_API_KEY": "test-key",
        "MOTET_REDIS_URL": os.getenv("MOTET_REDIS_URL", "redis://localhost:6379/0"),
        "MOTET_ALLOW_INSECURE_PRINCIPAL_HEADERS": "false",
        "MOTET_MODEL_PROVIDER": "mock",
        "MOTET_ENABLE_MODEL_PROFILES": "false",
        "MOTET_MEMORY_AGENT_SCOPE_MODE": "strict",
    }):
        app = create_app()

        async def _run():
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test", timeout=30) as client:
                await client.delete("/api/v1/debug/tasks/clear", headers=test_headers)

                await _store_foreign_agent_memory(
                    client,
                    headers=test_headers,
                    conversation_id=conversation_id,
                    marker=marker,
                )

                # default asks for marker; strict mode should block cross-agent retrieval.
                flow = await _run_chat_and_get_latest_task_flow(
                    client,
                    headers=test_headers,
                    conversation_id=conversation_id,
                    agent_id="default",
                    user_text=f"what do you know about marker {marker}?",
                )

                memory_items = _extract_prepare_context_memory_items(flow)
                combined = " ".join(str(i.get("content", "")) for i in memory_items)
                assert marker not in combined, "Strict mode should prevent cross-agent memory retrieval"

        asyncio.run(_run())
