"""
Motet - Shared filesystem path allowlist policy

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-24

Description:
    Shared helpers for builtin file tools (read/write/edit/search/grep). Resolves
    paths with ``os.path.realpath`` before allowlist checks so symlink escapes
    (e.g. ``ln -sf /proc/self/environ`` under an allowlisted tree) cannot leak
    credentials or write outside the workspace.

Dependencies:
    - os: path resolution and prefix checks

Usage:
    from motet.core.tools.builtin.path_policy import (
        allowed_roots_from_csv,
        resolve_allowlisted_path,
    )

    roots = allowed_roots_from_csv(os.getenv("MOTET_FILE_READ_ALLOWLIST"))
    resolved, err = resolve_allowlisted_path("/srv/repo/file.py", roots)
    if err:
        return err("...")

Notes:
    - When ``roots`` is empty, allowlist enforcement is off.
    - Denied prefixes (``/proc``, ``/sys``) always apply when roots are set.
    - Only the symlink-resolved ``realpath`` is checked against the (also
      resolved) roots — the resolved path is what actually gets opened, and
      checking the lexical path would falsely deny paths under roots that are
      themselves symlinks (e.g. macOS ``/tmp`` → ``/private/tmp``).
"""

from __future__ import annotations

import os
from typing import List, Optional, Tuple

# Always reject these after realpath when an allowlist is active.
_DENIED_PREFIXES = ("/proc", "/sys")


def allowed_roots_from_csv(raw: Optional[str]) -> List[str]:
    """Parse comma-separated allowlist roots into real absolute paths."""
    if not raw:
        return []
    roots: List[str] = []
    for part in raw.split(","):
        token = part.strip()
        if not token:
            continue
        roots.append(os.path.realpath(os.path.abspath(token)))
    return roots


def is_under_allowlist(candidate: str, roots: List[str]) -> bool:
    """Return True when *candidate* equals a root or is under one."""
    return any(
        candidate == root or candidate.startswith(root + os.sep) for root in roots
    )


def _denied_system_path(resolved_path: str) -> bool:
    return any(
        resolved_path == prefix or resolved_path.startswith(prefix + os.sep)
        for prefix in _DENIED_PREFIXES
    )


def resolve_allowlisted_path(
    path: str,
    roots: List[str],
    *,
    must_exist: bool = False,
) -> Tuple[Optional[str], Optional[str]]:
    """Resolve *path* and enforce allowlist.

    Returns ``(resolved_path, None)`` on success, or ``(None, error_message)``.
    """
    if not path or not str(path).strip():
        return None, "path is required"

    resolved = os.path.realpath(os.path.abspath(str(path).strip()))

    if roots:
        if _denied_system_path(resolved) or not is_under_allowlist(resolved, roots):
            return None, "path not in allowlist"

    if must_exist and not os.path.exists(resolved):
        return None, "file does not exist"

    return resolved, None
