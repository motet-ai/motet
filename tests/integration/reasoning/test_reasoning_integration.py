"""
Motet - Native Reasoning Distributed Tests

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-14

Description:
    Lane C-workers coverage for a reasoning-style native chat turn dispatched
    to Celery. Asserts the mock echo, not live-model arithmetic. Live-provider
    CoT/tool checks live in ``test_reasoning_live.py``.

Dependencies:
    - tests.integration.conftest: native_chat_client, ready workers, Redis reset

Usage:
    docker compose -f tests/docker-compose.test.yml --profile workers up -d worker-1
    docker compose -f tests/docker-compose.test.yml run --rm test-runner \\
        python -m pytest tests/integration/reasoning/test_reasoning_integration.py -v -m distributed

Notes:
    - mock-small replies ``You said: <prompt>``
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


async def test_reasoning_style_chat_runs_on_worker(native_chat_client) -> None:
    """A longer reasoning prompt still completes on a worker as a mock echo."""
    prompt = "Think step by step: what is 2+3*4?"
    response = await native_chat_client.post(
        "/api/v1/chat",
        json={"messages": [{"role": "user", "content": prompt}], "stream": False},
    )
    assert response.status_code == 200, response.text
    content = response.json().get("content") or ""
    assert "You said:" in content
    assert "2+3*4" in content


async def test_reasoning_style_stream_has_no_error(native_chat_client) -> None:
    """Streaming the same turn yields token/end events, not an error frame."""
    prompt = "Think step by step: math 10/2"
    response = await native_chat_client.post(
        "/api/v1/chat",
        json={"messages": [{"role": "user", "content": prompt}], "stream": True},
    )
    assert response.status_code == 200, response.text
    events = sse_event_names(response.text)
    assert "error" not in events, response.text
    assert "token" in events or "end" in events, response.text
    text = sse_assembled_text(response.text)
    assert "10/2" in text or "You said:" in text, text
