"""
Motet - Native Chat Memory Distributed Tests

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-14

Description:
    After a native chat turn dispatched to a Celery worker, the shared Redis
    memory store should contain an assistant_response the HTTP process can
    inspect. Vector inspect remains a shape check (indexing is optional).

Dependencies:
    - tests.integration.conftest: native_chat_client, ready workers, Redis reset
    - Celery workers with MOTET_ENABLE_MEMORY=true and Redis backend

Usage:
    docker compose -f tests/docker-compose.test.yml --profile workers up -d worker-1
    docker compose -f tests/docker-compose.test.yml run --rm test-runner \\
        python -m pytest tests/integration/memory/test_memory_integration.py -v -m distributed

Notes:
    - Uses a unique conversation_id so inspect is not polluted by other tests
    - mock-small echoes ``You said: <prompt>``
"""

from __future__ import annotations

import uuid

import pytest

pytestmark = [
    pytest.mark.integration,
    pytest.mark.distributed,
    pytest.mark.requires_redis,
]


@pytest.fixture(autouse=True)
def _async_redis(isolated_async_redis):
    """Chat + inspect both talk to Redis on this test's loop."""


@pytest.fixture(autouse=True)
def _workers(ready_celery_workers):
    """Skip unless a Celery worker is ready."""


async def test_assistant_memory_stored(native_chat_client) -> None:
    """A completed worker chat turn stores an assistant_response memory."""
    conversation_id = f"dist-mem-{uuid.uuid4().hex[:12]}"
    prompt = "hello there from distributed memory"
    chat = await native_chat_client.post(
        "/api/v1/chat",
        json={
            "messages": [{"role": "user", "content": prompt}],
            "conversation_id": conversation_id,
            "stream": False,
        },
    )
    assert chat.status_code == 200, chat.text
    body = chat.json()
    assert prompt in (body.get("content") or ""), body

    inspect = await native_chat_client.get("/api/v1/memories/inspect")
    assert inspect.status_code == 200, inspect.text
    data = inspect.json()
    counts = (data.get("memory") or {}).get("counts_by_type") or {}
    assert counts.get("assistant_response", 0) >= 1, data

    examples = (data.get("memory") or {}).get("recent_examples") or []
    texts = " ".join(str(item.get("text") or item.get("content") or "") for item in examples)
    assert prompt in texts or "You said:" in texts, examples


async def test_memory_inspect_includes_vector_section(native_chat_client) -> None:
    """Inspect always returns a vector section, even when indexing is off."""
    response = await native_chat_client.get("/api/v1/memories/inspect")
    assert response.status_code == 200, response.text
    data = response.json()
    assert "vector" in data
    assert isinstance(data["vector"], dict)
