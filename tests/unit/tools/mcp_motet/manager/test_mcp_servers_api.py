"""
Motet - MCP Servers API payload unit tests

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-13

Description:
    Maps Redis per-service status records onto GET /api/v1/mcp/servers entries.

Dependencies:
    - motet.interfaces.api.v1.mcp
    - motet.core.tools.mcp_motet.manager.service_status

Usage:
    pytest tests/unit/tools/mcp_motet/manager/test_mcp_servers_api.py
"""

from motet.core.tools.mcp_motet.manager.service_status import MCPServiceStatus
from motet.interfaces.api.v1.mcp import MCPServerEntry


def test_server_entry_from_status() -> None:
    rec = MCPServiceStatus(
        service_id="weather",
        manager_id="mcp-local-default",
        status="running",
        healthy=True,
        transport="stdio",
        visibility="motet",
        lifecycle_duration="permanent",
        state_model="stateless",
        auth_type="none",
        instance_count=1,
        instance_ids=["weather:t:m"],
        pids=[42],
        restart_count_window=0,
        restart_budget_remaining=3,
        tool_names=["get_forecast"],
        updated_at=1.0,
        disabled=False,
    )
    entry = MCPServerEntry(**rec.model_dump(), tool_count=len(rec.tool_names))
    assert entry.tool_count == 1
    assert entry.service_id == "weather"
    assert entry.healthy is True
