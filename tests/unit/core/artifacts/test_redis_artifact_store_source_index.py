"""
Motet - Redis Artifact Store Source Index Tests

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-17

Description:
    Unit tests for ADR-0062 derived artifact lookup by `source_artifact_id`.

    The key behavior validated here is that looking up a derived artifact by
    (tenant_id, kind, source_artifact_id) is correct even when the most recent
    derived artifact of that kind belongs to a different source. This is enforced
    by a dedicated Redis ZSET index:

        idx:art:tenant:{tenant_id}:source:{source_id}:kind:{kind}

Dependencies:
    - pytest: test runner
    - unittest.mock: patching Redis and config dependencies
    - motet.core.artifacts.redis_artifact_store: system under test

Usage:
    pytest tests/unit/core/artifacts/test_redis_artifact_store_source_index.py
"""

from __future__ import annotations

import base64
from typing import Any, Dict
from unittest.mock import patch

import pytest


class FakeRedis:
    def __init__(self) -> None:
        self.hashes: Dict[str, Dict[str, str]] = {}
        self.ttls: Dict[str, int] = {}
        self.zsets: Dict[str, Dict[str, float]] = {}

    def hset(self, key: str, mapping: Dict[str, str]) -> None:
        self.hashes.setdefault(key, {}).update(mapping)

    def exists(self, key: str) -> int:
        return 1 if key in self.hashes or key in self.zsets else 0

    def hgetall(self, key: str) -> Dict[str, str]:
        return dict(self.hashes.get(key, {}))

    def expire(self, key: str, ttl_seconds: int) -> None:
        self.ttls[key] = int(ttl_seconds)

    def zadd(self, key: str, mapping: Dict[str, float]) -> None:
        z = self.zsets.setdefault(key, {})
        for member, score in mapping.items():
            z[str(member)] = float(score)

    def zrevrange(self, key: str, start: int, stop: int):
        z = self.zsets.get(key, {})
        items = sorted(z.items(), key=lambda kv: kv[1], reverse=True)
        members = [m for m, _ in items]
        return members[int(start) : int(stop) + 1]

    def zrem(self, key: str, member: str) -> None:
        z = self.zsets.get(key, {})
        z.pop(str(member), None)

    def delete(self, key: str) -> int:
        existed = 1 if key in self.hashes else 0
        self.hashes.pop(key, None)
        self.ttls.pop(key, None)
        return existed


class DummyEncryptionService:
    def wrap_key(self, dek: bytes, tenant_id: str) -> Dict[str, Any]:
        return {
            "wrapped_key": base64.b64encode(dek).decode("ascii"),
            "iv": base64.b64encode(b"0123456789ab").decode("ascii"),
            "tenant_id": tenant_id,
            "encryption_version": "aes-256-gcm-v1",
        }

    def unwrap_key(self, wrapped_blob: Dict[str, Any]) -> bytes:
        return base64.b64decode(wrapped_blob["wrapped_key"])


class DummyConfig:
    artifact_store_encryption: bool = True
    artifact_store_max_bytes: int = 25_000_000
    artifact_store_ttl_seconds: int | None = None


def _make_store(fake_redis: FakeRedis):
    from motet.core.security import encrypted_payload_store as eps

    eps._sync_stores.pop("artifact_store_test", None)
    with patch("motet.core.distributed.redis_manager.get_sync_redis_client", return_value=fake_redis), patch(
        "motet.core.artifacts.redis_artifact_store.get_sync_redis_client", return_value=fake_redis
    ), patch(
        "motet.core.security.encryption_service.get_encryption_service", return_value=DummyEncryptionService()
    ), patch(
        "motet.core.artifacts.redis_artifact_store.Config", return_value=DummyConfig()
    ):
        from motet.core.artifacts.redis_artifact_store import RedisArtifactStore

        return RedisArtifactStore(service_name="artifact_store_test")


def test_list_by_source_artifact_id_is_correct_when_other_sources_exist():
    """
    Ensure list(kind=DERIVED_TEXT, source_artifact_id=..., limit=1) returns the correct artifact
    even if a newer DERIVED_TEXT exists for a different source.
    """
    redis = FakeRedis()
    store = _make_store(redis)

    from motet.core.artifacts.types import ArtifactKind

    tenant_id = "tenant-a"
    principal_id = "principal-1"
    motet_id = "default"

    # Create older derived text for source A
    source_a = "source-a"
    derived_a = store.put(
        payload="a",
        content_type="text/plain",
        kind=ArtifactKind.DERIVED_TEXT,
        source_artifact_id=source_a,
        tenant_id=tenant_id,
        principal_id=principal_id,
        motet_id=motet_id,
    )

    # Create newer derived text for source B (should not affect query for source A)
    source_b = "source-b"
    store.put(
        payload="b",
        content_type="text/plain",
        kind=ArtifactKind.DERIVED_TEXT,
        source_artifact_id=source_b,
        tenant_id=tenant_id,
        principal_id=principal_id,
        motet_id=motet_id,
    )

    results = store.list(
        kind=ArtifactKind.DERIVED_TEXT,
        source_artifact_id=source_a,
        limit=1,
        tenant_id=tenant_id,
        principal_id=principal_id,
        motet_id=motet_id,
    )

    assert len(results) == 1
    assert results[0].id == derived_a
    assert results[0].source_artifact_id == source_a


