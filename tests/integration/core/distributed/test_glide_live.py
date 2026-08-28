"""
Motet - Valkey GLIDE Live Integration Tests

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-17

Description:
    Hits live Valkey through the real GLIDE client (not a fake adapter).
    Covers resolve + UnifiedRedisManager wiring and set/get/hash/zset.

Dependencies:
    - pytest
    - valkey-glide / valkey-glide-sync (skip if missing)
    - Live Valkey via MOTET_REDIS_URL (requires_redis)

Usage:
    pytest tests/integration/core/distributed/test_glide_live.py -q

Notes:
    - Rebuild the test-runner image after adding the GLIDE wheels.
    - Does not flip the rest of the suite to MOTET_VALKEY_CLIENT=glide.
"""

from __future__ import annotations

import os
import uuid
from typing import Any, List

import pytest

from motet.core.distributed.glide_backend import (
    SyncGlideRedisAdapter,
    create_sync_glide_adapter,
    resolve_valkey_client_backend,
)
from motet.core.distributed.redis_manager import RedisConfig, UnifiedRedisManager


def _require_glide() -> None:
    pytest.importorskip("glide")
    pytest.importorskip("glide_sync")


def _redis_url() -> str:
    return os.environ.get("MOTET_REDIS_URL", "redis://localhost:6379/0")


def _cleanup(client: Any, keys: List[str]) -> None:
    try:
        if keys:
            client.delete(*keys)
    except Exception:
        pass
    closer = getattr(client, "close", None)
    if callable(closer):
        try:
            closer()
        except Exception:
            pass


def _round_trip(client: Any, prefix: str) -> None:
    str_key = f"{prefix}:str"
    hash_key = f"{prefix}:hash"
    zset_key = f"{prefix}:zset"
    lock_key = f"{prefix}:lock"

    assert client.ping() is True
    assert client.set(str_key, "hello") is True
    assert client.get(str_key) == "hello"

    assert client.set(lock_key, "owner", nx=True, ex=30) is True
    assert client.set(lock_key, "other", nx=True, ex=30) is None
    assert client.get(lock_key) == "owner"

    assert client.hset(hash_key, mapping={"tenant_id": "acme", "motet_id": "default"}) == 2
    assert client.hgetall(hash_key) == {"tenant_id": "acme", "motet_id": "default"}

    assert client.zadd(zset_key, {"a": 1.0, "b": 2.0}) == 2
    assert client.zrevrange(zset_key, 0, -1) == ["b", "a"]
    assert client.exists(str_key, hash_key, zset_key, lock_key) == 4
    assert client.delete(str_key, lock_key) == 2


@pytest.mark.integration
@pytest.mark.requires_redis
def test_live_glide_adapter_set_get_hash_zset() -> None:
    """Real GlideClient against the test Valkey: set/get, NX, hash, zset."""
    _require_glide()
    prefix = f"glide-live:{uuid.uuid4().hex}"
    client = create_sync_glide_adapter(_redis_url(), decode_responses=True)
    keys = [f"{prefix}:str", f"{prefix}:hash", f"{prefix}:zset", f"{prefix}:lock"]
    try:
        _round_trip(client, prefix)
    finally:
        _cleanup(client, keys)


@pytest.mark.integration
@pytest.mark.requires_redis
def test_live_unified_manager_uses_glide(monkeypatch: pytest.MonkeyPatch) -> None:
    """UnifiedRedisManager must hand back a live GLIDE adapter when opted in."""
    _require_glide()
    monkeypatch.setenv("MOTET_VALKEY_CLIENT", "glide")
    from motet.core.distributed import glide_backend as gb

    gb._GLIDE_UNAVAILABLE_LOGGED = False
    assert resolve_valkey_client_backend() == "glide"

    prefix = f"glide-mgr:{uuid.uuid4().hex}"
    manager = UnifiedRedisManager(RedisConfig(url=_redis_url()))
    manager.initialize()
    assert manager._valkey_backend == "glide"
    client = manager.get_sync_client("glide_live")
    assert isinstance(client, SyncGlideRedisAdapter)
    keys = [f"{prefix}:str", f"{prefix}:hash", f"{prefix}:zset", f"{prefix}:lock"]
    try:
        _round_trip(client, prefix)
    finally:
        _cleanup(client, keys)
