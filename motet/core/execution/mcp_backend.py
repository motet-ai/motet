"""
Motet - MCP process execution backend selection (Phase 2)

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-04-07

Description:
    Resolves whether MCP servers run as host subprocesses or inside Docker
    containers (Engine API), independent from ``MOTET_EXEC_BACKEND`` unless
    ``MOTET_MCP_EXEC_BACKEND`` is unset (then inherits worker exec backend).

Dependencies:
    - os

Usage:
    from motet.core.execution.mcp_backend import mcp_exec_uses_docker
"""

from __future__ import annotations

import os


def mcp_exec_backend() -> str:
    """
    Return ``docker`` or ``subprocess``.

    If ``MOTET_MCP_EXEC_BACKEND`` is set to ``docker`` or ``subprocess``, that
    value wins. Otherwise, when ``MOTET_EXEC_BACKEND=docker``, MCP uses Docker
    too; else subprocess.
    """
    explicit = (os.getenv("MOTET_MCP_EXEC_BACKEND") or "").strip().lower()
    if explicit in ("docker", "subprocess"):
        return explicit
    inherited = (os.getenv("MOTET_EXEC_BACKEND") or "subprocess").strip().lower()
    if inherited in ("docker", "container", "kata", "kata-fc"):
        return "docker"
    return "subprocess"


def mcp_exec_uses_docker() -> bool:
    return mcp_exec_backend() == "docker"


__all__ = ["mcp_exec_backend", "mcp_exec_uses_docker"]
