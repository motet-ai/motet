"""
Motet - Worker exec cwd allowlist (shared policy)

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-04-05

Description:
    Validates absolute cwd paths for worker-side execution (subprocess and
    Docker bind mounts) against MOTET_WORKER_EXEC_CWD_ALLOWLIST.

Dependencies:
    - os

Usage:
    from motet.core.execution.cwd_allowlist import worker_exec_cwd_allowed

    ok, msg = worker_exec_cwd_allowed(os.path.abspath(cwd))
"""

from __future__ import annotations

import os
from typing import Tuple


def worker_exec_cwd_allowed(cwd_abs: str) -> Tuple[bool, str]:
    raw = (os.getenv("MOTET_WORKER_EXEC_CWD_ALLOWLIST") or "").strip()
    if not raw:
        return (
            False,
            "MOTET_WORKER_EXEC_CWD_ALLOWLIST is not set; refusing worker_exec "
            "(set comma-separated absolute directory prefixes)",
        )
    allowed = [os.path.abspath(p.strip()) for p in raw.split(",") if p.strip()]
    for prefix in allowed:
        if cwd_abs == prefix or cwd_abs.startswith(prefix + os.sep):
            return True, ""
    return False, "cwd is not under MOTET_WORKER_EXEC_CWD_ALLOWLIST"


__all__ = ["worker_exec_cwd_allowed"]
