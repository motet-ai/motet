"""
Motet - API and Worker Readiness Distributed Tests

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-14

Description:
    Lane C-workers health coverage. API ``/health`` is in-process; worker
    liveness is ``WorkerReadinessService``, not HTTP to typed
    reasoning/tool/model worker URLs (that topology is gone).

Dependencies:
    - tests.integration.conftest: native_chat_client, ready workers
    - motet.core.distributed.worker_readiness: ready-worker registry

Usage:
    docker compose -f tests/docker-compose.test.yml --profile workers up -d worker-1
    docker compose -f tests/docker-compose.test.yml run --rm test-runner \\
        python -m pytest tests/integration/test_health_monitoring.py -v -m distributed

Notes:
    - Prometheus / Grafana / HAProxy checks were removed; they targeted a
      stack this compose file does not run
"""

from __future__ import annotations

import pytest

from motet.core.distributed.worker_readiness import WorkerReadinessService

pytestmark = [
    pytest.mark.integration,
    pytest.mark.distributed,
    pytest.mark.health,
]


@pytest.fixture(autouse=True)
def _async_redis(isolated_async_redis):
    """Readiness reads Redis on this test's event loop."""


async def test_api_health(native_chat_client) -> None:
    """The HTTP app reports an overall health document."""
    response = await native_chat_client.get("/health")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body.get("status") in {"ok", "degraded", "unhealthy"}
    components = body.get("components") or {}
    for name in ("orchestrator", "memory", "vector"):
        assert name in components, body
        assert isinstance(components[name], bool), body


def test_workers_are_ready(ready_celery_workers) -> None:
    """Compose workers have registered ready in Redis."""
    assert ready_celery_workers
    live = WorkerReadinessService().get_ready_workers()
    assert live, "WorkerReadinessService returned no ready workers"
    assert set(ready_celery_workers) <= set(live)
