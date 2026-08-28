"""
Motet - Memory browse helper tests

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-25

Description:
    Unit tests for manage-app memory filter/stats helpers (no Redis).

Dependencies:
    - pytest
    - motet.core.types.MemoryItem
    - motet.interfaces.api.shared.memory_ops

Usage:
    pytest tests/unit/interfaces/api/test_memory_ops.py -q
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from motet.core.types import MemoryItem
from motet.interfaces.api.shared.memory_ops import (
    _scan_finished,
    collect_memories_for_scope,
    compute_memory_stats,
    filter_memories,
    memory_agent_id,
    memory_index_scan_patterns,
    memory_tier,
)


def _item(
    *,
    item_id: str,
    content: str,
    mem_type: str = "note",
    tags: list[str] | None = None,
    conversation_id: str | None = None,
    created_at: datetime | None = None,
    scope_type: str = "conversation",
    metadata: dict | None = None,
) -> MemoryItem:
    return MemoryItem(
        id=item_id,
        type=mem_type,
        content=content,
        tags=tags or [],
        conversation_id=conversation_id,
        created_at=created_at or datetime.now(timezone.utc),
        scope_type=scope_type,
        tenant_id="acme",
        motet_id="production",
        metadata=metadata or {},
    )


def test_scan_finished_accepts_string_and_bytes_zero() -> None:
    assert _scan_finished(0) is True
    assert _scan_finished("0") is True
    assert _scan_finished(b"0") is True
    assert _scan_finished(12) is False


def test_collect_memories_uses_recent_and_skips_mismatched_default_store(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "motet.interfaces.api.shared.memory_ops.get_redis_manager",
        lambda: (_ for _ in ()).throw(RuntimeError("no redis")),
    )
    class _Store:
        def __init__(self, tenant_id: str) -> None:
            self._tenant_id = tenant_id
            self._motet_id = "production"
            self.all_calls = 0
            self.recent_limits: list[int] = []

        def all(self, scope: str = "global") -> list[MemoryItem]:
            self.all_calls += 1
            return [_item(item_id="all-1", content="should not decrypt")]

        def recent(self, limit: int = 5, tag: str | None = None, scope: str = "global") -> list[MemoryItem]:
            self.recent_limits.append(limit)
            return [_item(item_id="recent-1", content="newest")]

    default_store = _Store("other-tenant")
    stack = type("Stack", (), {"memory": default_store})()

    items = collect_memories_for_scope(stack, "acme", "production", limit=25)
    assert items == []
    assert default_store.all_calls == 0
    assert default_store.recent_limits == []

    matching = _Store("acme")
    stack.memory = matching
    items = collect_memories_for_scope(stack, "acme", "production", limit=25)
    assert [item.id for item in items] == ["recent-1"]
    assert matching.all_calls == 0
    assert matching.recent_limits == [25]


def test_memory_index_scan_patterns_cover_prefixed_and_legacy() -> None:
    assert memory_index_scan_patterns("acme", "default") == (
        "acme:mem:default:idx:global",
    )
    patterns = memory_index_scan_patterns(None, None)
    assert "*:mem:*:idx:global" in patterns
    assert "*:imf:mem:*:*:idx:global" not in patterns


def test_memory_tier_prefers_ltm() -> None:
    item = _item(item_id="1", content="x", tags=["wm", "stm", "ltm"])
    assert memory_tier(item) == "ltm"


def test_filter_memories_contains_type_tier_and_conversation() -> None:
    now = datetime.now(timezone.utc)
    items = [
        _item(
            item_id="1",
            content="Quarterly goals",
            mem_type="note",
            tags=["ltm", "docs"],
            conversation_id="conv-a",
            created_at=now,
        ),
        _item(
            item_id="2",
            content="hello there",
            mem_type="user_message",
            tags=["wm"],
            conversation_id="conv-b",
            created_at=now,
        ),
    ]
    assert [m.id for m in filter_memories(items, query="goals")] == ["1"]
    assert [m.id for m in filter_memories(items, query="docs")] == ["1"]
    assert [m.id for m in filter_memories(items, memory_type="user_message")] == ["2"]
    assert [m.id for m in filter_memories(items, tier="ltm")] == ["1"]
    assert [m.id for m in filter_memories(items, conversation_id="conv-b")] == ["2"]


def test_filter_memories_by_agent_id_tag_and_short_name() -> None:
    items = [
        _item(
            item_id="1",
            content="from default",
            tags=["ltm", "agent:core.default"],
            metadata={"agent_id": "core.default"},
        ),
        _item(
            item_id="2",
            content="from research",
            tags=["agent:core.research"],
        ),
        _item(item_id="3", content="no agent", tags=["ltm"]),
    ]
    assert memory_agent_id(items[0]) == "core.default"
    assert memory_agent_id(items[1]) == "core.research"
    assert [m.id for m in filter_memories(items, agent="core.default")] == ["1"]
    assert [m.id for m in filter_memories(items, agent="agent:core.research")] == ["2"]
    assert [m.id for m in filter_memories(items, agent="default")] == ["1"]
    assert [m.id for m in filter_memories(items, agent="research")] == ["2"]


def test_compute_memory_stats_last_24h_and_breakdowns() -> None:
    now = datetime(2026, 8, 25, 12, 0, tzinfo=timezone.utc)
    items = [
        _item(
            item_id="new",
            content="fresh",
            tags=["ltm", "agent:core.default"],
            created_at=now - timedelta(hours=2),
            scope_type="global",
            metadata={"agent_id": "core.default"},
        ),
        _item(
            item_id="old",
            content="stale",
            mem_type="summary",
            tags=["stm"],
            created_at=now - timedelta(days=3),
            scope_type="conversation",
        ),
        _item(
            item_id="plain",
            content="no tier",
            created_at=now - timedelta(hours=1),
        ),
    ]
    stats = compute_memory_stats(items, vector_enabled=True, now=now)
    assert stats["total_memories"] == 3
    assert stats["last_24h"] == 2
    assert stats["memory_types"] == 2
    assert stats["tagged_count"] == 2
    assert stats["type_breakdown"]["note"] == 2
    assert stats["type_breakdown"]["summary"] == 1
    assert stats["tier_breakdown"]["ltm"] == 1
    assert stats["tier_breakdown"]["stm"] == 1
    assert stats["tier_breakdown"]["untagged"] == 1
    assert stats["agent_breakdown"]["core.default"] == 1
    assert stats["agent_breakdown"]["unattributed"] == 2
    assert stats["vector_enabled"] is True
