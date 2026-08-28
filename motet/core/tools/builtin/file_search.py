"""
Motet - File Search Tool

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-24

Description:
    Recursively list file paths under an allowlisted root matching a safe glob/FNMATCH-style
    pattern. Uses the same allowlist policy as core.file_read (parameter or
    MOTET_FILE_READ_ALLOWLIST). Does not follow symlinks during traversal.

Dependencies:
    - os, fnmatch, pathlib, typing, pydantic
    - Tool registry and protocol (err)

Usage:
    result = run({
        "root": "/mnt/motet/read/0",
        "pattern": "*.md",
        "max_results": 50,
    })

Notes:
    - Routed only to edge workers (EDGE_EXECUTION + EDGE_FILE_SEARCH), like core.file_read.
    - Simple patterns without "/" or "**" match only the filename (any depth).
    - Use "**/*.py" style patterns for full relative-path glob matching.
"""

from __future__ import annotations

import fnmatch
import os
from pathlib import Path, PurePosixPath
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field

from ..protocol import err
from ..registry import ToolRegistry
from .path_policy import allowed_roots_from_csv, is_under_allowlist, resolve_allowlisted_path


class Params(BaseModel):
    root: str = Field(
        ...,
        description="Directory to search (must fall under allowlist when allowlist is set)",
    )
    pattern: str = Field(
        ...,
        description=(
            "Glob: if it contains '/' or '**', matched against path relative to root; "
            "otherwise matched against the file name only (e.g. '*.py')."
        ),
    )
    allowlist: Optional[str] = Field(
        default=None,
        description="Override comma-separated allowed roots (else MOTET_FILE_READ_ALLOWLIST)",
    )
    max_results: Optional[int] = Field(
        default=None,
        description="Cap on paths returned (else MOTET_FILE_SEARCH_MAX_RESULTS, default 200)",
    )
    max_depth: Optional[int] = Field(
        default=None,
        ge=1,
        description=(
            "Maximum number of relative path segments for a match "
            "(1 = only files directly in root; 2 = root and one directory level; unset = unlimited)."
        ),
    )
    include_hidden: bool = Field(
        default=False,
        description="If false, skip dot-files and dot-directories",
    )


def _allowed_roots(allowlist: Optional[str]) -> List[str]:
    return allowed_roots_from_csv(allowlist or os.getenv("MOTET_FILE_READ_ALLOWLIST"))


def _is_under_allowlist(abs_path: str, roots: List[str]) -> bool:
    return is_under_allowlist(abs_path, roots)


def _root_must_be_allowlisted(root_abs: str, roots: List[str]) -> Optional[Dict[str, Any]]:
    if not roots:
        return None
    resolved, deny = resolve_allowlisted_path(root_abs, roots)
    if deny or resolved is None:
        return err("root not in allowlist")
    return None


def _matches(rel_file: Path, pattern: str) -> bool:
    pat = pattern.strip()
    if not pat:
        return False
    if "/" in pat or "**" in pat:
        return PurePosixPath(rel_file.as_posix()).match(pat)
    return fnmatch.fnmatch(rel_file.name, pat)


def _run_walk(
    root_abs: str,
    pattern: str,
    max_results: int,
    max_depth: Optional[int],
    include_hidden: bool,
) -> tuple[List[str], bool]:
    found: List[str] = []
    truncated = False

    for dirpath, dirnames, filenames in os.walk(
        root_abs, topdown=True, followlinks=False
    ):
        if not include_hidden:
            dirnames[:] = [d for d in dirnames if not d.startswith(".")]
        rel_dir = os.path.relpath(dirpath, root_abs)
        if rel_dir == ".":
            dir_depth = 0
        else:
            dir_depth = len(rel_dir.split(os.sep))

        if max_depth is not None:
            # Shortest path under a child dir has len dir_depth + 2 (child + file).
            dirnames[:] = [d for d in dirnames if dir_depth + 2 <= max_depth]

        for name in filenames:
            if not include_hidden and name.startswith("."):
                continue
            full = os.path.join(dirpath, name)
            if not os.path.isfile(full):
                continue
            try:
                rel = Path(os.path.relpath(full, root_abs))
            except ValueError:
                continue
            if max_depth is not None and len(rel.parts) > max_depth:
                continue
            if not _matches(rel, pattern):
                continue
            found.append(os.path.abspath(full))
            if len(found) >= max_results:
                truncated = True
                return found, truncated

    return found, truncated


def run(params: Dict[str, Any]) -> Dict[str, Any]:
    """Synchronous path search (gevent/eventlet compatible)."""
    root = params.get("root")
    pattern = params.get("pattern")
    if not root or not str(root).strip():
        return err("root is required")
    if pattern is None or not str(pattern).strip():
        return err("pattern is required")

    allowlist = params.get("allowlist")
    max_results_raw = params.get("max_results") or os.getenv("MOTET_FILE_SEARCH_MAX_RESULTS") or 200
    max_results = int(max_results_raw)
    if max_results < 1:
        return err("max_results must be at least 1")

    max_depth = params.get("max_depth")
    if max_depth is not None:
        max_depth = int(max_depth)
    include_hidden = bool(params.get("include_hidden", False))

    roots = _allowed_roots(str(allowlist) if allowlist else None)
    try:
        root_abs = os.path.abspath(str(root).strip())
        if not os.path.isdir(root_abs):
            return err("root is not a directory or does not exist")
        deny = _root_must_be_allowlisted(root_abs, roots)
        if deny is not None:
            return deny

        paths, truncated = _run_walk(
            root_abs,
            str(pattern).strip(),
            max_results=max_results,
            max_depth=max_depth,
            include_hidden=include_hidden,
        )
        return {
            "root": root_abs,
            "pattern": str(pattern).strip(),
            "paths": paths,
            "count": len(paths),
            "truncated": truncated,
        }
    except Exception as exc:
        return err(str(exc))


def _fmt(res: Dict[str, Any]) -> str:
    if "error" in res:
        return f"file_search(error={res['error']})"
    return f"file_search(ok, count={res.get('count')}, truncated={res.get('truncated')})"


def register(registry: ToolRegistry) -> None:
    registry.register(
        name="core.file_search",
        description=(
            "Find files under a directory matching a glob pattern (local device worker only). "
            "Respects MOTET_FILE_READ_ALLOWLIST (or allowlist param) like core.file_read. "
            "Pattern without '/' or '**' matches filenames only at any depth; "
            "use e.g. '**/*.py' for path-aware globs."
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
            "EDGE_FILE_SEARCH",
        ],
    )


__all__ = ["register", "run"]
