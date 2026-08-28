"""
Motet - Unit tests for conversation lineage

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-18

Description:
Unit tests for motet.core.conversations.lineage: the {parent}__{suffix} child
conversation ID convention (mint/parse round-trip, sanitization, nesting) and
the Redis-backed parent→children index (record/list, best-effort failure).

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


# ---------------------------------------------------------------------------
# Mint / parse convention
# ---------------------------------------------------------------------------


def test_make_child_conversation_id_basic() -> None:
    child = lineage.make_child_conversation_id("api-exec-123", suffix="implement_chunk_0")
    assert child == "api-exec-123__implement_chunk_0"


def test_make_child_sanitizes_suffix() -> None:
    child = lineage.make_child_conversation_id("parent", suffix="review step! #1")
    assert child == "parent__review_step_1"


def test_make_child_generates_base_for_empty_parent() -> None:
    child = lineage.make_child_conversation_id("", suffix="step")
    assert child.startswith("workflow-")
    assert child.endswith("__step")


def test_root_of_round_trips_mint() -> None:
    child = lineage.make_child_conversation_id("api-exec-parent", suffix="implement_chunk_2")
    assert lineage.root_conversation_id_of(child) == "api-exec-parent"


def test_root_of_nested_child_is_top_level_root() -> None:
    assert lineage.root_conversation_id_of("root__a__b") == "root"


def test_root_of_non_child_is_none() -> None:
    assert lineage.root_conversation_id_of("plain-conversation") is None
    assert lineage.root_conversation_id_of("") is None
    assert lineage.root_conversation_id_of(None) is None
    # Separator with empty parent segment is not attributable.
    assert lineage.root_conversation_id_of("__orphan") is None


def test_is_child_conversation_id() -> None:
    assert lineage.is_child_conversation_id("p__c")
    assert not lineage.is_child_conversation_id("plain")
    assert not lineage.is_child_conversation_id("__orphan")
    assert not lineage.is_child_conversation_id(None)


# ---------------------------------------------------------------------------
# Redis-backed parent→children index
# ---------------------------------------------------------------------------


class FakeRedis:
    def __init__(self) -> None:
        self.sets: Dict[str, Set[str]] = {}
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

    def expire(self, key: str, ttl: int) -> bool:
        self.ttls[key] = ttl
        return True


@pytest.fixture()
def fake_redis(monkeypatch: pytest.MonkeyPatch) -> FakeRedis:
    fake = FakeRedis()
    monkeypatch.setattr(lineage, "get_sync_redis_client", lambda client_id: fake)
    return fake


def test_record_and_list_children(fake_redis: FakeRedis) -> None:
    root = lineage.record_conversation_lineage_sync(
        tenant_id="t1", child_conversation_id="parent__implement_chunk_1"
    )
    assert root == "parent"
    lineage.record_conversation_lineage_sync(
        tenant_id="t1", child_conversation_id="parent__implement_chunk_0"
    )
    children = lineage.list_child_conversations_sync(tenant_id="t1", conversation_id="parent")
    assert children == ["parent__implement_chunk_0", "parent__implement_chunk_1"]


def test_record_ignores_non_children(fake_redis: FakeRedis) -> None:
    assert (
        lineage.record_conversation_lineage_sync(tenant_id="t1", child_conversation_id="plain")
        is None
    )
    assert fake_redis.sets == {}


def test_record_sets_ttl(fake_redis: FakeRedis) -> None:
    lineage.record_conversation_lineage_sync(tenant_id="t1", child_conversation_id="p__c")
    key = "t1:conv:children:p"
    assert fake_redis.ttls[key] == lineage._LINEAGE_TTL_SECONDS


def test_record_and_list_swallow_redis_failures(monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom(client_id: str) -> Any:
        raise ConnectionError("redis down")

    monkeypatch.setattr(lineage, "get_sync_redis_client", _boom)
    assert (
        lineage.record_conversation_lineage_sync(tenant_id="t1", child_conversation_id="p__c")
        is None
    )
    assert lineage.list_child_conversations_sync(tenant_id="t1", conversation_id="p") == []


def test_list_empty_inputs(fake_redis: FakeRedis) -> None:
    assert lineage.list_child_conversations_sync(tenant_id="", conversation_id="p") == []
    assert lineage.list_child_conversations_sync(tenant_id="t1", conversation_id="") == []
