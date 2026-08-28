"""
Motet - File Write Tool

Copyright (c) 2024-2025 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-24

Description:
    File write tool for the Motet distributed framework. Writes UTF-8 text to a path
    under an allowlist (MOTET_FILE_WRITE_ALLOWLIST), with optional append mode,
    content size cap, and parent-directory existence checks. Allowlist checks use
    realpath so symlink escapes cannot write outside the workspace.

Dependencies:
    - os, typing, pydantic
    - Tool registry and protocol (ok, err)

Usage:
    result = run({
        "path": "/allowed/out.txt",
        "content": "hello",
        "mode": "write",
    })

Notes:
    - Routed only to edge workers (EDGE_EXECUTION + EDGE_FILE_WRITE), like core.file_read.
    - Mirrors core.file_read security policy: comma-separated allowlist dirs.
    - Default mode overwrites the file; append opens in binary append mode.
"""

from __future__ import annotations

import os
from typing import Any, Dict, Literal, Optional

from pydantic import BaseModel, Field

from ..protocol import err
from ..registry import ToolRegistry
from .path_policy import allowed_roots_from_csv, resolve_allowlisted_path


class Params(BaseModel):
    path: str = Field(..., description="Destination path (under allowlist)")
    content: str = Field(..., description="UTF-8 text to write")
    mode: Literal["write", "append"] = Field(
        default="write",
        description="'write' truncates/creates; 'append' appends bytes",
    )
    allowlist: Optional[str] = Field(
        default=None,
        description="Override comma-separated allowed roots (else MOTET_FILE_WRITE_ALLOWLIST)",
    )
    create_parents: bool = Field(
        default=False,
        description="If true, create missing parent directories under allowlist",
    )


def _fmt(res: Dict[str, Any]) -> str:
    if "error" in res:
        return f"write(error={res['error']})"
    return f"write(ok, path={res.get('path')}, bytes={res.get('bytes_written')})"


def run(params: Dict[str, Any]) -> Dict[str, Any]:
    """Synchronous file write (gevent/eventlet compatible)."""
    path = params.get("path")
    content = params.get("content")
    if content is None:
        return err("content is required")
    if not isinstance(content, str):
        return err("content must be a string")
    mode = str(params.get("mode") or "write").lower()
    if mode not in ("write", "append"):
        return err("mode must be 'write' or 'append'")
    create_parents = bool(params.get("create_parents", False))

    allowlist = params.get("allowlist") or os.getenv("MOTET_FILE_WRITE_ALLOWLIST")
    max_bytes = int(os.getenv("MOTET_FILE_WRITE_MAX_BYTES") or 1_048_576)
    if len(content.encode("utf-8")) > max_bytes:
        return err(f"content too large: > {max_bytes} bytes (UTF-8)")

    if not path:
        return err("path is required")
    try:
        roots = allowed_roots_from_csv(str(allowlist) if allowlist else None)
        abs_path, deny = resolve_allowlisted_path(str(path), roots)
        if deny:
            return err(deny)
        assert abs_path is not None

        parent = os.path.dirname(abs_path)
        if create_parents and parent:
            os.makedirs(parent, exist_ok=True)
        elif parent and not os.path.isdir(parent):
            return err("parent directory does not exist (set create_parents=true to create)")

        data = content.encode("utf-8")
        if mode == "append":
            with open(abs_path, "ab") as f:
                n = f.write(data)
        else:
            with open(abs_path, "wb") as f:
                n = f.write(data)
        return {"path": abs_path, "bytes_written": n, "mode": mode}
    except IsADirectoryError:
        return err("path is a directory")
    except PermissionError as e:
        return err(f"permission denied: {e}")
    except Exception as exc:
        return err(str(exc))


def register(registry: ToolRegistry) -> None:
    registry.register(
        name="core.file_write",
        description=(
            "Write or append UTF-8 text to a file under an allowlisted directory "
            "(local device worker only; use MOTET_FILE_WRITE_ALLOWLIST or pass allowlist). "
            "Parent directory must exist unless create_parents is true."
        ),
        func=run,
        tool_schema=Params,
        triggers=[],
        priority=5,
        estimate_tokens=lambda _: 20,
        parse_params=None,
        observation_formatter=_fmt,
        category="filesystem",
        required_capabilities=[
            "TOOL_EXECUTION",
            "EDGE_EXECUTION",
            "EDGE_FILE_WRITE",
        ],
    )


__all__ = ["register", "run"]
