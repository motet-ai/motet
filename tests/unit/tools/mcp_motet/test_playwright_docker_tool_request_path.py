"""Playwright MCP + Docker stdio: backend selection and stdin delivery of tools/call."""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock

import pytest

from motet.core.execution.mcp_backend import mcp_exec_uses_docker
from motet.core.tools.mcp_motet.protocol import StreamType
from motet.core.tools.mcp_motet.proxy import mcp_docker_stdio
from motet.core.tools.mcp_motet.proxy.motet_mcp_proxy import MCPServerConfig, MotetMCPProxy


@pytest.fixture
def docker_mcp_exec_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """MCP Docker even when worker one-shot exec is subprocess (decoupling)."""
    monkeypatch.setenv("MOTET_MCP_EXEC_BACKEND", "docker")
    monkeypatch.setenv("MOTET_EXEC_BACKEND", "subprocess")
    assert mcp_exec_uses_docker()


@pytest.mark.asyncio
async def test_playwright_docker_stdio_receives_tools_call_on_stdin(
    monkeypatch: pytest.MonkeyPatch,
    docker_mcp_exec_env: None,
) -> None:
    """
    With MOTET_MCP_EXEC_BACKEND=docker, Playwright startup goes through
    start_mcp_stdio_docker; downstream tools/call JSON-RPC must be written to the
    same stdin object (Docker attach / DockerStdinWriter in production).
    """
    captured_calls: list[dict[str, object]] = []

    class RecordingStdin:
        def __init__(self) -> None:
            self.payloads: list[bytes] = []

        def write(self, data: bytes) -> None:
            self.payloads.append(data)

        async def drain(self) -> None:
            pass

    async def fake_start_mcp_stdio_docker(**kwargs: object) -> object:
        captured_calls.append(dict(kwargs))
        rec = RecordingStdin()

        class FakeProc:
            stdin = rec
            stdout = asyncio.StreamReader()
            stderr = asyncio.StreamReader()
            pid = 4_242_424
            returncode: int | None = None

        return FakeProc()

    monkeypatch.setattr(
        mcp_docker_stdio,
        "start_mcp_stdio_docker",
        fake_start_mcp_stdio_docker,
    )
    monkeypatch.setattr(MotetMCPProxy, "_send_mcp_initialization", AsyncMock())

    pw_image = "mcr.microsoft.com/playwright:v1.58.2-noble"
    cfg = MCPServerConfig(
        server_id="playwright",
        command="npx",
        args=["-y", "@playwright/mcp"],
        env={"PLAYWRIGHT_MCP_HEADLESS": "true"},
        exec_image=pw_image,
    )
    instance_key = "playwright:tenant:motet:user"
    proxy = MotetMCPProxy(cfg, context_id=instance_key)

    await proxy._start_mcp_server()

    assert len(captured_calls) == 1
    assert captured_calls[0]["server_id"] == "playwright"
    assert captured_calls[0]["exec_image"] == pw_image
    assert captured_calls[0]["command"] == "npx"

    bridge_ack = AsyncMock()
    monkeypatch.setattr(proxy.stream_bridge, "acknowledge_message", bridge_ack)

    tool_rpc = {
        "jsonrpc": "2.0",
        "id": "req-tool-1",
        "method": "tools/call",
        "params": {
            "name": "browser_navigate",
            "arguments": {"url": "https://example.com"},
        },
    }
    await proxy._handle_request_message(
        {
            "message_id": "mid-1",
            "message_data": {
                "id": "req-tool-1",
                "stream_type": StreamType.REQUESTS,
                "service_id": "playwright",
                "instance_key": instance_key,
                "jsonrpc_request": tool_rpc,
            },
        }
    )

    stdin = proxy.mcp_process.stdin
    assert isinstance(stdin, RecordingStdin)
    assert stdin.payloads, "expected tools/call bytes on MCP stdin"
    last_line = stdin.payloads[-1].decode("utf-8").strip()
    parsed = json.loads(last_line)
    assert parsed["method"] == "tools/call"
    assert parsed["params"]["name"] == "browser_navigate"
    assert parsed["params"]["arguments"]["url"] == "https://example.com"

    bridge_ack.assert_awaited_once()
