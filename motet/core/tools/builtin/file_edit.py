"""
Motet - File Edit Tool

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-24

Description:
    Exact string-replacement editor for allowlisted files. Reads UTF-8 text,
    replaces ``old_string`` with ``new_string`` when the match is unique (or
    when ``replace_all`` is set), and writes the result back. Missing or
    ambiguous matches return structured errors rather than guessing. Allowlist checks use realpath so symlink escapes are denied.

Dependencies:
    - os, typing, pydantic
    - Tool registry and protocol (err)

Usage:
    result = run({
        "path": "/allowed/module.py",
        "old_string": "def foo():\\n    return 1",
        "new_string": "def foo():\\n    return 2",
    })

Notes:
    - Routed only to edge workers (EDGE_EXECUTION + EDGE_FILE_WRITE), like
      core.file_write.
    - Uses MOTET_FILE_WRITE_ALLOWLIST (or allowlist param) for path policy.
    - Prefer this over core.file_write when changing a region of an existing
      file; whole-file rewrite drifts on large modules.
"""

from __future__ import annotations

import os
from typing import Any, Dict, Optional

from pydantic import BaseModel, Field

from ..protocol import err
from ..registry import ToolRegistry
from .path_policy import allowed_roots_from_csv, resolve_allowlisted_path


class Params(BaseModel):
    path: str = Field(..., description="File path to edit (must fall under allowlist)")
    old_string: str = Field(
        ...,
        description="Exact text to find; must be unique unless replace_all is true",
    )
    new_string: str = Field(..., description="Replacement text")
    replace_all: bool = Field(
        default=False,
        description="If true, replace every occurrence of old_string",
    )
    allowlist: Optional[str] = Field(
        default=None,
        description="Override comma-separated allowed roots (else MOTET_FILE_WRITE_ALLOWLIST)",
    )


def run(params: Dict[str, Any]) -> Dict[str, Any]:
    """Synchronous exact string replacement (gevent/eventlet compatible)."""
    path = params.get("path")
    old_string = params.get("old_string")
    new_string = params.get("new_string")
    replace_all = bool(params.get("replace_all", False))

    if not path or not str(path).strip():
        return err("path is required")
    if old_string is None or not isinstance(old_string, str):
        return err("old_string is required and must be a string")
    if new_string is None or not isinstance(new_string, str):
        return err("new_string is required and must be a string")
    if old_string == "":
        return err("old_string must be non-empty")
    if old_string == new_string:
        return err("old_string and new_string are identical; nothing to change")

    allowlist = params.get("allowlist")
    roots = allowed_roots_from_csv(
        str(allowlist) if allowlist else os.getenv("MOTET_FILE_WRITE_ALLOWLIST")
    )

    try:
        abs_path, deny = resolve_allowlisted_path(str(path).strip(), roots)
        if deny:
            return err(deny)
        assert abs_path is not None
        if not os.path.exists(abs_path):
            return err("file does not exist")
        if not os.path.isfile(abs_path):
            return err("path is not a file")

        with open(abs_path, "r", encoding="utf-8") as f:
            content = f.read()

        count = content.count(old_string)
        if count == 0:
            return err("old_string not found")
        if count > 1 and not replace_all:
            return err(
                f"old_string is not unique ({count} matches); "
                "provide more context or set replace_all"
            )

        if replace_all:
            updated = content.replace(old_string, new_string)
            replacements = count
        else:
            updated = content.replace(old_string, new_string, 1)
            replacements = 1

        data = updated.encode("utf-8")
        with open(abs_path, "wb") as f:
            bytes_written = f.write(data)

        return {
            "path": abs_path,
            "replacements": replacements,
            "bytes_written": bytes_written,
        }
    except UnicodeDecodeError:
        return err("file is not valid UTF-8")
    except PermissionError as e:
        return err(f"permission denied: {e}")
    except Exception as exc:
        return err(str(exc))


def _fmt(res: Dict[str, Any]) -> str:
    if "error" in res:
        return f"file_edit(error={res['error']})"
    return (
        f"file_edit(ok, path={res.get('path')}, "
        f"replacements={res.get('replacements')}, bytes={res.get('bytes_written')})"
    )


def register(registry: ToolRegistry) -> None:
    registry.register(
        name="core.file_edit",
        description=(
            "Replace an exact string in an allowlisted UTF-8 file "
            "(local device worker only). old_string must match uniquely unless "
            "replace_all is true; missing or ambiguous matches return an error. "
            "Prefer this over core.file_write for surgical edits."
        ),
        func=run,
        tool_schema=Params,
        triggers=[],
        priority=5,
        estimate_tokens=lambda _: 25,
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
