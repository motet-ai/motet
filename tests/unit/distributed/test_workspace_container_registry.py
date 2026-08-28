"""
Motet - Workspace Container Registry Tests (ADR-0106 Slice A)

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-06-18

Pins the routing-primitive contract that ADR-0106 §"The primitive" defines
and that ADR-0103 Phase 2 will reuse without changing key shape:

    workspace:container:<tenant>:<conv>:<bundle>:<skill>:<image_stack> -> binding hash

Covers:
    * bind / lookup / touch / unbind round-trip
    * tenant-index list_for_tenant() with self-heal of stale members
    * cross-tenant guard (ADR-0106 §rule 7)
    * idle-TTL refresh on touch() for both routing key and tenant index
    * active exec refcount bookkeeping for reaper safety
    * per-tenant cardinality count
    * binding mapping survives Redis bytes/str ambiguity
"""

from __future__ import annotations

from typing import Any, Dict, Iterator, List, Optional, Set
from unittest.mock import patch

import pytest

from motet.core.distributed.workspace_container_registry import (
    WorkspaceContainerBinding,
    WorkspaceContainerRegistry,
)


# ---------------------------------------------------------------------------
# In-memory Redis stub
# ---------------------------------------------------------------------------
#
# We intentionally do NOT pull in fakeredis: the stub here exposes only the
# surface WorkspaceContainerRegistry exercises, so a behavior change in the
# registry will fail the test at the call boundary (loud) rather than in a
# matrix of incidental semantics from a third-party fake.


class _FakePipeline:
    def __init__(self, redis: "_FakeRedis") -> None:
        self._redis = redis
        self._ops: List[tuple] = []

    def delete(self, key: str) -> "_FakePipeline":
        self._ops.append(("delete", key))
        return self

    def hset(
        self,
        key: str,
        field: Any = None,
        value: Any = None,
        *,
        mapping: Optional[Dict[str, Any]] = None,
    ) -> "_FakePipeline":
        if mapping is not None:
            self._ops.append(("hset", key, dict(mapping)))
        elif field is not None:
            self._ops.append(("hset_field", key, field, value))
        return self

    def hincrby(self, key: str, field: str, amount: int) -> "_FakePipeline":
        self._ops.append(("hincrby", key, field, int(amount)))
        return self

    def expire(self, key: str, seconds: int) -> "_FakePipeline":
        self._ops.append(("expire", key, int(seconds)))
        return self

    def sadd(self, key: str, *members: str) -> "_FakePipeline":
        self._ops.append(("sadd", key, list(members)))
        return self

    def srem(self, key: str, *members: str) -> "_FakePipeline":
        self._ops.append(("srem", key, list(members)))
        return self

    def execute(self) -> List[Any]:
        results: List[Any] = []
        for op in self._ops:
            kind = op[0]
            if kind == "delete":
                results.append(self._redis.delete(op[1]))
            elif kind == "hset":
                results.append(self._redis.hset(op[1], mapping=op[2]))
            elif kind == "hset_field":
                results.append(self._redis.hset(op[1], op[2], op[3]))
            elif kind == "hincrby":
                results.append(self._redis.hincrby(op[1], op[2], op[3]))
            elif kind == "expire":
                results.append(self._redis.expire(op[1], op[2]))
            elif kind == "sadd":
                results.append(self._redis.sadd(op[1], *op[2]))
            elif kind == "srem":
                results.append(self._redis.srem(op[1], *op[2]))
        self._ops.clear()
        return results


class _FakeRedis:
    """Minimal in-memory Redis stub covering registry usage."""

    def __init__(self) -> None:
        self.hashes: Dict[str, Dict[str, str]] = {}
        self.sets: Dict[str, Set[str]] = {}
        self.ttls: Dict[str, int] = {}

    def hset(
        self,
        key: str,
        field: Optional[str] = None,
        value: Optional[str] = None,
        *,
        mapping: Optional[Dict[str, Any]] = None,
    ) -> int:
        bucket = self.hashes.setdefault(key, {})
        added = 0
        if mapping is not None:
            for k, v in mapping.items():
                if k not in bucket:
                    added += 1
                bucket[k] = v
        elif field is not None:
            if field not in bucket:
                added += 1
            bucket[field] = value if value is not None else ""
        return added

    def hgetall(self, key: str) -> Dict[str, str]:
        return dict(self.hashes.get(key, {}))

    def exists(self, key: str) -> int:
        return 1 if key in self.hashes or key in self.sets else 0

    def delete(self, key: str) -> int:
        deleted = 0
        if key in self.hashes:
            del self.hashes[key]
            deleted += 1
        if key in self.sets:
            del self.sets[key]
            deleted += 1
        self.ttls.pop(key, None)
        return deleted

    def expire(self, key: str, seconds: int) -> int:
        if key in self.hashes or key in self.sets:
            self.ttls[key] = int(seconds)
            return 1
        return 0

    def hincrby(self, key: str, field: str, amount: int) -> int:
        bucket = self.hashes.setdefault(key, {})
        current = int(bucket.get(field, "0") or "0")
        current += int(amount)
        bucket[field] = str(current)
        return current

    def ttl(self, key: str) -> int:
        return self.ttls.get(key, -1)

    def sadd(self, key: str, *members: str) -> int:
        bucket = self.sets.setdefault(key, set())
        before = len(bucket)
        bucket.update(members)
        return len(bucket) - before

    def srem(self, key: str, *members: str) -> int:
        bucket = self.sets.get(key)
        if bucket is None:
            return 0
        removed = 0
        for m in members:
            if m in bucket:
                bucket.discard(m)
                removed += 1
        if not bucket:
            del self.sets[key]
        return removed

    def smembers(self, key: str) -> Set[str]:
        return set(self.sets.get(key, set()))

    def keys(self, pattern: str) -> List[str]:
        prefix = pattern.rstrip("*")
        return [k for k in list(self.hashes.keys()) + list(self.sets.keys()) if k.startswith(prefix)]

    def scan_iter(self, match: str) -> Iterator[str]:
        import fnmatch

        for k in list(self.hashes.keys()) + list(self.sets.keys()):
            if fnmatch.fnmatch(k, match):
                yield k

    def pipeline(self) -> _FakePipeline:
        return _FakePipeline(self)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def fake_redis() -> _FakeRedis:
    return _FakeRedis()


@pytest.fixture
def registry(fake_redis: _FakeRedis, monkeypatch: pytest.MonkeyPatch) -> WorkspaceContainerRegistry:
    monkeypatch.setenv("MOTET_WORKSPACE_CONTAINER_IDLE_TTL_SECONDS", "60")
    with patch(
        "motet.core.distributed.workspace_container_registry.get_sync_redis_client",
        return_value=fake_redis,
    ):
        return WorkspaceContainerRegistry()


def _make_binding(
    tenant_id: str = "t1",
    conversation_id: str = "c1",
    bundle_id: str = "demo",
    skill_name: str = "pdf",
    image_stack: str = "python-minimal",
    container_id: str = "abcdef0123456789",
    image: str = "python:3.11-slim",
    mode: str = "cold",
) -> WorkspaceContainerBinding:
    return WorkspaceContainerBinding(
        tenant_id=tenant_id,
        conversation_id=conversation_id,
        bundle_id=bundle_id,
        skill_name=skill_name,
        image_stack=image_stack,
        container_id=container_id,
        image=image,
        mode=mode,  # type: ignore[arg-type]
        worker_attribution="cloud_worker1",
        metadata={"scratch_dir": "/scratch"},
    )


# ---------------------------------------------------------------------------
# bind / lookup / touch / unbind
# ---------------------------------------------------------------------------


def test_bind_and_lookup_round_trip(
    registry: WorkspaceContainerRegistry, fake_redis: _FakeRedis
) -> None:
    binding = _make_binding()
    registry.bind(binding)

    found = registry.lookup(
        tenant_id="t1",
        conversation_id="c1",
        bundle_id="demo",
        skill_name="pdf",
        image_stack="python-minimal",
    )
    assert found is not None
    assert found.container_id == binding.container_id
    assert found.image == "python:3.11-slim"
    assert found.mode == "cold"
    assert found.metadata == {"scratch_dir": "/scratch"}

    key = "t1:workspace:container:t1:c1:demo:pdf:python-minimal"
    assert key in fake_redis.hashes
    assert fake_redis.ttls[key] == 60


def test_lookup_returns_none_when_unbound(registry: WorkspaceContainerRegistry) -> None:
    assert registry.lookup(
        tenant_id="t1", conversation_id="missing", image_stack="python-minimal"
    ) is None


def test_touch_refreshes_ttl_and_last_active(
    registry: WorkspaceContainerRegistry, fake_redis: _FakeRedis
) -> None:
    binding = _make_binding()
    registry.bind(binding)
    key = "t1:workspace:container:t1:c1:demo:pdf:python-minimal"
    index_key = "t1:workspace:container:index:tenant:t1"

    initial_last_active = fake_redis.hashes[key]["last_active_at"]
    fake_redis.ttls[key] = 5
    fake_redis.ttls[index_key] = 7

    refreshed = registry.touch(
        tenant_id="t1",
        conversation_id="c1",
        bundle_id="demo",
        skill_name="pdf",
        image_stack="python-minimal",
    )

    assert refreshed is True
    assert fake_redis.ttls[key] == 60
    assert fake_redis.ttls[index_key] == 240
    assert fake_redis.hashes[key]["last_active_at"] >= initial_last_active


def test_touch_refreshes_tenant_index_ttl_past_original_window(
    registry: WorkspaceContainerRegistry, fake_redis: _FakeRedis
) -> None:
    registry.bind(_make_binding())
    key = "t1:workspace:container:t1:c1:demo:pdf:python-minimal"
    index_key = "t1:workspace:container:index:tenant:t1"

    fake_redis.ttls[key] = 1
    fake_redis.ttls[index_key] = 1

    assert registry.touch(
        tenant_id="t1",
        conversation_id="c1",
        bundle_id="demo",
        skill_name="pdf",
        image_stack="python-minimal",
    )
    assert fake_redis.ttls[index_key] == 240
    bindings = registry.list_for_tenant("t1")
    assert [b.conversation_id for b in bindings] == ["c1"]


def test_begin_and_end_activity_track_active_exec_refcount(
    registry: WorkspaceContainerRegistry, fake_redis: _FakeRedis
) -> None:
    registry.bind(_make_binding())
    key = "t1:workspace:container:t1:c1:demo:pdf:python-minimal"

    assert registry.begin_activity(
        tenant_id="t1",
        conversation_id="c1",
        bundle_id="demo",
        skill_name="pdf",
        image_stack="python-minimal",
    )
    assert fake_redis.hashes[key]["active_execs"] == "1"

    assert registry.end_activity(
        tenant_id="t1",
        conversation_id="c1",
        bundle_id="demo",
        skill_name="pdf",
        image_stack="python-minimal",
    )
    assert fake_redis.hashes[key]["active_execs"] == "0"


def test_touch_returns_false_when_evicted(
    registry: WorkspaceContainerRegistry, fake_redis: _FakeRedis
) -> None:
    refreshed = registry.touch(
        tenant_id="t1", conversation_id="evicted", image_stack="python-minimal"
    )
    assert refreshed is False


def test_unbind_removes_routing_and_index_entry(
    registry: WorkspaceContainerRegistry, fake_redis: _FakeRedis
) -> None:
    binding = _make_binding()
    registry.bind(binding)

    deleted = registry.unbind(
        tenant_id="t1",
        conversation_id="c1",
        bundle_id="demo",
        skill_name="pdf",
        image_stack="python-minimal",
    )
    assert deleted is True

    key = "t1:workspace:container:t1:c1:demo:pdf:python-minimal"
    assert key not in fake_redis.hashes
    assert "t1:workspace:container:index:tenant:t1" not in fake_redis.sets

    # second unbind is a no-op
    assert (
        registry.unbind(
            tenant_id="t1",
            conversation_id="c1",
            bundle_id="demo",
            skill_name="pdf",
            image_stack="python-minimal",
        )
        is False
    )


# ---------------------------------------------------------------------------
# Cross-tenant guard (ADR-0106 §rule 7)
# ---------------------------------------------------------------------------


def test_lookup_refuses_cross_tenant_via_rebound_payload(
    registry: WorkspaceContainerRegistry, fake_redis: _FakeRedis
) -> None:
    """If the stored payload's tenant_id no longer matches the requested
    tenant, lookup MUST return None rather than serve the binding.

    This guards against the (theoretically impossible but worth pinning)
    case where a corrupted or operator-edited payload would otherwise leak
    a container across tenants.
    """
    binding = _make_binding(tenant_id="t1")
    registry.bind(binding)

    key = "t1:workspace:container:t1:c1:demo:pdf:python-minimal"
    fake_redis.hashes[key]["tenant_id"] = "tEvil"

    found = registry.lookup(
        tenant_id="t1",
        conversation_id="c1",
        bundle_id="demo",
        skill_name="pdf",
        image_stack="python-minimal",
    )
    assert found is None


# ---------------------------------------------------------------------------
# list_for_tenant + tenant-index self-heal
# ---------------------------------------------------------------------------


def test_list_for_tenant_returns_only_tenant_bindings(
    registry: WorkspaceContainerRegistry,
) -> None:
    registry.bind(_make_binding(tenant_id="t1", conversation_id="c1"))
    registry.bind(_make_binding(tenant_id="t1", conversation_id="c2"))
    registry.bind(_make_binding(tenant_id="t2", conversation_id="c1"))

    t1 = registry.list_for_tenant("t1")
    t2 = registry.list_for_tenant("t2")

    assert {b.conversation_id for b in t1} == {"c1", "c2"}
    assert {b.conversation_id for b in t2} == {"c1"}
    assert all(b.tenant_id == "t1" for b in t1)


def test_list_for_tenant_self_heals_evicted_index_entries(
    registry: WorkspaceContainerRegistry, fake_redis: _FakeRedis
) -> None:
    registry.bind(_make_binding(tenant_id="t1", conversation_id="alive"))
    registry.bind(_make_binding(tenant_id="t1", conversation_id="evicted"))

    evicted_key = "t1:workspace:container:t1:evicted:demo:pdf:python-minimal"
    del fake_redis.hashes[evicted_key]

    bindings = registry.list_for_tenant("t1")
    assert {b.conversation_id for b in bindings} == {"alive"}
    assert evicted_key not in fake_redis.sets["t1:workspace:container:index:tenant:t1"]


def test_count_for_tenant(registry: WorkspaceContainerRegistry) -> None:
    assert registry.count_for_tenant("t1") == 0
    registry.bind(_make_binding(tenant_id="t1", conversation_id="c1"))
    registry.bind(_make_binding(tenant_id="t1", conversation_id="c2"))
    assert registry.count_for_tenant("t1") == 2


def test_list_all_returns_all_bindings_skipping_index_keys(
    registry: WorkspaceContainerRegistry,
) -> None:
    registry.bind(_make_binding(tenant_id="t1", conversation_id="c1"))
    registry.bind(_make_binding(tenant_id="t2", conversation_id="cX"))
    bindings = registry.list_all()
    assert {(b.tenant_id, b.conversation_id) for b in bindings} == {
        ("t1", "c1"),
        ("t2", "cX"),
    }


# ---------------------------------------------------------------------------
# TTL configuration
# ---------------------------------------------------------------------------


def test_idle_ttl_resolved_from_env(monkeypatch: pytest.MonkeyPatch, fake_redis: _FakeRedis) -> None:
    monkeypatch.setenv("MOTET_WORKSPACE_CONTAINER_IDLE_TTL_SECONDS", "300")
    with patch(
        "motet.core.distributed.workspace_container_registry.get_sync_redis_client",
        return_value=fake_redis,
    ):
        reg = WorkspaceContainerRegistry()
    assert reg.idle_ttl_seconds == 300


def test_idle_ttl_explicit_overrides_env(
    monkeypatch: pytest.MonkeyPatch, fake_redis: _FakeRedis
) -> None:
    monkeypatch.setenv("MOTET_WORKSPACE_CONTAINER_IDLE_TTL_SECONDS", "300")
    with patch(
        "motet.core.distributed.workspace_container_registry.get_sync_redis_client",
        return_value=fake_redis,
    ):
        reg = WorkspaceContainerRegistry(idle_ttl_seconds=42)
    assert reg.idle_ttl_seconds == 42


def test_idle_ttl_falls_back_to_default_on_invalid_env(
    monkeypatch: pytest.MonkeyPatch, fake_redis: _FakeRedis
) -> None:
    monkeypatch.setenv("MOTET_WORKSPACE_CONTAINER_IDLE_TTL_SECONDS", "not-a-number")
    with patch(
        "motet.core.distributed.workspace_container_registry.get_sync_redis_client",
        return_value=fake_redis,
    ):
        reg = WorkspaceContainerRegistry()
    assert reg.idle_ttl_seconds == WorkspaceContainerRegistry.DEFAULT_IDLE_TTL_SECONDS


# ---------------------------------------------------------------------------
# Bytes-vs-str robustness
# ---------------------------------------------------------------------------


def test_from_redis_mapping_handles_bytes_keys_and_values() -> None:
    """Redis can return bytes when decode_responses=False is in play."""
    raw: Dict[Any, Any] = {
        b"tenant_id": b"t1",
        b"conversation_id": b"c1",
        b"image_stack": b"python-minimal",
        b"container_id": b"abcdef0123456789",
        b"image": b"python:3.11-slim",
        b"mode": b"cold",
        b"endpoint": b"",
        b"created_at": b"100.0",
        b"last_active_at": b"200.0",
        b"worker_attribution": b"",
        b"metadata": b'{"scratch_dir": "/scratch"}',
    }
    binding = WorkspaceContainerBinding.from_redis_mapping(raw)
    assert binding.tenant_id == "t1"
    assert binding.endpoint is None
    assert binding.worker_attribution is None
    assert binding.created_at == 100.0
    assert binding.last_active_at == 200.0
    assert binding.metadata == {"scratch_dir": "/scratch"}
