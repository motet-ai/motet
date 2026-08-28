"""
Motet - Valkey GLIDE Backend Tests

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-24

Description:
    Unit tests for Redis URL parsing, MOTET_VALKEY_CLIENT resolution, and the
    redis-py-shaped GLIDE adapter (no live Valkey required). Includes SCAN
    cursor encoding so multi-page scan_iter never passes an int to GLIDE.

Dependencies:
    - pytest
    - motet.core.distributed.glide_backend

Usage:
    pytest tests/unit/core/distributed/test_glide_backend.py -q
"""

from __future__ import annotations

import sys
import types
from typing import Any, Dict, List, Optional

import pytest

from motet.core.distributed.glide_backend import (
    SyncGlideRedisAdapter,
    _encode_scan_cursor,
    glide_client_is_closed,
    parse_valkey_url,
    resolve_glide_inflight_limit,
    resolve_glide_request_timeout_ms,
    resolve_valkey_client_backend,
)


def test_parse_valkey_url_standalone() -> None:
    parsed = parse_valkey_url("redis://valkey:6379/2")
    assert parsed["host"] == "valkey"
    assert parsed["port"] == 6379
    assert parsed["database_id"] == 2
    assert parsed["use_tls"] is False
    assert parsed["insecure_tls"] is False


def test_parse_valkey_url_tls_insecure() -> None:
    parsed = parse_valkey_url("rediss://user:secret@redis-tls:6380/0?ssl_cert_reqs=CERT_NONE")
    assert parsed["use_tls"] is True
    assert parsed["insecure_tls"] is True
    assert parsed["username"] == "user"
    assert parsed["password"] == "secret"
    assert parsed["port"] == 6380


def test_resolve_backend_defaults_to_redis(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MOTET_VALKEY_CLIENT", raising=False)
    assert resolve_valkey_client_backend() == "redis"


def test_resolve_backend_glide_when_requested(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MOTET_VALKEY_CLIENT", "glide")
    monkeypatch.setattr(
        "motet.core.distributed.glide_backend._glide_importable",
        lambda: True,
    )
    assert resolve_valkey_client_backend() == "glide"


def test_resolve_glide_timeout_and_inflight_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MOTET_VALKEY_GLIDE_TIMEOUT_MS", raising=False)
    monkeypatch.delenv("MOTET_VALKEY_GLIDE_INFLIGHT", raising=False)
    assert resolve_glide_request_timeout_ms() == 30_000
    assert resolve_glide_inflight_limit() == 128


def test_resolve_glide_timeout_and_inflight_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MOTET_VALKEY_GLIDE_TIMEOUT_MS", "15000")
    monkeypatch.setenv("MOTET_VALKEY_GLIDE_INFLIGHT", "32")
    assert resolve_glide_request_timeout_ms() == 15_000
    assert resolve_glide_inflight_limit() == 32


def test_resolve_glide_timeout_and_inflight_invalid_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MOTET_VALKEY_GLIDE_TIMEOUT_MS", "nope")
    monkeypatch.setenv("MOTET_VALKEY_GLIDE_INFLIGHT", "nope")
    assert resolve_glide_request_timeout_ms() == 30_000
    assert resolve_glide_inflight_limit() == 128


def test_resolve_backend_glide_falls_back_when_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MOTET_VALKEY_CLIENT", "glide")
    monkeypatch.setattr(
        "motet.core.distributed.glide_backend._glide_importable",
        lambda: False,
    )
    assert resolve_valkey_client_backend() == "redis"


class _FakeGlide:
    def __init__(self) -> None:
        self.kv: Dict[str, Any] = {}
        self.hashes: Dict[str, Dict[str, Any]] = {}
        self.zsets: Dict[str, Dict[str, float]] = {}
        self.last_set_kwargs: Dict[str, Any] = {}

    def ping(self) -> bytes:
        return b"PONG"

    def close(self) -> None:
        self.closed = True

    def get(self, name: str) -> Optional[bytes]:
        value = self.kv.get(name)
        if value is None:
            return None
        return value if isinstance(value, bytes) else str(value).encode()

    def set(self, name: str, value: Any, conditional_set: Any = None, expiry: Any = None) -> Optional[bytes]:
        self.last_set_kwargs = {"conditional_set": conditional_set, "expiry": expiry}
        if conditional_set is not None and getattr(conditional_set, "value", None) == "NX" and name in self.kv:
            return None
        self.kv[name] = value
        return b"OK"

    def delete(self, keys: List[str]) -> int:
        deleted = 0
        for key in keys:
            if key in self.kv:
                del self.kv[key]
                deleted += 1
            if key in self.hashes:
                del self.hashes[key]
                deleted += 1
        return deleted

    def exists(self, keys: List[str]) -> int:
        return sum(1 for key in keys if key in self.kv or key in self.hashes)

    def expire(self, name: str, seconds: int) -> bool:
        return name in self.kv or name in self.hashes

    def hset(self, name: str, field_value_map: Dict[str, Any]) -> int:
        bucket = self.hashes.setdefault(name, {})
        added = 0
        for field, value in field_value_map.items():
            if field not in bucket:
                added += 1
            bucket[field] = value
        return added

    def hgetall(self, name: str) -> Dict[bytes, bytes]:
        bucket = self.hashes.get(name, {})
        return {
            (k.encode() if isinstance(k, str) else k): (v.encode() if isinstance(v, str) else v)
            for k, v in bucket.items()
        }

    def zadd(self, name: str, members_scores: Dict[str, float]) -> int:
        bucket = self.zsets.setdefault(name, {})
        added = 0
        for member, score in members_scores.items():
            if member not in bucket:
                added += 1
            bucket[member] = score
        return added

    def zrange(self, name: str, range_query: Any, reverse: bool = False) -> List[bytes]:
        items = sorted(self.zsets.get(name, {}).items(), key=lambda item: item[1], reverse=reverse)
        start = getattr(range_query, "start", 0)
        end = getattr(range_query, "end", -1)
        sliced = items[start:] if end == -1 else items[start : end + 1]
        return [member.encode() for member, _ in sliced]


def _install_fake_glide_sync(monkeypatch: pytest.MonkeyPatch) -> None:
    """Adapter set/zrange import GLIDE enums; unit tests must not need the wheel."""
    module = types.ModuleType("glide_sync")

    class ConditionalChange:
        ONLY_IF_DOES_NOT_EXIST = type("Flag", (), {"value": "NX"})()
        ONLY_IF_EXISTS = type("Flag", (), {"value": "XX"})()

    class ExpiryType:
        SEC = "SEC"
        MILLSEC = "MILLSEC"
        KEEP_TTL = "KEEP_TTL"

    class ExpirySet:
        def __init__(self, expiry_type: Any, value: Any) -> None:
            self.expiry_type = expiry_type
            self.value = value

    class RangeByIndex:
        def __init__(self, start: int, end: int) -> None:
            self.start = start
            self.end = end

    module.ConditionalChange = ConditionalChange
    module.ExpiryType = ExpiryType
    module.ExpirySet = ExpirySet
    module.RangeByIndex = RangeByIndex
    monkeypatch.setitem(sys.modules, "glide_sync", module)


def test_sync_adapter_set_nx_hset_and_zrevrange(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_fake_glide_sync(monkeypatch)
    fake = _FakeGlide()
    client = SyncGlideRedisAdapter(fake, decode_responses=True)

    assert client.ping() is True
    assert client.set("lock", "pid", nx=True, ex=90) is True
    assert client.set("lock", "other", nx=True, ex=90) is None
    assert client.get("lock") == "pid"

    assert client.hset("mem:1", mapping={"tenant_id": "acme", "motet_id": "default"}) == 2
    assert client.hgetall("mem:1") == {"tenant_id": "acme", "motet_id": "default"}

    assert client.zadd("idx", {"a": 1.0, "b": 2.0}) == 2
    assert client.zrevrange("idx", 0, -1) == ["b", "a"]
    assert client.exists("lock", "mem:1") == 2
    assert client.delete("lock") == 1


def test_sync_adapter_falls_back_for_pipeline() -> None:
    class Fallback:
        def pipeline(self) -> str:
            return "redis-py-pipeline"

    client = SyncGlideRedisAdapter(_FakeGlide(), fallback=Fallback())
    assert client.pipeline() == "redis-py-pipeline"


def test_encode_scan_cursor_is_always_bytes() -> None:
    assert _encode_scan_cursor(0) == b"0"
    assert _encode_scan_cursor(None) == b"0"
    assert _encode_scan_cursor("0") == b"0"
    assert _encode_scan_cursor(b"0") == b"0"
    assert _encode_scan_cursor(42) == b"42"
    assert _encode_scan_cursor("42") == b"42"
    assert _encode_scan_cursor(b"42") == b"42"


class _PagingGlide:
    """Mirrors GLIDE: SCAN cursor must be bytes/str, never int."""

    def __init__(self) -> None:
        self.cursors: List[Any] = []

    def scan(self, cursor: Any, match: Optional[str] = None, count: Optional[int] = None) -> Any:
        if isinstance(cursor, int):
            raise TypeError(f"Unsupported argument type: {type(cursor)}")
        if not isinstance(cursor, (bytes, bytearray, str)):
            raise TypeError(f"Unsupported argument type: {type(cursor)}")
        self.cursors.append(cursor)
        if cursor in (0, "0", b"0"):
            return (b"42", [b"workspace:container:a"])
        return (b"0", [b"workspace:container:b"])


def test_scan_iter_reencodes_int_cursor_for_glide() -> None:
    fake = _PagingGlide()
    client = SyncGlideRedisAdapter(fake, decode_responses=True)

    keys = list(client.scan_iter(match="workspace:container:*"))

    assert keys == ["workspace:container:a", "workspace:container:b"]
    assert fake.cursors == [b"0", b"42"]
    assert all(not isinstance(cursor, int) for cursor in fake.cursors)


def test_sync_adapter_close_leaves_shared_glide_open() -> None:
    fake = _FakeGlide()
    fallback_closed = {"value": False}

    class Fallback:
        def close(self) -> None:
            fallback_closed["value"] = True

    client = SyncGlideRedisAdapter(fake, fallback=Fallback())
    client.close()

    assert fallback_closed["value"] is True
    assert getattr(fake, "closed", False) is False
    assert glide_client_is_closed(fake) is False


def test_sync_adapter_close_shared_closes_glide() -> None:
    fake = _FakeGlide()
    fake._is_closed = False

    def _close() -> None:
        fake._is_closed = True

    fake.close = _close  # type: ignore[method-assign]
    client = SyncGlideRedisAdapter(fake)
    client.close(close_shared=True)

    assert fake._is_closed is True
    assert glide_client_is_closed(fake) is True


def test_glide_client_is_closed_none() -> None:
    assert glide_client_is_closed(None) is True


def test_scan_returns_int_cursor_to_redis_py_callers() -> None:
    fake = _PagingGlide()
    client = SyncGlideRedisAdapter(fake, decode_responses=True)

    cursor, keys = client.scan(cursor=0, match="workspace:container:*")
    assert cursor == 42
    assert keys == ["workspace:container:a"]

    cursor, keys = client.scan(cursor=cursor, match="workspace:container:*")
    assert cursor == 0
    assert keys == ["workspace:container:b"]
