"""
Motet - Unit tests for conversation lineage

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-31

Description:
    Unit tests for motet.core.conversations.lineage: opaque isolated
    conversation ids, stored parent/root pointers, the Redis-backed
    parent→children index, descendant walk, and lineage forget.

Dependencies:
    - pytest
    - motet.core.conversations.lineage

Usage:
    pytest tests/unit/core/conversations/test_lineage.py -q
"""

from __future__ import annotations

from typing import Any, Dict, Set

import pytest

import motet.core.conversations.lineage as lineage


def test_mint_isolated_conversation_is_opaque_and_unique() -> None:
    first = lineage.mint_isolated_conversation("api-exec-123")
    second = lineage.mint_isolated_conversation("api-exec-123")
    assert first.conversation_id.startswith("iso-")
    assert second.conversation_id.startswith("iso-")
    assert first.conversation_id != second.conversation_id
    assert first.parent_conversation_id == "api-exec-123"
    assert first.root_conversation_id == "api-exec-123"
    assert "api-exec-123" not in first.conversation_id


def test_mint_generates_parent_when_empty() -> None:
    iso = lineage.mint_isolated_conversation("")
    assert iso.parent_conversation_id.startswith("workflow-")
    assert iso.root_conversation_id == iso.parent_conversation_id
    assert iso.conversation_id.startswith("iso-")


def test_mint_uses_explicit_root(fake_redis: FakeRedis) -> None:
    iso = lineage.mint_isolated_conversation(
        "mid-child",
        tenant_id="t1",
        root_conversation_id="root-chat",
        kind="spawn",
    )
    assert iso.root_conversation_id == "root-chat"
    assert iso.parent_conversation_id == "mid-child"
    parentage = fake_redis.hashes[f"t1:conv:parentage:{iso.conversation_id}"]
    assert parentage["parent"] == "mid-child"
    assert parentage["root"] == "root-chat"
    assert parentage["kind"] == "spawn"


def test_nested_mint_inherits_stored_root(fake_redis: FakeRedis) -> None:
    child = lineage.mint_isolated_conversation("root-chat", tenant_id="t1")
    grandchild = lineage.mint_isolated_conversation(
        child.conversation_id, tenant_id="t1"
    )
    assert grandchild.parent_conversation_id == child.conversation_id
    assert grandchild.root_conversation_id == "root-chat"


def test_root_of_requires_parentage_record(fake_redis: FakeRedis) -> None:
    iso = lineage.mint_isolated_conversation("parent", tenant_id="t1")
    assert lineage.root_conversation_id_of(iso.conversation_id, tenant_id="t1") == "parent"
    assert lineage.root_conversation_id_of("plain-conversation", tenant_id="t1") is None
    assert lineage.root_conversation_id_of(iso.conversation_id) is None
    assert lineage.is_child_conversation_id(iso.conversation_id, tenant_id="t1")
    assert not lineage.is_child_conversation_id("plain", tenant_id="t1")


class FakeRedis:
    def __init__(self) -> None:
        self.sets: Dict[str, Set[str]] = {}
        self.hashes: Dict[str, Dict[str, str]] = {}
        self.ttls: Dict[str, int] = {}

    def pipeline(self) -> "FakeRedis":
        return self

    def execute(self) -> list:
        return []

    def sadd(self, key: str, *members: str) -> int:
        self.sets.setdefault(key, set()).update(members)
        return len(members)

    def smembers(self, key: str) -> Set[str]:
        return set(self.sets.get(key, set()))

    def hset(self, key: str, mapping: Dict[str, str] | None = None, **kwargs: str) -> int:
        body = self.hashes.setdefault(key, {})
        if mapping:
            body.update({str(k): str(v) for k, v in mapping.items()})
        body.update({str(k): str(v) for k, v in kwargs.items()})
        return len(body)

    def hgetall(self, key: str) -> Dict[str, str]:
        return dict(self.hashes.get(key, {}))

    def expire(self, key: str, ttl: int) -> bool:
        self.ttls[key] = ttl
        return True

    def srem(self, key: str, *members: str) -> int:
        bucket = self.sets.get(key)
        if not bucket:
            return 0
        removed = 0
        for member in members:
            if member in bucket:
                bucket.discard(member)
                removed += 1
        return removed

    def delete(self, *keys: str) -> int:
        removed = 0
        for key in keys:
            if key in self.sets:
                del self.sets[key]
                removed += 1
            if key in self.hashes:
                del self.hashes[key]
                removed += 1
        return removed


@pytest.fixture()
def fake_redis(monkeypatch: pytest.MonkeyPatch) -> FakeRedis:
    fake = FakeRedis()
    monkeypatch.setattr(lineage, "get_sync_redis_client", lambda client_id: fake)
    return fake


def test_record_and_list_children(fake_redis: FakeRedis) -> None:
    first = lineage.mint_isolated_conversation("parent", tenant_id="t1")
    second = lineage.mint_isolated_conversation("parent", tenant_id="t1")
    children = lineage.list_child_conversations_sync(tenant_id="t1", conversation_id="parent")
    assert children == sorted([first.conversation_id, second.conversation_id])


def test_record_ignores_incomplete(fake_redis: FakeRedis) -> None:
    assert (
        lineage.record_conversation_lineage_sync(
            tenant_id="t1", child_conversation_id="iso-abc"
        )
        is None
    )
    assert fake_redis.sets == {}


def test_record_sets_ttl(fake_redis: FakeRedis) -> None:
    iso = lineage.mint_isolated_conversation("p", tenant_id="t1")
    assert fake_redis.ttls[f"t1:conv:children:p"] == lineage._LINEAGE_TTL_SECONDS
    assert fake_redis.ttls[f"t1:conv:parentage:{iso.conversation_id}"] == lineage._LINEAGE_TTL_SECONDS


def test_record_and_list_swallow_redis_failures(monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom(client_id: str) -> Any:
        raise ConnectionError("redis down")

    monkeypatch.setattr(lineage, "get_sync_redis_client", _boom)
    assert (
        lineage.record_conversation_lineage_sync(
            tenant_id="t1",
            child_conversation_id="iso-abc",
            parent_conversation_id="p",
        )
        is None
    )
    assert lineage.list_child_conversations_sync(tenant_id="t1", conversation_id="p") == []


def test_list_empty_inputs(fake_redis: FakeRedis) -> None:
    assert lineage.list_child_conversations_sync(tenant_id="", conversation_id="p") == []
    assert lineage.list_child_conversations_sync(tenant_id="t1", conversation_id="") == []


def test_list_descendants_includes_nested(fake_redis: FakeRedis) -> None:
    child = lineage.mint_isolated_conversation("root-chat", tenant_id="t1")
    grand = lineage.mint_isolated_conversation(child.conversation_id, tenant_id="t1")
    found = lineage.list_descendant_conversations_sync(
        tenant_id="t1", conversation_id="root-chat"
    )
    assert found == sorted([child.conversation_id, grand.conversation_id])
    assert lineage.list_descendant_conversations_sync(
        tenant_id="t1", conversation_id=child.conversation_id
    ) == [grand.conversation_id]
    assert lineage.list_descendant_conversations_sync(
        tenant_id="t1", conversation_id=grand.conversation_id
    ) == []


def test_forget_removes_parentage_and_index(fake_redis: FakeRedis) -> None:
    child = lineage.mint_isolated_conversation("parent", tenant_id="t1")
    lineage.forget_conversation_lineage_sync(
        tenant_id="t1", conversation_id=child.conversation_id
    )
    assert lineage.list_child_conversations_sync(tenant_id="t1", conversation_id="parent") == []
    assert lineage.root_conversation_id_of(child.conversation_id, tenant_id="t1") is None


def test_forget_and_descendants_swallow_redis_failures(monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom(client_id: str) -> Any:
        raise ConnectionError("redis down")

    monkeypatch.setattr(lineage, "get_sync_redis_client", _boom)
    lineage.forget_conversation_lineage_sync(tenant_id="t1", conversation_id="iso-abc")
    assert lineage.list_descendant_conversations_sync(tenant_id="t1", conversation_id="p") == []
