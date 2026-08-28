"""
Motet - Native Chat WebSocket Distributed Tests

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-14

Description:
    Lane C coverage for ``/api/v1/chat/ws`` against real Celery workers.
    Native HTTP SSE lives in ``test_sse_streaming.py``; this file only
    covers the WebSocket seam.

Dependencies:
    - tests.integration.conftest: native_chat_app, ready workers, Redis reset
    - starlette.testclient: WebSocket client

Usage:
    docker compose -f tests/docker-compose.test.yml --profile workers up -d worker-1
    docker compose -f tests/docker-compose.test.yml run --rm test-runner \\
        python -m pytest tests/integration/streaming/test_websocket_streaming.py -v -m distributed

Notes:
    - mock-small replies ``You said: <prompt>``
    - An error frame is a failure, not a pass
"""

from __future__ import annotations

import uuid

import pytest
from starlette.testclient import TestClient

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


def test_ws_chat_echoes_prompt_from_worker(native_chat_app) -> None:
    """A streamed WebSocket turn returns the mock echo from a worker."""
    prompt = "distributed ws ping"
    headers = {
        "X-Principal-Id": f"dist-ws-{uuid.uuid4().hex[:8]}",
        "X-Tenant-Id": f"tenant-ws-{uuid.uuid4().hex[:8]}",
    }
    with TestClient(native_chat_app) as client:
        with client.websocket_connect("/api/v1/chat/ws", headers=headers) as ws:
            ws.send_json(
                {
                    "messages": [{"role": "user", "content": prompt}],
                    "stream": True,
                }
            )
            parts: list[str] = []
            saw_end = False
            for _ in range(40):
                msg = ws.receive_json()
                if msg.get("error") or msg.get("event") == "error":
                    raise AssertionError(f"worker stream error: {msg}")
                token = msg.get("token")
                if isinstance(token, str):
                    parts.append(token)
                content = msg.get("content")
                if isinstance(content, str):
                    parts.append(content)
                if msg.get("event") == "end":
                    saw_end = True
                    break

    text = "".join(parts)
    assert saw_end or text, "no token or end frame from websocket"
    assert prompt in text or "You said:" in text, text
