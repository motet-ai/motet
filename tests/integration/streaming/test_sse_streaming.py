"""
Motet - Native Chat SSE Distributed Tests

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-14

Description:
    Lane C coverage for POST /api/v1/chat?stream=true against real Celery
    workers. Asserts Motet SSE event names and that mock-small echo text
    crosses the process boundary as decrypted stream frames.

Dependencies:
    - tests.integration.conftest: native_chat_client, ready workers, Redis reset
    - Celery workers from compose --profile workers

Usage:
    docker compose -f tests/docker-compose.test.yml --profile workers up -d worker-1
    docker compose -f tests/docker-compose.test.yml run --rm test-runner \\
        python -m pytest tests/integration/streaming/test_sse_streaming.py -v -m distributed

Notes:
    - mock-small replies ``You said: <prompt>``
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


async def test_sse_chat_emits_token_or_end(native_chat_client) -> None:
    """A streamed native chat turn produces Motet SSE token/end events."""
    response = await native_chat_client.post(
        "/api/v1/chat",
        json={
            "messages": [{"role": "user", "content": "math: 1+1"}],
            "stream": True,
        },
    )
    assert response.status_code == 200, response.text
    assert "text/event-stream" in response.headers.get("content-type", "")

    events = sse_event_names(response.text)
    assert "error" not in events, response.text
    assert "token" in events or "end" in events, response.text


async def test_sse_chat_echoes_prompt_from_worker(native_chat_client) -> None:
    """Worker-written token frames decrypt to the mock echo of the prompt."""
    prompt = "distributed stream ping"
    response = await native_chat_client.post(
        "/api/v1/chat",
        json={
            "messages": [{"role": "user", "content": prompt}],
            "stream": True,
        },
    )
    assert response.status_code == 200, response.text
    events = sse_event_names(response.text)
    assert "error" not in events, response.text

    text = sse_assembled_text(response.text)
    assert prompt in text, f"expected mock echo of {prompt!r} in {text!r}"
