"""
Motet - Worker Event Bus Distributed Tests

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-19

Description:
    Lane C coverage for Redis pub/sub event delivery. A native chat turn
    dispatched to a Celery worker must publish onto
    ``{tenant}:events:channel`` (issue #233). Tests ``PSUBSCRIBE
    *:events:channel`` so tenant and leftover platform publishes are visible.

Dependencies:
    - tests.integration.conftest: native_chat_client, ready workers, Redis reset
    - motet.core.distributed.redis_manager: pub/sub client

Usage:
    docker compose -f tests/docker-compose.test.yml --profile workers up -d worker-1
    docker compose -f tests/docker-compose.test.yml run --rm test-runner \\
        python -m pytest tests/integration/events/test_event_delivery.py -v -m distributed

Notes:
    - Subscribe before the chat POST so the first publish is not missed
    - mock-small is enough; command start/complete events do not need a live LLM
"""

from __future__ import annotations

import json
import time
from typing import Any, Dict, List

import pytest

from motet.core.distributed.redis_manager import get_pubsub_redis_client
from motet.core.distributed.tenant_keys import EVENT_BUS_PSUBSCRIBE_PATTERN

EVENTS_PATTERN = EVENT_BUS_PSUBSCRIBE_PATTERN

pytestmark = [
    pytest.mark.integration,
    pytest.mark.distributed,
    pytest.mark.requires_redis,
]


@pytest.fixture(autouse=True)
def _async_redis(isolated_async_redis):
    """Pub/sub connections must bind to this test's event loop."""


@pytest.fixture(autouse=True)
def _workers(ready_celery_workers):
    """Skip unless a Celery worker is ready."""


async def test_worker_chat_publishes_events_on_redis(native_chat_client) -> None:
    """A worker chat turn publishes at least one event on a tenant channel."""
    redis = get_pubsub_redis_client("test_event_delivery")
    pubsub = redis.pubsub()
    await pubsub.psubscribe(EVENTS_PATTERN)
    try:
        # Drain the psubscribe acknowledgement so we only collect real publishes.
        await pubsub.get_message(timeout=0.5)

        prompt = "event delivery ping"
        response = await native_chat_client.post(
            "/api/v1/chat",
            json={
                "messages": [{"role": "user", "content": prompt}],
                "stream": False,
            },
        )
        assert response.status_code == 200, response.text
        assert prompt in (response.json().get("content") or "")

        seen: List[Dict[str, Any]] = []
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline:
            message = await pubsub.get_message(timeout=0.5)
            if not message or message.get("type") not in ("message", "pmessage"):
                continue
            raw = message.get("data")
            if isinstance(raw, bytes):
                raw = raw.decode("utf-8")
            if not isinstance(raw, str):
                continue
            try:
                payload = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if isinstance(payload, dict):
                seen.append(payload)
                break

        assert seen, f"no events on {EVENTS_PATTERN} after worker chat"
    finally:
        await pubsub.punsubscribe(EVENTS_PATTERN)
