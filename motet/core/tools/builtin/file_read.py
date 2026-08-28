"""
Motet - File Read Tool

Copyright (c) 2024-2025 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-24

Description:
    File read tool for the Motet distributed framework.
    Provides secure file reading capabilities with allowlist validation,
    size limits, and comprehensive error handling. Includes path
    validation and security checks for distributed file access (realpath
    allowlist — rejects symlink escapes outside the workspace).

Dependencies:
    - os: File system operations and path handling
    - pydantic: Data validation and model definitions
    - typing: Type hints and annotations
    - Tool registry and protocol system

Usage:
    from motet.core.tools.builtin.file_read import run

    # Read file
    result = run({
        "path": "/path/to/file.txt",
        "allowlist": "/allowed/path",
        "max_bytes": 1024
    })

Notes:
    - Routed only to edge workers (EDGE_EXECUTION + EDGE_FILE_READ); not cloud workers.
    - Provides secure file reading capabilities
    - Includes allowlist validation and security checks
    - Supports size limits and error handling
    - Includes path validation and file system operations
    - Supports distributed file access coordination
    - Integrates with tool registry and protocol system
    - Includes comprehensive observability and logging
"""

from __future__ import annotations

import os
from typing import Any, Dict, Optional

from pydantic import BaseModel

from ..protocol import err
from ..registry import ToolRegistry
from .path_policy import allowed_roots_from_csv, resolve_allowlisted_path


class Params(BaseModel):
    path: str
    allowlist: Optional[str] = None
    max_bytes: Optional[int] = None


def _parse(ln: str, trig: str) -> Dict[str, Any]:
    return {"path": ln[len(trig):].strip()}


def _fmt(res: Dict[str, Any]) -> str:
    if "error" in res:
        return f"read(error={res['error']})"
    text = (res.get("text") or "")[:64]
    return f"read(ok, snippet={text})"


def run(params: Dict[str, Any]) -> Dict[str, Any]:
    """Synchronous file reading (ADR-0033: gevent/eventlet compatible)."""
    path = params.get("path")
    allowlist = params.get("allowlist") or os.getenv("MOTET_FILE_READ_ALLOWLIST")
    max_bytes = int(params.get("max_bytes") or os.getenv("MOTET_FILE_READ_MAX_BYTES") or 65536)
    if not path:
        return err("path is required")
    try:
        roots = allowed_roots_from_csv(str(allowlist) if allowlist else None)
        abs_path, deny = resolve_allowlisted_path(str(path), roots)
        if deny:
            return err(deny)
        assert abs_path is not None
        size = os.path.getsize(abs_path)
        if size > max_bytes:
            return err(f"file too large: {size} bytes > {max_bytes}")
        with open(abs_path, "rb") as f:
            data = f.read(max_bytes)
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            text = data[:128].hex()
        return {"path": abs_path, "bytes": len(data), "text": text}
    except FileNotFoundError:
        return err("file not found")
    except Exception as exc:
        return err(str(exc))


def register(registry: ToolRegistry) -> None:
    registry.register(
        name="core.file_read",
        description=(
            "Read a file from an allowlisted directory with size cap. "
            "Runs on the local device worker (host paths / mounts), not cloud workers."
        ),
        func=run,
        tool_schema=Params,
        triggers=["read:"],
        priority=5,
        estimate_tokens=lambda _: 30,
        parse_params=_parse,
        observation_formatter=_fmt,
        category="filesystem",
        required_capabilities=[
            "TOOL_EXECUTION",
            "EDGE_EXECUTION",
            "EDGE_FILE_READ",
        ],
    )


__all__ = ["register"]


