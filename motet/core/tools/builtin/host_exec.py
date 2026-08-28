"""
Motet - Host exec tool (shell bridge)

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-24

Description:
    Runs argv on the host via MOTET_SHELL_BRIDGE_URL (device start --shell-exec-bridge).
    No shell interpolation — argv is passed directly to subprocess.run(shell=False) on the host.

Dependencies:
    - shell_bridge_client (urllib)
    - Tool registry

Notes:
    - cwd is system-determined; callers do not provide cwd.
    - Public tool id is core.host_exec.
    - Routed only to edge workers with edge_shell_exec (bridge + MOTET_ENABLE_SHELL_EXEC).
"""

from __future__ import annotations

import datetime
import os
import uuid
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field

from ..protocol import err
from ..registry import ToolRegistry
from .shell_bridge_client import exec_via_bridge


class Params(BaseModel):
    argv: List[str] = Field(
        ...,
        description="Executable and arguments only (no shell); e.g. [\"git\", \"status\"]",
    )
    timeout_seconds: Optional[int] = Field(
        default=None,
        description="Subprocess timeout (seconds); bridge applies a maximum cap",
    )


def _fmt(res: Dict[str, Any]) -> str:
    if "error" in res:
        return f"host_exec(error={res['error']})"
    return (
        f"host_exec(rc={res.get('returncode')}, "
        f"timed_out={res.get('timed_out')}, "
        f"out_len={len(res.get('stdout') or '')})"
    )


def _first_host_allowlist_prefix() -> Optional[str]:
    raw = (os.getenv("MOTET_SHELL_BRIDGE_CWD_ALLOWLIST") or "").strip()
    if not raw:
        return None
    for part in raw.split(","):
        p = part.strip()
        if p:
            return os.path.abspath(p)
    return None


def _default_host_exec_root() -> Optional[str]:
    configured = (os.getenv("MOTET_HOST_EXEC_DEFAULT_CWD_ROOT") or "").strip()
    if configured:
        return os.path.abspath(configured)
    return _first_host_allowlist_prefix()


def _resolve_effective_cwd() -> tuple[Optional[str], bool, Optional[str]]:
    root = _default_host_exec_root()
    if not root:
        return (
            None,
            True,
            "cwd is required unless MOTET_HOST_EXEC_DEFAULT_CWD_ROOT is set "
            "(and host bridge allowlists that root)",
        )
    now = datetime.datetime.utcnow().strftime("%Y%m%dT%H%M%S")
    token = uuid.uuid4().hex[:12]
    generated = os.path.join(root, "runs", f"{now}-{token}")
    return os.path.abspath(generated), True, None


def run(params: Dict[str, Any]) -> Dict[str, Any]:
    argv = params.get("argv")
    if not isinstance(argv, list) or not argv:
        return err("argv must be a non-empty list")
    str_argv: List[str] = []
    for i, a in enumerate(argv):
        if not isinstance(a, str):
            return err(f"argv[{i}] must be a string")
        if "\x00" in a:
            return err("argv must not contain null bytes")
        str_argv.append(a)

    cwd_s, cwd_generated, cwd_error = _resolve_effective_cwd()
    if cwd_error:
        return err(cwd_error)
    assert cwd_s is not None

    timeout = params.get("timeout_seconds")
    if timeout is not None:
        try:
            timeout = int(timeout)
        except (TypeError, ValueError):
            return err("timeout_seconds must be an integer")

    out = exec_via_bridge(str_argv, cwd_s, timeout)
    out["effective_cwd"] = cwd_s
    out["cwd_generated"] = cwd_generated
    return out


def register(registry: ToolRegistry) -> None:
    registry.register(
        name="core.host_exec",
        description=(
            "Run a command on the **host** via the shell bridge (argv only, no shell). "
            "Requires `device start --shell-exec-bridge` and host MOTET_SHELL_BRIDGE_CWD_ALLOWLIST. "
            "The tool always determines cwd from MOTET_HOST_EXEC_DEFAULT_CWD_ROOT and generates a unique run directory."
        ),
        func=run,
        tool_schema=Params,
        triggers=[],
        priority=3,
        estimate_tokens=lambda _: 20,
        parse_params=None,
        observation_formatter=_fmt,
        category="shell",
        required_capabilities=[
            "TOOL_EXECUTION",
            "EDGE_EXECUTION",
            "EDGE_SHELL_EXEC",
        ],
    )


__all__ = ["register", "run"]
