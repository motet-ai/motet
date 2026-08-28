"""
Motet - ManagerStatusRegistry unit tests

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-26

Description:
    Tests ManagerStatusRegistry publish and list against the
    ``manager:registered`` membership set, including manager_id values
    that contain colons.

Dependencies:
    - pytest
    - unittest.mock

Usage:
    pytest tests/unit/distributed/test_manager_status_registry.py
"""

from __future__ import annotations

import json
from typing import Any, Dict
from unittest.mock import MagicMock

from motet.core.distributed.manager_status import ManagerStatus, ManagerStatusRegistry, ManagerType


class _RecordingPipeline:
    """Minimal redis-py pipeline that executes queued HGETALLs against the mock."""

    def __init__(self, redis):
        self._redis = redis
        self._keys = []

    def hgetall(self, key):
        self._keys.append(key)
        return self

    def execute(self):
        return [self._redis.hgetall(key) for key in self._keys]


def _status_hash(status: ManagerStatus) -> Dict[str, Any]:
    return {
        k: json.dumps(v) if isinstance(v, (dict, list)) else str(v)
        for k, v in status.to_dict().items()
    }


def _registry() -> ManagerStatusRegistry:
    reg = ManagerStatusRegistry.__new__(ManagerStatusRegistry)
    reg.REDIS_KEY_PREFIX = ManagerStatusRegistry.REDIS_KEY_PREFIX
    reg.REGISTERED_MANAGERS_SET = ManagerStatusRegistry.REGISTERED_MANAGERS_SET
    reg.STATUS_TTL = ManagerStatusRegistry.STATUS_TTL
    reg.redis = MagicMock()
    reg.redis.smembers.return_value = set()
    reg.redis.hgetall.return_value = {}
    reg.redis.keys = MagicMock(return_value=[])
    reg.redis.pipeline = lambda transaction=False: _RecordingPipeline(reg.redis)
    return reg


def test_get_all_statuses_reads_membership_set_not_keys() -> None:
    """Listing uses SMEMBERS of full status keys; manager_id may contain ':'."""
    reg = _registry()
    key = "manager:status:my:complex:manager:id:mcp"
    sample = ManagerStatus(
        worker_id="bootstrap-worker",
        manager_type=ManagerType.MCP,
        status="running",
        last_update=1.0,
        manager_id="my:complex:manager:id",
    )
    reg.redis.smembers.return_value = {key.encode()}
    reg.redis.hgetall.return_value = _status_hash(sample)

    rows = ManagerStatusRegistry.get_all_statuses(reg)

    reg.redis.smembers.assert_called_once_with("manager:registered")
    reg.redis.keys.assert_not_called()
    assert len(rows) == 1
    assert rows[0].manager_id == "my:complex:manager:id"
    assert rows[0].manager_type == ManagerType.MCP


def test_get_all_statuses_skips_missing_hashes() -> None:
    """Expired TTL leaves set members; empty hashes are dropped."""
    reg = _registry()
    live_key = "manager:status:mcp-local-default:mcp"
    ghost_key = "manager:status:expired:mcp"
    sample = ManagerStatus(
        worker_id="mcp-manager",
        manager_type=ManagerType.MCP,
        status="running",
        last_update=1.0,
        manager_id="mcp-local-default",
    )

    def _hgetall(key):
        if key == live_key:
            return _status_hash(sample)
        return {}

    reg.redis.smembers.return_value = {live_key, ghost_key}
    reg.redis.hgetall.side_effect = _hgetall

    rows = ManagerStatusRegistry.get_all_statuses(reg)

    assert [row.manager_id for row in rows] == ["mcp-local-default"]
    reg.redis.srem.assert_called_once_with("manager:registered", ghost_key)
    reg.redis.keys.assert_not_called()


def test_publish_status_keys_redis_by_manager_id_not_worker_id() -> None:
    """ADR-0105 §R3 regression: two managers sharing the same observability
    ``worker_id`` (e.g. both deployments named the service ``mcp-manager``)
    must occupy DISTINCT Redis status keys, otherwise their heartbeats
    overwrite each other and the FE shows rows flickering on/off.
    """
    reg = _registry()

    # Two siblings with identical worker_id but different manager_id.
    ManagerStatusRegistry.publish_status(
        reg,
        worker_id="mcp-manager",
        manager_type=ManagerType.MCP,
        status="running",
        manager_id="mcp-local-default",
    )
    ManagerStatusRegistry.publish_status(
        reg,
        worker_id="mcp-manager",
        manager_type=ManagerType.MCP,
        status="running",
        manager_id="mcp-edge_deviceA",
    )

    keys_written = [call.args[0] for call in reg.redis.hset.call_args_list]
    assert keys_written == [
        "manager:status:mcp-local-default:mcp",
        "manager:status:mcp-edge_deviceA:mcp",
    ], (
        "publish_status must key by manager_id; otherwise siblings sharing a "
        "worker_id observability tag overwrite each other in Redis."
    )
    sadd_keys = [call.args[1] for call in reg.redis.sadd.call_args_list]
    assert sadd_keys == keys_written


def test_manager_status_defaults_for_identity_fields() -> None:
    """ADR-0105 §R3 — ``manager_id`` and ``served_workers`` are additive
    fields with safe defaults so existing in-flight publishers keep working
    until the sibling deployment lands (M1 + M4) and starts populating them
    explicitly from ``MOTET_MCP_MANAGER_ID``.
    """
    status = ManagerStatus(
        worker_id="cloud_worker1",
        manager_type=ManagerType.MCP,
        status="running",
        last_update=1.0,
    )

    assert status.manager_id == ""
    assert status.served_workers == []


def test_publish_status_synthesizes_manager_identity_when_omitted() -> None:
    """ADR-0105 §R3 back-compat — when the legacy in-worker publisher calls
    ``publish_status`` without the new ``manager_id`` / ``served_workers``
    args, the registry synthesizes ``manager_id`` from worker_id+type and
    sets ``served_workers=[worker_id]`` so the FE always sees populated
    identity fields. Once M1 + M4 land, real publishers pass these
    explicitly and the synthesis is bypassed.
    """
    reg = _registry()
    reg._get_redis_key = MagicMock(return_value="key")

    ManagerStatusRegistry.publish_status(
        reg,
        worker_id="cloud_worker1",
        manager_type=ManagerType.MCP,
        status="running",
    )

    assert reg.redis.hset.called
    mapping = reg.redis.hset.call_args.kwargs["mapping"]
    assert mapping["manager_id"] == "mcp-cloud_worker1"
    reg.redis.sadd.assert_called_once_with("manager:registered", "key")
    assert json.loads(mapping["served_workers"]) == ["cloud_worker1"]


def test_publish_status_honors_explicit_manager_identity() -> None:
    """When the sibling manager publishes, its ``MOTET_MCP_MANAGER_ID`` and
    served-workers list flow through unchanged.
    """
    reg = _registry()
    reg._get_redis_key = MagicMock(return_value="key")

    ManagerStatusRegistry.publish_status(
        reg,
        worker_id="bootstrap-worker",
        manager_type=ManagerType.MCP,
        status="running",
        manager_id="mcp-edge_deviceA",
        served_workers=["edge_worker1", "edge_worker2"],
    )

    mapping = reg.redis.hset.call_args.kwargs["mapping"]
    assert mapping["manager_id"] == "mcp-edge_deviceA"
    assert json.loads(mapping["served_workers"]) == ["edge_worker1", "edge_worker2"]
    reg.redis.sadd.assert_called_once_with("manager:registered", "key")
