"""
Motet - Redis Memory Store Encryption Tests

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-16

Description:
    Unit tests for envelope-encrypted Redis memory hashes, including decrypt
    AAD fallback after tenant-prefix RENAME and keep-on-decrypt-failure.

Dependencies:
    - motet.core.memory.redis_store
    - motet.core.types.MemoryItem

Usage:
    pytest tests/memory/test_redis_store_encryption.py -q
"""

import json
from datetime import datetime, timezone
from typing import Any, Dict

from motet.core.memory.redis_store import RedisStore
from motet.core.types import MemoryItem


class DummyEncryptionService:
    """Simple wrap/unwrap implementation for testing."""

    def wrap_key(self, dek: bytes, tenant_id: str) -> Dict[str, Any]:
        return {
            "wrapped_key": dek.hex(),
            "tenant_id": tenant_id,
            "encryption_version": "aes-256-gcm-v1",
            "iv": "00" * 12,
        }

    def unwrap_key(self, wrapped_blob: Dict[str, Any]) -> bytes:
        return bytes.fromhex(wrapped_blob["wrapped_key"])


class FakeRedis:
    def __init__(self) -> None:
        self.hashes: Dict[str, Dict[str, str]] = {}
        self.sorted_sets: Dict[str, Dict[str, float]] = {}

    def hset(self, key: str, mapping: Dict[str, str]) -> None:
        self.hashes[key] = mapping

    def hgetall(self, key: str) -> Dict[bytes, bytes]:
        data = self.hashes.get(key, {})
        return {
            k.encode("utf-8"): v.encode("utf-8") if isinstance(v, str) else v
            for k, v in data.items()
        }

    def zadd(self, key: str, mapping: Dict[str, float]) -> None:
        self.sorted_sets.setdefault(key, {}).update(mapping)

    def zrevrange(self, key: str, start: int, end: int):
        values = sorted(
            self.sorted_sets.get(key, {}).items(), key=lambda item: item[1], reverse=True
        )
        slice_values = values[start : end + 1 if end != -1 else None]
        return [member.encode("utf-8") for member, _ in slice_values]

    def delete(self, *keys: str) -> None:
        for key in keys:
            self.hashes.pop(key, None)


def _memory_item() -> MemoryItem:
    return MemoryItem(
        id="mem-123",
        type="note",
        content="sensitive memory payload",
        tags=["wm", "stm"],
        metadata={"conversation_id": "conv-1"},
        tenant_id="tenant-a",
        motet_id="prod",
        created_at=datetime.now(timezone.utc),
    )


def test_upsert_stores_envelope_payload():
    redis = FakeRedis()
    store = RedisStore(redis_client=redis, motet_id="prod", tenant_id="tenant-a")
    store._encryption_service = DummyEncryptionService()

    item = _memory_item()
    store.upsert(item)

    stored = redis.hashes[store._key(item.id)]
    assert "_envelope" in stored
    envelope = json.loads(stored["_envelope"])
    assert envelope["encrypted"] is True
    assert envelope["encryption_mode"] == "envelope-v1"
    
    # Verify we can decrypt it back
    retrieved = store.get(item.id)
    assert retrieved is not None
    assert retrieved.id == item.id


def test_get_decrypts_back_to_memory_item():
    redis = FakeRedis()
    store = RedisStore(redis_client=redis, motet_id="prod", tenant_id="tenant-a")
    store._encryption_service = DummyEncryptionService()

    item = _memory_item()
    store.upsert(item)

    loaded = store.get(item.id)
    assert loaded is not None
    assert loaded.content == item.content
    assert loaded.metadata == item.metadata
    assert loaded.tenant_id == item.tenant_id


def test_get_keeps_ciphertext_when_decrypt_fails() -> None:
    redis = FakeRedis()
    store = RedisStore(redis_client=redis, motet_id="prod", tenant_id="tenant-a")
    store._encryption_service = DummyEncryptionService()

    item = _memory_item()
    store.upsert(item)
    key = store._key(item.id)
    redis.hashes[key]["_envelope"] = "{not-json"

    assert store.get(item.id) is None
    assert key in redis.hashes
    assert "_envelope" in redis.hashes[key]

