"""
Motet - HTTP MCP Transport Tests

Copyright (c) 2024-2025 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-06-18

Description:
    Unit tests for Streamable HTTP behavior in HTTPMCPTransport, including
    headers, SSE parsing, and fallback behavior for 202/204 responses.

Dependencies:
    - pytest: test runner
    - unittest.mock: patching async methods
    - motet.core.tools.mcp_motet.transports.http: class under test

Usage:
    pytest tests/unit/tools/mcp_motet/transports/test_http_mcp_transport.py
"""

from __future__ import annotations

import json
from typing import Any, Dict, List
from unittest.mock import AsyncMock, patch

import pytest

from motet.core.tools.mcp_motet.transports.http import HTTPMCPTransport


class _FakeContent:
    def __init__(self, chunks: List[bytes]) -> None:
        self._chunks = chunks

    async def iter_any(self):
        for chunk in self._chunks:
            yield chunk


class _FakeResponse:
    def __init__(
        self,
        *,
        status: int = 200,
        headers: Dict[str, str] | None = None,
        json_payload: Dict[str, Any] | None = None,
        chunks: List[bytes] | None = None,
    ) -> None:
        self.status = status
        self.headers = headers or {}
        self._json_payload = json_payload or {}
        self.content = _FakeContent(chunks or [])

    def raise_for_status(self) -> None:
        return None

    async def json(self) -> Dict[str, Any]:
        return self._json_payload


class _FakeRequestContext:
    def __init__(self, response: _FakeResponse) -> None:
        self._response = response

    async def __aenter__(self) -> _FakeResponse:
        return self._response

    async def __aexit__(self, exc_type, exc, tb) -> bool:
        return False


class _FakeSession:
    def __init__(self, post_response: _FakeResponse, get_response: _FakeResponse | None = None) -> None:
        self._post_response = post_response
        self._get_response = get_response or post_response

    def post(self, *args, **kwargs):
        return _FakeRequestContext(self._post_response)

    def get(self, *args, **kwargs):
        return _FakeRequestContext(self._get_response)


def _build_transport(streamable_http_sse: bool = True) -> HTTPMCPTransport:
    transport = HTTPMCPTransport(
        service_id="test_service",
        config={"base_url": "http://localhost:8100/mcp", "streamable_http_sse": streamable_http_sse},
    )
    transport.is_running = True
    return transport


@pytest.mark.asyncio
async def test_json_request_headers_include_streamable_accept_header():
    transport = _build_transport(streamable_http_sse=True)
    headers = transport._get_json_request_headers()
    assert headers["Content-Type"] == "application/json"
    assert headers["Accept"] == "application/json, text/event-stream"


@pytest.mark.asyncio
async def test_parse_sse_response_supports_multiline_data_events():
    transport = _build_transport(streamable_http_sse=True)
    response_payload = {
        "jsonrpc": "2.0",
        "id": "req-1",
        "result": {"ok": True},
    }
    sse_body = (
        'event: message\n'
        'data: {"jsonrpc":"2.0",\n'
        'data: "id":"req-1","result":{"ok":true}}\n\n'
    ).encode("utf-8")
    response = _FakeResponse(
        headers={"Content-Type": "text/event-stream"},
        chunks=[sse_body],
    )

    result = await transport._parse_sse_response(response, request_id="req-1", timeout_seconds=2)
    assert result == {"ok": True}


@pytest.mark.asyncio
async def test_post_jsonrpc_uses_get_sse_fallback_for_202():
    transport = _build_transport(streamable_http_sse=True)
    transport._initialized = True
    post_response = _FakeResponse(status=202, headers={"Content-Type": "application/json"})
    session = _FakeSession(post_response=post_response)
    transport.session = session

    with patch.object(
        transport, "_consume_sse_via_get", AsyncMock(return_value={"from_get": True})
    ) as mock_consume:
        result = await transport._post_jsonrpc("tools/list", {}, timeout_seconds=3)
        assert result == {"from_get": True}
        assert mock_consume.await_count == 1


@pytest.mark.asyncio
async def test_post_jsonrpc_raises_on_response_id_mismatch():
    transport = _build_transport(streamable_http_sse=False)
    transport._initialized = True
    payload = {"jsonrpc": "2.0", "id": "other-id", "result": {"ok": True}}
    post_response = _FakeResponse(
        status=200,
        headers={"Content-Type": "application/json"},
        json_payload=payload,
    )
    transport.session = _FakeSession(post_response=post_response)

    with pytest.raises(RuntimeError, match="response id mismatch"):
        await transport._post_jsonrpc("tools/list", {}, timeout_seconds=3)


@pytest.mark.asyncio
async def test_post_jsonrpc_auto_initializes_http_session_once():
    transport = _build_transport(streamable_http_sse=False)
    transport.session = _FakeSession(
        post_response=_FakeResponse(
            status=200,
            headers={"Content-Type": "application/json"},
            json_payload={"jsonrpc": "2.0", "id": "x", "result": {}},
        )
    )
    with patch.object(transport, "_ensure_initialized", AsyncMock()) as mock_init:
        with patch.object(transport, "_get_json_request_headers", return_value={"Content-Type": "application/json"}):
            with patch("motet.core.tools.mcp_motet.transports.http.uuid.uuid4", return_value="x"):
                result = await transport._post_jsonrpc("tools/list", {}, timeout_seconds=3)
    assert result == {}
    assert mock_init.await_count == 1
