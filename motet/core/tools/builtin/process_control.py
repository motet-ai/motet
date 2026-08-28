"""
Motet - Host process control tool (bridge)

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-03-30

Description:
    List or terminate host processes visible only under the bridge cwd/exe allowlist.
    Requires device start --process-control-bridge and EDGE_PROCESS_CONTROL capability.
"""

from __future__ import annotations

from typing import Any, Dict, Literal, Optional

from pydantic import BaseModel, Field

from ..protocol import err
from ..registry import ToolRegistry
from .process_control_bridge_client import list_processes_via_bridge, terminate_via_bridge


class Params(BaseModel):
    operation: Literal["list", "terminate"] = Field(
        ...,
        description="'list' enumerates matching host processes; 'terminate' sends a signal to one pid",
    )
    limit: Optional[int] = Field(
        default=None,
        ge=1,
        description="Max rows for list (bridge caps)",
    )
    pid: Optional[int] = Field(
        default=None,
        description="Target PID for terminate",
    )
    signal: Optional[str] = Field(
        default=None,
        description="SIGTERM (default), SIGKILL, or SIGINT (host bridge; Windows maps INT to terminate)",
    )


def _fmt(res: Dict[str, Any]) -> str:
    if "error" in res:
        return f"process_control(error={res['error']})"
    if "processes" in res:
        return f"process_control(list, count={res.get('count', 0)})"
    return f"process_control(terminate pid={res.get('pid')}, signal={res.get('signal')})"


def run(params: Dict[str, Any]) -> Dict[str, Any]:
    op = params.get("operation")
    if op == "list":
        lim = params.get("limit")
        if lim is not None:
            try:
                lim = int(lim)
            except (TypeError, ValueError):
                return err("limit must be an integer")
        out = list_processes_via_bridge(lim)
        return out
    if op == "terminate":
        pid = params.get("pid")
        if pid is None:
            return err("pid is required for terminate")
        try:
            pid_i = int(pid)
        except (TypeError, ValueError):
            return err("pid must be an integer")
        sig = params.get("signal")
        if sig is not None and not isinstance(sig, str):
            return err("signal must be a string")
        return terminate_via_bridge(pid_i, sig if sig else None)

    return err("operation must be 'list' or 'terminate'")


def register(registry: ToolRegistry) -> None:
    registry.register(
        name="core.process_control",
        description=(
            "List or terminate **host** processes under the process-control bridge allowlist "
            "(cwd/exe must match MOTET_PROCESS_CONTROL_CWD_ALLOWLIST or shell cwd allowlist on the host). "
            "Use `device start --process-control-bridge`. Terminate accepts SIGTERM, SIGKILL, SIGINT only."
        ),
        func=run,
        tool_schema=Params,
        triggers=[],
        priority=2,
        estimate_tokens=lambda _: 25,
        parse_params=None,
        observation_formatter=_fmt,
        category="process",
        required_capabilities=[
            "TOOL_EXECUTION",
            "EDGE_EXECUTION",
            "EDGE_PROCESS_CONTROL",
        ],
    )


__all__ = ["register", "run"]
