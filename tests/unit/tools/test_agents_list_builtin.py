"""
Motet - Built-in agents_list Tool Tests

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-06-18
"""

from __future__ import annotations

from unittest.mock import patch

from motet.core.tools.builtin.agents_list import register, run
from motet.core.tools.registry import ToolRegistry


def test_agents_list_run_returns_result_payload() -> None:
    """run() should return ok(result=...) payload with filtered agents."""
    fake_agents = [
        {
            "qualified_id": "core.default",
            "bundle_id": None,
            "display_name": "Motet Agent",
            "description": "Core",
        },
        {
            "qualified_id": "agent-configured.support",
            "bundle_id": "agent-configured",
            "display_name": "Support Agent",
            "description": "Bundle",
        },
    ]
    with patch(
        "motet.core.agents.discovery.list_visible_agents",
        return_value=fake_agents,
    ):
        out = run({"bundle_id": "core"})

    assert out.get("status") == "success"
    result = out.get("result", {})
    assert result.get("total") == 1
    assert result.get("agents", [])[0].get("qualified_id") == "core.default"


def test_agents_list_registers_tool() -> None:
    """register() should add core.agents_list into the registry."""
    registry = ToolRegistry()
    register(registry)
    assert registry.supports("core.agents_list")


def test_agents_list_is_eagerly_registered_in_core_tools() -> None:
    """core.tools eager built-in registration should include core.agents_list."""
    from motet.core.tools import registry as global_registry

    assert global_registry.supports("core.agents_list")

