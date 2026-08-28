"""
Motet - Built-in Tool Registration Drift Guard Tests

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-26

Description:
    Guards against the class of bug where a new built-in tool is added to one
    registration list but not the other (which previously left image_generation
    and search_artifacts registered in dead code yet absent from the live runtime
    registry and the discovery index). Asserts that:
      1. register_all_builtin_tools registers every spec cleanly (strict mode).
      2. The runtime singleton registry (motet.core.tools.registry, used by
         MotetStack.tool_registry) contains every tool the canonical registrar
         produces — i.e. no drift between the canonical list and the live state.
      3. Notable media/RAG tools are present.

Dependencies:
    - motet.core.tools.builtin.register_all_builtin_tools (single source of truth)
    - motet.core.tools.registry (runtime singleton registry)

Usage:
    pytest tests/unit/core/tools/test_builtin_registration.py

Notes:
    - Pure unit test: no Redis/distributed stack required.
"""

from __future__ import annotations

from motet.core.tools import registry as runtime_registry
from motet.core.tools.builtin import _BUILTIN_TOOL_SPECS, register_all_builtin_tools
from motet.core.tools.registry import ToolRegistry


def test_register_all_builtin_tools_strict_registers_every_spec() -> None:
    """Every registrar in the canonical table must register without error."""
    fresh = ToolRegistry()
    registered = register_all_builtin_tools(fresh, strict=True)

    # admin_tools registers multiple tools, so the registry has >= number of specs.
    assert len(registered) == len(_BUILTIN_TOOL_SPECS)
    assert len(fresh.list_items()) >= len(_BUILTIN_TOOL_SPECS)


def test_runtime_singleton_has_no_drift_from_canonical_list() -> None:
    """The live runtime registry must contain everything the canonical registrar produces.

    This is the regression guard: adding a tool to the builtin table but failing
    to wire it into the runtime registry (or vice versa) fails here.
    """
    canonical = ToolRegistry()
    register_all_builtin_tools(canonical, strict=True)

    canonical_tools = set(canonical.list_items().keys())
    runtime_tools = set(runtime_registry.list_items().keys())

    missing_from_runtime = canonical_tools - runtime_tools
    assert not missing_from_runtime, (
        "Built-in tools registered by register_all_builtin_tools are missing from "
        f"the runtime singleton registry: {sorted(missing_from_runtime)}"
    )


def test_media_and_rag_tools_registered() -> None:
    """Spot-check the tools whose absence motivated this guard."""
    runtime_tools = set(runtime_registry.list_items().keys())
    assert "core.image_generation" in runtime_tools
    assert "core.search_artifacts" in runtime_tools
    assert "core.docs_read" in runtime_tools


def test_current_time_tool_registered() -> None:
    """core.current_time must be present in the runtime registry."""
    runtime_tools = set(runtime_registry.list_items().keys())
    assert "core.current_time" in runtime_tools


def test_handoff_tool_registered() -> None:
    """core.handoff is a catalog-visible builtin, same as core.spawn_agents."""
    runtime_tools = set(runtime_registry.list_items().keys())
    assert "core.handoff" in runtime_tools
    assert "core.spawn_agents" in runtime_tools
