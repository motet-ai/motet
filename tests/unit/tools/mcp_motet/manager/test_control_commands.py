"""
Motet - MCP control-plane enqueue unit tests

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-13

Description:
    Validation for Redis control-stream command enqueue (no Redis I/O).

Dependencies:
    - motet.core.tools.mcp_motet.manager.control_commands

Usage:
    pytest tests/unit/tools/mcp_motet/manager/test_control_commands.py
"""

import pytest

from motet.core.tools.mcp_motet.manager.control_commands import (
    MCP_CONTROL_OPS,
    enqueue_mcp_control_command,
    mcp_control_stream_key,
)


def test_stream_key() -> None:
    assert mcp_control_stream_key("mcp-local-default") == "mcp-local-default:mcp-control"


def test_enqueue_requires_manager_id() -> None:
    with pytest.raises(ValueError, match="manager_id"):
        enqueue_mcp_control_command("", {"op": "restart", "service_id": "weather"})


def test_enqueue_rejects_unknown_op() -> None:
    with pytest.raises(ValueError, match="Unsupported"):
        enqueue_mcp_control_command("mcp-x", {"op": "scale", "service_id": "weather"})


def test_enqueue_requires_service_id() -> None:
    with pytest.raises(ValueError, match="service_id"):
        enqueue_mcp_control_command("mcp-x", {"op": "restart"})


def test_known_ops() -> None:
    assert MCP_CONTROL_OPS == {"register", "unregister", "restart", "disable", "enable"}
