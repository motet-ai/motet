"""
Motet - Redis Manager Pool Configuration Tests

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-24

Description:
    Unit tests for Redis connection pool sizing, pub/sub pool separation, and
    GLIDE health-check eviction that must not close the shared sync client.
"""

from motet.core.constants import REDIS_MAX_CONNECTIONS
from motet.core.distributed.glide_backend import SyncGlideRedisAdapter
from motet.core.distributed.redis_manager import (
    UnifiedRedisManager,
    warn_if_redis_pool_below_concurrency,
)


def test_resolve_max_connections_from_env(monkeypatch) -> None:
    monkeypatch.setenv("MOTET_REDIS_MAX_CONNECTIONS", "200")
    monkeypatch.setenv("MOTET_REDIS_PUBSUB_MAX_CONNECTIONS", "48")
    manager = UnifiedRedisManager()
    assert manager.config.max_connections == 200
    assert manager.config.pubsub_max_connections == 48


def test_resolve_max_connections_invalid_env_falls_back(monkeypatch) -> None:
    monkeypatch.setenv("MOTET_REDIS_MAX_CONNECTIONS", "not-a-number")
    manager = UnifiedRedisManager()
    assert manager.config.max_connections == REDIS_MAX_CONNECTIONS


def test_pubsub_client_uses_dedicated_pool(monkeypatch) -> None:
    monkeypatch.delenv("MOTET_REDIS_URL", raising=False)
    manager = UnifiedRedisManager(
        config=UnifiedRedisManager()._get_default_config()
    )
    manager.initialize()
    command_pool = manager.get_client("commands").connection_pool
    pubsub_pool = manager.get_pubsub_client("events").connection_pool
    assert command_pool is not pubsub_pool


def test_warn_if_redis_pool_below_concurrency() -> None:
    assert (
        warn_if_redis_pool_below_concurrency(
            max_connections=1250, concurrency=1000
        )
        is False
    )
    assert (
        warn_if_redis_pool_below_concurrency(
            max_connections=1000, concurrency=1000
        )
        is True
    )
    assert (
        warn_if_redis_pool_below_concurrency(
            max_connections=500, concurrency=1000
        )
        is True
    )
    assert (
        warn_if_redis_pool_below_concurrency(
            max_connections=1250, concurrency=0
        )
        is False
    )


def test_health_check_sync_does_not_close_shared_glide() -> None:
    class Shared:
        def __init__(self) -> None:
            self.closed = False

        def close(self) -> None:
            self.closed = True

    manager = UnifiedRedisManager()
    manager._initialized = True
    manager._valkey_backend = "glide"
    shared = Shared()
    manager._shared_sync_glide = shared

    adapter = SyncGlideRedisAdapter(shared)
    adapter.ping = lambda: (_ for _ in ()).throw(TimeoutError("timed out"))  # type: ignore[method-assign]
    manager._sync_clients["default"] = adapter

    assert manager.health_check_sync("default") is False
    assert shared.closed is False
    assert "default" not in manager._sync_clients
    assert manager._shared_sync_glide is shared
