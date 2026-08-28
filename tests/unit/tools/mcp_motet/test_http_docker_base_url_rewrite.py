"""
Motet - Unit tests for HTTP MCP Docker base_url rewriting

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-13

Description:
    Ensures localhost / 127.0.0.1 base URLs used for subprocess HTTP MCP are
    rewritten to a Docker-reachable host when the sidecar path is used.

Dependencies:
    - pytest

Usage:
    pytest tests/unit/tools/mcp_motet/test_http_docker_base_url_rewrite.py

Notes:
    - Complements HTTPMCPTransport Docker sidecar integration (Phase 2).
"""

from __future__ import annotations

import pytest

from motet.core.tools.mcp_motet.transports.http import (
    mcp_http_client_host_for_docker_sidecar,
    rewrite_localhost_base_url_for_mcp_docker_sidecar,
)


def test_rewrite_localhost_to_default_client_host() -> None:
    out = rewrite_localhost_base_url_for_mcp_docker_sidecar("http://localhost:3301/mcp", 3301)
    assert out == "http://host.docker.internal:3301/mcp"


def test_rewrite_127_to_custom_client_host(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MOTET_MCP_HTTP_CLIENT_HOST", "172.17.0.1")
    out = rewrite_localhost_base_url_for_mcp_docker_sidecar("http://127.0.0.1:8100/mcp", 8100)
    assert out == "http://172.17.0.1:8100/mcp"
    assert mcp_http_client_host_for_docker_sidecar() == "172.17.0.1"


def test_no_rewrite_when_host_is_explicit() -> None:
    url = "http://mcp.internal:9000/mcp"
    assert rewrite_localhost_base_url_for_mcp_docker_sidecar(url, 9000) == url


def test_rewrite_uses_config_port_when_url_has_no_port() -> None:
    out = rewrite_localhost_base_url_for_mcp_docker_sidecar("http://localhost/mcp", 3301)
    assert out == "http://host.docker.internal:3301/mcp"


def test_rewrite_uses_mapped_host_port_even_when_url_has_port() -> None:
    out = rewrite_localhost_base_url_for_mcp_docker_sidecar("http://localhost:3301/mcp", 3302)
    assert out == "http://host.docker.internal:3302/mcp"


def test_attach_client_rewrites_localhost_when_docker_backend(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MOTET_MCP_EXEC_BACKEND", "docker")
    monkeypatch.delenv("MOTET_EXEC_BACKEND", raising=False)
    from motet.core.tools.mcp_motet.transports.http import HTTPMCPTransport

    transport = HTTPMCPTransport(
        "everything_http_test",
        {
            "start_server": False,
            "base_url": "http://127.0.0.1:3301/mcp",
            "port": 3301,
        },
        worker_id="mcp-local-default",
    )
    transport._rewrite_base_url_for_docker_sidecar_client()
    assert transport.base_url == "http://host.docker.internal:3301/mcp"


def test_http_singleton_attach_copies_owner_rewritten_url() -> None:
    from motet.core.tools.mcp_motet.manager.config import MCPInstance, MCPInstanceConfig
    from motet.core.tools.mcp_motet.proxy.mcp_instance_manager import MCPInstanceManager

    mgr = MCPInstanceManager(config_dict={"services": []})
    cfg = MCPInstanceConfig(
        service_id="everything_http_test",
        command="true",
        transport="http",
        start_server=True,
        base_url="http://127.0.0.1:3301/mcp",
        port=3301,
        startup_timeout_seconds=90,
    )
    owner_transport = type(
        "T",
        (),
        {
            "base_url": "http://host.docker.internal:3301/mcp",
            "_process": type("P", (), {"host_port": 3301})(),
        },
    )()
    owner = MCPInstance(
        instance_id="everything_http_test:discovery-tenant:default",
        service_id="everything_http_test",
        context_id="everything_http_test:discovery-tenant:default",
        transport=owner_transport,
        created_at=0.0,
        last_used=0.0,
    )
    settings = mgr._http_singleton_attach_client_settings(cfg, owner)
    assert settings["base_url"] == "http://host.docker.internal:3301/mcp"
    assert settings["port"] == 3301
    assert settings["startup_timeout_seconds"] == 10
