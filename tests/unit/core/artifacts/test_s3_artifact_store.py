"""
Motet - S3-Compatible Artifact Store Tests

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-06-18

Description:
    Unit tests for the S3-compatible artifact store backend. These tests verify
    encrypted payload round-tripping, Redis-backed metadata indexes, and backend
    factory selection without requiring a live S3 or SeaweedFS service.

Dependencies:
    - pytest: test runner
    - unittest.mock: factory selection patching
    - motet.core.artifacts.s3_artifact_store: system under test

Usage:
    pytest tests/unit/core/artifacts/test_s3_artifact_store.py
"""

from __future__ import annotations

import base64
import io
from typing import Any, Dict
from unittest.mock import patch


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
        zset = self.zsets.setdefault(key, {})
        for member, score in mapping.items():
            zset[str(member)] = float(score)

    def zrevrange(self, key: str, start: int, stop: int):
        zset = self.zsets.get(key, {})
        items = sorted(zset.items(), key=lambda item: item[1], reverse=True)
        members = [member for member, _ in items]
        return members[int(start) : int(stop) + 1]

    def zrem(self, key: str, member: str) -> None:
        self.zsets.get(key, {}).pop(str(member), None)

    def delete(self, key: str) -> int:
        existed = 1 if key in self.hashes else 0
        self.hashes.pop(key, None)
        self.ttls.pop(key, None)
        return existed


class FakeS3Client:
    def __init__(self) -> None:
        self.objects: Dict[tuple[str, str], bytes] = {}
        self.deleted: list[tuple[str, str]] = []
        self.put_kwargs: Dict[str, Dict[str, Any]] = {}
        self.get_calls: list[Dict[str, Any]] = []

    def put_object(self, *, Bucket: str, Key: str, Body: bytes, **kwargs: Any) -> None:
        self.objects[(Bucket, Key)] = Body
        self.put_kwargs[Key] = dict(kwargs)

    def get_object(self, *, Bucket: str, Key: str, Range: str | None = None, **kwargs: Any) -> Dict[str, Any]:
        self.get_calls.append({"Key": Key, "Range": Range})
        data = self.objects[(Bucket, Key)]
        if Range:
            spec = Range.replace("bytes=", "")
            start_str, end_str = spec.split("-", 1)
            start, end = int(start_str), int(end_str)
            data = data[start : end + 1]
        return {"Body": io.BytesIO(data)}

    def delete_object(self, *, Bucket: str, Key: str) -> None:
        self.deleted.append((Bucket, Key))
        self.objects.pop((Bucket, Key), None)


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


class DummyS3Config:
    artifact_store_backend = "s3"
    artifact_store_encryption = True
    artifact_store_max_bytes = 25_000_000
    artifact_max_video_bytes = 536_870_912
    artifact_store_ttl_seconds = None
    artifact_store_s3_raw_video_payloads = True
    artifact_store_s3_sse = "aws:kms"
    artifact_store_s3_sse_kms_key_id = "kms-key-1"
    artifact_store_s3_bucket = "motet-artifacts"
    artifact_store_s3_prefix = "artifacts"
    artifact_store_s3_region = "us-east-1"
    artifact_store_s3_endpoint_url = "http://seaweedfs:8333"
    artifact_store_s3_access_key_id = "motet-dev"
    artifact_store_s3_secret_access_key = "motet-dev-secret"
    artifact_store_s3_session_token = None
    artifact_store_s3_force_path_style = True
    artifact_store_s3_use_ssl = False


def _make_store(redis: FakeRedis, s3: FakeS3Client):
    from motet.core.artifacts.s3_artifact_store import S3ArtifactStore

    return S3ArtifactStore(
        service_name="artifact_store_test",
        s3_client=s3,
        redis_client=redis,
        config=DummyS3Config(),
        encryption_service=DummyEncryptionService(),
    )


def test_s3_store_roundtrips_encrypted_bytes_payload() -> None:
    redis = FakeRedis()
    s3 = FakeS3Client()
    store = _make_store(redis, s3)

    payload = b"\x00\x01artifact"
    artifact_id = store.put(
        payload=payload,
        content_type="application/octet-stream",
        tenant_id="tenant-a",
        principal_id="principal-1",
        motet_id="default",
    )

    object_keys = {key for _, key in s3.objects}
    assert any(key.endswith(f"{artifact_id}.json") for key in object_keys)
    assert any(key.endswith(f"{artifact_id}.metadata.json") for key in object_keys)
    assert redis.hashes[f"tenant-a:meta:art:{artifact_id}"]["tenant_id"] == "tenant-a"
    assert redis.ttls == {}
    assert store.get(artifact_id, tenant_id="tenant-a", principal_id="principal-1", motet_id="default") == payload


def test_s3_store_applies_explicit_ttl_to_redis_metadata() -> None:
    redis = FakeRedis()
    s3 = FakeS3Client()
    store = _make_store(redis, s3)

    artifact_id = store.put(
        payload="temporary",
        content_type="text/plain",
        tenant_id="tenant-a",
        principal_id="principal-1",
        motet_id="default",
        ttl_seconds=123,
    )

    assert redis.ttls[f"tenant-a:meta:art:{artifact_id}"] == 123
    assert redis.hashes[f"tenant-a:meta:art:{artifact_id}"]["expires_at"]


def test_s3_store_list_uses_redis_source_index() -> None:
    redis = FakeRedis()
    s3 = FakeS3Client()
    store = _make_store(redis, s3)

    from motet.core.artifacts.types import ArtifactKind

    derived_a = store.put(
        payload="a",
        content_type="text/plain",
        kind=ArtifactKind.DERIVED_TEXT,
        source_artifact_id="source-a",
        metadata={"conversation_id": "conv-1"},
        tenant_id="tenant-a",
        principal_id="principal-1",
        motet_id="default",
    )
    store.put(
        payload="b",
        content_type="text/plain",
        kind=ArtifactKind.DERIVED_TEXT,
        source_artifact_id="source-b",
        metadata={"conversation_id": "conv-1"},
        tenant_id="tenant-a",
        principal_id="principal-1",
        motet_id="default",
    )

    results = store.list(
        kind=ArtifactKind.DERIVED_TEXT,
        source_artifact_id="source-a",
        tenant_id="tenant-a",
        principal_id="principal-1",
        motet_id="default",
    )

    assert [item.id for item in results] == [derived_a]


def test_s3_store_delete_removes_object_and_metadata() -> None:
    redis = FakeRedis()
    s3 = FakeS3Client()
    store = _make_store(redis, s3)

    artifact_id = store.put(payload="data", tenant_id="tenant-a", principal_id="principal-1", motet_id="default")
    object_key = redis.hashes[f"tenant-a:meta:art:{artifact_id}"]["object_key"]
    metadata_object_key = redis.hashes[f"tenant-a:meta:art:{artifact_id}"]["metadata_object_key"]

    assert store.delete(artifact_id, tenant_id="tenant-a", principal_id="principal-1", motet_id="default") is True
    assert ("motet-artifacts", object_key) in s3.deleted
    assert ("motet-artifacts", metadata_object_key) in s3.deleted
    assert f"tenant-a:meta:art:{artifact_id}" not in redis.hashes


def test_s3_store_recovers_metadata_from_sidecar_and_rehydrates_indexes() -> None:
    redis = FakeRedis()
    s3 = FakeS3Client()
    store = _make_store(redis, s3)

    from motet.core.artifacts.types import ArtifactKind

    artifact_id = store.put(
        payload="data",
        content_type="text/plain",
        kind=ArtifactKind.DERIVED_TEXT,
        source_artifact_id="source-a",
        metadata={"conversation_id": "conv-1"},
        tenant_id="tenant-a",
        principal_id="principal-1",
        motet_id="default",
    )

    redis.hashes.clear()
    redis.zsets.clear()

    meta = store.get_metadata(
        artifact_id,
        tenant_id="tenant-a",
        principal_id="principal-1",
        motet_id="default",
    )

    assert meta is not None
    assert meta.id == artifact_id
    assert meta.source_artifact_id == "source-a"
    assert redis.hashes[f"tenant-a:meta:art:{artifact_id}"]["tenant_id"] == "tenant-a"
    assert artifact_id in redis.zsets["tenant-a:idx:art:tenant:tenant-a"]
    assert artifact_id in redis.zsets["tenant-a:idx:art:tenant:tenant-a:kind:derived_text"]
    assert artifact_id in redis.zsets["tenant-a:idx:art:tenant:tenant-a:source:source-a:kind:derived_text"]


def test_s3_store_video_payload_stored_raw_with_sse() -> None:
    redis = FakeRedis()
    s3 = FakeS3Client()
    store = _make_store(redis, s3)

    payload = b"\x00\x01" * 64
    artifact_id = store.put(
        payload=payload,
        content_type="video/mp4",
        tenant_id="tenant-a",
        principal_id="principal-1",
        motet_id="default",
    )

    object_key = redis.hashes[f"tenant-a:meta:art:{artifact_id}"]["object_key"]
    assert object_key.endswith(f"{artifact_id}.bin")
    assert redis.hashes[f"tenant-a:meta:art:{artifact_id}"]["payload_format"] == "raw"
    # Raw object body is the plaintext bytes (encryption via S3 SSE, not envelope)
    assert s3.objects[("motet-artifacts", object_key)] == payload
    assert s3.put_kwargs[object_key]["ServerSideEncryption"] == "aws:kms"
    assert s3.put_kwargs[object_key]["SSEKMSKeyId"] == "kms-key-1"
    assert s3.put_kwargs[object_key]["ContentType"] == "video/mp4"

    meta = store.get_metadata(artifact_id, tenant_id="tenant-a", principal_id="principal-1", motet_id="default")
    assert meta is not None and meta.payload_format == "raw"
    assert store.get(artifact_id, tenant_id="tenant-a", principal_id="principal-1", motet_id="default") == payload


def test_s3_store_raw_get_range_uses_native_ranged_get() -> None:
    redis = FakeRedis()
    s3 = FakeS3Client()
    store = _make_store(redis, s3)

    payload = bytes(range(256))
    artifact_id = store.put(
        payload=payload,
        content_type="video/mp4",
        tenant_id="tenant-a",
        principal_id="principal-1",
        motet_id="default",
    )

    s3.get_calls.clear()
    chunk = store.get_range(
        artifact_id,
        start=10,
        end=19,
        tenant_id="tenant-a",
        principal_id="principal-1",
        motet_id="default",
    )

    assert chunk == payload[10:20]
    ranged = [call for call in s3.get_calls if call["Range"]]
    assert ranged and ranged[0]["Range"] == "bytes=10-19"
    # No full-object fetch happened for the payload key
    assert all(call["Range"] for call in s3.get_calls if call["Key"].endswith(".bin"))


def test_s3_store_raw_get_range_clamps_to_payload_size() -> None:
    redis = FakeRedis()
    s3 = FakeS3Client()
    store = _make_store(redis, s3)

    payload = b"0123456789"
    artifact_id = store.put(
        payload=payload,
        content_type="video/mp4",
        tenant_id="tenant-a",
        principal_id="principal-1",
        motet_id="default",
    )

    assert (
        store.get_range(artifact_id, start=8, end=500, tenant_id="tenant-a", principal_id="principal-1", motet_id="default")
        == b"89"
    )
    assert (
        store.get_range(artifact_id, start=50, end=60, tenant_id="tenant-a", principal_id="principal-1", motet_id="default")
        == b""
    )


def test_s3_store_raw_update_metadata_skips_wrapper_rewrite() -> None:
    redis = FakeRedis()
    s3 = FakeS3Client()
    store = _make_store(redis, s3)

    artifact_id = store.put(
        payload=b"video-bytes",
        content_type="video/mp4",
        metadata={"filename": "clip.mp4"},
        tenant_id="tenant-a",
        principal_id="principal-1",
        motet_id="default",
    )
    object_key = redis.hashes[f"tenant-a:meta:art:{artifact_id}"]["object_key"]
    body_before = s3.objects[("motet-artifacts", object_key)]

    updated = store.update_metadata(
        artifact_id,
        {"memo_asset_id": "asset-1"},
        tenant_id="tenant-a",
        principal_id="principal-1",
        motet_id="default",
    )

    assert updated is not None
    assert updated.metadata["memo_asset_id"] == "asset-1"
    assert updated.metadata["filename"] == "clip.mp4"
    # Payload object untouched — raw format has no wrapper to rewrite
    assert s3.objects[("motet-artifacts", object_key)] == body_before


def test_s3_store_raw_disabled_falls_back_to_envelope() -> None:
    redis = FakeRedis()
    s3 = FakeS3Client()
    store = _make_store(redis, s3)
    store._cfg.artifact_store_s3_raw_video_payloads = False

    payload = b"video-bytes"
    artifact_id = store.put(
        payload=payload,
        content_type="video/mp4",
        tenant_id="tenant-a",
        principal_id="principal-1",
        motet_id="default",
    )

    object_key = redis.hashes[f"tenant-a:meta:art:{artifact_id}"]["object_key"]
    assert object_key.endswith(f"{artifact_id}.json")
    assert redis.hashes[f"tenant-a:meta:art:{artifact_id}"]["payload_format"] == "envelope"
    assert store.get(artifact_id, tenant_id="tenant-a", principal_id="principal-1", motet_id="default") == payload
    assert (
        store.get_range(artifact_id, start=0, end=4, tenant_id="tenant-a", principal_id="principal-1", motet_id="default")
        == payload[:5]
    )


def test_get_artifact_store_selects_s3_backend() -> None:
    from motet.core.artifacts import redis_artifact_store

    sentinel = object()
    redis_artifact_store._global_store = None
    try:
        with patch("motet.core.artifacts.redis_artifact_store.Config", return_value=DummyS3Config()), patch(
            "motet.core.artifacts.s3_artifact_store.S3ArtifactStore", return_value=sentinel
        ):
            assert redis_artifact_store.get_artifact_store() is sentinel
    finally:
        redis_artifact_store._global_store = None
