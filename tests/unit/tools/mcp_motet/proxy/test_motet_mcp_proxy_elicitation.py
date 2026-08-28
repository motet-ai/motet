"""
Motet - Motet MCP Proxy Elicitation Tests

Copyright (c) 2024-2025 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-06-18

Description:
    Unit tests for server-initiated MCP request handling in MotetMCPProxy,
    focused on full elicitation support introduced in ADR-0076.

Dependencies:
    - pytest: Test runner
    - unittest.mock: AsyncMock and patch support
    - motet.core.tools.mcp_motet.proxy.motet_mcp_proxy: Class under test

Usage:
    pytest tests/unit/tools/mcp_motet/proxy/test_motet_mcp_proxy_elicitation.py
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import pytest

from motet.core.tools.mcp_motet.proxy.motet_mcp_proxy import (
    MotetMCPProxy,
    MCPServerConfig,
)


class _FakeStdin:
    def __init__(self) -> None:
        self.writes: list[bytes] = []

    def write(self, data: bytes) -> None:
        self.writes.append(data)

    async def drain(self) -> None:
        return None


class _FakeProcess:
    def __init__(self) -> None:
        self.stdin = _FakeStdin()
        self.stdout = AsyncMock()
        self.stderr = AsyncMock()
        self.returncode = None


@pytest.fixture
def proxy() -> MotetMCPProxy:
    config = MCPServerConfig(server_id="weather", command="echo", args=["ok"])
    with patch(
        "motet.core.tools.mcp_motet.proxy.motet_mcp_proxy.MotetMCPStreamBridge"
    ) as bridge_cls:
        bridge = AsyncMock()
        bridge_cls.return_value = bridge
        created = MotetMCPProxy(config=config, context_id="weather:global")
        created.mcp_process = _FakeProcess()
        return created


@pytest.mark.asyncio
async def test_elicitation_request_form_responds_accept_with_content(proxy: MotetMCPProxy, monkeypatch):
    monkeypatch.setenv("MOTET_MCP_ELICITATION_FORM_POLICY", "auto_accept_defaults")
    monkeypatch.setenv("MOTET_MCP_ELICITATION_MODES", "form,url")

    request = {
        "jsonrpc": "2.0",
        "id": "elic-1",
        "method": "elicitation/create",
        "params": {
            "mode": "form",
            "message": "Need details",
            "requestedSchema": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "default": "octocat"},
                    "age": {"type": "integer"},
                    "enabled": {"type": "boolean"},
                },
                "required": ["name", "age", "enabled"],
            },
        },
    }

    await proxy._handle_server_request(request)

    assert proxy.mcp_process is not None
    writes = proxy.mcp_process.stdin.writes
    assert len(writes) == 1
    response = json.loads(writes[0].decode("utf-8").strip())
    assert response["id"] == "elic-1"
    assert response["result"]["action"] == "accept"
    assert response["result"]["content"] == {
        "name": "octocat",
        "age": 0,
        "enabled": False,
    }


@pytest.mark.asyncio
async def test_elicitation_request_url_accept_policy(proxy: MotetMCPProxy, monkeypatch):
    monkeypatch.setenv("MOTET_MCP_ELICITATION_URL_POLICY", "accept")

    request = {
        "jsonrpc": "2.0",
        "id": "elic-url-1",
        "method": "elicitation/create",
        "params": {
            "mode": "url",
            "message": "Authorize access",
            "elicitationId": "abc",
            "url": "https://example.com/connect",
        },
    }

    await proxy._handle_server_request(request)

    writes = proxy.mcp_process.stdin.writes
    response = json.loads(writes[-1].decode("utf-8").strip())
    assert response["id"] == "elic-url-1"
    assert response["result"] == {"action": "accept"}


@pytest.mark.asyncio
async def test_unknown_server_request_returns_method_not_found(proxy: MotetMCPProxy):
    request = {
        "jsonrpc": "2.0",
        "id": "srv-unknown-1",
        "method": "foo/bar",
        "params": {},
    }

    await proxy._handle_server_request(request)

    writes = proxy.mcp_process.stdin.writes
    response = json.loads(writes[-1].decode("utf-8").strip())
    assert response["id"] == "srv-unknown-1"
    assert response["error"]["code"] == -32601


@pytest.mark.asyncio
async def test_initialize_advertises_elicitation_modes_and_protocol(proxy: MotetMCPProxy, monkeypatch):
    monkeypatch.setenv("MOTET_MCP_ELICITATION_MODES", "form,url")

    # First read satisfies initialize response.
    proxy.mcp_process.stdout.readline = AsyncMock(
        side_effect=[b'{"jsonrpc":"2.0","id":"init-ok","result":{"capabilities":{}}}\n']
    )
    proxy.mcp_process.stderr.read = AsyncMock(return_value=b"")

    await proxy._send_mcp_initialization()

    writes = proxy.mcp_process.stdin.writes
    assert len(writes) == 2  # initialize + notifications/initialized

    initialize = json.loads(writes[0].decode("utf-8").strip())
    assert initialize["method"] == "initialize"
    assert initialize["params"]["protocolVersion"] == "2025-11-25"
    assert initialize["params"]["capabilities"]["elicitation"] == {
        "form": {},
        "url": {},
    }
