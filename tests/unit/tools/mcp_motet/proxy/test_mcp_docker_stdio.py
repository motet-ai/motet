"""
Motet - Docker stdio MCP process unit tests

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-13

Description:
    Regression tests for Docker MCP child-death visibility. A killed sidecar
    must set returncode and unblock wait() without hanging the manager loop.

Dependencies:
    - pytest / asyncio
    - motet.core.tools.mcp_motet.proxy.mcp_docker_stdio

Usage:
    pytest tests/unit/tools/mcp_motet/proxy/test_mcp_docker_stdio.py
"""

from __future__ import annotations

import asyncio
import socket
import threading

import pytest

from motet.core.tools.mcp_motet.proxy.mcp_docker_stdio import (
    DockerMCPAsyncProcess,
    DockerStdinWriter,
)


def _make_process() -> tuple[DockerMCPAsyncProcess, socket.socket]:
    peer, raw = socket.socketpair()
    proc = DockerMCPAsyncProcess(
        stdin=DockerStdinWriter(raw, threading.Lock()),
        stdout=asyncio.StreamReader(),
        stderr=asyncio.StreamReader(),
        raw_sock=raw,
        container_id="abc123def456",
        sock_path="/tmp/no.sock",
        api_pfx="/v1.44",
    )
    return proc, peer


@pytest.mark.asyncio
async def test_mark_attach_closed_sets_returncode_and_unblocks_wait() -> None:
    proc, peer = _make_process()
    try:
        proc.mark_attach_closed()
        assert proc.returncode == -1
        code = await asyncio.wait_for(proc.wait(), timeout=1.0)
        assert code == -1
    finally:
        peer.close()


@pytest.mark.asyncio
async def test_wait_unblocks_when_exit_monitor_sets_code() -> None:
    proc, peer = _make_process()
    try:
        waiter = asyncio.create_task(proc.wait())
        await asyncio.sleep(0)
        assert not waiter.done()
        proc._set_returncode(137)
        code = await asyncio.wait_for(waiter, timeout=1.0)
        assert code == 137
        assert proc.returncode == 137
    finally:
        peer.close()


def test_stdin_write_after_close_raises_broken_pipe() -> None:
    peer, raw = socket.socketpair()
    stdin = DockerStdinWriter(raw, threading.Lock())
    peer.close()
    raw.close()
    with pytest.raises(BrokenPipeError):
        stdin.write(b'{"jsonrpc":"2.0","id":1}\n')
