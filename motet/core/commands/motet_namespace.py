"""
Motet - Motet decorator namespace

Copyright (c) 2024-2025 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-24

Description:
    Single entry point for Motet decorators: @motet.command and @motet.tool.
    Provides brand clarity in mixed codebases and aligns with namespaced
    decorator conventions (Click, Ray, Dask)..

Dependencies:
    -.decorator: distributed_command (command impl), motet_tool (tool impl)

Usage:
    from motet import motet

    @motet.command(timeout_seconds=60)
    def my_command(data: MyData, motet: MotetContext) -> Dict[str, Any]:
        return {"ok": True}

    @motet.tool(description="Does something useful")
    def my_tool(params: Dict[str, Any]) -> Dict[str, Any]:
        return {"result": params}
"""

from __future__ import annotations

from typing import Any

# Import module and attach same references so motet.command is distributed_command (alias)
from motet.core.commands import decorator as _decorator_mod


class _MotetNamespace:
    """
    Namespace object for Motet decorators (ADR-0089).
    Use @motet.command for distributed commands, @motet.tool for bundle tools.
    Attributes are set on the instance so they are not bound as methods when called.
    """

    command: Any
    tool: Any


motet = _MotetNamespace()
motet.command = _decorator_mod.distributed_command
motet.tool = _decorator_mod.motet_tool
