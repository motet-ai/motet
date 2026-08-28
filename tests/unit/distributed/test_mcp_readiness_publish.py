"""
Motet - unit tests for MCP watcher readiness publishing

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-04-06

Description:
    Verifies pool-aware rules for pushing MCP tool counts to WorkerReadinessService.

Dependencies:
    - pytest
"""

from __future__ import annotations

import pytest


def test_should_publish_gevent_pool(monkeypatch: pytest.MonkeyPatch) -> None:
    from motet.core.workers import worker_utils

    monkeypatch.setattr(worker_utils, "detect_worker_pool_type", lambda: "gevent")

    from motet.core.distributed.worker_mcp_startup import _should_publish_mcp_readiness_to_redis

    assert _should_publish_mcp_readiness_to_redis() is True


def test_should_publish_fork_child(monkeypatch: pytest.MonkeyPatch) -> None:
    from motet.core.workers import parent_coordinator
    from motet.core.workers import worker_utils

    monkeypatch.setattr(worker_utils, "detect_worker_pool_type", lambda: "fork")
    monkeypatch.setattr(parent_coordinator, "is_celery_parent_process", lambda: False)

    from motet.core.distributed.worker_mcp_startup import _should_publish_mcp_readiness_to_redis

    assert _should_publish_mcp_readiness_to_redis() is True


def test_should_not_publish_fork_parent(monkeypatch: pytest.MonkeyPatch) -> None:
    from motet.core.workers import parent_coordinator
    from motet.core.workers import worker_utils

    monkeypatch.setattr(worker_utils, "detect_worker_pool_type", lambda: "fork")
    monkeypatch.setattr(parent_coordinator, "is_celery_parent_process", lambda: True)

    from motet.core.distributed.worker_mcp_startup import _should_publish_mcp_readiness_to_redis

    assert _should_publish_mcp_readiness_to_redis() is False


def test_mcp_watcher_parallel_max_workers_default() -> None:
    from motet.core.distributed.worker_mcp_startup import _mcp_watcher_parallel_max_workers

    assert _mcp_watcher_parallel_max_workers(100) == 1
    assert _mcp_watcher_parallel_max_workers(5) == 1
    assert _mcp_watcher_parallel_max_workers(1) == 1


def test_mcp_watcher_parallel_max_workers_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MOTET_MCP_WATCHER_DISCOVERY_MAX_WORKERS", "3")
    from motet.core.distributed.worker_mcp_startup import _mcp_watcher_parallel_max_workers

    assert _mcp_watcher_parallel_max_workers(10) == 3
    assert _mcp_watcher_parallel_max_workers(2) == 2


def test_discover_services_parallel_invokes_discover_per_service(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    def fake_discover(sid: str, _registry: object, _wid: str) -> int:
        calls.append(sid)
        return 1 if sid == "b" else 0

    monkeypatch.setattr(
        "motet.core.distributed.worker_mcp_startup._discover_and_register_tools_for_service",
        fake_discover,
    )
    from motet.core.distributed.worker_mcp_startup import _discover_services_parallel

    reg: set[str] = set()
    cfg: set[str] = set()
    _discover_services_parallel(["a", "b", "c"], object(), "worker1", reg, cfg, phase="catchup")

    assert set(calls) == {"a", "b", "c"}
    assert reg == {"b"}
    assert cfg == {"a", "b", "c"}
