"""
Motet - Redis Artifact Store Encryption Tests

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-17

Description:
    Unit tests for ADR-0061 ToolArtifact storage when backed by Redis and protected by
    ADR-0056 envelope encryption. These tests assert:
      - ToolArtifacts are stored with an `_envelope` field (no plaintext payload fallback)
      - Access control checks (tenant/principal/motet) are enforced before decrypt
      - Byte payloads are supported via base64 encoding inside the encrypted wrapper

Dependencies:
    - pytest: test runner
    - unittest.mock: patching Redis and encryption service dependencies
    - motet.core.artifacts.redis_artifact_store: system under test

Usage:
    pytest tests/unit/core/artifacts/test_redis_artifact_store_encryption.py
"""

from __future__ import annotations

import base64
from typing import Any, Dict, Optional
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
        # Redis zrevrange is inclusive of stop
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
    """Minimal KEK wrapper used by envelope_helper (wrap/unwrap only)."""

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

    # Process-global cache would otherwise reuse another test's Redis client.
    eps._sync_stores.pop("artifact_store_test", None)
    # Patch both artifact store and encrypted payload store redis client acquisition.
    with patch("motet.core.distributed.redis_manager.get_sync_redis_client", return_value=fake_redis), patch(
        "motet.core.artifacts.redis_artifact_store.get_sync_redis_client", return_value=fake_redis
    ), patch(
        "motet.core.security.encryption_service.get_encryption_service", return_value=DummyEncryptionService()
    ), patch(
        "motet.core.artifacts.redis_artifact_store.Config", return_value=DummyConfig()
    ):
        from motet.core.artifacts.redis_artifact_store import RedisArtifactStore

        return RedisArtifactStore(service_name="artifact_store_test")


def test_put_stores_envelope_not_plaintext_payload():
    redis = FakeRedis()
    store = _make_store(redis)

    artifact_id = store.put(
        payload={"hello": "world"},
        content_type="application/json",
        tenant_id="tenant-a",
        principal_id="principal-1",
        motet_id="default",
        ttl_seconds=123,
    )

    key = f"tenant-a:art:{artifact_id}"
    stored = redis.hashes[key]

    assert "_envelope" in stored
    assert "tenant_id" in stored and stored["tenant_id"] == "tenant-a"
    assert redis.ttls[key] == 123
    # Plaintext payload should never be present as a top-level field.
    assert "payload" not in stored


def test_get_enforces_tenant_before_decrypt():
    redis = FakeRedis()
    store = _make_store(redis)

    artifact_id = store.put(
        payload={"secret": "data"},
        content_type="application/json",
        tenant_id="tenant-a",
        principal_id="principal-1",
        motet_id="default",
    )

    # If decryption were attempted, this mock would raise.
    with patch("motet.core.security.encrypted_payload_store.envelope_decrypt_bytes") as mock_decrypt:
        mock_decrypt.side_effect = AssertionError("decrypt should not be called on tenant mismatch")
        result = store.get(artifact_id, tenant_id="tenant-b", principal_id="principal-1", motet_id="default")
        assert result is None


def test_bytes_payload_roundtrip_returns_bytes():
    redis = FakeRedis()
    store = _make_store(redis)

    payload = b"\x00\x01binary"
    artifact_id = store.put(
        payload=payload,
        content_type="application/octet-stream",
        tenant_id="tenant-a",
        principal_id="principal-1",
        motet_id="default",
    )

    loaded = store.get(artifact_id, tenant_id="tenant-a", principal_id="principal-1", motet_id="default")
    assert loaded == payload
    assert f"art:{artifact_id}" not in redis.ttls
    assert f"tenant-a:art:{artifact_id}" not in redis.ttls


def test_update_metadata_merges_without_changing_payload():
    redis = FakeRedis()
    store = _make_store(redis)

    artifact_id = store.put(
        payload={"hello": "world"},
        content_type="application/json",
        metadata={"filename": "sample.json"},
        tenant_id="tenant-a",
        principal_id="principal-1",
        motet_id="default",
    )

    updated = store.update_metadata(
        artifact_id,
        {"artifact_indexing_enabled": False},
        tenant_id="tenant-a",
        principal_id="principal-1",
        motet_id="default",
    )

    assert updated is not None
    assert updated.metadata["filename"] == "sample.json"
    assert updated.metadata["artifact_indexing_enabled"] is False
    assert store.get(artifact_id, tenant_id="tenant-a", principal_id="principal-1", motet_id="default") == {
        "hello": "world"
    }


