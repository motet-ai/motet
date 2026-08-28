"""
Motet - MCP Control Plane Health Tests

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-26

Description:
    Unit tests for the sibling MCP manager /health payload, including the
    Motet product version stamp used by GET /api/v1/version.

Dependencies:
    - pytest: Test framework
    - motet.core.tools.mcp_motet.manager.control_plane: ControlPlaneMixin
    - motet._version: Expected product version

Usage:
    pytest tests/unit/tools/mcp_motet/manager/test_control_plane_health.py -q
"""

from __future__ import annotations

import json

import pytest

from motet._version import get_version
from motet.core.tools.mcp_motet.manager.control_plane import ControlPlaneMixin


class _HealthManager(ControlPlaneMixin):
    def __init__(self) -> None:
        self.running = True
        self.service_configs: dict = {}
        self.instances: dict = {}
        self.stats: dict = {}


@pytest.mark.asyncio
async def test_handle_health_includes_motet_version() -> None:
    response = await _HealthManager()._handle_health(None)
    payload = json.loads(response.body.decode())
    assert payload["motet_version"] == get_version()
    assert payload["status"] == "healthy"
    assert response.status == 200
