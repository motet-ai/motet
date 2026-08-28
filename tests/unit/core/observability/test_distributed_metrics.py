"""
Motet - Distributed Metrics Collector Tests

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-26

Description:
    Unit tests for DistributedMetricsCollector listing via
    ``worker:metrics:index`` (no Redis KEYS fallback).

Dependencies:
    - pytest
    - unittest.mock

Usage:
    pytest tests/unit/core/observability/test_distributed_metrics.py
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from motet.core.observability.distributed_metrics import (
    DistributedMetricsCollector,
    MetricSample,
)


def _sample_payload(*, name: str = "cpu_usage") -> str:
    return json.dumps(
        {
            "name": name,
            "value": 1.5,
            "labels": {"worker_id": "w1"},
            "timestamp": 1.0,
            "help_text": "",
            "metric_type": "gauge",
        }
    )


@pytest.mark.asyncio
async def test_get_all_metrics_reads_index_not_keys() -> None:
    redis = MagicMock()
    redis.smembers = AsyncMock(return_value={b"w1:cpu_usage"})
    redis.get = AsyncMock(return_value=_sample_payload())
    redis.keys = AsyncMock(return_value=["should-not-be-used"])
    redis.srem = AsyncMock()

    collector = DistributedMetricsCollector(redis)
    rows = await collector.get_all_metrics()

    redis.smembers.assert_awaited_once_with("worker:metrics:index")
    redis.keys.assert_not_called()
    assert len(rows) == 1
    assert rows[0].name == "cpu_usage"
    redis.srem.assert_not_called()


@pytest.mark.asyncio
async def test_get_all_metrics_empty_index_is_empty() -> None:
    redis = MagicMock()
    redis.smembers = AsyncMock(return_value=set())
    redis.keys = AsyncMock(return_value=["worker:metrics:orphan:cpu"])
    redis.get = AsyncMock()

    collector = DistributedMetricsCollector(redis)
    rows = await collector.get_all_metrics()

    assert rows == []
    redis.keys.assert_not_called()
    redis.get.assert_not_called()


@pytest.mark.asyncio
async def test_get_all_metrics_drops_missing_samples() -> None:
    redis = MagicMock()
    redis.smembers = AsyncMock(return_value={"w1:cpu_usage", "ghost:cpu"})
    redis.get = AsyncMock(
        side_effect=lambda key: _sample_payload() if key.endswith("w1:cpu_usage") else None
    )
    redis.srem = AsyncMock()
    redis.keys = AsyncMock()

    collector = DistributedMetricsCollector(redis)
    rows = await collector.get_all_metrics()

    assert [row.name for row in rows] == ["cpu_usage"]
    redis.srem.assert_awaited_once_with("worker:metrics:index", "ghost:cpu")
    redis.keys.assert_not_called()


def test_push_metric_sync_indexes_member() -> None:
    collector = DistributedMetricsCollector(MagicMock())
    sync = MagicMock()
    sample = MetricSample(
        name="cpu_usage",
        value=2.0,
        labels={"worker_id": "w1"},
        timestamp=1.0,
    )

    from unittest.mock import patch

    with patch(
        "motet.core.distributed.redis_manager.get_sync_redis_client",
        return_value=sync,
    ):
        collector.push_metric_sync("w1", sample)

    sync.setex.assert_called_once()
    sync.sadd.assert_called_once_with("worker:metrics:index", "w1:cpu_usage")
