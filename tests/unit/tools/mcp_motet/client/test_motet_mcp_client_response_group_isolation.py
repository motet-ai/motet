"""
Motet - Motet MCP Client Response Group Isolation Tests

Copyright (c) 2024-2025 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-06-18

Description:
    Unit tests for MotetMCPClient response waiting behavior to ensure
    per-request consumer groups are created and cleaned up safely.

Dependencies:
    - pytest: test runner
    - motet.core.tools.mcp_motet.client.motet_mcp_client: class under test

Usage:
    pytest tests/unit/tools/mcp_motet/client/test_motet_mcp_client_response_group_isolation.py
"""

from __future__ import annotations

import pytest

from motet.core.tools.mcp_motet.client.motet_mcp_client import MotetMCPClient


class _FakeRedis:
    def __init__(self) -> None:
        self.xgroup_create_calls = []
        self.xgroup_destroy_calls = []

    def xgroup_create(self, stream, group_name, id, mkstream):
        self.xgroup_create_calls.append((stream, group_name, id, mkstream))
        return True

    def xreadgroup(self, group_name, consumer_name, streams, count, block):
        return []

    def xgroup_destroy(self, stream, group_name):
        self.xgroup_destroy_calls.append((stream, group_name))
        return 1


def test_wait_for_response_uses_per_request_group_and_cleans_up():
    client = MotetMCPClient(manager_id="mgr-test")
    redis_client = _FakeRedis()

    request_id = "req-abc"
    with pytest.raises(TimeoutError):
        client._wait_for_response_sync(
            request_id=request_id,
            response_stream="worker:mcp-responses:test:global",
            timeout_ms=5,
            redis_client=redis_client,
        )

    assert len(redis_client.xgroup_create_calls) == 1
    stream, group_name, start_id, mkstream = redis_client.xgroup_create_calls[0]
    assert group_name == f"manager-{client.manager_id}-{request_id}"
    assert start_id == "$"
    assert mkstream is True

    assert redis_client.xgroup_destroy_calls
    assert redis_client.xgroup_destroy_calls[0][1] == group_name


def test_prepare_response_group_creates_group_before_publish():
    client = MotetMCPClient(manager_id="mgr-test")
    redis_client = _FakeRedis()

    client._prepare_response_group(
        request_id="req-fast",
        response_stream="worker:mcp-responses:test:global",
        redis_client=redis_client,
    )

    assert redis_client.xgroup_create_calls == [
        (
            "worker:mcp-responses:test:global",
            "manager-mgr-test-req-fast",
            "$",
            True,
        )
    ]
