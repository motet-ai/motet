"""
Motet - Reasoning Strategy Deletion Tests

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-22

Description:
    Guards the current shape: the agent loop is the only executor, and
    parallel work is ``core.spawn_agents``. These assert that no extra
    reasoning packages import.

Dependencies:
    - pytest: test framework
    - motet.core.tools.builtin.spawn_agents: the fan-out tool

Usage:
    pytest tests/integration/test_reasoning_strategies.py

Notes:
    - Behavioral coverage of fan-out lives in tests/unit/core/test_spawn_agents.py.
"""

from __future__ import annotations

import pytest


@pytest.mark.parametrize(
    "module",
    [
        "motet.core.reasoning.tot",
        "motet.core.reasoning.tot.tot_data",
        "motet.core.reasoning.cot",
        "motet.core.reasoning.cot.cot_data",
        "motet.core.reasoning.cot.cot_reasoning",
        "motet.core.reasoning.budget_gates",
    ],
)
def test_deleted_orchestrator_modules_are_gone(module: str) -> None:
    """ADR-0138 deleted both orchestrators and the escalation budget gate."""
    with pytest.raises(ModuleNotFoundError):
        __import__(module)


def test_fanout_is_a_registered_tool() -> None:
    """Fan-out is reachable the way every other capability is: as a tool."""
    from motet.core.tools.builtin.spawn_agents import TOOL_NAME, register
    from motet.core.tools.registry import ToolRegistry

    registry = ToolRegistry()
    register(registry)

    assert TOOL_NAME in registry.list_items()


def test_escalate_reasoning_meta_tool_is_gone() -> None:
    """The loop no longer has a way to hand the turn to another executor."""
    from motet.core.reasoning.react import agentic_loop

    assert not hasattr(agentic_loop, "_get_escalation_meta_tools")
    assert not hasattr(agentic_loop, "_ensure_meta_tools_in_schemas")
