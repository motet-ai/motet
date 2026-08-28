"""
Motet - Edge Exec Tool Tests

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-07-16

Description:
    Unit tests for core.edge_exec (ADR-0122), the edge-routed sibling of
    core.worker_exec. Verifies that capability inference includes EDGE_EXECUTION
    (so the router never places it on cloud workers), that the tool shares the
    worker_exec run implementation, and that registration metadata (name,
    category, observation formatter) is correct.

Dependencies:
    - motet.core.tools.builtin.edge_exec: tool under test
    - motet.core.commands.builtin.tool: capability inference
    - motet.core.tools.registry: fresh ToolRegistry for registration checks

Usage:
    pytest tests/unit/core/test_edge_exec.py -q

Notes:
    - Pure unit test: no Redis/distributed stack required.
"""

from __future__ import annotations

import sys

import pytest

from motet.core.commands.command_data_classes import ToolExecutionData
from motet.core.commands.capabilities import WorkerCapability
from motet.core.commands.builtin.tool import _infer_tool_capabilities
from motet.core.tools.builtin import edge_exec, worker_exec
from motet.core.tools.registry import ToolRegistry


def test_edge_exec_infer_capabilities_requires_edge() -> None:
    """Routing must demand EDGE_EXECUTION so cloud workers never match."""
    caps = _infer_tool_capabilities(
        ToolExecutionData(
            tool_name="core.edge_exec",
            parameters={"argv": ["true"]},
        )
    )
    assert WorkerCapability.EDGE_EXECUTION in caps
    assert WorkerCapability.WORKER_SHELL_EXEC in caps
    assert WorkerCapability.TOOL_EXECUTION in caps


def test_edge_exec_shares_worker_exec_run() -> None:
    """The edge tool is registration-only delegation; behavior is worker_exec's."""
    assert edge_exec.run is worker_exec.run


def test_edge_exec_registration_metadata() -> None:
    fresh = ToolRegistry()
    edge_exec.register(fresh)
    tool = fresh.get("core.edge_exec")
    assert tool is not None
    assert tool.category == "shell"
    assert tool.contextualize_observation is False
    assert set(tool.required_capabilities) == {
        "TOOL_EXECUTION",
        "EDGE_EXECUTION",
        "WORKER_SHELL_EXEC",
    }


def test_edge_exec_runs_with_allowlist(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """Sanity check the shared run path executes under the allowlist."""
    root = tmp_path / "allowed"
    root.mkdir()
    monkeypatch.setenv("MOTET_WORKER_EXEC_CWD_ALLOWLIST", str(root))
    r = edge_exec.run({"argv": [sys.executable, "-c", "print('edge-ok')"]})
    assert r.get("returncode") == 0
    assert "edge-ok" in (r.get("stdout") or "")


def test_edge_exec_observation_formatter() -> None:
    ok = edge_exec._fmt({"returncode": 0, "timed_out": False, "stdout": "abc"})
    assert ok == "edge_exec(rc=0, timed_out=False, out_len=3)"
    bad = edge_exec._fmt({"error": "boom"})
    assert bad == "edge_exec(error=boom)"
