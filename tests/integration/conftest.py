"""
Motet - Integration Test Shared Fixtures

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-14

Description:
    Fixtures for integration tests that dispatch /api/v1/chat to real Celery
    workers (compose ``--profile workers``). Mirrors the OpenAI-compat
    distributed suite: reset async Redis so connections bind to this test's
    loop, and skip when no worker has registered ready.

Dependencies:
    - motet.core.distributed.redis_manager: async client cache reset
    - motet.core.distributed.worker_readiness: ready-worker gate
    - motet.interfaces.http: create_app
    - httpx: ASGI transport client

Usage:
    async def test_chat(native_chat_client):
        response = await native_chat_client.post("/api/v1/chat", json={...})

Notes:
    - Do not mark these autouse here; each suite opts in so lane B stays fast
    - mock/mock-small echoes "You said: <prompt>"
"""

from __future__ import annotations

import json
import os
import uuid
from typing import Any, AsyncIterator, Dict, List

import httpx
import pytest

from motet.interfaces.http import create_app


@pytest.fixture
def isolated_async_redis():
    """Rebuild async Redis clients on this test's event loop.

    Cached clients from a previous loop raise ``Event loop is closed`` and
    turn stream reads into empty/error SSE bodies.
    """
    from motet.core.distributed import redis_manager

    def reset() -> None:
        manager = redis_manager.get_redis_manager()
        manager._async_clients.clear()
        manager._pubsub_clients.clear()
        manager._async_connection_pool = None
        manager._pubsub_connection_pool = None
        manager._initialized = False

    reset()
    yield
    reset()


@pytest.fixture
def ready_celery_workers() -> List[str]:
    """Skip unless compose ``--profile workers`` has a ready Celery worker."""
    from motet.core.distributed.worker_readiness import WorkerReadinessService

    workers = WorkerReadinessService().get_ready_workers()
    if not workers:
        pytest.skip(
            "No ready Celery workers; start them with "
            "`docker compose -f tests/docker-compose.test.yml "
            "--profile workers up -d worker-1`"
        )
    return workers


@pytest.fixture
def native_chat_app(monkeypatch: pytest.MonkeyPatch):
    """In-process HTTP app that dispatches chat to mock-backed workers."""
    env = {
        "MOTET_API_KEY": "",
        "MOTET_ALLOW_INSECURE_PRINCIPAL_HEADERS": "true",
        "MOTET_JWT_JWKS_URL": "",
        "MOTET_JWT_PUBLIC_KEY_PEM": "",
        "MOTET_MODEL_PROVIDER": "mock",
        "MOTET_MODEL_NAME": "mock-small",
        "MOTET_ENABLE_MEMORY": "true",
        "MOTET_MEMORY_BACKEND": "redis",
        "MOTET_REDIS_URL": os.getenv("MOTET_REDIS_URL", "redis://localhost:6379/0"),
    }
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    return create_app()


@pytest.fixture
async def native_chat_client(native_chat_app: Any) -> AsyncIterator[httpx.AsyncClient]:
    """Authenticated ASGI client with a unique principal/tenant per test."""
    headers = {
        "X-Principal-Id": f"dist-chat-{uuid.uuid4().hex[:8]}",
        "X-Tenant-Id": f"tenant-dist-{uuid.uuid4().hex[:8]}",
    }
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=native_chat_app),
        base_url="http://test",
        timeout=120,
        headers=headers,
    ) as client:
        yield client


def sse_event_names(body: str) -> List[str]:
    """Return SSE event names from a buffered Motet chat stream."""
    names: List[str] = []
    for line in body.splitlines():
        if line.startswith("event:"):
            names.append(line.split(":", 1)[1].strip())
    return names


def sse_data_payloads(body: str) -> List[Dict[str, Any]]:
    """Parse JSON ``data:`` payloads from a Motet chat SSE body."""
    out: List[Dict[str, Any]] = []
    for line in body.splitlines():
        if not line.startswith("data:"):
            continue
        payload = line[len("data:") :].strip()
        if not payload or payload == "[DONE]":
            continue
        try:
            parsed = json.loads(payload)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            out.append(parsed)
    return out


def sse_assembled_text(body: str) -> str:
    """Join token fragments (``t``) from a Motet chat SSE body."""
    parts: List[str] = []
    for payload in sse_data_payloads(body):
        token = payload.get("t")
        if isinstance(token, str):
            parts.append(token)
        content = payload.get("content")
        if isinstance(content, str) and content:
            parts.append(content)
    return "".join(parts)
