"""
Motet - Unit tests for conversation cost rollups

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-17

Description:
Unit tests for per-conversation running cost totals in CostTrackingService
(ADR-0122 / ADR-0018): _update_conversation_totals write path (hash increments,
model/provider sets, isolate_conversation child indexing) and
get_conversation_cost_summary read path (exact O(1) totals, include_children
rollup, empty/error handling). Uses a fake Redis — no stream scan involved.

Dependencies:
- pytest
- motet.core.cost.cost_tracking_service

Usage:
  pytest tests/unit/core/cost/test_conversation_cost_summary.py -q
"""

from __future__ import annotations

from typing import Any, Dict, Set

import pytest

import motet.core.cost.cost_tracking_service as cts
from motet.core.cost.cost_tracking_service import CostTrackingService


class FakeRedis:
    """Minimal Redis fake: hashes, sets, pipeline with immediate apply."""

    def __init__(self) -> None:
        self.hashes: Dict[str, Dict[str, Any]] = {}
        self.sets: Dict[str, Set[str]] = {}
        self.ttls: Dict[str, int] = {}

    # -- pipeline (applies immediately; execute is a no-op flush) --
    def pipeline(self) -> "FakeRedis":
        return self

    def execute(self) -> list:
        return []

    # -- hash ops --
    def hincrbyfloat(self, key: str, field: str, amount: float) -> float:
        h = self.hashes.setdefault(key, {})
        h[field] = float(h.get(field, 0.0)) + float(amount)
        return h[field]

    def hincrby(self, key: str, field: str, amount: int) -> int:
        h = self.hashes.setdefault(key, {})
        h[field] = int(h.get(field, 0)) + int(amount)
        return h[field]

    def hgetall(self, key: str) -> Dict[str, str]:
        return {k: str(v) for k, v in self.hashes.get(key, {}).items()}

    # -- set ops --
    def sadd(self, key: str, *members: str) -> int:
        s = self.sets.setdefault(key, set())
        before = len(s)
        s.update(members)
        return len(s) - before

    def smembers(self, key: str) -> Set[str]:
        return set(self.sets.get(key, set()))

    # -- misc --
    def expire(self, key: str, ttl: int) -> bool:
        self.ttls[key] = ttl
        return True

    def exists(self, key: str) -> int:
        return 1 if key in self.hashes or key in self.sets else 0


@pytest.fixture()
def fake_redis(monkeypatch: pytest.MonkeyPatch) -> FakeRedis:
    fake = FakeRedis()
    monkeypatch.setattr(cts, "get_sync_redis_client", lambda client_id: fake)
    return fake


def _event(
    *,
    cost_usd: float,
    total_tokens: int = 0,
    prompt_tokens: int = 0,
    output_tokens: int = 0,
    model: str = "grok-4.5",
    provider: str = "xai",
) -> Dict[str, Any]:
    return {
        "cost_usd": cost_usd,
        "full_cost_usd": cost_usd,
        "prompt_tokens": prompt_tokens,
        "output_tokens": output_tokens,
        "total_tokens": total_tokens,
        "model": model,
        "provider": provider,
    }


def test_totals_write_and_exact_read(fake_redis: FakeRedis) -> None:
    service = CostTrackingService()
    service._update_conversation_totals(
        "motet-global", "cid-a", _event(cost_usd=0.10, prompt_tokens=100, output_tokens=20, total_tokens=120)
    )
    service._update_conversation_totals(
        "motet-global", "cid-a", _event(cost_usd=0.05, prompt_tokens=50, output_tokens=10, total_tokens=60)
    )
    # Unrelated conversation must not leak in.
    service._update_conversation_totals("motet-global", "cid-b", _event(cost_usd=9.99, total_tokens=999))

    summary = service.get_conversation_cost_summary("motet-global", "cid-a")
    assert summary["event_count"] == 2
    assert summary["cost_usd"] == 0.15
    assert summary["prompt_tokens"] == 150
    assert summary["output_tokens"] == 30
    assert summary["total_tokens"] == 180
    assert summary["models"] == ["grok-4.5"]
    assert summary["providers"] == ["xai"]
    assert summary["child_conversation_ids"] == []


def test_include_children_rollup(fake_redis: FakeRedis) -> None:
    service = CostTrackingService()
    parent = "api-exec-parent"
    service._update_conversation_totals("motet-global", parent, _event(cost_usd=0.10, total_tokens=10))
    service._update_conversation_totals(
        "motet-global", f"{parent}__implement_chunk_0", _event(cost_usd=0.20, total_tokens=20)
    )
    service._update_conversation_totals(
        "motet-global",
        f"{parent}__review",
        _event(cost_usd=0.05, total_tokens=5, model="gpt-5.2", provider="openai"),
    )
    # Similar-looking id without the __ separator is a different conversation.
    service._update_conversation_totals("motet-global", f"{parent}Xother", _event(cost_usd=9.99, total_tokens=999))

    exact = service.get_conversation_cost_summary("motet-global", parent)
    assert exact["event_count"] == 1
    assert exact["cost_usd"] == 0.10

    rolled = service.get_conversation_cost_summary("motet-global", parent, include_children=True)
    assert rolled["event_count"] == 3
    assert rolled["cost_usd"] == 0.35
    assert rolled["total_tokens"] == 35
    assert rolled["include_children"] is True
    assert rolled["child_conversation_ids"] == [
        f"{parent}__implement_chunk_0",
        f"{parent}__review",
    ]
    assert set(rolled["models"]) == {"grok-4.5", "gpt-5.2"}
    assert set(rolled["providers"]) == {"xai", "openai"}


def test_children_indexed_under_root_not_immediate_parent(fake_redis: FakeRedis) -> None:
    """Nested child ids (parent__a__b) roll up to the root parent."""
    service = CostTrackingService()
    service._update_conversation_totals("t", "root__a__b", _event(cost_usd=0.01, total_tokens=1))
    assert "root__a__b" in fake_redis.smembers("t:cost:conversation:t:root:children")


def test_totals_keys_have_ttl(fake_redis: FakeRedis) -> None:
    service = CostTrackingService()
    service._update_conversation_totals("t", "cid-ttl", _event(cost_usd=0.01, total_tokens=1))
    base = "t:cost:conversation:t:cid-ttl"
    assert fake_redis.ttls[base] == cts._CONVERSATION_TOTALS_TTL_SECONDS
    assert fake_redis.ttls[f"{base}:models"] == cts._CONVERSATION_TOTALS_TTL_SECONDS


def test_summary_empty_without_id(fake_redis: FakeRedis) -> None:
    service = CostTrackingService()
    summary = service.get_conversation_cost_summary("t", "")
    assert summary["event_count"] == 0
    assert summary["cost_usd"] == 0.0
    assert summary["conversation_id"] is None


def test_summary_zero_for_unknown_conversation(fake_redis: FakeRedis) -> None:
    service = CostTrackingService()
    summary = service.get_conversation_cost_summary("t", "never-seen")
    assert summary["event_count"] == 0
    assert summary["cost_usd"] == 0.0
    assert summary["models"] == []


def test_summary_returns_error_shape_on_redis_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom(client_id: str) -> Any:
        raise ConnectionError("redis down")

    monkeypatch.setattr(cts, "get_sync_redis_client", _boom)
    service = CostTrackingService()
    summary = service.get_conversation_cost_summary("t", "cid-x")
    assert summary["event_count"] == 0
    assert summary["cost_usd"] == 0.0
    assert "redis down" in summary["error"]


def test_totals_write_failure_is_swallowed(monkeypatch: pytest.MonkeyPatch) -> None:
    """Write path is non-critical: failures log but never raise."""

    def _boom(client_id: str) -> Any:
        raise ConnectionError("redis down")

    monkeypatch.setattr(cts, "get_sync_redis_client", _boom)
    service = CostTrackingService()
    service._update_conversation_totals("t", "cid-x", _event(cost_usd=0.01, total_tokens=1))
