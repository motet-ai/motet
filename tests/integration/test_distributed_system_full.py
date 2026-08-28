"""
Motet - Native Chat Distributed Integration Tests

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-14

Description:
    Lane C coverage for POST /api/v1/chat against real Celery workers.
    Replaces the older in-process suite that expected live-model essays and
    exact ``text/event-stream`` Content-Type strings.

    Process-boundary stream decryption for the OpenAI facade lives in
    ``tests/integration/api/test_openai_compat_distributed.py``. Native SSE
    event names live in ``tests/integration/streaming/test_sse_streaming.py``.

Dependencies:
    - tests.integration.conftest: native_chat_client, ready workers, Redis reset
    - Celery workers from compose --profile workers

Usage:
    docker compose -f tests/docker-compose.test.yml --profile workers up -d worker-1
    docker compose -f tests/docker-compose.test.yml run --rm test-runner \\
        python -m pytest tests/integration/test_distributed_system_full.py -v -m distributed

Notes:
    - mock-small replies ``You said: <prompt>`` — assert echo, not topic terms
    - Isolated async Redis prevents ``Event loop is closed`` on stream reads
"""

from __future__ import annotations

import pytest

from tests.integration.conftest import sse_assembled_text, sse_event_names

pytestmark = [
    pytest.mark.integration,
    pytest.mark.distributed,
    pytest.mark.requires_redis,
]


@pytest.fixture(autouse=True)
def _async_redis(isolated_async_redis):
    """Stream reads need connections bound to this test's event loop."""


@pytest.fixture(autouse=True)
def _workers(ready_celery_workers):
    """Skip unless a Celery worker is ready."""


async def test_basic_chat_distributed(native_chat_client) -> None:
    """Non-streaming native chat returns the mock echo from a worker."""
    prompt = "What is 2+2?"
    response = await native_chat_client.post(
        "/api/v1/chat",
        json={"messages": [{"role": "user", "content": prompt}], "stream": False},
    )
    assert response.status_code == 200, response.text
    content = (response.json().get("content") or "").lower()
    assert content
    assert prompt.lower() in content or "you said:" in content


async def test_streaming_chat_distributed(native_chat_client) -> None:
    """Streaming native chat is SSE and carries the mock echo."""
    prompt = "Count from 1 to 5."
    response = await native_chat_client.post(
        "/api/v1/chat",
        json={"messages": [{"role": "user", "content": prompt}], "stream": True},
    )
    assert response.status_code == 200, response.text
    assert "text/event-stream" in response.headers.get("content-type", "")

    events = sse_event_names(response.text)
    assert "error" not in events, response.text
    assert "token" in events or "end" in events, response.text
    text = sse_assembled_text(response.text)
    assert prompt in text or "You said:" in text, text


async def test_longer_prompt_still_echoes_on_worker(native_chat_client) -> None:
    """A longer prompt still completes on a worker (mock echo, not an essay)."""
    prompt = "Explain photosynthesis and its importance for climate. Use simple terms."
    response = await native_chat_client.post(
        "/api/v1/chat",
        json={"messages": [{"role": "user", "content": prompt}], "stream": False},
    )
    assert response.status_code == 200, response.text
    content = response.json().get("content") or ""
    assert "You said:" in content
    assert "photosynthesis" in content.lower()


async def test_invalid_chat_payload_is_422(native_chat_client) -> None:
    """Malformed chat bodies fail validation before a worker is involved."""
    response = await native_chat_client.post("/api/v1/chat", json={"invalid": "request"})
    assert response.status_code == 422


async def test_math_eval_runs_on_worker(native_chat_client) -> None:
    """POST /api/v1/tools/execute dispatches core.math_eval to a worker."""
    response = await native_chat_client.post(
        "/api/v1/tools/execute",
        json={"name": "core.math_eval", "params": {"expression": "2 + 2 * 3"}},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    text = str(body)
    assert "8" in text or body.get("result") == 8 or body.get("value") == 8, body
