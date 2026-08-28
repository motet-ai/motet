"""
Motet - Edge exec tool (edge worker domain)

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-25

Description:
    Edge-routed sibling of ``core.worker_exec``. Same argv contract
    and execution path, but requires EDGE_EXECUTION in addition to
    TOOL_EXECUTION + WORKER_SHELL_EXEC, so the router only dispatches it to
    edge workers (e.g. edge_app_builder) whose MOTET_EXEC_BACKEND=subprocess
    runs argv in-process against the mounted, allowlisted workspace.

    Rationale: ``core.worker_exec`` routes to any worker advertising
    WORKER_SHELL_EXEC. Cloud workers run it with MOTET_EXEC_BACKEND=docker in
    a disposable container that has neither git nor the builder clone mounted,
    so agents that must shell inside an edge workspace (app-builder engineer
    running ``git`` / targeted pytest in /srv/app-builder/imf) silently got a
    useless sandbox. This tool makes the edge placement explicit, mirroring
    how core.file_edit / core.file_grep force EDGE_* routing.

    Registered as ``core.edge_exec``, pairing with ``core.host_exec`` /
    ``core.worker_exec``.

Dependencies:
    - motet.core.tools.builtin.worker_exec: run() implementation and Params
      schema (this module is registration-only delegation)
    - Tool registry, protocol

Usage:
    from motet.core.tools.builtin.edge_exec import register
    register(tool_registry)
    # Agents whose tool_filter includes "core.edge_exec" get argv
    # execution guaranteed to land on an edge worker's allowlisted cwd.

Notes:
    - Behavior (cwd resolution, allowlists, timeouts, workspace_mode) is
      identical to core.worker_exec on the worker that executes it; only the
      routing capabilities differ.
    - Edge workers advertise EDGE_EXECUTION via MOTET_EDGE_WORKER_ID; cloud
      workers never match, so there is no docker-backend fallback path.
"""

from __future__ import annotations

from typing import Any, Dict

from ..registry import ToolRegistry
from .worker_exec import Params, run


def _fmt(res: Dict[str, Any]) -> str:
    if "error" in res:
        return f"edge_exec(error={res['error']})"
    return (
        f"edge_exec(rc={res.get('returncode')}, "
        f"timed_out={res.get('timed_out')}, "
        f"out_len={len(res.get('stdout') or '')})"
    )


def register(registry: ToolRegistry) -> None:
    registry.register(
        name="core.edge_exec",
        description=(
            "Run one-shot argv on an **edge** worker inside its allowlisted workspace "
            "(no shell by default; subprocess backend with the workspace mounted — git and "
            "project tooling available). Prefer explicit argv like [\"git\", \"status\"]; for shell "
            "features invoke a shell explicitly, e.g. [\"bash\", \"-lc\", \"pytest -q && git diff\"]. "
            "Same contract as core.worker_exec but routed only to edge workers "
            "(EDGE_EXECUTION), never to cloud workers' disposable containers. Use this when "
            "commands must run against an edge-mounted working tree such as the app-builder "
            "clone."
        ),
        func=run,
        tool_schema=Params,
        triggers=[],
        priority=3,
        estimate_tokens=lambda _: 20,
        parse_params=None,
        observation_formatter=_fmt,
        category="shell",
        # Same as core.worker_exec: keep full argv result for programmatic
        # consumers (app-builder.run_tests parses pytest summaries from stdout).
        contextualize_observation=False,
        required_capabilities=[
            "TOOL_EXECUTION",
            "EDGE_EXECUTION",
            "WORKER_SHELL_EXEC",
        ],
    )


__all__ = ["register", "run"]
